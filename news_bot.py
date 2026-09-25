import os
import re
import sys
import html
import calendar
from datetime import datetime, timezone, timedelta
import feedparser
import requests
from deep_translator import GoogleTranslator

# ==========================================
# 1. 텔레그램 설정 (GitHub Secrets에서 불러옴)
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ==========================================
# 2. RSS 뉴스 소스 목록 (경제, 국제정세, 테크)
# ==========================================
RSS_FEEDS = {
    "경제/환율/금리": "https://rss.donga.com/economy.xml",
    "국제/전쟁/정세": "https://rss.donga.com/international.xml",
    "IT/테크": "https://rss.donga.com/it.xml",
    # 영어권 소스
    "[EN] BBC Business": "http://feeds.bbci.co.uk/news/business/rss.xml",
    "[EN] BBC Technology": "http://feeds.bbci.co.uk/news/technology/rss.xml",
    "[EN] Tom's Hardware Semiconductors": "https://www.tomshardware.com/feeds/tag/semiconductors",
}

# ==========================================
# 3. 필터링할 핵심 키워드 목록 (한글 + 영어, 대소문자 구분 없이 매칭)
# ==========================================
TARGET_KEYWORDS = [
    # 반도체 기업 (한글)
    "삼성전자", "SK하이닉스", "하이닉스", "마이크론", "TSMC", "엔비디아", "인텔",
    "AMD", "퀄컴", "브로드컴", "ASML", "AI칩",
    # 반도체 기술/산업 (한글)
    "반도체", "HBM", "DRAM", "NAND", "파운드리", "EUV",
    # 매크로 & 국제정세 (한글)
    "환율", "금리", "연준", "FOMC", "인플레이션", "유가", "전쟁", "대만", "중동", "지정학",
    # 반도체 기업 (영어)
    "Samsung", "SK Hynix", "Hynix", "Micron", "Nvidia", "Intel", "Qualcomm",
    "Broadcom", "ASML",
    # 반도체 기술/산업 (영어)
    "semiconductor", "chip", "foundry",
    # 매크로 & 국제정세 (영어)
    "Federal Reserve", "interest rate", "inflation", "oil price", "Taiwan",
    "Middle East", "geopolitics", "tariff",
]

MAX_ARTICLES = 10
MAX_MESSAGE_LEN = 4000  # 텔레그램 한도(4096)보다 여유있게 설정
MAX_ENTRIES_PER_FEED = 50  # 피드별 탐색할 최신 기사 개수
ARTICLE_MAX_AGE_HOURS = 24  # 이 시간 이내에 발행된 기사만 포함


def clean_text(raw_html):
    """HTML 태그 제거 및 텍스트 정리"""
    cleanr = re.compile('<.*?>')
    cleantext = re.sub(cleanr, '', raw_html)
    return html.unescape(cleantext).strip()


