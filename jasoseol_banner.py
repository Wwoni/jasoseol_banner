# jasoseol_banner.py
import os
import re
import time
import json
import pandas as pd
from urllib.parse import unquote
from typing import List, Dict, Tuple

BASE_URL = "https://jasoseol.com/"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ------------------------- Google Drive -------------------------
def upload_to_gdrive(local_path: str, filename: str) -> str:
    import json as _json
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError

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
        if not os.path.exists(sa_path):
            raise FileNotFoundError(f"서비스계정 파일을 찾을 수 없습니다: {sa_path}")
        creds = service_account.Credentials.from_service_account_file(sa_path, scopes=scopes)

    drive = build("drive", "v3", credentials=creds)

    # 폴더 확인
    try:
        folder_meta = drive.files().get(
            fileId=folder_id, fields="id,name,driveId,mimeType", supportsAllDrives=True
        ).execute()
        if folder_meta.get("mimeType") != "application/vnd.google-apps.folder":
            raise RuntimeError(f"GDRIVE_FOLDER_ID가 폴더가 아닙니다: {folder_meta.get('mimeType')}")
    except HttpError as he:
        raise RuntimeError(f"폴더 조회 실패: {he}")

    # 동일 파일명 검색
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
        drive.files().update(fileId=file_id, media_body=media, supportsAllDrives=True).execute()
        return file_id
    else:
        metadata = {"name": filename, "parents": [folder_id]}
        file = drive.files().create(
            body=metadata, media_body=media, fields="id", supportsAllDrives=True
        ).execute()
        return file["id"]

# ------------------------- Playwright helpers -------------------------
def close_modal_if_present(page) -> None:
    """초기 모달을 최대한 닫는다(버튼/ESC/JS 강제 숨김)."""
    try:
        selectors = [
            "button:has-text('오늘 하루 보지')",
            "button:has-text('닫기')",
            "text=오늘 하루 보지 않기",
            "text=닫기",
            "[aria-label='close']",
            "[aria-label='Close']",
            "img[alt='close'], img[alt='Close']",
        ]
        for sel in selectors:
            loc = page.locator(sel)
            if loc.count() > 0:
                try:
                    loc.first.click(timeout=800, force=True)
                    return
                except Exception:
                    pass
        # ESC
        try:
            page.keyboard.press("Escape")
            return
        except Exception:
            pass
        # 최후: 큰 overlay 숨김
        page.evaluate(
            """
            (() => {
              const els = Array.from(document.querySelectorAll('div'));
              for (const el of els) {
                const w = el.getBoundingClientRect().width;
                const h = el.getBoundingClientRect().height;
                const c = el.className ? el.className.toString() : '';
                if ((w >= 400 && h >= 200) && (c.includes('overflow-hidden') || c.includes('fixed') || c.includes('backdrop'))) {
                  el.style.setProperty('display','none','important');
                }
              }
            })()
            """
        )
    except Exception:
        pass

def get_counter(page) -> Tuple[int,int]:
    """'X / Y' 형태 카운터에서 (X,Y) 반환. 실패 시 (cur, total)=(1, count(.main-banner-ggs))."""
    cur = 1
    total = 0
    try:
        total = page.locator(".main-banner-ggs").count()
    except Exception:
        pass
    try:
        node = page.locator("div:has-text('/ ')").first
        txt = node.inner_text(timeout=1500).strip()
        m = re.search(r"(\d+)\s*/\s*(\d+)", txt)
        if m:
            cur = int(m.group(1))
            total = int(m.group(2))
    except Exception:
        pass
    return cur, max(total, 0)

def get_active_slide(page):
    loc = page.locator(".main-banner-ggs.opacity-100").first
    if loc.count() == 0:
        loc = page.locator(".main-banner-ggs.z-1").first
    if loc.count() == 0:
        loc = page.locator(".main-banner-ggs").first
    return loc

def read_slide_signature(slide) -> Tuple[str,str]:
    """현재 보이는 슬라이드의 (title, src) 반환."""
    alt, src = "", ""
    try:
        img = slide.locator("img").first
        alt = (img.get_attribute("alt") or "").strip()
        src = unquote(img.get_attribute("src") or "")
    except Exception:
        pass
    return alt, src

