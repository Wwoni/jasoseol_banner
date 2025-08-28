# jasoseol_banner.py
import os
import re
import json
import requests
import pandas as pd
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote, parse_qs
from typing import Any, Iterable

BASE_URL = "https://jasoseol.com/"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

IMG_EXT_RE = re.compile(r"\.(?:png|jpg|jpeg|webp|gif)(?:\?.*)?$", re.IGNORECASE)

# -------------------------
# HTTP / Parsing helpers
# -------------------------
def fetch_html(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    resp.raise_for_status()
    return resp.text

def safe_urljoin(base: str, maybe_url: str | None) -> str:
    if not maybe_url:
        return ""
    return urljoin(base, maybe_url)

def looks_like_img(url: str) -> bool:
    if not url:
        return False
    if IMG_EXT_RE.search(url):
        return True
    if "/_next/image" in url and "url=" in url:
        return True
    return False

def normalize_img_url(url: str) -> str:
    # /_next/image?url=... → 실제 원본 URL로 변환 시도
    if "/_next/image" in url and "url=" in url:
        try:
            qs = parse_qs(urlparse(url).query)
            raw = qs.get("url", [""])[0]
            if raw:
                return safe_urljoin(BASE_URL, raw)
        except Exception:
            pass
    return safe_urljoin(BASE_URL, url)

def strip_query_and_unquote(u: str) -> str:
    if not u:
        return ""
    u = unquote(u)
    parsed = urlparse(u)
    return parsed._replace(query="", fragment="").geturl()

def url_basename(u: str) -> str:
    if not u:
        return ""
    u = strip_query_and_unquote(u)
    return os.path.basename(urlparse(u).path)

# -------------------------
# DOM collectors (배너 IMG만 수집)
# -------------------------
def collect_from_dom(soup: BeautifulSoup) -> list[dict]:
    rows = []
    # 배너 컨테이너
    candidates = soup.select(".main-banner-ggs")
    if not candidates:
        # 백업 셀렉터
        candidates = soup.select(".swiper .swiper-slide, .banner, .main-banner, .main_banner")

    for node in candidates:
        img = node.find("img")
        if not img:
            continue
        alt = img.get("alt") or ""
        src = normalize_img_url(img.get("src") or "")
        if not looks_like_img(src):
            continue
        rows.append(
            {
                "Title": alt,
                "Src": strip_query_and_unquote(src),  # 비교 용이하게 정규화
                "Link": "",  # 나중에 NEXT_DATA로 채움
                "Source": "DOM",
            }
        )
    # 중복 제거(이미지 기준)
    uniq = {}
    for r in rows:
        uniq[r["Src"]] = r
    return list(uniq.values())

# -------------------------
# __NEXT_DATA__ → (image_url, link_url) 페어만 뽑기
# -------------------------
def iter_nodes(v: Any):
    if isinstance(v, dict):
        yield v
        for vv in v.values():
            yield from iter_nodes(vv)
    elif isinstance(v, list):
        for vv in v:
            yield from iter_nodes(vv)

def extract_pairs_from_next(next_json: dict) -> list[dict]:
    """
    __NEXT_DATA__ 전체를 훑어서
      - image_url (이미지 주소)
      - link_url  (클릭시 이동 주소)
    가 같은 dict에 공존하는 경우만 추출.
    """
    pairs = []
    for node in iter_nodes(next_json):
        if not isinstance(node, dict):
            continue
        img = node.get("image_url")
        link = node.get("link_url")
        if img and link and isinstance(img, str) and isinstance(link, str):
            if looks_like_img(img):
                pairs.append({"img": strip_query_and_unquote(img), "link": link})
    # 중복 제거
    uniq = {}
    for p in pairs:
        key = (url_basename(p["img"]), p["link"])
        uniq[key] = p
    return list(uniq.values())

def collect_next_pairs(soup: BeautifulSoup) -> list[dict]:
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.text:
        return []
    try:
        data = json.loads(tag.text)
    except Exception:
        return []
    pairs = extract_pairs_from_next(data)
    # 디버깅 저장
    try:
        os.makedirs("debug", exist_ok=True)
        with open("debug/next_pairs.jsonl", "w", encoding="utf-8") as f:
            for p in pairs:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return pairs

# -------------------------
# Link 매칭 (Src ↔ image_url)
# -------------------------
def fill_links(rows: list[dict], pairs: list[dict]) -> list[dict]:
    if not rows or not pairs:
        return rows

    # 이미지 basename -> link 후보
    idx: dict[str, set[str]] = {}
    for p in pairs:
        b = url_basename(p["img"])
        if not b:
            continue
        idx.setdefault(b, set()).add(p["link"])

    for r in rows:
        if r.get("Link"):
            continue
        b = url_basename(r.get("Src", ""))
        cand = sorted(idx.get(b, []))
        if cand:
            r["Link"] = cand[0]  # 동일 파일명은 보통 1:1
    return rows

# -------------------------
# Google Drive 업로드
# -------------------------
def upload_to_gdrive(local_path: str, filename: str) -> str:
    """
    CSV를 구글 '공유드라이브' 폴더에 업로드(동일 파일명 있으면 update, 없으면 create).
    환경변수:
      - GDRIVE_FOLDER_ID (필수)
      - GDRIVE_CREDENTIALS_JSON (필수)  # 서비스계정 JSON 원문 (권장)
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
            "공유드라이브 폴더 ID가 맞는지, 서비스계정이 해당 드라이브의 구성원인지 확인하세요."
        )

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
            "서비스계정 권한(공유드라이브 구성원)과 GDRIVE_FOLDER_ID가 공유드라이브 폴더인지 확인하세요."
        )

# -------------------------
# main
# -------------------------
def main():
    html = fetch_html(BASE_URL)

    # 디버그: 원문 저장
    try:
        os.makedirs("debug", exist_ok=True)
        with open("debug/home.html", "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass

    soup = BeautifulSoup(html, "lxml")

    # 1) DOM에서 배너 IMG 수집
    rows = collect_from_dom(soup)

    # 2) __NEXT_DATA__에서 (image_url, link_url)만 추출해서 1:1 매칭
    pairs = collect_next_pairs(soup)
    rows = fill_links(rows, pairs)

    # 3) CSV 저장 (Title/Alt 통일)
    for r in rows:
        r["Alt"] = r.pop("Title", "")

    df = pd.DataFrame(rows, columns=["Link", "Alt", "Src"])
    out_csv = "jasoseol_banner.csv"
    df.to_csv(out_csv, index=False, encoding="utf-8-sig", lineterminator="\n")
    print(f"[OK] {len(df)}개 배너 수집 완료 → {out_csv}")

    # 4) 구글 드라이브 업로드
    print("[INFO] Google Drive 업로드 시작…")
    file_id = upload_to_gdrive(out_csv, "jasoseol_banner.csv")
    print(f"[OK] Drive 업로드 완료 fileId={file_id}")

if __name__ == "__main__":
    main()
