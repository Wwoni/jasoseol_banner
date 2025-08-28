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

def looks_like_img(url: str) -> bool:
    if not url:
        return False
    if IMG_EXT_RE.search(url):
        return True
    if "/_next/image" in url and "url=" in url:
        return True
    return False

def normalize_img_url(url: str) -> str:
    if "/_next/image" in url and "url=" in url:
        try:
            qs = parse_qs(urlparse(url).query)
            raw = qs.get("url", [""])[0]
            if raw:
                return urljoin(BASE_URL, raw)
        except Exception:
            pass
    return urljoin(BASE_URL, url)

def url_basename(url: str) -> str:
    if not url:
        return ""
    u = unquote(url)
    path = urlparse(u).path
    return os.path.basename(path)

def parse_srcset_modified(srcset: str) -> str:
    """Selenium 코드에서 쓰던 규칙을 그대로 재현"""
    if not srcset:
        return ""
    parts = [p.strip() for p in srcset.split(",") if p.strip()]
    if len(parts) > 1:
        parts = parts[:-1]  # 마지막 항목 제거
    urls = []
    for p in parts:
        url_only = p.split()[0]
        if not (url_only.startswith("http://") or url_only.startswith("https://")):
            url_only = urljoin(BASE_URL, url_only)
        urls.append(url_only)
    return ", ".join(urls)

def lcs_len(a: str, b: str) -> int:
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
    for node in soup.select(".main-banner-ggs"):
        img = node.find("img")
        if not img:
            continue
        alt = img.get("alt", "")
        src = normalize_img_url(img.get("src") or "")
        src = unquote(src)
        srcset_mod = parse_srcset_modified(img.get("srcset") or "")

        rows.append({
            "Link": "",  # Link는 나중에 채움
            "Alt": alt,
            "Src": src,
            "Srcset": srcset_mod,
        })
    return rows

# -------------------------
# __NEXT_DATA__ collectors
# -------------------------
def deep_iter(v: Any) -> Iterable[Any]:
    if isinstance(v, dict):
        for vv in v.values():
            yield from deep_iter(vv)
    elif isinstance(v, list):
        for vv in v:
            yield from deep_iter(vv)
    else:
        yield v

def collect_next_banner_pairs(soup: BeautifulSoup) -> list[dict]:
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.text:
        return []
    try:
        data = json.loads(tag.text)
    except Exception:
        return []

    pairs = []
    def walk(node):
        if isinstance(node, dict):
            flat = []
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)
                else:
                    flat.append(v)
            imgs, links = [], []
            for s in flat:
                if not isinstance(s, str):
                    continue
                if looks_like_img(s):
                    imgs.append(normalize_img_url(s))
                elif s.startswith("/") and not looks_like_img(s):
                    links.append(s)
            if imgs and links:
                for si in imgs:
                    pairs.append({
                        "img": unquote(si),
                        "link": urljoin(BASE_URL, links[0]),
                    })
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(data)
    uniq = {}
    for p in pairs:
        key = (url_basename(p["img"]), p["link"])
        uniq[key] = p
    return list(uniq.values())

# -------------------------
# Link 매칭
# -------------------------
def fill_links_with_next_pairs(rows: list[dict], pairs: list[dict]) -> list[dict]:
    if not rows or not pairs:
        return rows
    index = {}
    for p in pairs:
        bname = url_basename(p["img"])
        index.setdefault(bname, set()).add(p["link"])
    for r in rows:
        if r.get("Link"):
            continue
        b = url_basename(r.get("Src", ""))
        if b in index and index[b]:
            r["Link"] = sorted(index[b])[0]
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
        if best_link and best_score >= max(5, len(b) // 3):
            r["Link"] = best_link
    for r in rows:
        if not r.get("Link"):
            r["Link"] = urljoin(BASE_URL, "/desktop")
    return rows

# -------------------------
# Google Drive 업로드
# -------------------------
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
        creds = service_account.Credentials.from_service_account_file(sa_path, scopes=scopes)

    drive = build("drive", "v3", credentials=creds)

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

# -------------------------
# main
# -------------------------
def main():
    html = fetch_html(BASE_URL)
    soup = BeautifulSoup(html, "lxml")

    rows = collect_from_static_dom(soup)
    pairs = collect_next_banner_pairs(soup)
    rows = fill_links_with_next_pairs(rows, pairs)

    cleaned = [
        r for r in rows if any([r.get("Link"), r.get("Alt"), r.get("Srcset")])
    ]
    df = pd.DataFrame(cleaned).drop_duplicates()

    out_csv = "jasoseol_banner.csv"
    df = df[["Link", "Alt", "Srcset"]]  # CSV 컬럼 통일
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")

    print(f"[OK] {len(df)}개 배너 수집 완료 → {out_csv}")

    print("[INFO] Google Drive 업로드 시작…")
    file_id = upload_to_gdrive(out_csv, "jasoseol_banner.csv")
    print(f"[OK] Drive 업로드 완료 fileId={file_id}")

if __name__ == "__main__":
    main()
