# jasoseol_banner.py
import os
import re
import json
import time
import pandas as pd
from urllib.parse import urljoin, urlparse, unquote
from typing import List, Dict

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

# ------------------------- Playwright Scraper -------------------------
def close_modal_if_present(page) -> bool:
    """
    최초 진입 모달을 닫는다. 여러 전략을 순차 시도:
      - 닫기/오늘하루/close 텍스트 버튼
      - X 아이콘(alt, aria-label)
      - ESC
      - 최후: JS로 모달 컨테이너 display:none
    성공 시 True, 없거나 실패 시 False
    """
    closed = False
    try:
        # 1) 텍스트 기반 버튼
        candidates = [
            "button:has-text('오늘 하루 보지')",
            "button:has-text('닫기')",
            "text=오늘 하루 보지 않기",
            "text=닫기",
            "[aria-label='close']",
            "[aria-label='Close']",
            "img[alt='close'], img[alt='Close']",
        ]
        for sel in candidates:
            loc = page.locator(sel)
            if loc.count() > 0:
                try:
                    loc.first.click(timeout=1000, force=True)
                    closed = True
                    break
                except Exception:
                    pass

        # 2) ESC 시도
        if not closed:
            try:
                page.keyboard.press("Escape")
                closed = True  # 많은 모달이 ESC로 닫힘
            except Exception:
                pass

        # 3) 여전히 가려지면 최후 수단: 모달로 추정되는 큰 overlay를 숨김
        if not closed:
            # 모달 컨테이너 패턴(업로드한 HTML 참고: w-[550px] h-[400px] overflow-hidden)
            page.evaluate(
                """
                (() => {
                  const isModal = (el) => {
                    const c = el.className ? el.className.toString() : '';
                    return c.includes('overflow-hidden') || c.includes('fixed') || c.includes('backdrop');
                  };
                  const els = Array.from(document.querySelectorAll('div'));
                  let changed = 0;
                  for (const el of els) {
                    if (isModal(el) && el.getBoundingClientRect().width >= 400 && el.getBoundingClientRect().height >= 200) {
                      el.style.setProperty('display','none','important');
                      changed++;
                    }
                  }
                  return changed;
                })()
                """
            )
            closed = True
    except Exception:
        pass
    return closed

def get_total_slides(page) -> int:
    # "X / Y" 카운터에서 Y 추출
    try:
        counter = page.locator("div:has-text('/ ')").first
        txt = counter.inner_text(timeout=1500).strip()
        m = re.search(r"/\s*(\d+)", txt)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    # fallback: DOM 개수
    try:
        return page.locator(".main-banner-ggs").count()
    except Exception:
        return 0

def scrape_banners_via_playwright() -> List[Dict[str, str]]:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    rows: List[Dict[str, str]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1400, "height": 900})
        page = ctx.new_page()

        page.goto(BASE_URL, wait_until="domcontentloaded")

        # 모달 닫기 시도
        _ = close_modal_if_present(page)
        # 배너 영역 보장
        page.wait_for_selector(".main-banner-ggs", timeout=10000)

        total = max(get_total_slides(page), 1)

        # 우측 화살표
        next_arrow = page.locator("img[alt='right icon']").first
        if next_arrow.count() == 0:
            next_arrow = page.locator("img[src*='ic_arrow_right']").first

        def visible_slide():
            loc = page.locator(".main-banner-ggs.opacity-100").first
            if loc.count() == 0:
                loc = page.locator(".main-banner-ggs.z-1").first
            if loc.count() == 0:
                loc = page.locator(".main-banner-ggs").first
            return loc

        def extract_img(slide):
            alt, src = "", ""
            try:
                img = slide.locator("img").first
                alt = (img.get_attribute("alt") or "").strip()
                src = unquote(img.get_attribute("src") or "")
            except Exception:
                pass
            return alt, src

        def click_and_capture(slide) -> str:
            link_url = ""
            # 1) 새 탭 팝업
            try:
                with page.expect_popup(timeout=1200) as pop:
                    slide.click(force=True)
                new = pop.value
                new.wait_for_load_state("domcontentloaded", timeout=5000)
                link_url = new.url
                new.close()
                return link_url
            except PWTimeout:
                pass
            except Exception:
                pass
            # 2) 동일 탭 이동
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

        seen = set()
        for i in range(total):
            s = visible_slide()
            time.sleep(0.25)  # 전환 안정화

            alt, src = extract_img(s)
            link = click_and_capture(s)

            key = (src, link)
            if src and key not in seen:
                rows.append({"Title": alt, "Link": link, "Src": src})
                seen.add(key)

            if i < total - 1:
                # 다음 슬라이드
                try:
                    next_arrow.click(force=True)
                except Exception:
                    try:
                        page.keyboard.press("ArrowRight")
                    except Exception:
                        pass
                time.sleep(0.4)

        browser.close()

    return rows

# ------------------------- main -------------------------
def main():
    rows = scrape_banners_via_playwright()

    df = pd.DataFrame(rows)
    for col in ["Title", "Link", "Src"]:
        if col not in df.columns:
            df[col] = ""
    df = df.drop_duplicates(subset=["Src", "Link"]).reset_index(drop=True)

    out_csv = "jasoseol_banner.csv"
    df.to_csv(out_csv, index=False, encoding="utf-8-sig", lineterminator="\n")
    print(f"[OK] {len(df)}개 배너 수집 완료 → {out_csv}")

    print("[INFO] Google Drive 업로드 시작…")
    file_id = upload_to_gdrive(out_csv, "jasoseol_banner.csv")
    print(f"[OK] Drive 업로드 완료 fileId={file_id}")

if __name__ == "__main__":
    main()
