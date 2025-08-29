# jasoseol_banner.py
import os, time, re, json, pathlib, traceback
import pandas as pd
from urllib.parse import unquote
from typing import List, Dict, Tuple

BASE_URL = "https://jasoseol.com/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")

DEBUG_DIR = pathlib.Path("debug")

# ------------------------- Debug helpers -------------------------
def ensure_debug_dir():
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

def slug(s: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^0-9A-Za-z가-힣_]+", "_", s or "").strip("_")
    return (s[:maxlen] or "no_title")

def write_debug_txt(name: str, content: str):
    ensure_debug_dir()
    p = DEBUG_DIR / name
    try:
        p.write_text(content, encoding="utf-8")
    except Exception:
        # 최후 수단
        try:
            p.write_bytes(content.encode("utf-8", "ignore"))
        except Exception:
            pass

def append_jsonl(name: str, obj: dict):
    ensure_debug_dir()
    p = DEBUG_DIR / name
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

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
    drive.files().get(fileId=folder_id, fields="id", supportsAllDrives=True).execute()

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
                try:
                    loc.first.click(timeout=1000, force=True)
                    return
                except Exception:
                    pass
        try:
            page.keyboard.press("Escape"); return
        except Exception:
            pass
        # 최후: 오버레이 숨김
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

def click_and_capture_url(page, slide) -> Tuple[str, str]:
    """
    반환: (url, note)
    note에는 팝업/동일탭/실패사유 기록
    """
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
            slide.click(force=True)
        new = pop.value
        new.wait_for_load_state("domcontentloaded", timeout=7000)
        url = new.url
        new.close()
        return url, "popup"
    except Exception as e:
        popup_err = repr(e)

    # 2) 동일 탭
    old = page.url
    try:
        slide.click(force=True)
        page.wait_for_load_state("domcontentloaded", timeout=5000)
        if page.url != old:
            url = page.url
            page.go_back(wait_until="domcontentloaded")
            return url, "same_tab"
    except Exception as e:
        same_err = repr(e)

    return "", f"fail: popup={popup_err if 'popup_err' in locals() else ''} same={same_err if 'same_err' in locals() else ''}"

def scrape_banners_via_playwright() -> List[Dict[str,str]]:
    from playwright.sync_api import sync_playwright
    ensure_debug_dir()
    rows: List[Dict[str,str]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1400, "height": 900})
        page = ctx.new_page()

        # 0) 진입 & 모달처리
        page.goto(BASE_URL, wait_until="domcontentloaded")
        close_modal_if_present(page)
        wait_images_loaded(page)

        total = page.locator(".main-banner-ggs").count()
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

        write_debug_txt("unique_list.txt", "\n".join([f"{i+1}. {t} | {s}" for i,(t,s) in enumerate(unique)]))

        # 패스2: 링크 채우기 + 슬라이드별 디버그 기록
        for idx, (title, src) in enumerate(unique, start=1):
            slug_title = slug(title or f"no_title_{idx}")
            dbg_name = f"active_src_{idx:02d}_{slug_title}.txt"
            step_logs = []
            step_logs.append(f"[STEP] target_idx={idx} title={title}")
            step_logs.append(f"[STEP] target_src={src}")

            aligned = False
            try:
                aligned = wait_active_src(page, src, max_steps=total + 6)
                step_logs.append(f"[STEP] aligned={aligned}")
            except Exception as e:
                step_logs.append(f"[ERR] align_exc={repr(e)}")

            link, note = "", ""
            try:
                if aligned:
                    slide = get_active_slide(page)
                    link, note = click_and_capture_url(page, slide)
                    step_logs.append(f"[STEP] click_note={note}")
                else:
                    step_logs.append("[WARN] cannot align to target src; skip click")
            except Exception as e:
                step_logs.append(f"[ERR] click_exc={repr(e)}\n{traceback.format_exc()}")

            step_logs.append(f"[OUT] Link={link}")
            write_debug_txt(dbg_name, "\n".join(step_logs))
            append_jsonl("rows.jsonl", {"idx": idx, "title": title, "src": src, "link": link, "note": note})

            rows.append({"Title": title, "Link": link, "Src": src})

        browser.close()
    return rows

# ------------------------- main -------------------------
def main():
    rows = scrape_banners_via_playwright()

    # (Title, Src) 기준으로 유니크
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
