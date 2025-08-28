import os
import re
import json
import requests
import pandas as pd
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote, parse_qs
from datetime import datetime
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

def parse_srcset(srcset: str, base: str) -> list[str]:
    if not srcset:
        return []
    parts = [p.strip() for p in srcset.split(",") if p.strip()]
    if len(parts) > 1:
        parts = parts[:-1]  # 원 요청 로직: 마지막 항목 제외
    urls = []
    for p in parts:
        url_only = p.split()[0]
        urls.append(safe_urljoin(base, url_only))
    return urls

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

def url_basename(url: str) -> str:
    """쿼리/파라미터 제거, 퍼센트 디코딩해서 파일명만 추출"""
    if not url:
        return ""
    u = unquote(url)  # 사람이 읽기 쉬운 형태
    path = urlparse(u).path
    return os.path.basename(path)

def lcs_len(a: str, b: str) -> int:
    """아주 가벼운 매칭 점수: 공통 부분 문자열 길이(간이). 파일명 유사도 판단에 사용."""
    # 완전한 LCS 대신 간단히 교집합 substr 길이를 추정
    # (파일명이 길어 유사도 판단은 충분)
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    best = 0
    for i in range(len(short)):
        for j in range(i + 1, len(short) + 1):
            seg = short[i:j]
            if seg and seg in long:
                best = max(best, j - i)
    return best

# -------------------------
# DOM collectors
# -------------------------
def collect_from_static_dom(soup: BeautifulSoup) -> list[dict]:
    rows = []
    candidates = soup.select(".main-banner-ggs")
    if not candidates:
        candidates = soup.select(".swiper .swiper-slide, .banner, .main-banner, .main_banner")

    for node in candidates:
        img = node.find("img")
        if not img:
            continue
        alt = img.get("alt") or ""
        src = normalize_img_url(img.get("src") or "")
        src = unquote(src)  # 가독성
        srcset = img.get("srcset") or ""
        srcset_urls = parse_srcset(srcset, BASE_URL)
        rows.append(
            {
                "Link": "",  # 나중에 NEXT_DATA 매칭으로 채움
                "Alt": alt,
                "Src": src,
                "Srcset_Modified": ", ".join(srcset_urls),
                "Source": "static_dom",
            }
        )
    return rows

# -------------------------
# __NEXT_DATA__ collectors
# -------------------------
def deep_iter(v: Any) -> Iterable[Any]:
    """임의의 중첩 구조에서 모든 원소 순회"""
    if isinstance(v, dict):
        for k, vv in v.items():
            yield from deep_iter(vv)
    elif isinstance(v, list):
        for vv in v:
            yield from deep_iter(vv)
    else:
        yield v

def collect_next_banner_pairs(soup: BeautifulSoup) -> list[dict]:
    """
    __NEXT_DATA__에서 '이미지 URL'과 '링크 경로(/로 시작, 이미지 아님)'가
    같은 객체/인접 범위에 함께 존재하는 경우를 찾아 pair로 반환.
    """
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.text:
        return []

    # 디버깅 보관
    try:
        with open("debug_next_data.json", "w", encoding="utf-8") as f:
            f.write(tag.text)
    except Exception:
        pass

    try:
        data = json.loads(tag.text)
    except Exception:
        return []

    pairs = []

    def walk(node):
        # dict인 경우: 같은 오브젝트 안에서 이미지/링크가 함께 있나 확인
        if isinstance(node, dict):
            strings = []
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)
                else:
                    strings.append(v)

            imgs = []
            links = []
            for s in strings:
                if not isinstance(s, str):
                    continue
                if looks_like_img(s):
                    imgs.append(normalize_img_url(s))
                elif s.startswith("/") and not looks_like_img(s):
                    links.append(s)

            if imgs and links:
                for si in imgs:
                    pairs.append(
                        {
                            "img": unquote(si),
                            "link": urljoin(BASE_URL, links[0]),  # 대표 1개
                        }
                    )
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    # 중복 제거
    uniq = {}
    for p in pairs:
        key = (url_basename(p["img"]), p["link"])
        uniq[key] = p
    return list(uniq.values())

