# jasoseol_banner.py
import os, json
import pandas as pd
from urllib.parse import urljoin, urlparse, unquote
from typing import List, Dict, Tuple, Optional
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

BASE_URL = "https://jasoseol.com/"
OUTPUT_CSV = "jasoseol_banner.csv"

# -------------------------
# 작은 유틸들
# -------------------------
def url_basename(url: str) -> str:
    if not url:
        return ""
    return os.path.basename(urlparse(unquote(url)).path)

def looks_like_img(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return (".png" in u) or (".jpg" in u) or (".jpeg" in u) or (".webp" in u) or (".gif" in u) or ("/_next/image" in u and "url=" in u)

def lcs_len(a: str, b: str) -> int:
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    best = 0
    for i in range(len(short)):
        for j in range(i + 1, len(short) + 1):
            seg = short[i:j]
            if seg and seg in long:
                best = max(best, j - i)
    return best

# -------------------------
# __NEXT_DATA__에서 (img, link) 페어 추출
# -------------------------
def collect_next_pairs(page: Page) -> List[Dict]:
    pairs: List[Dict] = []
    tag = page.query_selector("script#__NEXT_DATA__")
    if not tag:
        return pairs
    try:
        data = json.loads(tag.inner_text())

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
                    elif s.startswith("/") or s.startswith("http"):
                        # 이미지 문자열이 아닌 링크 후보만
                        if not looks_like_img(s):
                            links.append(s)
                if imgs and links:
                    # 동일 object 내에 같이 있던 첫 링크를 우선 채택
                    for si in imgs:
                        link_abs = urljoin(BASE_URL, links[0]) if links[0].startswith("/") else links[0]
                        pairs.append({"img": si, "link": link_abs})
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(data)
    except Exception:
        pass

    # 유니크 처리 (img basename + link)
    uniq = {}
    for p in pairs:
        key = (url_basename(p["img"]), p["link"])
        uniq[key] = p
    return list(uniq.values())

# -------------------------
# Playwright로 전체 수집 + 실제 클릭 URL 캡처
# -------------------------
def scrape_all() -> List[Dict]:
    rows: List[Dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BASE_URL, timeout=40000, wait_until="networkidle")
        page.wait_for_selector(".main-banner-ggs img", timeout=15000)

        # 1) DOM에서 Alt, Src, 노드 핸들 수집
        imgs = page.query_selector_all(".main-banner-ggs img")
        banners = page.query_selector_all(".main-banner-ggs")
        # 안전장치: 길이 차이나면 img 기준으로 맞춤
        n = min(len(imgs), len(banners)) if banners else len(imgs)

        temp = []
        for i in range(n):
            alt = imgs[i].get_attribute("alt") or ""
            src = imgs[i].get_attribute("src") or ""
            if src and not src.startswith("http"):
                src = urljoin(BASE_URL, src)
            temp.append({"Alt": alt, "Src": src, "Link": "", "_idx": i})

        # 2) __NEXT_DATA__ 페어 인덱스
        pairs = collect_next_pairs(page)
        idx = {}
        for pz in pairs:
            b = url_basename(pz["img"])
            idx.setdefault(b, set()).add(pz["link"])

        # 3) 먼저 JSON 매칭(정확/근사)
        for r in temp:
            b = url_basename(r["Src"])
            if b in idx and idx[b]:
                r["Link"] = sorted(idx[b])[0]
                continue
            # 근사 매칭
            best_link, best_score = "", 0
            for pb, links in idx.items():
                score = lcs_len(b, pb)
                if score > best_score:
                    best_score, best_link = score, sorted(links)[0]
            if best_link and best_score >= max(5, len(b)//3):
                r["Link"] = best_link

        # 4) 여전히 비었거나, 도메인이 이미지와 무관해 보이는 경우 → 실제 클릭으로 재확인
        for r in temp:
            need_click = False
            if not r["Link"]:
                need_click = True
            else:
                # 휴리스틱: 이미지 파일명/alt와 링크가 전혀 연관 없어 보이면 클릭으로 재검증
                host = urlparse(r["Link"]).netloc
                b = url_basename(r["Src"]).lower()
                alt = (r["Alt"] or "").lower()
                if ("jasoseol" not in host) and (lcs_len(host, b) < 3 and lcs_len(host, alt) < 3):
                    need_click = True

            if not need_click:
                continue

            i = r["_idx"]
            # 클릭 시 실제 이동 URL 캡처 (팝업/동일탭 내비게이션 모두 커버)
            origin_url = page.url
            real_url: Optional[str] = None

            try:
                with page.expect_event("popup", timeout=2000) as pop_watcher:
                    banners[i].click(force=True)
                new_page = pop_watcher.value
                try:
                    new_page.wait_for_load_state("domcontentloaded", timeout=5000)
                except PWTimeout:
                    pass
                real_url = new_page.url
                new_page.close()
            except PWTimeout:
                # 팝업이 아니면 동일 탭 내 라우팅일 수 있음
                try:
                    # URL 변경 대기 (Next.js 클라이언트 라우팅)
                    page.wait_for_function("url => url !== window.location.href", arg=origin_url, timeout=2000)
                except PWTimeout:
                    pass
                if page.url != origin_url:
                    real_url = page.url
                    # 원래 페이지로 복귀
                    try:
                        page.go_back(wait_until="domcontentloaded", timeout=5000)
                    except PWTimeout:
                        page.goto(BASE_URL, wait_until="networkidle", timeout=15000)

            # 클릭으로 얻은 URL이 있으면 교체
            if real_url:
                r["Link"] = real_url

        # 5) 최종 정리
        for r in temp:
            rows.append({"Alt": r["Alt"], "Src": r["Src"], "Link": r["Link"] or r["Src"]})

        browser.close()

    return rows

# -------------------------
# Google Drive 업로드
# -------------------------
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

# -------------------------
# main
# -------------------------
def main():
    rows = scrape_all()
    df = pd.DataFrame(rows).drop_duplicates()
    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig", lineterminator="\n")
    print(f"[OK] {len(df)}개 배너 수집 완료 → {OUTPUT_CSV}")

    print("[INFO] Google Drive 업로드 시작…")
    fid = upload_to_gdrive(OUTPUT_CSV, "jasoseol_banner.csv")
    print(f"[OK] Drive 업로드 완료 fileId={fid}")

if __name__ == "__main__":
    main()

