"""
AI 뉴스 수집 스크립트
- sources.json 에서 소스 목록 로드
- 각 사이트 크롤링 + Claude API 요약
- YouTube yt-dlp 영상 수집
- data.json 저장 (GitHub Pages 대시보드용)
- Gmail SMTP 이메일 발송
"""

import os
import re
import json
import subprocess
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from collections import Counter
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from anthropic import Anthropic

# ── 설정 ──────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).strftime('%Y-%m-%d')

ANTHROPIC_API_KEY  = os.environ.get('ANTHROPIC_API_KEY', '')
GMAIL_USER         = os.environ.get('GMAIL_USER', '')
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '')
RECIPIENT_EMAIL    = os.environ.get('RECIPIENT_EMAIL', GMAIL_USER)

YOUTUBE_CHANNEL    = 'https://www.youtube.com/@ntdkorea'
MAX_ARTICLES_PER_SOURCE = 3
MAX_YOUTUBE_VIDEOS = 5
TOP_KEYWORDS       = 10

client = Anthropic(api_key=ANTHROPIC_API_KEY)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    )
}


# ── 1. 웹 수집 ────────────────────────────────────────────

def fetch_text(url: str, max_chars: int = 6000) -> str:
    """URL에서 텍스트 추출"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe']):
            tag.decompose()
        return soup.get_text(separator=' ', strip=True)[:max_chars]
    except Exception as e:
        print(f"  [WARN] fetch_text({url}): {e}")
        return ''


def extract_articles(url: str, limit: int = 20) -> list[dict]:
    """사이트에서 기사 링크 목록 추출"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        base = urlparse(url)
        articles = []

        for a in soup.find_all('a', href=True):
            title = a.get_text(strip=True)
            href  = a['href']

            if not (20 < len(title) < 200):
                continue

            if href.startswith('http'):
                full_url = href
            elif href.startswith('/'):
                full_url = f"{base.scheme}://{base.netloc}{href}"
            else:
                continue

            articles.append({'title': title, 'url': full_url})

            if len(articles) >= limit:
                break

        return articles
    except Exception as e:
        print(f"  [WARN] extract_articles({url}): {e}")
        return []


# ── 2. Claude API 요약 ─────────────────────────────────────

def summarize_article(title: str, content: str, source: str, category: str) -> dict:
    """뉴스 기사 요약 및 키워드 추출"""
    prompt = f"""다음 AI/기술 뉴스를 분석해줘.

제목: {title}
출처: {source}
본문: {content[:3000]}

아래 JSON 형식으로만 응답해. 다른 텍스트는 절대 포함하지 마:
{{
  "title": "한국어 기사 제목 (핵심만 담게)",
  "summary": "3~4문장 한국어 요약 (AI·비즈니스 관점 중심)",
  "category": "{category}",
  "keywords": ["키워드1", "키워드2", "키워드3", "키워드4", "키워드5"]
}}"""

    try:
        msg = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=700,
            messages=[{'role': 'user', 'content': prompt}]
        )
        text = msg.content[0].text.strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        print(f"  [WARN] summarize_article: {e}")

    return {
        'title': title,
        'summary': '요약을 생성하지 못했습니다.',
        'category': category,
        'keywords': []
    }


def summarize_youtube(title: str, description: str = '') -> str:
    """YouTube 영상 제목·설명 기반 2~3문장 요약"""
    content = f"제목: {title}"
    if description:
        content += f"\n설명: {description[:800]}"

    try:
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=300,
            messages=[{
                'role': 'user',
                'content': (
                    f"다음 YouTube 영상을 AI/기술 관점에서 2~3문장 한국어로 요약해줘:\n{content}"
                )
            }]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"  [WARN] summarize_youtube: {e}")
        return title


# ── 3. 뉴스 수집 ──────────────────────────────────────────

def collect_news(sources: list[dict]) -> list[dict]:
    """활성화된 소스에서 뉴스 수집"""
    news_items = []

    for source in sources:
        if not source.get('enabled', True):
            continue

        name     = source.get('name', '')
        url      = source.get('url', '')
        category = source.get('category', 'tech')

        print(f"\n[INFO] 수집 중: {name}")

        articles = extract_articles(url)
        count = 0

        for article in articles:
            if count >= MAX_ARTICLES_PER_SOURCE:
                break

            art_title = article.get('title', '')
            art_url   = article.get('url', url)

            content = fetch_text(art_url)
            if not content:
                content = art_title

            result = summarize_article(art_title, content, name, category)

            news_items.append({
                'category': result.get('category', category),
                'title':    result.get('title', art_title),
                'summary':  result.get('summary', ''),
                'source':   name,
                'date':     TODAY,
                'url':      art_url,
                'keywords': result.get('keywords', [])
            })

            count += 1
            print(f"  ✓ {art_title[:60]}")

    return news_items