# -------------------------
# Link 매칭 로직
# -------------------------
def fill_links_with_next_pairs(rows: list[dict], pairs: list[dict]) -> list[dict]:
    """
    DOM에서 수집한 rows(Src/Alt 포함)에 대해,
    NEXT_DATA에서 추출한 (img, link) 페어를 파일명 유사도 기준으로 매칭해 Link를 채움.
    """
    if not rows or not pairs:
        return rows

    # 준비: pairs를 이미지 basename -> 후보 링크 리스트로 인덱싱
    index = {}
    for p in pairs:
        bname = url_basename(p["img"])
        index.setdefault(bname, set()).add(p["link"])

    # 1차: 동일 basename 매칭
    for r in rows:
        if r.get("Link"):
            continue
        b = url_basename(r.get("Src", ""))
        if b in index and index[b]:
            r["Link"] = sorted(index[b])[0]

    # 2차: 근사 매칭(공통 부분이 긴 것 우선)
    for r in rows:
        if r.get("Link"):
            continue
        b = url_basename(r.get("Src", ""))
        if not b:
            continue
        best_link = None
        best_score = 0
        for pbname, links in index.items():
            score = lcs_len(b, pbname)
            if score > best_score:
                best_score = score
                best_link = sorted(links)[0]
        # 최소 길이(휴리스틱)로 과매칭 방지
        if best_link and best_score >= max(5, len(b) // 3):
            r["Link"] = best_link

    # 3차: 여전히 비었으면 사이트 홈(혹은 그대로 빈 값 유지 원하면 주석 처리)
    for r in rows:
        if not r.get("Link"):
            r["Link"] = urljoin(BASE_URL, "/desktop")

    return rows

# -------------------------
# Google Drive 업로드
# -------------------------
def upload_to_gdrive(local_path: str, filename: str) -> str:
    """
    CSV를 구글 '공유드라이브' 폴더에 업로드(동일 파일명 있으면 update, 없으면 create).
    환경변수:
      - GDRIVE_FOLDER_ID (필수)
      - GDRIVE_CREDENTIALS_JSON (필수)  # 서비스계정 JSON 원문 (권장 방식)
        또는 GDRIVE_SA_JSON_PATH (선택)  # gdrive_sa.json 같은 파일 경로
      - GDRIVE_DRIVE_ID (선택)          # 공유드라이브 ID (검색 최적화)
    반환: file_id
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

    # 1) 시크릿 문자열 방식 우선
    creds = None
    if raw_json:
        try:
            info = _json.loads(raw_json)
            creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        except Exception as e:
            raise RuntimeError(f"GDRIVE_CREDENTIALS_JSON 파싱 실패: {e}")

    # 2) 파일 방식 백업
    if creds is None:
        if not sa_path:
            sa_path = "gdrive_sa.json"
        if not os.path.exists(sa_path):
            raise FileNotFoundError(f"서비스계정 파일을 찾을 수 없습니다: {sa_path}")
        creds = service_account.Credentials.from_service_account_file(sa_path, scopes=scopes)

    drive = build("drive", "v3", credentials=creds)

    # 폴더 유효성 & 권한 검증
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
            "서비스계정 권한(공유드라이브 구성원)과 GDRIVE_FOLDER_ID가 공유드라이브 폴더인지 확인하세요."
        )

# -------------------------
# main
# -------------------------
def main():
    html = fetch_html(BASE_URL)

    # 디버그: 원문 저장
    try:
        with open("debug_home.html", "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass

    soup = BeautifulSoup(html, "lxml")

    # 1) DOM에서 이미지/alt 수집
    rows = collect_from_static_dom(soup)

    # 2) __NEXT_DATA__에서 (img, link) 페어 수집 후 매칭
    pairs = collect_next_banner_pairs(soup)
    rows = fill_links_with_next_pairs(rows, pairs)

    # 3) 정제/저장
    cleaned = [
        r for r in rows
        if any([r.get("Link"), r.get("Src"), r.get("Srcset_Modified")])
    ]
    df = pd.DataFrame(cleaned).drop_duplicates()

    out_csv = "jasoseol_banner.csv"
    df.to_csv(out_csv, index=False, encoding="utf-8-sig", lineterminator="\n")
    print(f"[OK] {len(df)}개 배너 수집 완료 → {out_csv}")

    # 4) 구글 드라이브 업로드
    print("[INFO] Google Drive 업로드 시작…")
    file_id = upload_to_gdrive(out_csv, "jasoseol_banner.csv")
    print(f"[OK] Drive 업로드 완료 fileId={file_id}")

if __name__ == "__main__":
    main()

