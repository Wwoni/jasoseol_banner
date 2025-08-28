import os
import re
import json
import requests
import pandas as pd
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from datetime import datetime
from typing import Any, Iterable

BASE_URL = "https://jasoseol.com/"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

IMG_EXT_RE = re.compile(r"\.(?:png|jpg|jpeg|webp|gif)(?:\?.*)?$", re.IGNORECASE)

def fetch_html(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    resp.raise_for_status()
    return resp.text

def safe_urljoin(base: str, maybe_url: str | None) -> str:
    if not maybe_url:
        return ""
    return urljoin(base, maybe_url)

def parse_srcset(srcset: str, base: str) -> list[str]:
    if not srcset:
        return []
    parts = [p.strip() for p in srcset.split(",") if p.strip()]
    # 원 요청 로직: 마지막 항목 제외
    if len(parts) > 1:
        parts = parts[:-1]
    urls = []
    for p in parts:
        url_only = p.split()[0]
        urls.append(safe_urljoin(base, url_only))
    return urls

def collect_from_static_dom(soup: BeautifulSoup) -> list[dict]:
    rows = []

    # 1) 원래 셀렉터
    candidates = soup.select(".main-banner-ggs")

    # 2) 흔한 배너 패턴(예: swiper/picture/img)
    if not candidates:
        candidates = soup.select(
            ".swiper .swiper-slide, .banner, .main-banner, .main_banner"
        )

    for node in candidates:
        a = node.find("a")
        img = node.find("img")
        link = safe_urljoin(BASE_URL, a.get("href") if a else "")
        alt = img.get("alt") if img else ""
        src = safe_urljoin(BASE_URL, img.get("src") if img else "")
        srcset = img.get("srcset") if img else ""
        srcset_urls = parse_srcset(srcset, BASE_URL)
        rows.append(
            {
                "Link": link,
                "Alt": alt or "",
                "Src": src,
                "Srcset_Modified": ", ".join(srcset_urls),
                "Source": "static_dom",
            }
        )
    return rows

def deep_iter(v: Any) -> Iterable[str]:
    """JSON 등 임의의 중첩 구조에서 문자열만 뽑아냄."""
    if isinstance(v, dict):
        for k, vv in v.items():
            yield from deep_iter(vv)
    elif isinstance(v, list):
        for vv in v:
            yield from deep_iter(vv)
    elif isinstance(v, str):
        yield v

def looks_like_img(url: str) -> bool:
    if not url:
        return False
    if IMG_EXT_RE.search(url):
        return True
    # Next.js 이미지는 /_next/image?url=... 형태일 수 있음 → 원본 URL 추출
    if "/_next/image" in url and "url=" in url:
        return True
    return False

def normalize_img_url(url: str) -> str:
    # /_next/image?url=encoded&... → 실제 원본 URL로 교체 시도
    if "/_next/image" in url and "url=" in url:
        try:
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(url).query)
            raw = qs.get("url", [""])[0]
            if raw:
                return safe_urljoin(BASE_URL, raw)
        except Exception:
            pass
    return safe_urljoin(BASE_URL, url)

def collect_from_next_data(soup: BeautifulSoup) -> list[dict]:
    rows = []
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.text:
        return rows

    # 디버깅용 저장
    with open("debug_next_data.json", "w", encoding="utf-8") as f:
        f.write(tag.text)

    try:
        data = json.loads(tag.text)
    except Exception:
        return rows

    # 모든 문자열 중 이미지 URL 후보 수집
    images = []
    links = []
    for s in deep_iter(data):
        if looks_like_img(s):
            images.append(normalize_img_url(s))
        # a태그 href 대신 라우팅 path만 있는 경우 대비
        # (ex) "/events/123" 같은 path
        elif isinstance(s, str) and s.startswith("/"):
            links.append(safe_urljoin(BASE_URL, s))

    # 중복 제거
    images = list(dict.fromkeys(images))
    links = list(dict.fromkeys(links))

    # 이미지/링크 매칭은 정확치 않으므로, 우선 이미지 기준 행 생성
    for img_url in images:
        rows.append(
            {
                "Link": links[0] if links else "",
                "Alt": "",
                "Src": img_url,
                "Srcset_Modified": "",
                "Source": "__NEXT_DATA__",
            }
        )

    return rows

