# jasoseol_banner.py
import os, time, re, json
import pandas as pd
from urllib.parse import unquote
from typing import List, Dict, Tuple

BASE_URL = "https://jasoseol.com/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")

# ------------------------- Google Drive -------------------------
def upload_to_gdrive(local_path: str, filename: str) -> str:
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
        creds = service_account.Credentials.from_service_account_info(
            json.loads(raw_json), scopes=scopes
        )
    if creds is None:
        sa_path = sa_path or "gdrive_sa.json"
        if not os.path.exists(sa_path):
            raise FileNotFoundError(f"서비스계정 파일을 찾을 수 없습니다: {sa_path}")
        creds = service_account.Credentials.from_service_account_file(sa_path, scopes=scopes)
    drive = build("drive", "v3", credentials=creds)
    # 폴더 검증
    drive.files().get(fileId=folder_id, fields="id", supportsAllDrives=True).execute()
    # upsert
    q = f"name = '{filename}' and '{folder_id}' in parents and trashed = false"
    kw = dict(q=q, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True)
    if drive_id: kw.update(driveId=drive_id, corpora="drive")
    found = drive.files().list(**kw).execute().get("files", [])
    media = MediaFileUpload(local_path, mimetype="text/csv", resumable=True)
    if found:
        fid = found[0]["id"]
        drive.files().update(fileId=fid, media_body=media, supportsAllDrives=True).execute()
        return fid
    meta = {"name": filename, "parents": [folder_id]}
    created = drive.files().create(body=meta, media_body=media, fields="id",
                                   supportsAllDrives=True).execute()
    return created["id"]

# ------------------------- Playwright helpers -------------------------
def close_modal_if_present(page) -> None:
    try:
        sels = [
            "button:has-text('닫기')", "text=닫기",
            "button:has-text('오늘 하루')", "text=오늘 하루 보지 않기",
            "[aria-label='close']", "[aria-label='Close']",
            "img[alt='close']", "img[alt='Close']",
        ]
        for s in sels:
            loc = page.locator(s)
            if loc.count() > 0:
                try: loc.first.click(timeout=1000, force=True); return
                except Exception: pass
        try: page.keyboard.press("Escape"); return
        except Exception: pass
        # 최후 수단: 오버레이 숨김
        page.evaluate("""
        (() => { for (const el of document.querySelectorAll('div')) {
          const r=el.getBoundingClientRect();
          const c=String(el.className||'');
          if ((r.width>=400 && r.height>=200) && (c.includes('overflow-hidden')||c.includes('fixed')||c.includes('backdrop'))) {
            el.style.setProperty('display','none','important');
          }
        } })()
        """)
    except Exception:
        pass

def get_active_slide(page):
    loc = page.locator(".main-banner-ggs.opacity-100").first
    if loc.count()==0: loc = page.locator(".main-banner-ggs.z-1").first
    if loc.count()==0: loc = page.locator(".main-banner-ggs").first
    return loc

def read_slide_signature(slide) -> Tuple[str,str]:
    title, src = "", ""
    try:
        img = slide.locator("img").first
        title = (img.get_attribute("alt") or "").strip()
        src = unquote(img.get_attribute("src") or "")
    except Exception:
        pass
    return title, src

def go_next(page):
    try:
        nxt = page.locator("img[alt='right icon'], img[src*='ic_arrow_right']").first
        if nxt.count()>0:
            nxt.click(force=True); return
    except Exception:
        pass
    try: page.keyboard.press("ArrowRight")
    except Exception: pass

def wait_images_loaded(page):
    page.wait_for_selector(".main-banner-ggs img", timeout=12000)
    page.wait_for_function(
        """() => {
          const imgs = Array.from(document.querySelectorAll('.main-banner-ggs img'));
          return imgs.length>0 && imgs.every(i => i.getAttribute('src'));
        }""", timeout=12000
    )

def wait_slide_changed(page, prev_src: str, timeout_s=3.0):
    t0 = time.time()
    while time.time()-t0 < timeout_s:
        cur = read_slide_signature(get_active_slide(page))[1]
        if cur and cur != prev_src: return True
        time.sleep(0.05)
    return False

def wait_active_src(page, target_src: str, max_steps: int):
    for _ in range(max_steps):
        t, s = read_slide_signature(get_active_slide(page))
        if s == target_src: return True
        prev = s
        go_next(page)
        wait_slide_changed(page, prev, timeout_s=2.5)
        time.sleep(0.15)
    return False

def click_and_capture_url(page, slide) -> str:
    # 컨테이너 중앙 클릭 (상위 핸들러 보장)
    try:
        box = slide.bounding_box()
        if box:
            page.mouse.click(box["x"] + box["width"]/2, box["y"] + box["height"]/2)
    except Exception:
        try: slide.click(force=True)
        except Exception: pass

    # 1) 새 탭
    try:
        with page.expect_popup(timeout=5000) as pop:
            # 두 번째 클릭(일부 사이트는 첫 클릭에만 focus 변경)
            slide.click(force=True)
        new = pop.value
        new.wait_for_load_state("domcontentloaded", timeout=7000)
        url = new.url
        new.close()
        return url
    except Exception:
        pass
    # 2) 동일 탭
    old = page.url
    try:
        slide.click(force=True)
        page.wait_for_load_state("domcontentloaded", timeout=5000)
        if page.url != old:
            url = page.url
            page.go_back(wait_until="domcontentloaded")
            return url
    except Exception:
        pass
    return ""

def scrape_banners_via_playwright() -> List[Dict[str,str]]:
    from playwright.sync_api import sync_playwright
    rows: List[Dict[str,str]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1400, "height": 900})
        page = ctx.new_page()
        page.goto(BASE_URL, wait_until="domcontentloaded")
        close_modal_if_present(page)
        wait_images_loaded(page)

        total = page.locator(".main-banner-ggs").count()  # ← 신뢰 가능한 총 개수
        total = max(total, 1)

        # 패스1: 고유 슬라이드 모으기
        seen = set()
        unique: List[Tuple[str,str]] = []
        guard = 0
        while len(unique) < total and guard < total * 4:
            guard += 1
            slide = get_active_slide(page)
            title, src = read_slide_signature(slide)
            if src and (title, src) not in seen:
                seen.add((title, src))
                unique.append((title, src))
            prev = src
            go_next(page)
            wait_slide_changed(page, prev, timeout_s=2.5)
            time.sleep(0.15)

        # 패스2: 링크 채우기
        for title, src in unique:
            aligned = wait_active_src(page, src, max_steps=total + 6)
            link = ""
            if aligned:
                slide = get_active_slide(page)
                link = click_and_capture_url(page, slide)
            rows.append({"Title": title, "Link": link, "Src": src})

        browser.close()
    return rows

# ------------------------- main -------------------------
def main():
    rows = scrape_banners_via_playwright()

    # (Title, Src) 기준으로 중복 제거
    uniq = {}
    for r in rows:
        key = (r.get("Title") or "", r.get("Src") or "")
        if key not in uniq:
            uniq[key] = r
    final = list(uniq.values())

    df = pd.DataFrame(final, columns=["Title", "Link", "Src"])
    out_csv = "jasoseol_banner.csv"
    df.to_csv(out_csv, index=False, encoding="utf-8-sig", lineterminator="\n")
    print(f"[OK] {len(df)}개 배너 수집 완료 → {out_csv}")

    print("[INFO] Google Drive 업로드 시작…")
    fid = upload_to_gdrive(out_csv, "jasoseol_banner.csv")
    print(f"[OK] Drive 업로드 완료 fileId={fid}")

if __name__ == "__main__":
    main()