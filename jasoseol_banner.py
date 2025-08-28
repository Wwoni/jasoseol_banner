import os
import json
import pandas as pd
from urllib.parse import urljoin, urlparse, unquote
from typing import List, Dict, Tuple
from playwright.sync_api import sync_playwright

BASE_URL = "https://jasoseol.com/"
OUTPUT_CSV = "jasoseol_banner.csv"

# -------------------------
# 유틸
# -------------------------
def url_basename(u: str) -> str:
    if not u:
        return ""
    u = unquote(u)
    return os.path.basename(urlparse(u).path)

def parse_srcset_modified(srcset: str) -> str:
    """
    Selenium 수동 코드 규칙 재현:
    - ', ' 로 split
    - 마지막 항목 제거
    - 각 항목은 'url width' 중 url만 취함
    - 상대경로면 BASE_URL 붙임
    """
    if not srcset:
        return ""
    parts = [p.strip() for p in srcset.split(",") if p.strip()]
    if len(parts) > 1:
        parts = parts[:-1]
    urls = []
    for p in parts:
        url_only = p.split()[0]
        if not (url_only.startswith("http://") or url_only.startswith("https://")):
            url_only = urljoin(BASE_URL, url_only)
        urls.append(url_only)
    return ", ".join(urls)

def looks_like_img(url: str) -> bool:
    if not url:
        return False
    url_l = url.lower()
    if any(url_l.endswith(ext) or (ext + "?") in url_l for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif")):
        return True
    if "/_next/image" in url_l and "url=" in url_l:
        return True
    return False

def lcs_len(a: str, b: str) -> int:
    """간단 근사 유사도(최장 공통 부분 길이)"""
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    best = 0
    for i in range(len(short)):
        for j in range(i + 1, len(short) + 1):
            seg = short[i:j]
            if seg and seg in long:
                best = max(best, j - i)
    return best

# -------------------------
# __NEXT_DATA__에서 (img, link) 페어 수집
# -------------------------
def collect_next_pairs_from_page(page) -> List[Dict]:
    """
    Next.js 초기 상태(JSON)에서 이미지 URL과 라우팅 path를 함께 가진 덩어리를 찾아
    (img, link) 페어 목록으로 반환.
    """
    try:
        json_text = page.evaluate("() => (document.getElementById('__NEXT_DATA__') || {}).textContent || ''")
    except Exception:
        json_text = ""
    if not json_text:
        return []

    try:
        data = json.loads(json_text)
    except Exception:
        return []

    pairs: List[Dict] = []

    def walk(node):
        if isinstance(node, dict):
            flat = []
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)
                else:
                    flat.append(v)
            imgs, links = [], []
            for s in flat:
                if not isinstance(s, str):
                    continue
                if looks_like_img(s):
                    imgs.append(s)
                elif s.startswith("/") and not looks_like_img(s):
                    links.append(s)
            if imgs and links:
                for si in imgs:
                    pairs.append({"img": si, "link": urljoin(BASE_URL, links[0])})
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)

    # (basename, link)로 유니크 처리
    uniq = {}
    for p in pairs:
        key = (url_basename(p["img"]), p["link"])
        uniq[key] = p
    return list(uniq.values())