def collect_fallback_imgs(soup: BeautifulSoup) -> list[dict]:
    """정말 아무것도 못 찾았을 때, 페이지 내의 모든 <img>를 수집(보수적)."""
    rows = []
    imgs = soup.find_all("img")
    for img in imgs:
        src = normalize_img_url(img.get("src") or "")
        if not src or not looks_like_img(src):
            continue
        alt = img.get("alt") or ""
        srcset_urls = parse_srcset(img.get("srcset") or "", BASE_URL)
        rows.append(
            {
                "Link": "",
                "Alt": alt,
                "Src": src,
                "Srcset_Modified": ", ".join(srcset_urls),
                "Source": "fallback_img",
            }
        )
    return rows

def main():
    html = fetch_html(BASE_URL)

    # 디버그: 원문 저장
    with open("debug_home.html", "w", encoding="utf-8") as f:
        f.write(html)

    soup = BeautifulSoup(html, "lxml")

    rows = []
    rows += collect_from_static_dom(soup)
    if not rows:
        rows += collect_from_next_data(soup)
    if not rows:
        rows += collect_fallback_imgs(soup)

    # 정제: 완전 빈 행 제거
    cleaned = [
        r for r in rows
        if any([r.get("Link"), r.get("Src"), r.get("Srcset_Modified")])
    ]

    df = pd.DataFrame(cleaned).drop_duplicates()

    out_csv = "jasoseol_banner.csv"
    df.to_csv(out_csv, index=False, encoding="utf-8-sig", lineterminator="\n")
    print(f"[OK] {len(df)}개 배너 수집 완료 → {out_csv}")

    # === 여기부터 드라이브 업로드 “필수” 호출 ===
    print("[INFO] Google Drive 업로드 시작…")
    file_id = upload_to_drive(out_csv, "jasoseol_banner.csv")
    print(f"[OK] Drive 업로드 완료 fileId={file_id}")
        
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from google.oauth2 import service_account

def upload_to_gdrive(file_path: str, drive_id: str, folder_id: str | None = None):
    # Secret에 저장한 JSON 키 불러오기
    creds_json = os.environ.get("GDRIVE_CREDENTIALS_JSON")
    if not creds_json:
        raise RuntimeError("환경변수 GDRIVE_CREDENTIALS_JSON 이 없습니다.")
    
    creds_dict = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/drive.file",
                "https://www.googleapis.com/auth/drive"]
    )

    service = build("drive", "v3", credentials=creds)

    file_metadata = {
        "name": os.path.basename(file_path),
        "driveId": drive_id,
        "parents": [folder_id] if folder_id else None
    }
    media = MediaFileUpload(file_path, mimetype="text/csv")

    uploaded = service.files().create(
        body=file_metadata,
        media_body=media,
        supportsAllDrives=True,
        fields="id, name"
    ).execute()

    print(f"[OK] Google Drive 업로드 완료 → {uploaded.get('name')} (id={uploaded.get('id')})")

def main():
    html = fetch_html(BASE_URL)
    soup = BeautifulSoup(html, "lxml")
    rows = collect_from_static_dom(soup) or collect_from_next_data(soup) or collect_fallback_imgs(soup)
    df = pd.DataFrame(rows).drop_duplicates()

    out_csv = "jasoseol_banner.csv"
    df.to_csv(out_csv, index=False, encoding="utf-8-sig", lineterminator="\n")
    print(f"[OK] {len(df)}개 배너 수집 완료 → {out_csv}")

    # === 업로드 강제 호출 (로그 반드시 찍기) ===
    print("[INFO] Google Drive 업로드 시작…")
    file_id = upload_to_drive(out_csv, "jasoseol_banner.csv")  # 예외는 그대로 raise
    print(f"[OK] Drive 업로드 완료 fileId={file_id}")

if __name__ == "__main__":
    main()
