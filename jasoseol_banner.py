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
# __NEXT_DATA__ 보조 추출(보수적)
# -------------------------
def collect_next_pairs(page) -> List[Dict]:
    pairs: List[Dict] = []
    tag = page.query_selector("script#__NEXT_DATA__")
    if not tag:
        return pairs
    try:
        data = json.loads(tag.inner_text())

        CANDIDATE_KEYS = {"link", "url", "href", "landingUrl", "targetUrl"}

        def walk(node):
            if isinstance(node, dict):
                # 같은 오브젝트 범위에서 문자열만 평가
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
# Playwright: 이미지가 포함된 배너 컨테이너를 클릭 → 실제 랜딩 URL 캡처
# -------------------------
def scrape_all() -> List[Dict]:
    rows: List[Dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BASE_URL, timeout=45000, wait_until="networkidle")
        page.wait_for_selector(".main-banner-ggs img", timeout=20000)

        # 자동회전 간섭 완화
        wrapper = page.locator(".main-banner-ggs").first
        try:
            wrapper.hover(timeout=2000)
        except PWTimeout:
            pass

        img_loc = page.locator(".main-banner-ggs img")
        count = img_loc.count()

        # __NEXT_DATA__ 인덱스 (보조용)
        next_pairs = collect_next_pairs(page)
        by_name = {}
        for pz in next_pairs:
            b = url_basename(pz["img"])
            by_name.setdefault(b, set()).add(pz["link"])

        for i in range(count):
            img = img_loc.nth(i)
            # 이 이미지가 “포함된” 배너 컨테이너
            container = page.locator(".main-banner-ggs").filter(has=img)

            # Alt/Src
            alt = img.get_attribute("alt") or ""
            src = img.get_attribute("src") or ""
            if src and not src.startswith("http"):
                src = urljoin(BASE_URL, src)

            link_final: Optional[str] = None
            origin_url = page.url

            # 배너 컨테이너를 정확히 클릭
            try:
                img.scroll_into_view_if_needed(timeout=2000)
                container.hover(timeout=2000)
            except PWTimeout:
                pass

            # 1) 팝업 우선
            try:
                with page.expect_event("popup", timeout=2500) as pop_waiter:
                    container.click(force=True, timeout=2000)
                new_page = pop_waiter.value
                try:
                    new_page.wait_for_load_state("domcontentloaded", timeout=6000)
                except PWTimeout:
                    pass
                link_final = new_page.url
                new_page.close()
            except PWTimeout:
                # 2) 동일 탭 라우팅
                try:
                    container.click(force=True, timeout=2000)
                except PWTimeout:
                    pass

                # URL 변경 또는 히스토리 변화를 잠시 대기
                changed = False
                for _ in range(3):
                    try:
                        page.wait_for_load_state("networkidle", timeout=2000)
                    except PWTimeout:
                        pass
                    if page.url != origin_url:
                        changed = True
                        break
                if changed:
                    link_final = page.url
                    # 원래 페이지 복귀
                    try:
                        page.go_back(wait_until="networkidle", timeout=6000)
                    except PWTimeout:
                        page.goto(BASE_URL, wait_until="networkidle", timeout=15000)
                        page.wait_for_selector(".main-banner-ggs img", timeout=15000)

            # 3) 클릭에서 못 얻었으면 __NEXT_DATA__로만 보완(동일 파일명일 때만)
            if not link_final:
                bname = url_basename(src)
                if bname in by_name and by_name[bname]:
                    link_final = sorted(by_name[bname])[0]

            # 4) 그래도 없으면 '빈 값' 유지(가짜 링크 방지)
            rows.append({"Alt": alt, "Src": src, "Link": link_final or ""})

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
    creds = service_account.Credentials.from_service_account_info(json.loads(raw_json), scopes=scopes) if raw_json \
        else service_account.Credentials.from_service_account_file(sa_path, scopes=scopes)
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

