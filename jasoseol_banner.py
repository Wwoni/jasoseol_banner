# jasoseol_banner.py
import os
import re
import json
import time
import pandas as pd
from urllib.parse import urljoin, urlparse, unquote
from typing import List, Dict

BASE_URL = "https://jasoseol.com/"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ------------------------- Google Drive -------------------------
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
        if not os.path.exists(sa_path):
            raise FileNotFoundError(f"서비스계정 파일을 찾을 수 없습니다: {sa_path}")
        creds = service_account.Credentials.from_service_account_file(sa_path, scopes=scopes)

    drive = build("drive", "v3", credentials=creds)

    # 폴더 확인
    try:
        folder_meta = drive.files().get(
            fileId=folder_id, fields="id,name,driveId,mimeType", supportsAllDrives=True
        ).execute()
        if folder_meta.get("mimeType") != "application/vnd.google-apps.folder":
            raise RuntimeError(f"GDRIVE_FOLDER_ID가 폴더가 아닙니다: {folder_meta.get('mimeType')}")
    except HttpError as he:
        raise RuntimeError(f"폴더 조회 실패: {he}")

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

    if files:
        file_id = files[0]["id"]
        drive.files().update(fileId=file_id, media_body=media, supportsAllDrives=True).execute()
        return file_id
    else:
        metadata = {"name": filename, "parents": [folder_id]}
        file = drive.files().create(
            body=metadata, media_body=media, fields="id_

