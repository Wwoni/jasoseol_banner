import os
import json
import pandas as pd
from urllib.parse import urljoin, urlparse, unquote
from playwright.sync_api import sync_playwright
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

BASE_URL = "https://jasoseol.com/"

# --------------------
# Helpers
# --------------------
def url_basename(url: str) -> str:
    if not url:
        return ""
    u = unquote(url)
    return os.path.basename(urlparse(u).path)

def lcs_len(a: str, b: str) -> int:
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    best = 0
    for i in range(len(short)):
        for j in range(i + 1, len(short) + 1):
            seg = short[i:j]
            if seg and seg in long:
                best = max(best, j - i)
    return best

# --------------------
# Playwright 크롤링
# --------------------
def scrape_banners():
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BASE_URL, timeout=30000)
        page.wait_for_selector(".main-banner-ggs img", timeout=10000)

        # 이미지 태그들 추출
        banners = page.query_selector_all(".main-banner-ggs img")
        for img in banners:
            alt = img.get_attribute("alt") or ""
            src = img.get_attribute("src") or ""
            rows.append({"Alt": alt, "Src": src, "Link": ""})

        # __NEXT_DATA__ JSON 추출
        tag = page.query_selector("script#__NEXT_DATA__")
        pairs = []
        if tag:
            try:
                data = json.loads(tag.inner_text())
                def walk(node):
                    if isinstance(node, dict):
                        strings = []
                        for v in node.values():
                            if isinstance(v, (dict, list)):
                                walk(v)
                            else:
                                strings.append(v)
                        imgs, links = [], []
                        for s in strings:
                            if not isinstance(s, str):
                                continue
                            if ".png" in s or ".jpg" in s or ".jpeg" in s or ".webp" in s:
                                imgs.append(s)
                            elif isinstance(s, str) and s.startswith("/"):
                                links.append(s)
                        if imgs and links:
                            for si in imgs:
                                pairs.append({"img": si, "link": urljoin(BASE_URL, links[0])})
                    elif isinstance(node, list):
                        for v in node:
                            walk(v)
                walk(data)
            except Exception:
                pass

        # 이미지 ↔ 링크 매칭
        index = {}
        for p_ in pairs:
            index.setdefault(url_basename(p_["img"]), set()).add(p_["link"])

        for r in rows:
            b = url_basename(r["Src"])
            if b in index:
                r["Link"] = sorted(index[b])[0]
            else:
                # 근사 매칭
                best_link, best_score = None, 0
                for pbname, links in index.items():
                    score = lcs_len(b, pbname)
                    if score > best_score:
                        best_score, best_link = score, sorted(links)[0]
                if best_link and best_score >= max(5, len(b) // 3):
                    r["Link"] = best_link

        browser.close()
    return rows

# --------------------
# Google Drive Upload
# --------------------
def upload_to_gdrive(local_path: str, filename: str) -> str:
    folder_id = os.environ.get("GDRIVE_FOLDER_ID")
    raw_json = os.environ.get("GDRIVE_CREDENTIALS_JSON")
    sa_path = os.environ.get("GDRIVE_SA_JSON_PATH", "gdrive_sa.json")

    if not folder_id:
        raise RuntimeError("GDRIVE_FOLDER_ID가 설정되지 않았습니다.")

    scopes = ["https://www.googleapis.com/auth/drive"]
    creds = None
    if raw_json:
        info = json.loads(raw_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    else:
        if not os.path.exists(sa_path):
            raise FileNotFoundError(f"서비스계정 파일을 찾을 수 없습니다: {sa_path}")
        creds = service_account.Credentials.from_service_account_file(sa_path, scopes=scopes)

    drive = build("drive", "v3", credentials=creds)

    # 동일 파일명 검색
    query = f"name = '{filename}' and '{folder_id}' in parents and trashed = false"
    resp = drive.files().list(q=query, fields="files(id,name)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    files = resp.get("files", [])
    media = MediaFileUpload(local_path, mimetype="text/csv", resumable=True)

    if files:
        file_id = files[0]["id"]
        drive.files().update(fileId=file_id, media_body=media, supportsAllDrives=True).execute()
        return file_id
    else:
        metadata = {"name": filename, "parents": [folder_id]}
        file = drive.files().create(body=metadata, media_body=media, fields="id", supportsAllDrives=True).execute()
        return file["id"]

# --------------------
# main
# --------------------
def main():
    rows = scrape_banners()
    df = pd.DataFrame(rows).drop_duplicates()
    out_csv = "jasoseol_banner.csv"
    df.to_csv(out_csv, index=False, encoding="utf-8-sig", lineterminator="\n")
    print(f"[OK] {len(df)}개 배너 수집 완료 → {out_csv}")

    print("[INFO] Google Drive 업로드 시작…")
    file_id = upload_to_gdrive(out_csv, "jasoseol_banner.csv")
    print(f"[OK] Drive 업로드 완료 fileId={file_id}")

if __name__ == "__main__":
    main()
