import os, re, json
import pandas as pd
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote, parse_qs
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

BASE_URL = "https://jasoseol.com/"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

IMG_EXT_RE = re.compile(r"\.(?:png|jpg|jpeg|webp|gif)(?:\?.*)?$", re.IGNORECASE)

# -------------------------
# Helpers
# -------------------------
def fetch_html(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    resp.raise_for_status()
    return resp.text

def normalize_img_url(url: str) -> str:
    if not url:
        return ""
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
    u = unquote(url)
    path = urlparse(u).path
    return os.path.basename(path)

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
# Collectors
# -------------------------
def collect_from_dom(soup: BeautifulSoup) -> list[dict]:
    rows = []
    banners = soup.select(".main-banner-ggs img")
    for img in banners:
        alt = img.get("alt") or ""
        src = normalize_img_url(img.get("src") or "")
        rows.append({"Link": "", "Alt": alt, "Src": src})
    return rows

def collect_next_pairs(soup: BeautifulSoup) -> list[dict]:
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
            imgs, links = [], []
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)
                elif isinstance(v, str):
                    if IMG_EXT_RE.search(v) or "/_next/image" in v:
                        imgs.append(normalize_img_url(v))
                    elif v.startswith("/"):
                        links.append(v)
            if imgs and links:
                for img in imgs:
                    pairs.append({"img": img, "link": urljoin(BASE_URL, links[0])})
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)

    uniq = {}
    for p in pairs:
        key = (url_basename(p["img"]), p["link"])
        uniq[key] = p
    return list(uniq.values())

def match_links(rows: list[dict], pairs: list[dict]) -> list[dict]:
    index = {}
    for p in pairs:
        bname = url_basename(p["img"])
        index.setdefault(bname, set()).add(p["link"])

    for r in rows:
        b = url_basename(r.get("Src", ""))
        if not b:
            continue
        # exact match
        if b in index:
            r["Link"] = sorted(index[b])[0]
            continue
        # fuzzy match
        best_link, best_score = None, 0
        for pb, links in index.items():
            score = lcs_len(b, pb)
            if score > best_score:
                best_score, best_link = score, sorted(links)[0]
        if best_link and best_score >= max(5, len(b)//3):
            r["Link"] = best_link
    return rows

# -------------------------
# GDrive Upload
# -------------------------
def upload_to_gdrive(local_path: str, filename: str) -> str:
    folder_id = os.environ.get("GDRIVE_FOLDER_ID")
    raw_json = os.environ.get("GDRIVE_CREDENTIALS_JSON")
    sa_path = "gdrive_sa.json"
    if not folder_id:
        raise RuntimeError("GDRIVE_FOLDER_ID not set")

    scopes = ["https://www.googleapis.com/auth/drive"]
    creds = None
    if raw_json:
        try:
            info = json.loads(raw_json)
            creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        except Exception:
            pass
    if creds is None and os.path.exists(sa_path):
        creds = service_account.Credentials.from_service_account_file(sa_path, scopes=scopes)

    drive = build("drive", "v3", credentials=creds)

    resp = drive.files().list(
        q=f"name='{filename}' and '{folder_id}' in parents and trashed=false",
        fields="files(id,name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get("files", [])
    media = MediaFileUpload(local_path, mimetype="text/csv", resumable=True)
    if files:
        file_id = files[0]["id"]
        drive.files().update(fileId=file_id, media_body=media, supportsAllDrives=True).execute()
        return file_id
    else:
        meta = {"name": filename, "parents": [folder_id]}
        file = drive.files().create(body=meta, media_body=media, fields="id", supportsAllDrives=True).execute()
        return file["id"]

# -------------------------
# Main
# -------------------------
def main():
    html = fetch_html(BASE_URL)
    soup = BeautifulSoup(html, "lxml")

    rows = collect_from_dom(soup)
    pairs = collect_next_pairs(soup)
    rows = match_links(rows, pairs)

    df = pd.DataFrame(rows).drop_duplicates()
    out_csv = "jasoseol_banner.csv"
    df.to_csv(out_csv, index=False, encoding="utf-8-sig", lineterminator="\n")
    print(f"[OK] {len(df)}개 배너 수집 완료 → {out_csv}")

    print("[INFO] Google Drive 업로드 시작…")
    fid = upload_to_gdrive(out_csv, "jasoseol_banner.csv")
    print(f"[OK] Drive 업로드 완료 fileId={fid}")

if __name__ == "__main__":
    main()
