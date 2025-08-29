# jasoseol_banner.py
import os, time, re, json, pathlib, traceback, datetime as dt
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

def slug(s: str, maxlen: int = 80) -> str:
    s = re.sub(r"[^0-9A-Za-z가-힣_]+", "_", s or "").strip("_")
    return (s[:maxlen] or "no_title")

def write_debug_txt(name: str, content: str):
    ensure_debug_dir()
    p = DEBUG_DIR / name
    try:
        p.write_text(content, encoding="utf-8")
    except Exception:
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
        # 빠르게 닫기 버튼 후보들
        sels = [
            "button:has-text('닫기')", "text=닫기",
            "button:has-text('오늘 하루')", "text=오늘 하루 보지 않기",
            "[aria-label='close']", "[aria-label='Close']",
            "img[alt='close']", "img[alt='Close']",
            ".fixed .cursor-pointer:has(img[alt*='close'])",
        ]
        for s in sels:
            loc = page.locator(s)
            if loc.count() > 0:
                try:
                    loc.first.click(timeout=800, force=True)
                    time.sleep(0.3)
                    return
                except Exception:
                    pass
        # ESC 시도
        try:
            page.keyboard.press("Escape")
            time.sleep(0.25)
            return
        except Exception:
            pass
        # 최후: 화면 덮개 숨김
        page.evaluate("""
        (() => { for (const el of document.querySelectorAll('div')) {
          const r = el.getBoundingClientRect();
          const cls = String(el.className||'');
          const style = window.getComputedStyle(el);
          const z = parseInt(style.zIndex||'0',10);
          if ((r.width>=400 && r.height>=200) &&
              (cls.includes('overflow-hidden')||cls.includes('fixed')||cls.includes('backdrop')||z>1000)) {
            el.style.setProperty('display','none','important');
            el.style.setProperty('visibility','hidden','important');
            el.style.setProperty('pointer-events','none','important');
          }
        } })()
        """)
        time.sleep(0.2)
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
    # 우측 화살표 우선
    try:
        nxt = page.locator("img[alt='right icon'], img[src*='ic_arrow_right']").first
        if nxt.count()>0:
            nxt.click(force=True)
            return
    except Exception:
        pass
    # 컨테이너 오른쪽 영역 클릭(드래그 캡처 회피)
    try:
        cont = page.locator(".relative.box-border.w-\\[1200px\\].h-\\[280px\\]").first
        if cont.count()>0:
            box = cont.bounding_box() or {}
            if box:
                page.mouse.click(box["x"] + box["width"]*0.9, box["y"] + box["height"]/2)
                return
    except Exception:
        pass
    # 키보드
    try:
        page.keyboard.press("ArrowRight")
    except Exception:
        pass

def wait_images_loaded(page):
    page.wait_for_selector(".main-banner-ggs img", timeout=15000)
    page.wait_for_function(
        """() => {
          const imgs = Array.from(document.querySelectorAll('.main-banner-ggs img'));
          return imgs.length>0 && imgs.every(i => i.getAttribute('src'));
        }""", timeout=15000
    )

def wait_slide_changed(page, prev_src: str, timeout_s=3.0):
    t0 = time.time()
    while time.time()-t0 < timeout_s:
        cur = read_slide_signature(get_active_slide(page))[1]
        if cur and cur != prev_src: return True
        time.sleep(0.06)
    return False

def wait_active_src(page, target_src: str, max_steps: int):
    for _ in range(max_steps):
        t, s = read_slide_signature(get_active_slide(page))
        if s == target_src: return True
        prev = s
        go_next(page)
        wait_slide_changed(page, prev, timeout_s=2.8)
        time.sleep(0.12)
    return False