def is_recent(entry, max_age_hours):
    """기사 발행 시각이 max_age_hours 이내인지 확인. 발행 시각을 알 수 없으면 통과시킴."""
    time_struct = getattr(entry, 'published_parsed', None) or getattr(entry, 'updated_parsed', None)
    if not time_struct:
        return True  # 발행 시각 정보가 없으면 걸러내지 않음

    published_dt = datetime.fromtimestamp(calendar.timegm(time_struct), tz=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    return published_dt >= cutoff


def translate_to_korean(text):
    """영어 텍스트를 한국어로 번역. 실패하면 None을 반환(번역 없이 진행)."""
    if not text:
        return None
    try:
        return GoogleTranslator(source='en', target='ko').translate(text)
    except Exception as e:
        print(f"⚠️ 번역 실패: {e}")
        return None


def send_telegram(text):
    """텔레그램 메시지 발송"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code != 200:
            print(f"❌ 텔레그램 응답 오류: {res.status_code} {res.text}")
            print("힌트: chat_id가 올바른지, 봇과 대화를 시작(/start)했는지 확인하세요.")
        return res.status_code == 200
    except requests.RequestException as e:
        print(f"❌ 전송 중 네트워크 오류: {e}")
        return False


def fetch_articles():
    matching_articles = []

    for category, url in RSS_FEEDS.items():
        feed = feedparser.parse(url)
        if feed.bozo:
            print(f"⚠️ {category} 피드 파싱 경고: {feed.bozo_exception}")

        for entry in feed.entries[:MAX_ENTRIES_PER_FEED]:
            title = getattr(entry, 'title', '')
            summary = clean_text(getattr(entry, 'summary', ''))
            link = getattr(entry, 'link', '')

            if not title or not link:
                continue

            if not is_recent(entry, ARTICLE_MAX_AGE_HOURS):
                continue

            found_keywords = [kw for kw in TARGET_KEYWORDS if kw.lower() in title.lower() or kw.lower() in summary.lower()]
            if found_keywords:
                is_english = category.startswith("[EN]")
                article = {
                    "category": category,
                    "title": title,
                    "summary": summary[:120] + "..." if len(summary) > 120 else summary,
                    "link": link,
                    "keywords": list(set(found_keywords)),
                    "translated_title": None,
                    "translated_summary": None,
                }
                if is_english:
                    article["translated_title"] = translate_to_korean(title)
                    article["translated_summary"] = translate_to_korean(article["summary"])
                matching_articles.append(article)

    return matching_articles


def diversify_order(matching_articles):
    """한글/영어 기사를 번갈아 배치하되, 한쪽이 부족하면 다른 쪽으로 최대 MAX_ARTICLES개까지 채운다."""
    korean_articles = [a for a in matching_articles if not a['category'].startswith("[EN]")]
    english_articles = [a for a in matching_articles if a['category'].startswith("[EN]")]

    ordered = []
    i = 0
    while len(ordered) < MAX_ARTICLES and (i < len(korean_articles) or i < len(english_articles)):
        if i < len(korean_articles):
            ordered.append(korean_articles[i])
            if len(ordered) >= MAX_ARTICLES:
                break
        if i < len(english_articles):
            ordered.append(english_articles[i])
        i += 1
    return ordered[:MAX_ARTICLES]


def build_message(matching_articles):
    msg_lines = ["<b>📊 [반도체 & 글로벌 정세 핵심 브리핑]</b>\n"]

    seen_titles = set()
    count = 0
    for art in diversify_order(matching_articles):
        if art['title'] in seen_titles:
            continue
        seen_titles.add(art['title'])

        safe_title = html.escape(art['title'])
        safe_summary = html.escape(art['summary']) if art['summary'] else ""
        kw_str = ", ".join([f"#{k}" for k in art['keywords'][:3]])

        block = [f"🔹 <b>{safe_title}</b>", f"태그: <i>{kw_str}</i>"]
        if safe_summary:
            block.append(safe_summary)

        if art.get('translated_title'):
            block.append(f"🇰🇷 <i>{html.escape(art['translated_title'])}</i>")
        if art.get('translated_summary'):
            block.append(html.escape(art['translated_summary']))

        block.append(f"<a href='{art['link']}'>👉 기사 원문 보기</a>\n")

        candidate = "\n".join(msg_lines + block)
        if len(candidate) > MAX_MESSAGE_LEN:
            break

        msg_lines.extend(block)
        count += 1
        if count >= MAX_ARTICLES:
            break

    return "\n".join(msg_lines)


def main():
    matching_articles = fetch_articles()

    if not matching_articles:
        success = send_telegram("📢 현재 조건에 맞는 최신 반도체/국제정세 기사가 없습니다.")
        if success:
            print("ℹ️ 조건에 맞는 기사가 없어 알림만 발송했습니다.")
        else:
            print("❌ 발송 실패. 토큰 또는 Chat ID를 다시 확인하세요.")
            sys.exit(1)
        return

    final_message = build_message(matching_articles)
    success = send_telegram(final_message)

    if success:
        print("✅ 텔레그램 발송 완료!")
    else:
        print("❌ 발송 실패. 토큰 또는 Chat ID를 다시 확인하세요.")
        sys.exit(1)


if __name__ == "__main__":
    main()
