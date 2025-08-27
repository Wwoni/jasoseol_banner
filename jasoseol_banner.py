import requests
from bs4 import BeautifulSoup
import pandas as pd
from urllib.parse import urljoin
from datetime import datetime

BASE_URL = "https://jasoseol.com/"

def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.text

def parse_srcset_to_urls(srcset: str, base: str) -> list:
    """
    srcset 문자열에서 url 부분만 절대경로로 추출.
    원래 코드 로직처럼 '마지막 항목 제외'까지 반영.
    """
    if not srcset:
        return []
    parts = [p.strip() for p in srcset.split(",") if p.strip()]
    # 마지막 항목 제외
    parts = parts[:-1] if len(parts) > 1 else parts
    urls = []
    for p in parts:
        # "url width" 형태에서 url만
        url_only = p.split()[0]
        abs_url = urljoin(base, url_only)
        urls.append(abs_url)
    return urls

def scrape_banners(html: str) -> pd.DataFrame:
    soup = BeautifulSoup(html, "lxml")
    banners = soup.select(".main-banner-ggs")
    rows = []
    for b in banners:
        a = b.find("a")
        img = b.find("img")
        link = urljoin(BASE_URL, a.get("href")) if a and a.get("href") else None
        alt = img.get("alt") if img and img.get("alt") else ""
        srcset = img.get("srcset") if img and img.get("srcset") else ""
        # srcset -> URL 리스트(절대경로), 마지막 항목 제외 로직 반영
        srcset_urls = parse_srcset_to_urls(srcset, BASE_URL)
        # 원래 코드처럼 ", " 로 조인
        modified_srcset = ", ".join(srcset_urls)

        # 대체로 src(단일)도 같이 저장해두면 분석에 유용
        src = urljoin(BASE_URL, img.get("src")) if img and img.get("src") else ""

        if link or src or modified_srcset:
            rows.append(
                {
                    "Link": link or "",
                    "Alt": alt,
                    "Src": src,
                    "Srcset_Modified": modified_srcset,
                }
            )
    return pd.DataFrame(rows)

def main():
    html = fetch_html(BASE_URL)
    df = scrape_banners(html)

    # 타임스탬프 포함 파일명(옵션) 또는 고정 파일명
    out_csv = "jasoseol_banner.csv"
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")

    print(f"[OK] {len(df)}개 배너 수집 완료 → {out_csv}")
    # 변경 추적 위한 로그 파일(선택)
    with open("last_run.txt", "w", encoding="utf-8") as f:
        f.write(datetime.now().isoformat())

if __name__ == "__main__":
    main()