# ── 4. YouTube 수집 ───────────────────────────────────────

def collect_youtube() -> list[dict]:
    """yt-dlp로 채널 최신 영상 메타데이터 수집"""
    print(f"\n[INFO] YouTube 수집: {YOUTUBE_CHANNEL}")
    items = []

    try:
        result = subprocess.run(
            [
                'yt-dlp',
                '--dump-json',
                '--flat-playlist',
                '--playlist-items', f'1:{MAX_YOUTUBE_VIDEOS}',
                YOUTUBE_CHANNEL
            ],
            capture_output=True, text=True, timeout=90
        )

        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                continue
            try:
                video = json.loads(line)
                title   = video.get('title', '')
                vid_id  = video.get('id', '')
                desc    = video.get('description', '')
                raw_date = video.get('upload_date', TODAY.replace('-', ''))

                if len(raw_date) == 8:
                    date_str = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
                else:
                    date_str = TODAY

                yt_url  = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else ''
                summary = summarize_youtube(title, desc)

                items.append({
                    'title':   title,
                    'summary': summary,
                    'date':    date_str,
                    'url':     yt_url
                })
                print(f"  ✓ {title[:60]}")

            except json.JSONDecodeError:
                continue

    except Exception as e:
        print(f"  [WARN] YouTube 수집 실패: {e}")

    return items


# ── 5. 키워드 추출 ─────────────────────────────────────────

def extract_keywords(news_items: list[dict], youtube_items: list[dict]) -> list[dict]:
    """전체 뉴스 키워드 빈도 Top N 산출"""
    pool = []

    # 각 기사의 keywords 필드
    for item in news_items:
        pool.extend(item.get('keywords', []))

    # 전체 제목 → Claude로 추가 키워드 추출
    all_titles = ' '.join([i['title'] for i in news_items + youtube_items])
    try:
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=300,
            messages=[{
                'role': 'user',
                'content': (
                    f"다음 AI 뉴스 제목들에서 핵심 기술·기업 키워드 15개를 추출해줘.\n"
                    f"2~6자 한국어 또는 영어 용어만. JSON 배열로만 응답:\n\n{all_titles[:3000]}"
                )
            }]
        )
        text = msg.content[0].text.strip()
        m = re.search(r'\[.*\]', text, re.DOTALL)
        if m:
            pool.extend(json.loads(m.group()))
    except Exception as e:
        print(f"  [WARN] extract_keywords: {e}")

    STOP = {'the','a','an','and','or','but','in','on','at','to','for','of',
            '이','그','저','것','수','등','및','을','를','이다'}
    counter = Counter(w for w in pool if w.lower() not in STOP and len(w) >= 2)

    return [
        {'word': word, 'count': count}
        for word, count in counter.most_common(TOP_KEYWORDS)
    ]


# ── 6. 이메일 발송 ─────────────────────────────────────────

