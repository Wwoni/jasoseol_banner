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
        try: p.write_bytes(content.encode("utf-8", "ignore"))
        except Exception: pass

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
    """팝업 배너 및 오버레이 완전 차단"""
    try:
        # 버튼 시도
        sels = [
            "button:has-text('닫기')","text=닫기",
            "button:has-text('오늘 하루')","text=오늘 하루 보지 않기",
            "[aria-label='close']","[aria-label='Close']",
            "img[alt='close']","img[alt='Close']",
        ]
        for s in sels:
            loc = page.locator(s)
            if loc.count()>0:
                try: loc.first.click(timeout=1000, force=True); return
                except: pass
        # ESC
        try: page.keyboard.press("Escape"); return
        except: pass
        # 팝업 캐러셀/오버레이 무력화
        page.evaluate("""
        (() => {
          const as = Array.from(document.querySelectorAll('a[href] img[alt^="광고 배너"]'));
          for (const img of as) {
            let el = img;
            for (let i=0;i<6 && el;i++){
              el = el.parentElement;
              if (!el) break;
              const cs = window.getComputedStyle(el);
              const isFixed = cs.position==='fixed'||cs.position==='sticky';
              const big = el.getBoundingClientRect().width>=300 && el.getBoundingClientRect().height>=200;
              if (isFixed && big){ el.style.setProperty('display','none','important'); break; }
            }
          }
          document.querySelectorAll('div,section,aside').forEach(el=>{
            const cs = window.getComputedStyle(el);
            const r = el.getBoundingClientRect();
            if((cs.position==='fixed'||cs.position==='sticky')&&r.width>=300&&r.height>=200){
              el.style.setProperty('pointer-events','none','important');
            }
          });
        })();
        """)
    except: pass

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
    except: pass
    return title, src

def go_next(page):
    try:
        nxt = page.locator("img[alt='right icon'], img[src*='ic_arrow_right']").first
        if nxt.count()>0: nxt.click(force=True); return
    except: pass
    try: page.keyboard.press("ArrowRight")
    except: pass

def wait_images_loaded(page):
    page.wait_for_selector(".main-banner-ggs img", timeout=12000)
    page.wait_for_function(
        "() => { const imgs=[...document.querySelectorAll('.main-banner-ggs img')]; return imgs.length>0 && imgs.every(i=>i.getAttribute('src')); }",
        timeout=12000
    )

def wait_slide_changed(page, prev_src: str, timeout_s=3.0):
    t0=time.time()
    while time.time()-t0<timeout_s:
        cur=read_slide_signature(get_active_slide(page))[1]
        if cur and cur!=prev_src: return True
        time.sleep(0.05)
    return False

def wait_active_src(page, target_src: str, max_steps: int):
    for _ in range(max_steps):
        t,s=read_slide_signature(get_active_slide(page))
        if s==target_src:
            time.sleep(0.25)  # 안정화
            return True
        prev=s
        go_next(page)
        wait_slide_changed(page, prev, timeout_s=2.5)
        time.sleep(0.15)
    time.sleep(0.25)
    return False

