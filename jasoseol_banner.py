# jasoseol_banner.py
import os, time, re, json, pathlib, traceback
import pandas as pd
from urllib.parse import unquote, urlparse
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
    """첫 진입 모달/광고 팝업을 닫는다(여러 셀렉터 시도 + Escape + 강제 숨김)."""
    try:
        sels = [
            "button:has-text('닫기')", "text=닫기",
            "button:has-text('오늘 하루')", "text=오늘 하루 보지 않기",
            "[aria-label='close']", "[aria-label='Close']",
            "img[alt='close']", "img[alt='Close']",
            ".modal [role='button']",
        ]
        for s in sels:
            loc = page.locator(s)
            if loc.count() > 0:
                try:
                    loc.first.click(timeout=800, force=True)
                    time.sleep(0.1)
                    return
                except Exception:
                    pass
        try:
            page.keyboard.press("Escape")
            time.sleep(0.05)
        except Exception:
            pass
        # 최후: 흔한 오버레이/백드롭 제거
        page.evaluate("""
        (() => {
          for (const el of document.querySelectorAll('div,section')) {
            const r = el.getBoundingClientRect();
            const c = String(el.className||'');
            if ((r.width>=400 && r.height>=200) && (c.includes('overflow-hidden')||c.includes('fixed')||c.includes('backdrop')||c.includes('modal'))) {
              el.style.setProperty('display','none','important');
            }
          }
        })()
        """)
    except Exception:
        pass

def get_slides_locator(page):
    return page.locator(".main-banner-ggs")

def wait_images_loaded(page):
    page.wait_for_selector(".main-banner-ggs img", timeout=15000)
    page.wait_for_function(
        """() => {
          const imgs = Array.from(document.querySelectorAll('.main-banner-ggs img'));
          return imgs.length>0 && imgs.every(i => i.getAttribute('src'));
        }""",
        timeout=15000
    )

def read_slide_signature_by_index(page, idx: int) -> Tuple[str, str]:
    """nth(idx) 슬라이드에서 alt/src 추출 (화면에 보이든 말든)"""
    try:
        item = get_slides_locator(page).nth(idx)
        img = item.locator("img").first
        title = (img.get_attribute("alt") or "").strip()
        src = unquote(img.get_attribute("src") or "")
        return title, src
    except Exception:
        return "", ""

def go_next(page):
    """오른쪽 화살표(없으면 키보드)"""
    try:
        arrow = page.locator("img[alt='right icon'], img[src*='ic_arrow_right']").first
        if arrow.count()>0:
            arrow.click(force=True)
            return
    except Exception:
        pass
    try:
        page.keyboard.press("ArrowRight")
    except Exception:
        pass

def align_to_index(page, target_idx: int, total: int, step_log: list):
    """
    현재 active 기준이 불명확할 수 있어서,
    '총 슬라이드 수만큼 우측 이동'을 최대 2회 반복하며 대상 인덱스의 src가 화면 중앙에 나올 때까지 시도.
    """
    for loop in range(2):  # 최대 2바퀴
        for _ in range(total+2):
            # 현재 중앙 슬라이드의 src
            # heuristic: opacity가 가장 큰 요소를 중앙으로 간주
            cand = page.locator(".main-banner-ggs.opacity-100,.main-banner-ggs.z-1").first
            if cand.count()==0:
                cand = page.locator(".main-banner-ggs").first
            cur_img = cand.locator("img").first
            cur_src = unquote(cur_img.get_attribute("src") or "")
            # 대상 인덱스의 src
            _, target_src = read_slide_signature_by_index(page, target_idx)
            if target_src and cur_src and (os.path.basename(cur_src)==os.path.basename(target_src)):
                step_log.append(f"[ALIGN] matched cur={os.path.basename(cur_src)} target={os.path.basename(target_src)}")
                return True
            go_next(page)
            time.sleep(0.15)
    step_log.append("[ALIGN] failed")
    return False

