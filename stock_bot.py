import os
import sys
import unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
from deep_translator import GoogleTranslator, MyMemoryTranslator

# ==========================================
# 1. 텔레그램 설정 (GitHub Secrets에서 불러옴 — 기존 뉴스 봇과 동일한 값 재사용)
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_IDS = [cid.strip() for cid in os.environ["TELEGRAM_CHAT_ID"].split(",") if cid.strip()]

# ==========================================
# 2. 추적할 종목 (한국 반도체 시장에 영향을 주는 미국 종목)
# ==========================================
TICKERS = [
    ("SK하이닉스(ADR)", "SKHY"),
    ("마이크론", "MU"),
    ("엔비디아", "NVDA"),
    ("TSMC", "TSM"),
    ("AMD", "AMD"),
    ("인텔", "INTC"),
    ("퀄컴", "QCOM"),
    ("브로드컴", "AVGO"),
    ("ASML", "ASML"),
]

# ==========================================
# 2-1. 비교 참고용 국내 반도체 종목 (코스피 원주)
# ==========================================
KR_TICKERS = [
    ("삼성전자", "005930.KS"),
    ("SK하이닉스(국내)", "000660.KS"),
]

MAX_MESSAGE_LEN = 4000


def display_width(s):
    """한글은 2칸, 영문/숫자는 1칸으로 계산해서 모노스페이스 폰트에서 정렬을 맞추기 위한 폭 계산."""
    width = 0
    for ch in s:
        if unicodedata.east_asian_width(ch) in ('W', 'F'):
            width += 2
        else:
            width += 1
    return width


def pad(s, width):
    return s + ' ' * max(0, width - display_width(s))


def fetch_price_change(symbol):
    """전일 종가 기준 등락률(%)과 그 기준이 된 거래일(현지 거래소 기준 날짜)을 계산. 실패하면 (None, None)."""
    try:
        hist = yf.Ticker(symbol).history(period="5d")
        if len(hist) < 2:
            return None, None
        last_close = hist['Close'].iloc[-1]
        prev_close = hist['Close'].iloc[-2]
        last_date = hist.index[-1]
        if prev_close == 0:
            return None, None
        change = round((last_close - prev_close) / prev_close * 100, 2)
        return change, last_date.strftime("%Y-%m-%d")
    except Exception as e:
        print(f"⚠️ [{symbol}] 주가 조회 실패: {e}")
        return None, None


def fetch_next_earnings_date(symbol):
    """다음 예정 실적 발표일(문자열). 못 찾으면 None."""
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.get_earnings_dates(limit=12)
        if df is None or df.empty:
            return None
        now = datetime.now(timezone.utc)
        future_dates = [idx for idx in df.index if idx.to_pydatetime().replace(tzinfo=timezone.utc) >= now] \
            if df.index.tz is None else [idx for idx in df.index if idx.to_pydatetime() >= now]
        if not future_dates:
            return None
        next_date = min(future_dates)
        return next_date.strftime("%m/%d")
    except Exception as e:
        print(f"⚠️ [{symbol}] 실적일 조회 실패: {e}")
        return None


def contains_korean(text):
    return any('\uac00' <= ch <= '\ud7a3' for ch in text)


def looks_like_translation_error(text):
    """번역 API가 실패 메시지를 결과처럼 반환하는 경우를 걸러내기 위한 방어 로직."""
    error_markers = [
        "INVALID SOURCE LANGUAGE",
        "MYMEMORY WARNING",
        "QUERY LENGTH LIMIT",
        "IS AN INVALID",
        "AMOUNT OF LETTERS",
    ]
    upper = text.upper()
    return any(marker in upper for marker in error_markers)


def translate_to_korean(text):
    """영어 텍스트를 한국어로 번역. 이미 한국어면 그대로 반환. 구글이 막히면 MyMemory로 재시도.
    두 서비스 모두 실패하거나 오류 메시지를 반환하면 원문을 그대로 반환한다."""
    if not text:
        return text

    if contains_korean(text):
        return text  # 이미 한국어면 번역 불필요

    try:
        result = GoogleTranslator(source='en', target='ko').translate(text)
        if result and not looks_like_translation_error(result) and result.strip().lower() != text.strip().lower():
            return result
    except Exception as e:
        print(f"⚠️ 구글 번역 실패, MyMemory로 재시도: {e}")

    try:
        result = MyMemoryTranslator(source='en-GB', target='ko-KR').translate(text)
        if result and not looks_like_translation_error(result):
            return result
    except Exception as e:
        print(f"⚠️ MyMemory 번역도 실패: {e}")

    return text  # 둘 다 실패하면 원문이라도 반환


def fetch_latest_news_headline(symbol):
    """최근 뉴스 헤드라인 하나를 가져와 한국어로 번역. 실패하면 None."""
    try:
        ticker = yf.Ticker(symbol)
        news_list = ticker.news or []
        if not news_list:
            return None

        first = news_list[0]
        # yfinance 버전에 따라 구조가 다를 수 있어 두 가지 형태 모두 대응
        title = first.get('title')
        if not title and 'content' in first:
            title = first['content'].get('title')

        if not title:
            return None

        return translate_to_korean(title)
    except Exception as e:
        print(f"⚠️ [{symbol}] 뉴스 조회 실패: {e}")
        return None