def click_and_capture_url(page, slide) -> Tuple[str,str]:
    """현재 슬라이드 이미지 중심을 직접 클릭해서 링크 포착"""
    note_details=[]
    # 모든 배너 잠금
    page.evaluate("""() => {
      document.querySelectorAll('.main-banner-ggs').forEach(el=>{
        el.style.setProperty('pointer-events','none','important');
      });
    }""")
    # 타겟만 풀기
    page.evaluate("(el)=>{el.style.setProperty('pointer-events','auto','important');}", slide)

    img=slide.locator("img").first
    try: box=img.bounding_box()
    except: box=None

    # popup
    try:
        with page.expect_popup(timeout=3500) as pop:
            if box: page.mouse.click(box["x"]+box["width"]/2, box["y"]+box["height"]/2)
            else: img.click(force=True)
        new=pop.value; new.wait_for_load_state("domcontentloaded",timeout=7000)
        url=new.url; new.close()
        return url,"popup"
    except Exception as e: note_details.append("no_popup:"+type(e).__name__)

    # same-tab
    old=page.url
    try:
        if box: page.mouse.click(box["x"]+box["width"]/2, box["y"]+box["height"]/2)
        else: img.click(force=True)
        page.wait_for_load_state("domcontentloaded",timeout=5000)
        if page.url!=old and page.url.startswith("http"):
            url=page.url; page.go_back(wait_until="domcontentloaded")
            return url,"same_tab"
        else: note_details.append("same_tab_no_nav")
    except Exception as e: note_details.append("same_tab_err:"+type(e).__name__)

    return "","fail:"+",".join(note_details)

# ------------------------- Scraper -------------------------
def scrape_banners_via_playwright()->List[Dict[str,str]]:
    from playwright.sync_api import sync_playwright
    ensure_debug_dir()
    rows=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=["--no-sandbox"])
        ctx=browser.new_context(user_agent=UA,viewport={"width":1400,"height":900})
        page=ctx.new_page()
        page.goto(BASE_URL,wait_until="domcontentloaded")
        close_modal_if_present(page)
        wait_images_loaded(page)

        total=page.locator(".main-banner-ggs").count()
        total=max(total,1)

        seen=set(); unique=[]
        guard=0
        while len(unique)<total and guard<total*4:
            guard+=1
            slide=get_active_slide(page)
            title,src=read_slide_signature(slide)
            if src and (title,src) not in seen:
                seen.add((title,src)); unique.append((title,src))
            prev=src; go_next(page)
            wait_slide_changed(page,prev,timeout_s=2.5); time.sleep(0.15)
        write_debug_txt("unique_list.txt","\n".join([f"{i+1}. {t} | {s}" for i,(t,s) in enumerate(unique)]))

        for idx,(title,src) in enumerate(unique,1):
            slug_title=slug(title or f"no_title_{idx}")
            dbg=f"active_src_{idx:02d}_{slug_title}.txt"
            logs=[f"[STEP] target_idx={idx}",f"[STEP] target_src={src}"]
            aligned=False
            try:
                aligned=wait_active_src(page,src,max_steps=total+6); logs.append(f"aligned={aligned}")
            except Exception as e: logs.append("align_exc="+repr(e))

            link,note="",""
            try:
                if aligned:
                    slide=get_active_slide(page)
                    link,note=click_and_capture_url(page,slide)
                    logs.append(f"click_note={note}")
                else: logs.append("WARN: not aligned")
            except Exception as e:
                logs.append("click_exc="+repr(e)+"\n"+traceback.format_exc())
            logs.append(f"OUT Link={link}")
            write_debug_txt(dbg,"\n".join(logs))
            append_jsonl("rows.jsonl",{"idx":idx,"title":title,"src":src,"link":link,"note":note})
            rows.append({"Title":title,"Link":link,"Src":src})
        browser.close()
    return rows

# ------------------------- main -------------------------
def main():
    rows=scrape_banners_via_playwright()
    uniq={}
    for r in rows:
        key=(r.get("Title") or "",r.get("Src") or "")
        if key not in uniq: uniq[key]=r
    final=list(uniq.values())

    df=pd.DataFrame(final,columns=["Title","Link","Src"])
    out_csv="jasoseol_banner.csv"
    df.to_csv(out_csv,index=False,encoding="utf-8-sig",lineterminator="\n")
    print(f"[OK] {len(df)}개 배너 수집 완료 → {out_csv}")

    print("[INFO] Google Drive 업로드 시작…")
    fid=upload_to_gdrive(out_csv,"jasoseol_banner.csv")
    print(f"[OK] Drive 업로드 완료 fileId={fid}")

if __name__=="__main__":
    main()
