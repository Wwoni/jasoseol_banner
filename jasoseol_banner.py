# jasoseol_banner.py
import os, json, time
import pandas as pd
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote
from typing import List, Dict, Optional, Any
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# Google Drive
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

BASE_URL = "https://jasoseol.com/"
OUTPUT_CSV = "jasoseol_banner.csv"
DEBUG_DIR = Path("debug")
DEBUG_DIR.mkdir(exist_ok=True)

# =========================
# Small utils
# =========================
def url_basename(url: str) -> str:
    if not url:
        return ""
    return os.path.basename(urlparse(unquote(url)).path)

def looks_like_img(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return any(ext in u for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif")) or ("/_next/image" in u and "url=" in u)

# =========================
# __NEXT_DATA__ 보조 페어 추출(보수적)
#   - 같은 오브젝트 안에 이미지/링크가 함께 있을 때만 페어링
# =========================
def collect_next_pairs(page) -> List[Dict[str, str]]:
    pairs: List[Dict[str, str]] = []
    tag = page.query_selector("script#__NEXT_DATA__")
    if not tag:
        return pairs
    try:
        data = json.loads(tag.inner_text())
    except Exception:
        return pairs

    def walk(node: Any):
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

    # 유니크(이미지 파일명 + 링크)
    seen = set()
    uniq = []
    for p in pairs:
        key = (url_basename(p["img"]), p["link"])
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    # 디버그 덤프
    (DEBUG_DIR / "next_pairs.json").write_text(json.dumps(uniq, ensure_ascii=False, indent=2), encoding="utf-8")
    return uniq

# =========================
# 디버그 훅: 콘솔/네트워크 요청/응답 기록
# =========================
def attach_debug_listeners(page, capture_list: List[Dict]):
    page.on("console", lambda msg: capture_list.append(
        {"type": "console", "text": msg.text(), "level": msg.type()}))
    page.on("request", lambda req: capture_list.append(
        {"type": "request", "url": req.url, "method": req.method, "resource": req.resource_type}))
    page.on("response", lambda res: capture_list.append(
        {"type": "response", "url": res.url, "status": res.status}))

# =========================
# 클릭 & 캡처: 해당 이미지가 포함된 배너 컨테이너를 클릭해 랜딩 URL 획득
# =========================
def click_and_capture(page, container, img, idx: int) -> Optional[str]:
    # 클릭 전 DOM 저장
    (DEBUG_DIR / f"before_{idx}.html").write_text(page.content(), encoding="utf-8")

    logs: List[Dict] = []
    attach_debug_listeners(page, logs)

    origin_url = page.url
    real_url: Optional[str] = None

    # 자동 회전 간섭 완화
    try:
        img.scroll_into_view_if_needed(timeout=2000)
        container.hover(timeout=2000)
    except PWTimeout:
        pass

    # 1) 팝업 시도
    try:
        with page.expect_event("popup", timeout=3000) as pop_waiter:
            container.click(force=True, timeout=2500)
        new_page = pop_waiter.value
        try:
            new_page.wait_for_load_state("domcontentloaded", timeout=6000)
        except PWTimeout:
            pass
        real_url = new_page.url
        # 팝업 페이지 URL도 기록
        with (DEBUG_DIR / f"popup_{idx}.txt").open("w", encoding="utf-8") as f:
            f.write(real_url or "")
        new_page.close()
    except PWTimeout:
        # 2) 동일 탭 라우팅
        try:
            container.click(force=True, timeout=2500)
        except PWTimeout:
            pass
        # URL 변경 또는 네트워크 안정 대기
        changed = False
        for _ in range(4):
            try:
                page.wait_for_load_state("networkidle", timeout=2000)
            except PWTimeout:
                pass
            if page.url != origin_url:
                changed = True
                break
            time.sleep(0.2)

        if changed:
            real_url = page.url
            # 원래 페이지로 복귀
            try:
                page.go_back(wait_until="networkidle", timeout=6000)
            except PWTimeout:
                page.goto(BASE_URL, wait_until="networkidle", timeout=15000)
                page.wait_for_selector(".main-banner-ggs img", timeout=15000)

    # 클릭 후 DOM 저장 + 로그 저장
    (DEBUG_DIR / f"after_{idx}.html").write_text(page.content(), encoding="utf-8")
    with (DEBUG_DIR / f"requests_{idx}.jsonl").open("w", encoding="utf-8") as f:
        for row in logs:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return real_url

# =========================
# 메인 스크레이퍼
# =========================
def scrape_all() -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        # CSR 완성까지 대기
        page.goto(BASE_URL, timeout=45000, wait_until="networkidle")
        page.wait_for_selector(".main-banner-ggs img", timeout=20000)

        # __NEXT_DATA__ 보조 인덱스
        next_pairs = collect_next_pairs(page)
        pair_by_name: Dict[str, set] = {}
        for pr in next_pairs:
            pair_by_name.setdefault(url_basename(pr["img"]), set()).add(pr["link"])

        # 전체 배너 이미지 목록
        img_loc = page.locator(".main-banner-ggs img")
        count = img_loc.count()

        for i in range(count):
            img = img_loc.nth(i)
            container = page.locator(".main-banner-ggs").filter(has=img)

            alt = img.get_attribute("alt") or ""
            src = img.get_attribute("src") or ""
            if src and not src.startswith("http"):
                src = urljoin(BASE_URL, src)

            link_final: Optional[str] = click_and_capture(page, container, img, i)

            # 클릭으로 못 얻으면 __NEXT_DATA__로 보완(같은 파일명일 때만)
            if not link_final:
                bname = url_basename(src)
                if bname in pair_by_name and pair_by_name[bname]:
                    link_final = sorted(pair_by_name[bname])[0]

            # 그래도 없으면 빈 값(가짜 링크 금지)
            rows.append({"Alt": alt, "Src": src, "Link": link_final or ""})

        browser.close()
    return rows

# =========================
# Google Drive 업로드
# =========================
def upload_to_gdrive(local_path: str, filename: str) -> str:
    folder_id = os.environ.get("GDRIVE_FOLDER_ID")
    raw_json = os.environ.get("GDRIVE_CREDENTIALS_JSON")
    sa_path = os.environ.get("GDRIVE_SA_JSON_PATH", "gdrive_sa.json")
    if not folder_id:
        raise RuntimeError("GDRIVE_FOLDER_ID가 설정되지 않았습니다.")

    scopes = ["https://www.googleapis.com/auth/drive"]
    creds = (service_account.Credentials.from_service_account_info(json.loads(raw_json), scopes=scopes)
             if raw_json else
             service_account.Credentials.from_service_account_file(sa_path, scopes=scopes))
    drive = build("drive", "v3", credentials=creds)

    q = f"name = '{filename}' and '{folder_id}' in parents and trashed = false"
    try:
        resp = drive.files().list(
            q=q,
            fields="files(id,name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
    except HttpError as he:
        raise RuntimeError(f"폴더/리스트 조회 실패: {he}")

    files = resp.get("files", [])
    media = MediaFileUpload(local_path, mimetype="text/csv", resumable=True)

    if files:
        fid = files[0]["id"]
        drive.files().update(fileId=fid, media_body=media, supportsAllDrives=True).execute()
        return fid
    else:
        meta = {"name": filename, "parents": [folder_id]}
        file = drive.files().create(
            body=meta, media_body=media, fields="id", supportsAllDrives=True
        ).execute()
        return file["id"]

# =========================
# main
# =========================
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
