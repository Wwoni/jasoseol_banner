import os
import re
import json
import time
import pandas as pd
from urllib.parse import urljoin, urlparse, unquote
from typing import List, Dict, Optional

# ---------- 기본 설정 ----------
BASE_URL = "https://jasoseol.com/"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ---------- Google Drive 업로드 ----------
def upload_to_gdrive(local_path: str, filename: str) -> str:
    """
    CSV를 구글 '공유드라이브' 폴더에 업로드(동일 파일명 있으면 update, 없으면 create).
    환경변수:
      - GDRIVE_FOLDER_ID (필수)
      - GDRIVE_CREDENTIALS_JSON (필수)  # 서비스계정 JSON 원문
        또는 GDRIVE_SA_JSON_PATH (선택)  # gdrive_sa.json 파일 경로
      - GDRIVE_DRIVE_ID (선택)          # 공유드라이브 ID
    """
    import json as _json
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError

    folder_id = os.environ.get("GDRIVE_FOLDER_ID")
    drive_id = os.environ.get("GDRIVE_DRIVE_ID")  # optional
    raw_json = os.environ.get("GDRIVE_CREDENTIALS_JSON")
    sa_path = os.environ.get("GDRIVE_SA_JSON_PATH")  # optional

    if not folder_id:
        raise RuntimeError("GDRIVE_FOLDER_ID가 설정되지 않았습니다.")

    scopes = ["https://www.googleapis.com/auth/drive"]

    creds = None
    if raw_json:
        try:
            info = _json.loads(raw_json)
            creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        except Exception as e:
            raise RuntimeError(f"GDRIVE_CREDENTIALS_JSON 파싱 실패: {e}")

    if creds is None:
        if not sa_path:
            sa_path = "gdrive_sa.json"
        if not os.path.exists(sa_path):
            raise FileNotFoundError(f"서비스계정 파일을 찾을 수 없습니다: {sa_path}")
        creds = service_account.Credentials.from_service_account_file(sa_path, scopes=scopes)

    drive = build("drive", "v3", credentials=creds)

    # 폴더 유효성 체크
    try:
        folder_meta = drive.files().get(
            fileId=folder_id,
            fields="id,name,driveId,mimeType",
            supportsAllDrives=True,
        ).execute()
        if folder_meta.get("mimeType") != "application/vnd.google-apps.folder":
            raise RuntimeError(f"GDRIVE_FOLDER_ID가 폴더가 아닙니다: {folder_meta.get('mimeType')}")
    except HttpError as he:
        raise RuntimeError(
            f"폴더 조회 실패: {he}. "
            "공유드라이브 폴더 ID와 서비스계정 구성원 권한을 확인하세요."
        )

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

    try:
        if files:
            file_id = files[0]["id"]
            drive.files().update(
                fileId=file_id,
                media_body=media,
                supportsAllDrives=True,
            ).execute()
            return file_id
        else:
            metadata = {"name": filename, "parents": [folder_id]}
            file = drive.files().create(
                body=metadata,
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            ).execute()
            return file["id"]
    except HttpError as he:
        raise RuntimeError(
            f"업로드 실패: {he}. "
            "서비스계정 권한과 폴더 ID를 확인하세요."
        )

