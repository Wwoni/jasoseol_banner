# jasoseol_banner.py
import os, json
import pandas as pd
from urllib.parse import urljoin, urlparse, unquote
from typing import List, Dict, Optional
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

BASE_URL = "https://jasoseol.com/"
OUTPUT_CSV = "jasoseol_banner.csv"

# -------------------------
# Utils
# -------------------------
def url_basename(url: str) -> str:
    if not url:
        return ""
    return os.path.basename(urlparse(unquote(url)).path)

def looks_like_img(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return any(ext in u for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"]) or ("/_next/image" in u and "url=" in u)

# -------------------------
# Fallback: __NEXT_DATA__에서 (img, link) 보수적 추출
# -------------------------
def collect_next_pairs(page) -> List[Dict]:
    pairs: List[Dict] = []
    tag = page.query_selector("script#__NEXT_DATA__")
    if not tag:
        return pairs
    try:
        data = json.loads(tag.inner_text())

        def walk(node):
            if isinstance(node, dict):
                # 같은 오브젝트 내 문자열만 평가
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
                    elif (s.startswith("/") or s.startswith("http")) and not looks_like_img(s):
                        links.append(s)

                # 이미지와 링크가 같은 오브젝트 안에 같이 있을 때만 페어링
                if imgs and links:
                    link0 = links[0]
                    link_abs = urljoin(BASE_URL, link0) if link0.startswith("/") else link0
                    for si in imgs:
                        pairs.append({"img": si, "link": link_abs})

            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(data)

        # 유니크
        seen = set()
        uniq = []
        for p in pairs:
            key = (url_basename(p["img"]), p["link"])
            if key not in seen:
                seen.add(key)
                uniq.append(p)
        return uniq
    except Exception:
        return pairs

# -------------------------
# Playwright: 각 이미지 "그 자체"를 클릭해 랜딩 URL 캡처
# -------------------------
def scrape_all() -> List[Dict]:
    rows: List[Dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        # 네트워크가 모두 잠잠할 때까지: CSR 렌더 완성
        page.goto(BASE_URL, timeout=45000, wait_until="networkidle")
        page.wait_for_selector(".main-banner-ggs img", timeout=20000)

        # 슬라이더 자동 회전 간섭 방지: 살짝 호버 & 스크롤
        first = page.locator(".main-banner-ggs img").first
        try:
            first.scroll_into_view_if_needed(timeout=2000)
            first.hover(timeout=2000)
        except PWTimeout:
            pass

        img_loc = page.locator(".main-banner-ggs img")
        count = img_loc.count()

        # __NEXT_DATA__ 보조 인덱스 (클릭 실패 시에만 사용)
        pairs = collect_next_pairs(page)
        by_name = {}
        for pz in pairs:
            b = url_basename(pz["img"])
            by_name.setdefault(b, set()).add(pz["link"])

        for i in range(count):
            img = img_loc.nth(i)

            # Alt, Src 확보
            alt = img.get_attribute("alt") or ""
            src = img.get_attribute("src") or ""
            if src and not src.startswith("http"):
                src = urljoin(BASE_URL, src)

            link_final: Optional[str] = None
            origin_url = page.url

            # 이미지 요소 자체 클릭 → 팝업 또는 동일 탭 네비게이션 감지
            try:
                img.scroll_into_view_if_needed(timeout=2000)
            except PWTimeout:
                pass

            # 1) 팝업 우선 감지
            try:
                with page.expect_event("popup", timeout=2000) as pop_waiter:
                    img.click(force=True, timeout=2000)
                new_page = pop_waiter.value
                try:
                    new_page.wait_for_load_state("domcontentloaded", timeout=6000)
                except PWTimeout:
                    pass
                link_final = new_page.url
                new_page.close()
            except PWTimeout:
                # 2) 동일 탭 라우팅 감지
                try:
                    img.click(force=True, timeout=2000)
                except PWTimeout:
                    pass

                # URL 변경 기다림
                try:
                    page.wait_for_load_state("networkidle", timeout=3000)
                except PWTimeout:
                    pass

                if page.url != origin_url:
                    link_final = page.url
                    # 원래 페이지로 복귀
                    try:
                        page.go_back(wait_until="networkidle", timeout=6000)
                    except PWTimeout:
                        page.goto(BASE_URL, wait_until="networkidle", timeout=15000)
                        page.wait_for_selector(".main-banner-ggs img", timeout=15000)

            # 3) 클릭으로 못 얻었으면 __NEXT_DATA__에서 보수적으로 매칭
            if not link_final:
                bname = url_basename(src)
                if bname in by_name and by_name[bname]:
                    link_final = sorted(by_name[bname])[0]

            # 4) 그래도 없으면 이미지 URL로 폴백
            if not link_final:
                link_final = src

            rows.append({"Alt": alt, "Src": src, "Link": link_final})

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
    creds = service_account.Credentials.from_service_account_info(json.loads(raw_json), scopes=scopes) if raw_json else service_account.Credentials.from_service_account_file(sa_path, scopes=scopes)
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