def build_html_email(news_items: list[dict], youtube_items: list[dict], updated_at: str) -> str:
    """HTML 이메일 본문 생성"""

    by_cat = {
        'tech':   [n for n in news_items if n['category'] == 'tech'],
        'biz':    [n for n in news_items if n['category'] == 'biz'],
        'policy': [n for n in news_items if n['category'] == 'policy'],
    }

    SECTION_STYLES = {
        'tech':    ('#1a6ef0', 'Technology Trends'),
        'biz':     ('#009955', 'Business'),
        'policy':  ('#c07800', 'Policy & Regulation'),
    }

    def news_cards(items: list[dict]) -> str:
        if not items:
            return '<p style="color:#888;font-size:13px">해당 카테고리 뉴스가 없습니다.</p>'
        html = ''
        for item in items:
            link = (
                f'<a href="{item["url"]}" style="font-size:12px;color:#1a6ef0;'
                f'text-decoration:none;font-weight:500">원문 보기 →</a>'
                if item.get('url') else ''
            )
            html += f'''
            <div style="background:#f6f8fb;border-radius:8px;padding:16px;
                        margin-bottom:12px;border-left:3px solid #1a6ef0">
              <div style="font-size:11px;color:#888;margin-bottom:5px">
                {item["source"]} · {item["date"]}
              </div>
              <h3 style="font-size:15px;font-weight:600;margin:0 0 7px;color:#0d1526">
                {item["title"]}
              </h3>
              <p style="font-size:13px;color:#536480;line-height:1.65;margin:0 0 10px">
                {item["summary"]}
              </p>
              {link}
            </div>'''
        return html

    def yt_cards(items: list[dict]) -> str:
        if not items:
            return '<p style="color:#888;font-size:13px">최근 영상이 없습니다.</p>'
        html = ''
        for item in items:
            link = (
                f'<a href="{item["url"]}" style="font-size:12px;color:#d43030;'
                f'text-decoration:none;font-weight:500">영상 보기 →</a>'
                if item.get('url') else ''
            )
            html += f'''
            <div style="background:#fff5f5;border-radius:8px;padding:16px;
                        margin-bottom:12px;border-left:3px solid #d43030">
              <div style="font-size:11px;color:#888;margin-bottom:5px">
                NTD Korea · {item["date"]}
              </div>
              <h3 style="font-size:15px;font-weight:600;margin:0 0 7px;color:#0d1526">
                {item["title"]}
              </h3>
              <p style="font-size:13px;color:#536480;line-height:1.65;margin:0 0 10px">
                {item["summary"]}
              </p>
              {link}
            </div>'''
        return html

    sections_html = ''
    for cat, (color, label) in SECTION_STYLES.items():
        sections_html += f'''
        <div style="background:#fff;border-radius:12px;padding:22px;margin-bottom:14px">
          <h2 style="font-size:13px;font-weight:700;color:{color};text-transform:uppercase;
                     letter-spacing:.07em;margin:0 0 16px;padding-bottom:12px;
                     border-bottom:1px solid #e8ecf0">{label}</h2>
          {news_cards(by_cat[cat])}
        </div>'''

    sections_html += f'''
        <div style="background:#fff;border-radius:12px;padding:22px;margin-bottom:14px">
          <h2 style="font-size:13px;font-weight:700;color:#d43030;text-transform:uppercase;
                     letter-spacing:.07em;margin:0 0 16px;padding-bottom:12px;
                     border-bottom:1px solid #e8ecf0">Media</h2>
          {yt_cards(youtube_items)}
        </div>'''

    return f'''<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,'Segoe UI',sans-serif;
             background:#edf1f9;margin:0;padding:24px">
  <div style="max-width:680px;margin:0 auto">

    <div style="background:linear-gradient(135deg,#1a6ef0,#7c52cc);
                border-radius:12px;padding:26px 28px;margin-bottom:18px">
      <div style="font-size:11px;color:rgba(255,255,255,.65);
                  letter-spacing:.1em;text-transform:uppercase;margin-bottom:6px">
        EZER Corp · AI Intelligence
      </div>
      <h1 style="color:#fff;font-size:22px;font-weight:700;margin:0">
        AI 뉴스 브리핑
      </h1>
      <div style="color:rgba(255,255,255,.7);font-size:13px;margin-top:8px">
        {updated_at} · 총 {len(news_items)}건 수집
      </div>
    </div>

    {sections_html}

    <div style="text-align:center;padding:14px;font-size:11px;color:#8a9bb8">
      AI 뉴스 대시보드 · EZER Corp · 자동 생성 브리핑
    </div>
  </div>
</body></html>'''


def send_email(html_body: str, subject: str) -> None:
    """Gmail SMTP SSL로 이메일 발송"""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        print('\n[WARN] GMAIL_USER / GMAIL_APP_PASSWORD 미설정 — 이메일 건너뜀')
        return

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = GMAIL_USER
        msg['To']      = RECIPIENT_EMAIL
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.send_message(msg)

        print(f'[INFO] 이메일 발송 완료 → {RECIPIENT_EMAIL}')
    except Exception as e:
        print(f'[ERROR] 이메일 발송 실패: {e}')


# ── Main ──────────────────────────────────────────────────

def main():
    print(f'{"="*50}')
    print(f'AI 뉴스 수집 시작: {TODAY}')
    print(f'{"="*50}')

    # 1. sources.json 로드
    try:
        with open('sources.json', 'r', encoding='utf-8') as f:
            sources_data = json.load(f)
        sources = [s for s in sources_data.get('sources', []) if s.get('enabled', True)]
        print(f'\n[INFO] 활성 소스: {len(sources)}개')
    except Exception as e:
        print(f'[ERROR] sources.json 로드 실패: {e}')
        sources = []

    # 2. 뉴스 수집
    news_items = collect_news(sources)
    print(f'\n[INFO] 뉴스 수집 완료: {len(news_items)}건')

    # 3. YouTube 수집
    youtube_items = collect_youtube()
    print(f'[INFO] YouTube 수집 완료: {len(youtube_items)}건')

    # 4. 키워드 추출
    keywords = extract_keywords(news_items, youtube_items)
    print(f'[INFO] 키워드 추출 완료: {len(keywords)}개')

    # 5. data.json 저장
    updated_at = datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')
    data = {
        'updated_at': updated_at,
        'news':       news_items,
        'youtube':    youtube_items,
        'keywords':   keywords
    }

    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f'[INFO] data.json 저장 완료')

    # 6. 이메일 발송
    subject   = f'AI 뉴스 브리핑 {TODAY} — {len(news_items)}건'
    html_body = build_html_email(news_items, youtube_items, updated_at)
    send_email(html_body, subject)

    print(f'\n{"="*50}')
    print('완료!')
    print(f'{"="*50}')


if __name__ == '__main__':
    main()
