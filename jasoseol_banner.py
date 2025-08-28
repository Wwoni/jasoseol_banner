import os
import json
import pandas as pd
from urllib.parse import urljoin, urlparse, unquote
from typing import List, Dict
from playwright.sync_api import sync_playwright
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

BASE_URL = "https://jasoseol.com/"
OUTPUT_CSV = "jasoseol_banner.csv"

# --------------------
# Helpers
# --------------------
def url_basename(url: str) -> str:
    if not url:
        return ""
    u = unquote(url)
    return os.path.basename(urlparse(u).path)

def looks_like_img(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return any(ext in u for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"]) or ("/_next/image" in u and "url=" in u)

def lcs_len(a: str, b: str) -> int:
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    best = 0
    for i in range(len(short)):
        for j in range(i + 1, len(short) + 1):
            seg = short[i:j]
            if seg and seg in long:
                best = max(best, j - i)
    return best

# --------------------
# Playwright 크롤링
# --------------------
def scrape_banners() -> List[Dict]:
    rows: List[Dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BASE_URL, timeout=40000, wait_until="networkidle")

        # 모든 슬라이드 이미지 로딩 대기
        page.wait_for_selector(".main-banner-ggs img", timeout=15000)

        # DOM에서 Alt, Src 수집 (보이는+숨김 모두)
        imgs = page.query_selector_all(".main-banner-ggs img")
        for img in imgs:
            alt = img.get_attribute("alt") or ""
            src = img.get_attribute("src") or ""
            if src and not src.startswith("http"):
                src = urljoin(BASE_URL, src)
            rows.append({"Alt": alt, "Src": src, "Link": ""})

        # __NEXT_DATA__ 파싱해서 (img, link) 페어 수집
        pairs: List[Dict] = []
        tag = page.query_selector("script#__NEXT_DATA__")
        if tag:
            try:
                data = json.loads(tag.inner_text())

                def walk(node):
                    if isinstance(node, dict):
                        # 이 dict 안의 단순 문자열 모아 판단
                        flat = []
                        for v in node.values():
                            if isinstance(v, (dict, list)):
                                walk(v)
                            else:
                                flat.append(v)
                        imgs_, links_ = [], []
                        for s in flat:
                            if not isinstance(s, str):
                                continue
                            if looks_like_img(s):
                                imgs_.append(s)
                            # 여기! 절대URL(https://...)도 링크로 인정
                            elif s.startswith("/") or s.startswith("http"):
                                if not looks_like_img(s):
                                    links_.append(s)
                        if imgs_ and links_:
                            for si in imgs_:
                                link_abs = urljoin(BASE_URL, links_[0]) if links_[0].startswith("/") else links_[0]
                                pairs.append({"img": si, "link": link_abs})

                    elif isinstance(node, list):
                        for v in node:
                            walk(v)

                walk(data)
            except Exception:
                pass

        # 이미지 ↔ 링크 매칭 (파일명 동일 → 근사 유사도)
        index = {}
        for pz in pairs:
            b = url_basename(pz["img"])
            index.setdefault(b, set()).add(pz["link"])

        for r in rows:
            if r["Link"]:
                continue
            b = url_basename(r["Src"])
            if not b:
                continue
            # exact
            if b in index and index[b]:
                r["Link"] = sorted(index[b])[0]
                continue
            # fuzzy
            best_link, best_score = "", 0
            for pb, links in index.items():
                score = lcs_len(b, pb)
                if score > best_score:
                    best_score, best_link = score, sorted(links)[0]
            if best_link and best_score >= max(5, len(b)//3):
                r["Link"] = best_link

        # 여전히 비었으면 이미지 주소로 폴백 (요청 반영)
        for r in rows:
            if not r["Link"]:
                r["Link"] = r["Src"]

        browser.close()

    return rows

# --------------------
# Google Drive Upload
# --------------------
def upload_to_gdrive(local_path: str, filename: str) -> str:
    folder_id = os.environ.get("GDRIVE_FOLDER_ID")
    raw_json = os.environ.get("GDRIVE_CREDENTIALS_JSON")
    sa_path = os.environ.get("GDRIVE_SA_JSON_PATH", "gdrive_sa.json")
    if not folder_id:
        raise RuntimeError("GDRIVE_FOLDER_ID가 설정되지 않았습니다.")

    scopes = ["https://www.googleapis.com/auth/drive"]
    creds = None
    if raw_json:
        info = json.loads(raw_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    else:
        creds = service_account.Credentials.from_service_account_file(sa_path, scopes=scopes)

    drive = build("drive", "v3", credentials=creds)
    q = f"name = '{filename}' and '{folder_id}' in parents and trashed = false"
    resp = drive.files().list(q=q, fields="files(id,name)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    files = resp.get("files", [])
    media = MediaFileUpload(local_path, mimetype="text/csv", resumable=True)

    if files:
        fid = files[0]["id"]
        drive.files().update(fileId=fid, media_body=media, supportsAllDrives=True).execute()
        return fid
    else:
        meta = {"name": filename, "parents": [folder_id]}
        file = drive.files().create(body=meta, media_body=media, fields="id", supportsAllDrives=True).execute()
        return file["id"]

# --------------------
# main
# --------------------
def main():
    rows = scrape_banners()
    df = pd.DataFrame(rows).drop_duplicates()
    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig", lineterminator="\n")
    print(f"[OK] {len(df)}개 배너 수집 완료 → {OUTPUT_CSV}")

    print("[INFO] Google Drive 업로드 시작…")
    fid = upload_to_gdrive(OUTPUT_CSV, "jasoseol_banner.csv")
    print(f"[OK] Drive 업로드 완료 fileId={fid}")

if __name__ == "__main__":
    main()