# ---------- Playwright 기반 수집 ----------
def scrape_banners_via_playwright() -> List[Dict[str, str]]:
    """
    - 배너 캐러셀의 총 슬라이드 수를 읽고(오른쪽 아래 'n / total'),
    - 오른쪽 화살표로 슬라이드를 순회하면서
      1) 현재 보이는 슬라이드(img.alt, img.src) 추출
      2) 배너를 실제 클릭하여 렌딩 URL 수집
         - 새 탭 팝업: expect_popup
         - 동일 탭 이동: wait_for_url
    - 슬라이드 이동 간 약간의 대기 필요(페이드 전환)
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    rows: List[Dict[str, str]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1400, "height": 900})
        page = ctx.new_page()

        page.goto(BASE_URL, wait_until="domcontentloaded")
        # 캐러셀 래퍼 대기
        page.wait_for_selector(".main-banner-ggs", timeout=10000)

        # 총 슬라이드 수 파악: "X / Y" 표시
        total = 0
        try:
            counter = page.locator("div:has-text('/ ') >> nth=0").first
            text = counter.inner_text(timeout=3000).strip()
            # 예: "2 / 16" 형태에서 뒤의 16 추출
            m = re.search(r"/\s*(\d+)", text)
            if m:
                total = int(m.group(1))
        except Exception:
            pass

        if total <= 0:
            # 캐러셀 DOM 갯수를 직접 세는 fallback
            total = page.locator(".main-banner-ggs").count()

        # 슬라이드 이동용 우측 화살표(안 보일 수 있어 opacity-0이라도 클릭은 됨)
        next_arrow = page.locator("img[alt='right icon']").first.or_(page.locator("img[src*='ic_arrow_right']"))

        def get_visible_slide_locator():
            # class에 opacity-100 이거나, z-1 등을 가진 항목이 현재 노출 슬라이드
            loc = page.locator(".main-banner-ggs.opacity-100").first
            if loc.count() == 0:
                # z-1 표시로 노출 판별
                loc = page.locator(".main-banner-ggs.z-1").first
            if loc.count() == 0:
                # 최후: 첫 번째 .main-banner-ggs
                loc = page.locator(".main-banner-ggs").first
            return loc

        def extract_img_info(slide_loc) -> Dict[str, str]:
            alt = ""
            src = ""
            try:
                img = slide_loc.locator("img").first
                alt = img.get_attribute("alt") or ""
                src = img.get_attribute("src") or ""
                src = unquote(src)
            except Exception:
                pass
            return {"Alt": alt, "Src": src}

        def click_and_get_link(slide_loc) -> str:
            """
            실제 클릭으로 렌딩 URL 확인.
            - 새 탭: popup 이벤트로 URL 획득 후 팝업 닫기
            - 동일 탭: click 후 wait_for_url로 URL 변화 감지, 다시 뒤로가기
            """
            link_url = ""
            # 클릭 타겟: 슬라이드 전체를 감싼 div
            target = slide_loc

            # 1) popup 케이스(새 탭)
            try:
                with page.expect_popup(timeout=1500) as pop_info:
                    target.click(force=True)
                new_page = pop_info.value
                new_page.wait_for_load_state("domcontentloaded", timeout=5000)
                link_url = new_page.url
                # 팝업은 닫아준다
                new_page.close()
                return link_url
            except PWTimeout:
                pass
            except Exception:
                pass

            # 2) 동일 탭 이동 케이스
            old_url = page.url
            try:
                target.click(force=True)
                page.wait_for_load_state("domcontentloaded", timeout=3000)
                # url이 바뀌었으면 렌딩으로 판단
                if page.url != old_url:
                    link_url = page.url
                    # 뒤로가기 (캐러셀 복귀)
                    page.go_back(wait_until="domcontentloaded")
            except PWTimeout:
                # 변화 없으면 링크 미노출/내부 핸들러 무시 케이스
                pass
            except Exception:
                # 실패해도 빈 문자열 반환
                pass

            return link_url

        # 첫 슬라이드가 이미 노출 상태이므로 0..total-1 만큼 순회
        seen_pairs = set()
        for i in range(max(total, 1)):
            # 현재 보이는 슬라이드
            slide = get_visible_slide_locator()
            time.sleep(0.2)  # 전환 안정화

            info = extract_img_info(slide)
            link = click_and_get_link(slide)

            row = {
                "Title": info.get("Alt", ""),
                "Link": link,
                "Src": info.get("Src", ""),
            }
            key = (row["Src"], row["Link"])
            if row["Src"] and key not in seen_pairs:
                rows.append(row)
                seen_pairs.add(key)

            # 다음 장으로 이동
            if i < total - 1:
                try:
                    next_arrow.click(force=True)
                except Exception:
                    # 화살표가 안 잡히면 키 이벤트로 시도
                    try:
                        page.keyboard.press("ArrowRight")
                    except Exception:
                        pass
                time.sleep(0.5)  # 전환 대기

        browser.close()

    return rows

# ---------- main ----------
def main():
    rows = scrape_banners_via_playwright()

    df = pd.DataFrame(rows)
    # 컬럼 고정
    for col in ["Title", "Link", "Src"]:
        if col not in df.columns:
            df[col] = ""
    # 중복 제거(같은 이미지가 다른 클릭으로 동일 URL이면 1건 유지)
    df = df.drop_duplicates(subset=["Src", "Link"]).reset_index(drop=True)

    out_csv = "jasoseol_banner.csv"
    df.to_csv(out_csv, index=False, encoding="utf-8-sig", lineterminator="\n")
    print(f"[OK] {len(df)}개 배너 수집 완료 → {out_csv}")

    # 업로드
    print("[INFO] Google Drive 업로드 시작…")
    file_id = upload_to_gdrive(out_csv, "jasoseol_banner.csv")
    print(f"[OK] Drive 업로드 완료 fileId={file_id}")

if __name__ == "__main__":
    main()