def build_table_section(rows, title):
    """등락률/실적일 표 (모노스페이스 정렬)"""
    name_width = max(display_width(r['name']) for r in rows) + 1
    change_width = 9
    earnings_width = 8

    header = pad("종목", name_width) + pad("등락률", change_width) + "실적발표일"
    lines = [header, "-" * (name_width + change_width + earnings_width)]

    for r in rows:
        if r['change'] is None:
            change_str = "N/A"
        else:
            arrow = "🔺" if r['change'] > 0 else ("🔻" if r['change'] < 0 else "➖")
            change_str = f"{arrow}{r['change']:+.2f}%"
        earnings_str = r['earnings'] if r['earnings'] else "미정"

        lines.append(pad(r['name'], name_width) + pad(change_str, change_width) + earnings_str)

    return f"<b>{title}</b>\n<pre>" + "\n".join(lines) + "</pre>"


def build_news_section(rows, title="📰 최근 이슈 한 줄"):
    lines = [f"<b>{title}</b>"]
    for r in rows:
        if r['news']:
            lines.append(f"🔹 <b>{r['name']}</b>: {r['news']}")
    return "\n".join(lines) if len(lines) > 1 else ""


def fetch_rows(ticker_list):
    rows = []
    for name, symbol in ticker_list:
        print(f"조회 중: {name} ({symbol})")
        change, as_of = fetch_price_change(symbol)
        earnings = fetch_next_earnings_date(symbol)
        news = fetch_latest_news_headline(symbol)
        rows.append({"name": name, "symbol": symbol, "change": change, "as_of": as_of,
                     "earnings": earnings, "news": news})
    return rows


def most_common_as_of(rows):
    """종목들 중 가장 흔한 거래 기준일을 대표값으로 사용 (개별 종목마다 휴장/지연 차이가 있을 수 있어서)."""
    dates = [r['as_of'] for r in rows if r.get('as_of')]
    if not dates:
        return None
    counts = {}
    for d in dates:
        counts[d] = counts.get(d, 0) + 1
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


MARKET_STATE_LABELS = {
    "REGULAR": "장중(진행중)",
    "PRE": "프리마켓",
    "POST": "애프터마켓",
    "CLOSED": "장마감",
}


def get_market_state_label(symbol):
    """대표 종목 하나로 해당 시장이 지금 열려있는지/닫혀있는지 확인. 실패하면 (None, None)."""
    try:
        state = yf.Ticker(symbol).info.get('marketState')
        return state, MARKET_STATE_LABELS.get(state)
    except Exception as e:
        print(f"⚠️ [{symbol}] 시장 상태 조회 실패: {e}")
        return None, None


def build_timing_label(as_of_date_str, state, exchange_tz_name, close_hour, close_minute):
    """표 제목에 들어갈 '기준일 + 시각 + 마감/진행중' 라벨을 만든다. 시각은 항상 한국시간(KST)으로 환산."""
    if not as_of_date_str:
        return None

    state_label = MARKET_STATE_LABELS.get(state, "정보없음")

    if state == "CLOSED" or state is None:
        # 마감 시각(해당 거래소 현지 마감 시각)을 KST로 환산해서 표시
        try:
            local_close = datetime.strptime(as_of_date_str, "%Y-%m-%d").replace(
                hour=close_hour, minute=close_minute, tzinfo=ZoneInfo(exchange_tz_name)
            )
            kst_close = local_close.astimezone(ZoneInfo("Asia/Seoul"))
            return f"{kst_close.strftime('%m/%d %H:%M')} KST 마감"
        except Exception:
            return f"{as_of_date_str} {state_label}"
    else:
        # 장이 열려있는 중이면 '지금 이 시각 기준'으로 표시
        now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
        return f"{now_kst.strftime('%m/%d %H:%M')} KST 기준 ({state_label})"


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    any_success = False
    for chat_id in TELEGRAM_CHAT_IDS:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        try:
            res = requests.post(url, json=payload, timeout=10)
            if res.status_code != 200:
                print(f"❌ [{chat_id}] 텔레그램 응답 오류: {res.status_code} {res.text}")
            else:
                any_success = True
        except requests.RequestException as e:
            print(f"❌ [{chat_id}] 전송 중 네트워크 오류: {e}")
    return any_success


def main():
    us_rows = fetch_rows(TICKERS)
    kr_rows = fetch_rows(KR_TICKERS)

    us_as_of = most_common_as_of(us_rows)
    kr_as_of = most_common_as_of(kr_rows)

    us_state, _ = get_market_state_label(TICKERS[0][1])
    kr_state, _ = get_market_state_label(KR_TICKERS[0][1])

    us_timing = build_timing_label(us_as_of, us_state, "America/New_York", 16, 0)
    kr_timing = build_timing_label(kr_as_of, kr_state, "Asia/Seoul", 15, 30)

    us_title = f"🇺🇸 미국 시장 ({us_timing})" if us_timing else "🇺🇸 미국 시장"
    kr_title = f"🇰🇷 국내 비교 ({kr_timing})" if kr_timing else "🇰🇷 국내 비교 (코스피 원주)"

    message_parts = [
        "<b>📈 [반도체 관련 종목 시황]</b>\n",
        build_table_section(us_rows, us_title),
        build_table_section(kr_rows, kr_title),
    ]

    us_news = build_news_section(us_rows, "🇺🇸 미국 관련 이슈")
    if us_news:
        message_parts.append("\n" + us_news)

    kr_news = build_news_section(kr_rows, "🇰🇷 국내 관련 이슈")
    if kr_news:
        message_parts.append("\n" + kr_news)

    final_message = "\n".join(message_parts)
    if len(final_message) > MAX_MESSAGE_LEN:
        final_message = final_message[:MAX_MESSAGE_LEN] + "\n...(생략)"

    success = send_telegram(final_message)
    if success:
        print("✅ 텔레그램 발송 완료!")
    else:
        print("❌ 발송 실패. 토큰 또는 Chat ID를 다시 확인하세요.")
        sys.exit(1)


if __name__ == "__main__":
    main()