def capture_link_after_click(page, slide, step_log: list) -> Tuple[str, str]:
    """
    클릭 후 URL을 포착. 우선 팝업, 실패 시 동일탭 내비게이션.
    반환: (url, how)  how ∈ {"popup","same_tab",""} 
    """
    # 클릭 전에 모달/오버레이 한 번 더 방어
    close_modal_if_present(page)
    page.wait_for_timeout(50)

    # 뷰포트 내로
    try:
        slide.scroll_into_view_if_needed(timeout=1000)
    except Exception:
        pass

    # 1) popup 시나리오
    try:
        with page.expect_popup(timeout=3500) as pop_waiter:
            slide.click(force=True)
        new_page = pop_waiter.value
        new_page.wait_for_load_state("domcontentloaded", timeout=7000)
        url = new_page.url
        step_log.append(f"[CLICK] popup url={url}")
        new_page.close()
        return url, "popup"
    except Exception as e:
        step_log.append(f"[CLICK] popup miss: {repr(e)}")

    # 2) same-tab 시나리오
    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=5000):
            slide.click(force=True)
        url = page.url
        step_log.append(f"[CLICK] same_tab url={url}")
        page.go_back(wait_until="domcontentloaded")
        return url, "same_tab"
    except Exception as e:
        step_log.append(f"[CLICK] same_tab miss: {repr(e)}")

    # 3) 마지막 시도: JS 핸들러가 a.href를 들고 있을 수도 → href 추출 후 강제 window.open
    try:
        href = slide.evaluate("""(el)=>{
          // el 내부에 a가 있으면 우선 사용
          const a = el.querySelector('a[href]');
          return a ? a.href : '';
        }""")
        if href:
            step_log.append(f"[CLICK] fallback href sniffed={href}")
            return href, "sniff"
    except Exception as e:
        step_log.append(f"[CLICK] sniff miss: {repr(e)}")

    return "", ""

# ------------------------- Scraper -------------------------
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

        slides = get_slides_locator(page)
        total = slides.count()
        total = max(total, 1)

        # Pass 1: 모든 슬라이드의 (index, title, src) 캐시
        unique: List[Tuple[int,str,str]] = []  # (idx, title, src)
        for i in range(total):
            title, src = read_slide_signature_by_index(page, i)
            if not src:
                continue
            # 같은 Src는 건너뛰기(질문 페이지에서 15~16개 고유 이미지)
            if not any(os.path.basename(src)==os.path.basename(s2) for _,_,s2 in unique):
                unique.append((i, title, src))
        write_debug_txt(
            "unique_list.txt",
            "\n".join([f"{i+1}. idx={idx} | {title} | {src}" for i,(idx,title,src) in enumerate(unique)])
        )

        # Pass 2: 각 인덱스로 정렬 → 클릭 → URL 포착
        for ord_i, (idx, title, src) in enumerate(unique, start=1):
            step_logs = [f"[STEP] order={ord_i} dom_idx={idx}",
                         f"[STEP] title={title}",
                         f"[STEP] src={src}"]
            slug_title = slug(title or f"no_title_{ord_i}")
            dbg_name = f"active_src_{ord_i:02d}_{slug_title}.txt"

            # 정렬
            aligned = align_to_index(page, idx, total, step_logs)

            link, how = "", ""
            try:
                if aligned:
                    slide = slides.nth(idx)
                    link, how = capture_link_after_click(page, slide, step_logs)
                else:
                    step_logs.append("[WARN] alignment failed → skip click")
            except Exception as e:
                step_logs.append(f"[ERR] click_exc={repr(e)}\n{traceback.format_exc()}")

            step_logs.append(f"[OUT] Link={link} how={how}")
            write_debug_txt(dbg_name, "\n".join(step_logs))
            append_jsonl("rows.jsonl", {"order": ord_i, "idx": idx, "title": title,
                                        "src": src, "link": link, "how": how})

            rows.append({"Title": title, "Link": link, "Src": src})

        browser.close()
    return rows

# ------------------------- main -------------------------
def main():
    rows = scrape_banners_via_playwright()

    # (Title, Src) 기준으로 유니크 정리
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
