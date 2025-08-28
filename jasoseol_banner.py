import os
import re
import json
import requests
import pandas as pd
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote, parse_qs
from typing import Any, Iterable, List, Dict, Tuple

BASE_ORIGIN = "https://jasoseol.com"
CANDIDATE_PATHS = ["/desktop", "/"]  # 데스크톱 페이지 우선, 실패 시 루트
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
}

IMG_EXT_RE = re.compile(r"\.(?:png|jpg|jpeg|webp|gif)(?:\?.*)?$", re.IGNORECASE)

# -------------------------
# HTTP / Parsing helpers
# -------------------------
def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=20)
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
    # /_next/image?url=... → 실제 원본 URL로 변환 시도
    if "/_next/image" in url and "url=" in url:
        try:
            qs = parse_qs(urlparse(url).query)
            raw = qs.get("url", [""])[0]
            if raw:
                return urljoin(BASE_ORIGIN, raw)
        except Exception:
            pass
    return urljoin(BASE_ORIGIN, url)

def url_basename(url: str) -> str:
    """쿼리 제거 + 디코딩된 파일명만 추출"""
    if not url:
        return ""
    u = unquote(url)
    return os.path.basename(urlparse(u).path)

def parse_srcset_modified(srcset: str) -> str:
    """
    Selenium 수동 코드 규칙 재현:
    - ', ' 로 split 후 마지막 항목 제거
    - 각 항목은 'url width' 중 url만 취함
    - 상대경로면 'https://jasoseol.com' 접두