# -------------------------
# Playwright로 전체 슬라이드 수집
# -------------------------
def scrape_all_banners() -> pd.DataFrame:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        # 네트워크 안정화까지 대기
        page.goto(BASE_URL, wait_until="networkidle")
        # 슬라이드 img들 로딩 대기(최소 1개)
        try:
            page.wait_for_selector(".main-banner-ggs img", timeout=15000)
        except Exception:
            pass  # 없으면 빈 결과로 진행

        # 숨겨진 배너 포함 전체 수집
        imgs = page.query_selector_all(".main-banner-ggs img")

        data = []
        for img in imgs:
            alt = img.get_attribute("alt") or ""
            src = img.get_attribute("src") or ""
            srcset = img.get_attribute("srcset") or ""

            # 절대 경로화
            if src and not src.startswith("http"):
                src = urljoin(BASE_URL, src)

            modified_srcset = parse_srcset_modified(srcset)

            data.append({
                "Link": "",              # 나중에 채움
                "Alt": alt,
                "Src": src,
                "Srcset": modified_srcset
            })

        # __NEXT_DATA__에서 (img, link) 페어 수집 후 매칭
        pairs = collect_next_pairs_from_page(page)
        # 인덱스: 이미지 basename -> 링크 후보 세트
        index = {}
        for pz in pairs:
            b = url_basename(pz["img"])
            index.setdefault(b, set()).add(pz["link"])

        # 1차 동일 basename, 2차 근사 매칭
        for row in data:
            if row["Link"]:
                continue
            b = url_basename(row["Src"])
            if b in index and index[b]:
                row["Link"] = sorted(index[b])[0]
                continue
            # 근사 매칭
            best_link, best_score = "", 0
            for pb, links in index.items():
                score = lcs_len(b, pb)
                if score > best_score:
                    best_score = score
                    best_link = sorted(links)[0]
            if best_link and best_score >= max(5, len(b)//3):
                row["Link"] = best_link

        # 못 채운 Link는 홈으로 보정(원하면 빈 값 유지로 바꿔도 됨)
        for row in data:
            if not row["Link"]:
                row["Link"] = BASE_URL

        browser.close()

    df = pd.DataFrame(data).drop_duplicates()
    # 컬럼 보장
    df = df.reindex(columns=["Link", "Alt", "Srcset"], fill_value="")
    return df

# -------------------------
# Google Drive 업로드
# -------------------------
def upload_to_gdrive(local_path: str, filename: str) -> str:
    import json as _json
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    folder_id = os.environ.get("GDRIVE_FOLDER_ID")
    drive_id = os.environ.get("GDRIVE_DRIVE_ID")
    raw_json = os.environ.get("GDRIVE_CREDENTIALS_JSON")
    sa_path = os.environ.get("GDRIVE_SA_JSON_PATH")

    if not folder_id:
        raise RuntimeError("GDRIVE_FOLDER_ID가 설정되지 않았습니다.")

    scopes = ["https://www.googleapis.com/auth/drive"]
    creds = None
    if raw_json:
        info = _json.loads(raw_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    if creds is None:
        if not sa_path:
            sa_path = "gdrive_sa.json"
        creds = service_account.Credentials.from_service_account_file(sa_path, scopes=scopes)

    drive = build("drive", "v3", credentials=creds)

    query = f"name = '{filename}' and '{folder_id}' in parents and trashed = false"
    list_kwargs = {
        "q": query,
        "fields": "files(id,name)",
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True,
    }
    if drive_id:
        list_kwargs.update({"driveId": drive_id, "corpora": "drive"})

    resp = drive.files().list(**list_kwargs).execute()
    files = resp.get("files", [])
    media = MediaFileUpload(local_path, mimetype="text/csv", resumable=True)

    if files:
        file_id = files[0]["id"]
        drive.files().update(
            fileId=file_id,
            media_body=media,
            supportsAllDrives=True,
        ).execute()
        return file_id
    else:
        metadata = {"name": filename, "parents": [folder_id]}
        file = drive.files().create(
            body=metadata,
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ).execute()
        return file["id"]

# -------------------------
# main
# -------------------------
def main():
    df = scrape_all_banners()
    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    print(f"[OK] {len(df)}개 배너(보이는+숨겨진 전체) 수집 완료 → {OUTPUT_CSV}")

    print("[INFO] Google Drive 업로드 시작…")
    file_id = upload_to_gdrive(OUTPUT_CSV, "jasoseol_banner.csv")
    print(f"[OK] Drive 업로드 완료 fileId={file_id}")

if __name__ == "__main__":
    main()