def _wait_url_change(page, old_url: str, timeout_s: float = 8.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if page.url != old_url:
            return True
        time.sleep(0.15)
    return False

def _click_topmost_via_element_from_point(page, x, y):
    # elementFromPoint를 통한 최상단 요소에 직접 click 이벤트 디스패치
    return page.evaluate("""([x, y]) => {
      const el = document.elementFromPoint(x, y);
      if (!el) return { ok:false, why:'no_element' };
      const evt = new MouseEvent('click', {bubbles:true, cancelable:true, view:window});
      const ok = el.dispatchEvent(evt);
      return { ok, tag: el.tagName, cls: (el.className||'').toString() };
    }""", [x, y])

def click_and_capture_url(page, slide, step_logs) -> Tuple[str, str]:
    """
    반환: (url, note)
    note: popup|same_tab|efp_popup|efp_same_tab|no_nav|fail:...
    """
    link, note = "", ""

    # 공통: 중앙 좌표 계산
    box = slide.bounding_box() or {}
    cx = box.get("x", 0) + box.get("width", 0)/2 if box else 700
    cy = box.get("y", 0) + box.get("height", 0)/2 if box else 260

    # ========== 경로 1) 일반 마우스 클릭 시도 ==========
    try:
        # 팝업 탭
        with page.expect_popup(timeout=2500) as pop:
            page.mouse.click(cx, cy)
        new = pop.value
        new.wait_for_load_state("domcontentloaded", timeout=7000)
        link = new.url
        new.close()
        step_logs.append(f"[CLICK] mouse_center popup | box={box}")
        return link, "popup"
    except Exception as e_popup_mouse:
        step_logs.append(f"[CLICK] mouse_center popup_fail | {repr(e_popup_mouse)} | box={box}")

    # 동일 탭 이동
    old = page.url
    try:
        page.mouse.click(cx, cy)
        changed = _wait_url_change(page, old, timeout_s=8.0)
        if changed and page.url != old:
            link = page.url
            step_logs.append(f"[CLICK] mouse_center same_tab | url={link}")
            # 뒤로 가서 배너로 복귀
            try:
                page.go_back(wait_until="domcontentloaded")
                time.sleep(0.4)
            except Exception:
                pass
            return link, "same_tab"
    except Exception as e_same_mouse:
        step_logs.append(f"[CLICK] mouse_center same_tab_fail | {repr(e_same_mouse)}")

    # ========== 경로 2) elementFromPoint 폴백 클릭 ==========
    try:
        # 팝업 탭
        with page.expect_popup(timeout=2500) as pop:
            r = _click_topmost_via_element_from_point(page, cx, cy)
            step_logs.append(f"[CLICK] efp_topmost popup_try | efp_result={r}")
        new = pop.value
        new.wait_for_load_state("domcontentloaded", timeout=7000)
        link = new.url
        new.close()
        step_logs.append(f"[CLICK] efp_topmost popup | url={link}")
        return link, "efp_popup"
    except Exception as e_popup_efp:
        step_logs.append(f"[CLICK] efp_topmost popup_fail | {repr(e_popup_efp)}")

    # 동일 탭 이동
    try:
        old = page.url
        r = _click_topmost_via_element_from_point(page, cx, cy)
        step_logs.append(f"[CLICK] efp_topmost same_try | efp_result={r}")
        changed = _wait_url_change(page, old, timeout_s=8.0)
        if changed and page.url != old:
            link = page.url
            step_logs.append(f"[CLICK] efp_topmost same_tab | url={link}")
            try:
                page.go_back(wait_until="domcontentloaded")
                time.sleep(0.4)
            except Exception:
                pass
            return link, "efp_same_tab"
    except Exception as e_same_efp:
        step_logs.append(f"[CLICK] efp_topmost same_tab_fail | {repr(e_same_efp)}")

    step_logs.append(f"[CLICK] no_nav | box={box}")
    return "", "no_nav"

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
        time.sleep(0.6)
        close_modal_if_present(page)
        wait_images_loaded(page)

        total = page.locator(".main-banner-ggs").count()
        total = max(total, 1)

        # 패스1: 고유 슬라이드 수집
        seen = set()
        unique: List[Tuple[str,str]] = []
        guard = 0
        while len(unique) < total and guard < total * 5:
            guard += 1
            slide = get_active_slide(page)
            title, src = read_slide_signature(slide)
            if src and (title, src) not in seen:
                seen.add((title, src))
                unique.append((title, src))
            prev = src
            go_next(page)
            wait_slide_changed(page, prev, timeout_s=3.2)
            time.sleep(0.12)

        write_debug_txt("unique_list.txt", "\n".join([f"{i+1}. {t} | {s}" for i,(t,s) in enumerate(unique)]))

        # 패스2: 각 슬라이드 정렬 → 클릭 → 링크 획득
        for idx, (title, src) in enumerate(unique, start=1):
            slug_title = slug(title or f"no_title_{idx}")
            dbg_name = f"active_src_{idx:02d}_{slug_title}.txt"
            step_logs = []
            step_logs.append(f"[TIME_UTC] {dt.datetime.utcnow().isoformat()}Z")
            step_logs.append(f"[STEP] target_idx={idx}")
            step_logs.append(f"[STEP] title={title}")
            step_logs.append(f"[STEP] src={src}")

            aligned = False
            try:
                aligned = wait_active_src(page, src, max_steps=total + 8)
                step_logs.append(f"[STEP] aligned={aligned}")
            except Exception as e:
                step_logs.append(f"[ERR] align_exc={repr(e)}")

            link, note = "", ""
            try:
                if aligned:
                    slide = get_active_slide(page)
                    link, note = click_and_capture_url(page, slide, step_logs)
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

    # (Title, Src) 기준 유니크 정리
    uniq = {}
    for r in rows:
        key = (r.get("Title") or "", r.get("Src") or "")
        if key not in uniq or (not uniq[key].get("Link") and r.get("Link")):
            # 링크가 채워진 쪽을 우선
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