def go_next(page):
    """오른쪽 화살표/키보드로 다음 슬라이드."""
    try:
        next_arrow = page.locator("img[alt='right icon']").first
        if next_arrow.count() == 0:
            next_arrow = page.locator("img[src*='ic_arrow_right']").first
        if next_arrow.count() > 0:
            next_arrow.click(force=True)
            return
    except Exception:
        pass
    try:
        page.keyboard.press("ArrowRight")
    except Exception:
        pass

def wait_active_src(page, target_src: str, max_steps: int = 20) -> bool:
    """
    현재 보이는 슬라이드의 img src가 target_src가 될 때까지 앞으로 넘겨서 맞춘다.
    """
    for _ in range(max_steps):
        slide = get_active_slide(page)
        _, cur_src = read_slide_signature(slide)
        if cur_src == target_src:
            return True
        go_next(page)
        time.sleep(0.35)
    return False

def capture_link_by_click(page, slide) -> str:
    """슬라이드를 실제 클릭하여 팝업/동일탭 이동 URL을 얻는다."""
    link_url = ""
    # 1) 새 탭
    try:
        with page.expect_popup(timeout=1200) as pop:
            slide.click(force=True)
        new = pop.value
        new.wait_for_load_state("domcontentloaded", timeout=5000)
        link_url = new.url
        new.close()
        return link_url
    except Exception:
        pass
    # 2) 동일 탭
    old = page.url
    try:
        slide.click(force=True)
        page.wait_for_load_state("domcontentloaded", timeout=2500)
        if page.url != old:
            link_url = page.url
            page.go_back(wait_until="domcontentloaded")
    except Exception:
        pass
    return link_url

def scrape_banners_via_playwright() -> List[Dict[str, str]]:
    from playwright.sync_api import sync_playwright

    rows: List[Dict[str, str]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1400, "height": 900})
        page = ctx.new_page()
        page.goto(BASE_URL, wait_until="domcontentloaded")

        # 0) 모달 닫기
        close_modal_if_present(page)

        # 1) 패스1: 슬라이드 식별 수집(Title, Src)만 고유하게 모음
        seen_src = set()
        unique_slides: List[Tuple[str,str]] = []  # (title, src)

        # 캐러셀 정보
        page.wait_for_selector(".main-banner-ggs", timeout=10000)
        _, total = get_counter(page)
        total = max(total, 1)

        guard = 0
        while len(unique_slides) < total and guard < total * 3:
            guard += 1
            slide = get_active_slide(page)
            title, src = read_slide_signature(slide)
            if src and src not in seen_src:
                seen_src.add(src)
                unique_slides.append((title, src))
            go_next(page)
            time.sleep(0.35)

        # 2) 패스2: 각 Src로 화면을 맞추고 실제 클릭하여 Link 채우기
        for title, src in unique_slides:
            ok = wait_active_src(page, src, max_steps=total + 5)
            link = ""
            if ok:
                slide = get_active_slide(page)
                link = capture_link_by_click(page, slide)
            rows.append({"Title": title, "Src": src, "Link": link})

        browser.close()

    return rows

# ------------------------- main -------------------------
def main():
    rows = scrape_banners_via_playwright()

    # 중복 방지: Src 기준으로 1개만 유지
    uniq = {}
    for r in rows:
        key = r.get("Src") or r.get("Title")
        if key and key not in uniq:
            uniq[key] = r
    final = list(uniq.values())

    df = pd.DataFrame(final, columns=["Title", "Link", "Src"])
    out_csv = "jasoseol_banner.csv"
    df.to_csv(out_csv, index=False, encoding="utf-8-sig", lineterminator="\n")
    print(f"[OK] {len(df)}개 배너 수집 완료 → {out_csv}")

    print("[INFO] Google Drive 업로드 시작…")
    file_id = upload_to_gdrive(out_csv, "jasoseol_banner.csv")
    print(f"[OK] Drive 업로드 완료 fileId={file_id}")

if __name__ == "__main__":
    main()
