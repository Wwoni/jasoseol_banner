def close_modal_if_present(page) -> None:
    """
    중앙 팝업(광고 배너 캐러셀) 및 잔여 오버레이를 완전히 무력화.
    """
    try:
        # 1) 보이는 '닫기'류 버튼 시도
        sels = [
            "button:has-text('닫기')", "text=닫기",
            "button:has-text('오늘 하루')", "text=오늘 하루 보지 않기",
            "[aria-label='close']", "[aria-label='Close']",
            "img[alt='close']", "img[alt='Close']",
        ]
        for s in sels:
            loc = page.locator(s)
            if loc.count() > 0:
                try:
                    loc.first.click(timeout=1000, force=True)
                    time.sleep(0.2)
                except Exception:
                    pass

        # 2) 팝업 캐러셀(‘광고 배너 n’)이 들어있는 고정 레이어 제거
        page.evaluate("""
        (() => {
          // 광고 배너 이미지가 들어있는 a[href]를 찾고, 가장 가까운 고정 레이어를 숨김
          const as = Array.from(document.querySelectorAll('a[href] img[alt^="광고 배너"]'));
          for (const img of as) {
            let el = img;
            for (let i=0;i<6 && el;i++){ // 상위 6단계까지만
              el = el.parentElement;
              if (!el) break;
              const cs = window.getComputedStyle(el);
              const isFixed = cs.position === 'fixed' || cs.position === 'sticky';
              const big = el.getBoundingClientRect().width >= 300 && el.getBoundingClientRect().height >= 200;
              if (isFixed && big) {
                el.style.setProperty('display', 'none', 'important');
                break;
              }
            }
          }
          // 혹시 남은 전면 레이어/백드롭류 무력화
          const candidates = Array.from(document.querySelectorAll('div,section,aside,nav'));
          for (const el of candidates) {
            const cs = window.getComputedStyle(el);
            const r = el.getBoundingClientRect();
            if ((cs.position === 'fixed' || cs.position === 'sticky') &&
                r.width >= 300 && r.height >= 200) {
              el.style.setProperty('pointer-events','none','important');
            }
          }
        })();
        """)

        # 3) 최후 수단: ESC
        try: page.keyboard.press("Escape")
        except: pass
    } except Exception:
        pass
