import argparse
import json
import sys
import time
import re
import concurrent.futures
import os
from datetime import datetime
import subprocess
import warnings

from bs4 import BeautifulSoup
from bs4 import XMLParsedAsHTMLWarning

try:
    from urllib3.exceptions import NotOpenSSLWarning
except Exception:  # pragma: no cover - environment-specific
    NotOpenSSLWarning = None  # type: ignore

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
if NotOpenSSLWarning is not None:
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)

import requests

# Headers for scraping to avoid basic bot detection
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def filter_items(items, keyword=None):
    if not keyword:
        return items
    keywords = [k.strip() for k in keyword.split(',') if k.strip()]
    pattern = '|'.join([r'\b' + re.escape(k) + r'\b' for k in keywords])
    regex = r'(?i)(' + pattern + r')'
    return [item for item in items if re.search(regex, item['title'])]

def fetch_url_content(url):
    """
    Fetches the content of a URL and extracts text from paragraphs.
    Truncates to 3000 characters.
    """
    if not url or not url.startswith('http'):
        return ""
    try:
        response = requests.get(url, headers=HEADERS, timeout=5)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
         # Remove script and style elements
        for script in soup(["script", "style", "nav", "footer", "header"]):
            script.extract()
        # Get text
        text = soup.get_text(separator=' ', strip=True)
        # Simple cleanup
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = ' '.join(chunk for chunk in chunks if chunk)
        return text[:3000]
    except Exception:
        return ""

def enrich_items_with_content(items, max_workers=10):
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_item = {executor.submit(fetch_url_content, item['url']): item for item in items}
        for future in concurrent.futures.as_completed(future_to_item):
            item = future_to_item[future]
            try:
                content = future.result()
                if content:
                    item['content'] = content
            except Exception:
                item['content'] = ""
    return items

# --- Source Fetchers ---

def fetch_hackernews(limit=5, keyword=None):
    if keyword:
        # Use Algolia API for keyword search (Much better recall for specific topics like "AI")
        try:
            # 24h window
            timestamp_24h = int(time.time() - 24 * 3600)
            
            # Query builder strategy
            raw_keywords = [k.strip() for k in keyword.split(',')]
            
            # 1. Try Complex Query with Quoted Phrases
            # "Github Copilot" needs quotes in Algolia search string if mixed with OR
            quoted_keywords = [f'"{k}"' if ' ' in k else k for k in raw_keywords]
            query_str = " OR ".join(quoted_keywords)
            
            api_url = f"http://hn.algolia.com/api/v1/search_by_date?tags=story&numericFilters=created_at_i>{timestamp_24h}&hitsPerPage={limit*2}&query={requests.utils.quote(query_str)}"
            
            data = requests.get(api_url, timeout=10).json()
            hits = data.get('hits', [])
            
            # 2. Level 2 Fallback: If 0 results, try just the first keyword (usually the most broad, e.g. "AI")
            if not hits and raw_keywords:
                simple_query = raw_keywords[0]
                api_url_simple = f"http://hn.algolia.com/api/v1/search_by_date?tags=story&numericFilters=created_at_i>{timestamp_24h}&hitsPerPage={limit*2}&query={requests.utils.quote(simple_query)}"
                data = requests.get(api_url_simple, timeout=10).json()
                hits = data.get('hits', [])

            items = []
            for hit in hits:
                hn_author = hit.get('author', '')
                items.append({
                    "source": "Hacker News",
                    "title": hit.get('title'),
                    "url": hit.get('url') or f"https://news.ycombinator.com/item?id={hit['objectID']}",
                    "hn_url": f"https://news.ycombinator.com/item?id={hit['objectID']}",
                    "heat": f"{hit.get('points', 0)} points",
                    "time": "Today", # Algolia return is recent by definition of filter
                    "developer_name": hn_author,
                    "developer_url": f"https://news.ycombinator.com/user?id={hn_author}" if hn_author else "",
                })
            
            # Only return if we actually found something. 
            # If we found nothing after all attempts, we might want to fall back to scraping frontpage 
            # but frontpage is unlikely to have keyword matches if deep search failed. 
            # However, returning [] is better than hallucinating.
            return items[:limit]
            
        except Exception as e:
            print(f"HN Algolia failed: {e}", file=sys.stderr)
            # Fallback to scraping logic below if API completely errors out (e.g. network/timeout)
            pass

    # Fallback / Default: Scrape Front Page
    base_url = "https://news.ycombinator.com"
    news_items = []
    page = 1
    max_pages = 5
    
    while len(news_items) < limit and page <= max_pages:
        url = f"{base_url}/news?p={page}"
        try:
            response = requests.get(url, headers=HEADERS, timeout=10)
            if response.status_code != 200: break
        except: break

        soup = BeautifulSoup(response.text, 'html.parser')
        rows = soup.select('.athing')
        if not rows: break
        
        page_items = []
        for row in rows:
            try:
                id_ = row.get('id')
                title_line = row.select_one('.titleline a')
                if not title_line: continue
                title = title_line.get_text()
                link = title_line.get('href')
                
                # Metadata
                score_span = soup.select_one(f'#score_{id_}')
                score = score_span.get_text() if score_span else "0 points"
                
                # Age/Time
                age_span = soup.select_one(f'.age a[href="item?id={id_}"]')
                time_str = age_span.get_text() if age_span else ""

                # Author（HN 用户名，在 subtext 行中的 .hnuser 链接）
                hn_author = ""
                user_link = soup.select_one(f'#score_{id_}')
                if user_link:
                    subtext = user_link.find_parent('td')
                    if subtext:
                        hn_user = subtext.select_one('.hnuser')
                        if hn_user:
                            hn_author = hn_user.get_text(strip=True)
                
                if link and link.startswith('item?id='): link = f"{base_url}/{link}"
                
                page_items.append({
                    "source": "Hacker News", 
                    "title": title, 
                    "url": link, 
                    "hn_url": f"{base_url}/item?id={id_}",
                    "heat": score,
                    "time": time_str,
                    "developer_name": hn_author,
                    "developer_url": f"{base_url}/user?id={hn_author}" if hn_author else "",
                })
            except: continue
        
        news_items.extend(filter_items(page_items, keyword))
        if len(news_items) >= limit: break
        page += 1
        time.sleep(0.5)

    return news_items[:limit]

def fetch_weibo(limit=5, keyword=None):
    # Use the PC Ajax API which returns JSON directly and is less rate-limited than scraping s.weibo.com
    url = "https://weibo.com/ajax/side/hotSearch"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://weibo.com/"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()
        items = data.get('data', {}).get('realtime', [])
        
        all_items = []
        for item in items:
            # key 'note' is usually the title, sometimes 'word'
            title = item.get('note', '') or item.get('word', '')
            if not title: continue
            
            # 'num' is the heat value
            heat = item.get('num', 0)
            
            # Construct URL (usually search query)
            # Web UI uses: https://s.weibo.com/weibo?q=%23TITLE%23&Refer=top
            full_url = f"https://s.weibo.com/weibo?q={requests.utils.quote(title)}&Refer=top"
            
            all_items.append({
                "source": "Weibo Hot Search", 
                "title": title, 
                "url": full_url, 
                "heat": f"{heat}",
                "time": "Real-time"
            })
            
        return filter_items(all_items, keyword)[:limit]
    except Exception: 
        return []

def _extract_github_embedded_payload(html_text):
    match = re.search(r'data-target="react-app\.embeddedData">(.*?)</script>', html_text, re.S)
    if not match:
        return {}
    try:
        return json.loads(match.group(1)).get('payload', {})
    except Exception:
        return {}


def _search_github_repositories(query, per_page=10, max_pages=1):
    results = []
    for page in range(1, max_pages + 1):
        try:
            response = requests.get(
                "https://github.com/search",
                headers=HEADERS,
                params={"q": query, "type": "repositories", "p": page},
                timeout=15,
            )
            response.raise_for_status()
        except Exception:
            break

        payload = _extract_github_embedded_payload(response.text)
        page_results = payload.get('results', [])
        if not page_results:
            break

        for item in page_results:
            repo_meta = item.get('repo', {}).get('repository', {})
            full_name = item.get('hl_name') or ''
            if '/' not in full_name:
                continue
            desc_html = item.get('hl_trunc_description') or ''
            desc_text = BeautifulSoup(desc_html, 'html.parser').get_text(' ', strip=True)
            results.append({
                "full_name": full_name,
                "url": f"https://github.com/{full_name}",
                "description": desc_text,
                "stars": item.get('followers') or 0,
                "updated_at": repo_meta.get('updated_at', ''),
                "language": item.get('language') or '',
            })

    return results


CHINESE_README_PATH_PATTERNS = [
    re.compile(r'(^|/)(readme)([._-](zh(?:[-_](?:cn|hans|tw|hk))?|cn|chs|cht|中文))(\.[^/]+)?$', re.I),
    re.compile(r'(^|/)(zh(?:[-_](?:cn|hans|tw|hk))?|cn|中文)/readme(\.[^/]+)?$', re.I),
    re.compile(r'(^|/)readme[^/]*中文(\.[^/]+)?$', re.I),
]
CHINESE_README_LINK_TEXT_HINTS = ["中文", "简体中文", "繁體中文", "中文版", "简中", "繁中"]
CHINESE_README_HREF_HINTS = ["readme.zh", "readme_cn", "readme-cn", "readme_zh", "/zh-cn/", "/zh/", "lang=zh", "locale=zh"]


def _detect_chinese_readme_marker(repo_url):
    try:
        response = requests.get(repo_url, headers=HEADERS, timeout=15)
        response.raise_for_status()
    except Exception:
        return None

    payload = _extract_github_embedded_payload(response.text)
    code_view = payload.get('codeViewRepoRoute', {})
    root_items = code_view.get('tree', {}).get('items', [])

    for item in root_items:
        path = item.get('path', '')
        if any(pattern.search(path) for pattern in CHINESE_README_PATH_PATTERNS):
            return path

    overview_files = code_view.get('overview', {}).get('overviewFiles', [])
    if not overview_files:
        return None

    rich_text = overview_files[0].get('richText', '') or ''
    if not rich_text:
        return None

    soup = BeautifulSoup(rich_text, 'html.parser')
    for link in soup.select('a[href]'):
        text = link.get_text(' ', strip=True)
        href = link.get('href') or ''
        href_lower = href.lower()
        if any(hint in text for hint in CHINESE_README_LINK_TEXT_HINTS):
            return text or href
        if any(hint in href_lower for hint in CHINESE_README_HREF_HINTS):
            return text or href
    return None


def _format_github_repo_item(repo, source_name, marker=None):
    title = repo['full_name']
    desc = repo.get('description') or ''
    if desc:
        title = f"{title} - {desc}"

    stars = repo.get('stars') or 0
    try:
        heat = f"{int(stars):,} stars"
    except Exception:
        heat = f"{stars} stars" if stars else "热度未提供"

    updated_at = repo.get('updated_at') or ''
    time_text = updated_at.split('T', 1)[0] if 'T' in updated_at else (updated_at or 'Updated recently')

    summary = desc
    if marker:
        summary = f"包含中文版 README：{marker}" + (f"。{desc}" if desc else "")

    # 提取开发者/组织信息（从 full_name 中获取）
    full_name = repo.get('full_name', '')
    dev_name = full_name.split('/')[0] if '/' in full_name else ''
    dev_url = f"https://github.com/{dev_name}" if dev_name else ''

    item = {
        "source": source_name,
        "title": title,
        "url": repo['url'],
        "heat": heat,
        "time": time_text,
        "developer_name": dev_name,
        "developer_url": dev_url,
    }
    if summary:
        item['summary'] = summary
    return item


def fetch_github(limit=5, keyword=None):
    if keyword:
        try:
            repos = _search_github_repositories(f"{keyword.split(',')[0]} sort:updated-desc", per_page=max(limit, 10), max_pages=1)
            items = [_format_github_repo_item(repo, "GitHub Search") for repo in repos]
            if items:
                return filter_items(items, keyword)[:limit]
        except Exception:
            pass

    try:
        response = requests.get("https://github.com/trending", headers=HEADERS, timeout=10)
    except Exception:
        return []

    soup = BeautifulSoup(response.text, 'html.parser')
    items = []
    for article in soup.select('article.Box-row'):
        try:
            h2 = article.select_one('h2 a')
            if not h2:
                continue
            title = h2.get_text(strip=True).replace('\n', '').replace(' ', '')
            link = "https://github.com" + h2['href']

            desc = article.select_one('p')
            desc_text = desc.get_text(strip=True) if desc else ""
            stars_tag = article.select_one('a[href$="/stargazers"]')
            stars = stars_tag.get_text(strip=True) if stars_tag else ""

            # 提取开发者/组织信息（从 repo 路径中获取）
            parts = h2['href'].strip('/').split('/')
            developer_name = parts[0] if len(parts) >= 2 else ""
            developer_url = f"https://github.com/{developer_name}" if developer_name else ""

            # 提取今日 star 增量
            today_stars_el = article.select_one('.float-sm-right') or article.select_one('span.d-inline-block.float-sm-right')
            today_stars = today_stars_el.get_text(strip=True) if today_stars_el else ""

            # 提取编程语言
            lang_el = article.select_one('[itemprop="programmingLanguage"]')
            language = lang_el.get_text(strip=True) if lang_el else ""

            item = {
                "source": "GitHub Trending",
                "title": f"{title} - {desc_text}" if desc_text else title,
                "url": link,
                "heat": f"{stars} stars" if stars else "热度未提供",
                "time": "Today",
                "developer_name": developer_name,
                "developer_url": developer_url,
                "developer_email": "",
                "today_stars": today_stars,
                "language": language,
            }
            if desc_text:
                item['summary'] = desc_text
            items.append(item)
        except Exception:
            continue

    # 尝试获取开发者的公开邮箱和联系方式
    _enrich_github_developer_info(items)

    return filter_items(items, keyword)[:limit]


def _enrich_github_developer_info(items):
    """
    通过 GitHub 用户主页 HTML 抓取开发者的公开邮箱和联系方式。
    GitHub API 需要 Token，所以我们直接爬取用户主页提取信息。
    同时也尝试无 Token 的 GitHub API（有速率限制但基本够用）。
    """
    seen_users = set()
    for item in items:
        dev_name = item.get("developer_name", "")
        if not dev_name or dev_name in seen_users:
            continue
        seen_users.add(dev_name)

        # 方法1: 尝试 GitHub REST API (无 Token，限 60 次/小时)
        email = _fetch_github_user_email_api(dev_name)
        if email:
            item["developer_email"] = email
            continue

        # 方法2: 爬取用户主页 HTML
        email = _fetch_github_user_email_html(dev_name)
        if email:
            item["developer_email"] = email


def _fetch_github_user_email_api(username):
    """通过 GitHub REST API 获取用户公开邮箱（无需 Token，有速率限制）"""
    try:
        # 首先获取用户公开信息
        api_url = f"https://api.github.com/users/{username}"
        gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
        headers = dict(HEADERS)
        headers["Accept"] = "application/vnd.github.v3+json"
        if gh_token:
            headers["Authorization"] = f"token {gh_token}"

        resp = requests.get(api_url, headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            email = data.get("email")
            if email:
                return email

            # 尝试从用户的 public events 中提取邮箱（commits 中有）
            blog = data.get("blog", "")
            if blog and "@" in blog:
                return blog  # 有些人把邮箱放在 blog 字段

        # 方法2: 从最近的 commit events 中提取
        events_url = f"https://api.github.com/users/{username}/events/public"
        resp = requests.get(events_url, headers=headers, timeout=5)
        if resp.status_code == 200:
            events = resp.json()
            for event in events[:10]:
                if event.get("type") == "PushEvent":
                    commits = event.get("payload", {}).get("commits", [])
                    for commit in commits:
                        author = commit.get("author", {})
                        email = author.get("email", "")
                        if email and "@" in email and "noreply" not in email.lower():
                            return email
    except Exception:
        pass
    return None


def _fetch_github_user_email_html(username):
    """从 GitHub 用户主页 HTML 中提取邮箱"""
    try:
        profile_url = f"https://github.com/{username}"
        resp = requests.get(profile_url, headers=HEADERS, timeout=5)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, 'html.parser')

        # 查找邮箱链接 (mailto:)
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            if href.startswith('mailto:'):
                email = href.replace('mailto:', '').strip()
                if '@' in email and 'noreply' not in email.lower():
                    return email

        # 查找 itemprop="email" 元素
        email_el = soup.find(attrs={"itemprop": "email"})
        if email_el:
            email_link = email_el.find('a')
            if email_link:
                email = email_link.get_text(strip=True)
                if '@' in email and 'noreply' not in email.lower():
                    return email

        # 查找 bio 或其他文本中的邮箱
        import re as _re
        bio_el = soup.select_one('.user-profile-bio')
        if bio_el:
            bio_text = bio_el.get_text()
            email_match = _re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', bio_text)
            if email_match:
                email = email_match.group()
                if 'noreply' not in email.lower():
                    return email

    except Exception:
        pass
    return None


def fetch_github_chinese_readme(limit=8, keyword=None):
    seen = set()
    candidate_repos = []
    search_queries = [
        "stars:>30000 fork:false archived:false mirror:false sort:stars-desc",
        "中文 stars:>1000 fork:false archived:false mirror:false sort:stars-desc",
        "README.zh-CN stars:>500 fork:false archived:false mirror:false sort:stars-desc",
    ]

    for query in search_queries:
        for repo in _search_github_repositories(
            query,
            per_page=max(limit * 2, 10),
            max_pages=2,
        ):
            full_name = repo['full_name']
            if full_name in seen:
                continue
            seen.add(full_name)
            candidate_repos.append(repo)
            if len(candidate_repos) >= max(limit * 6, 36):
                break
        if len(candidate_repos) >= max(limit * 6, 36):
            break

    candidate_repos.sort(key=lambda item: item.get('stars') or 0, reverse=True)

    matches = []
    for repo in candidate_repos:
        marker = _detect_chinese_readme_marker(repo['url'])
        if not marker:
            continue
        matches.append(_format_github_repo_item(repo, "GitHub 中文 README 热门", marker=marker))
        if len(matches) >= limit:
            break

    return filter_items(matches, keyword)[:limit]

def fetch_36kr(limit=5, keyword=None):
    try:
        response = requests.get("https://36kr.com/newsflashes", headers=HEADERS, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        items = []
        for item in soup.select('.newsflash-item'):
            title = item.select_one('.item-title').get_text(strip=True)
            href = item.select_one('.item-title')['href']
            time_tag = item.select_one('.time')
            time_str = time_tag.get_text(strip=True) if time_tag else ""
            
            items.append({
                "source": "36Kr", 
                "title": title, 
                "url": f"https://36kr.com{href}" if not href.startswith('http') else href,
                "time": time_str,
                "heat": ""
            })
        return filter_items(items, keyword)[:limit]
    except: return []

def fetch_v2ex(limit=5, keyword=None):
    try:
        # Hot topics json
        data = requests.get("https://www.v2ex.com/api/topics/hot.json", headers=HEADERS, timeout=10).json()
        items = []
        for t in data:
            # V2EX API fields: created, replies (heat), member (user info)
            replies = t.get('replies', 0)
            member = t.get('member', {})
            member_name = member.get('username', '') if member else ''
            member_url = f"https://www.v2ex.com/member/{member_name}" if member_name else ''
            items.append({
                "source": "V2EX", 
                "title": t['title'], 
                "url": t['url'],
                "heat": f"{replies} replies",
                "time": "Hot",
                "summary": t.get('content', '') or t.get('content_rendered', '') or '',
                "developer_name": member_name,
                "developer_url": member_url,
            })
        return filter_items(items, keyword)[:limit]
    except: return []

def fetch_tencent(limit=5, keyword=None):
    try:
        url = "https://i.news.qq.com/web_backend/v2/getTagInfo?tagId=aEWqxLtdgmQ%3D"
        data = requests.get(url, headers={"Referer": "https://news.qq.com/"}, timeout=10).json()
        items = []
        for news in data['data']['tabs'][0]['articleList']:
            items.append({
                "source": "Tencent News", 
                "title": news['title'], 
                "url": news.get('url') or news.get('link_info', {}).get('url'),
                "time": news.get('pub_time', '') or news.get('publish_time', '')
            })
        return filter_items(items, keyword)[:limit]
    except: return []

def fetch_wallstreetcn(limit=5, keyword=None):
    try:
        url = "https://api-one.wallstcn.com/apiv1/content/information-flow?channel=global-channel&accept=article&limit=30"
        data = requests.get(url, timeout=10).json()
        items = []
        for item in data['data']['items']:
            res = item.get('resource')
            if res and (res.get('title') or res.get('content_short')):
                 ts = res.get('display_time', 0)
                 time_str = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M') if ts else ""
                 items.append({
                     "source": "Wall Street CN", 
                     "title": res.get('title') or res.get('content_short'), 
                     "url": res.get('uri'),
                     "time": time_str
                 })
        return filter_items(items, keyword)[:limit]
    except: return []

def fetch_producthunt(limit=5, keyword=None):
    try:
        # Using Atom RSS feed for speed and reliability without API key
        response = requests.get("https://www.producthunt.com/feed", headers=HEADERS, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        items = []
        for entry in soup.find_all(['item', 'entry']):
            title = entry.find('title').get_text(strip=True)
            link_tag = entry.find('link')
            url = link_tag.get('href') or link_tag.get_text(strip=True) if link_tag else ""
            
            pubBox = entry.find('pubDate') or entry.find('published')
            pub = pubBox.get_text(strip=True) if pubBox else ""

            # 提取产品描述：Atom feed 用 <content> 标签（包含 HTML）
            description = ""
            content_tag = entry.find('content') or entry.find('description')
            if content_tag:
                content_html = content_tag.get_text(strip=True)
                content_soup = BeautifulSoup(content_html, 'html.parser')
                # 第一个 <p> 通常是产品一句话介绍
                first_p = content_soup.find('p')
                if first_p:
                    description = first_p.get_text(strip=True)
                    # 过滤掉 Discussion | Link 之类的导航文本
                    if "Discussion" in description or "Link" in description:
                        description = ""
                if not description:
                    # fallback: 获取所有文本，去掉导航链接
                    all_text = content_soup.get_text(separator=' ', strip=True)
                    # 去掉 Discussion | Link 部分
                    all_text = re.sub(r'Discussion\s*\|\s*Link', '', all_text).strip()
                    if all_text and len(all_text) > 5:
                        description = all_text[:300]

            # 提取 author / creator
            author_tag = entry.find('dc:creator') or entry.find('author')
            author_name = ""
            if author_tag:
                name_tag = author_tag.find('name')
                author_name = name_tag.get_text(strip=True) if name_tag else author_tag.get_text(strip=True)

            items.append({
                "source": "Product Hunt", 
                "title": title, 
                "url": url,
                "time": pub,
                "heat": "Top Product",
                "summary": description,
                "developer_name": author_name,
                "developer_url": "",
            })
        return filter_items(items, keyword)[:limit]
    except: return []

# --- New Fetchers (RSS/API) ---

from rss_parser import fetch_rss_feed

# fetch_tldr_ai removed: all known feed URLs (feed.tldr.tech/ai, tldr.tech/ai/rss) return 404.

def fetch_huggingface_papers(limit=5, keyword=None):
    """Fetch daily papers from HuggingFace using the official JSON API.
    
    API endpoint: https://huggingface.co/api/daily_papers
    Returns papers sorted by upvotes (descending).
    """
    items = []
    try:
        api_url = "https://huggingface.co/api/daily_papers"
        resp = requests.get(api_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        papers = resp.json()

        # Sort by upvotes descending to get the most popular papers first
        papers.sort(key=lambda p: p.get("paper", {}).get("upvotes", 0), reverse=True)

        for entry in papers[:limit * 2]:  # fetch extra in case some are filtered
            paper = entry.get("paper", {})
            title = paper.get("title", "").strip()
            if not title:
                continue
            paper_id = paper.get("id", "")
            url = f"https://huggingface.co/papers/{paper_id}" if paper_id else ""
            github = paper.get("githubRepo", "") or ""
            upvotes = paper.get("upvotes", 0)
            summary = paper.get("summary", "") or paper.get("ai_summary", "") or ""
            # Truncate summary to first 500 chars for storage
            if len(summary) > 500:
                summary = summary[:500] + "…"
            
            # Extract author names
            authors_list = paper.get("authors", [])
            author_names = ", ".join(a.get("name", "") for a in authors_list[:5] if a.get("name"))
            if len(authors_list) > 5:
                author_names += f" et al. ({len(authors_list)} authors)"

            # AI keywords from HF
            ai_keywords = paper.get("ai_keywords", [])
            keyword_str = ", ".join(ai_keywords[:5]) if ai_keywords else ""

            items.append({
                "source": "HF Papers",
                "title": title,
                "url": url,
                "github": github,
                "heat": f"👍 {upvotes}" if upvotes else "",
                "time": datetime.now().strftime("%Y-%m-%d"),
                "summary": summary,
                "description": keyword_str,
                "developer_name": author_names,
                "developer_url": github,
            })

            if len(items) >= limit:
                break

    except Exception as e:
        print(f"HF API Exception: {e}", file=sys.stderr)

    return filter_items(items[:limit], keyword)


def fetch_latentspace_ainews(limit=5, keyword=None):
    """Fetch AINews daily roundups from Latent Space Substack RSS.
    Filters for posts with [AINews] title prefix, separating them from podcast episodes."""
    items = []
    try:
        response = requests.get("https://www.latent.space/feed", headers=HEADERS, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        for entry in soup.find_all('item'):
            title_tag = entry.find('title')
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)
            
            # Filter: only AINews posts (title starts with [AINews])
            if not title.startswith('[AINews]'):
                continue
            
            # Extract link from guid (Substack RSS has link text empty, guid has the URL)
            guid_tag = entry.find('guid')
            link = guid_tag.get_text(strip=True) if guid_tag else ""
            
            # Fallback: try link tag
            if not link:
                link_tag = entry.find('link')
                if link_tag:
                    link = link_tag.get_text(strip=True) or (link_tag.get('href') or '')
            
            # Publication date
            pub_tag = entry.find('pubdate') or entry.find('published')
            pub_date = pub_tag.get_text(strip=True) if pub_tag else ""
            # Simplify date if possible
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(pub_date)
                pub_date = dt.strftime('%Y-%m-%d')
            except Exception:
                pass
            
            # Content snippet from description
            desc_tag = entry.find('description')
            content = ""
            if desc_tag:
                desc_html = desc_tag.get_text(strip=True)
                desc_soup = BeautifulSoup(desc_html, 'html.parser')
                content = desc_soup.get_text(separator=' ', strip=True)[:2000]
            
            items.append({
                "source": "Latent Space AINews",
                "title": title,
                "url": link,
                "time": pub_date,
                "heat": "Daily Roundup",
                "content": content,
                "developer_name": "Latent Space",
                "developer_url": "https://www.latent.space/",
            })
    except Exception as e:
        print(f"Latent Space AINews fetch error: {e}", file=sys.stderr)
    
    return filter_items(items[:limit], keyword)


# --- Source Definitions (Global for Access) ---

AI_NEWSLETTER_SOURCES = [
    # Bens Bites is protected by Cloudflare -> Use Playwright
    ("Ben's Bites", "https://www.bensbites.com/feed"), 
    ("Interconnects", "https://www.interconnects.ai/feed"),  # Fixed: needs www.
    ("One Useful Thing", "https://www.oneusefulthing.org/feed"), 
    # Removed: The Rundown (beehiiv feed 404), The Neuron (403 Forbidden)
    ("ChinAI", "https://chinai.substack.com/feed"),
    ("Memia", "https://memia.substack.com/feed"),
    ("AI to ROI", "https://ai2roi.substack.com/feed"),
    ("KDnuggets", "https://www.kdnuggets.com/feed"),
]

# ... (rest of sources)

def fetch_rss_with_playwright(url, source_name, limit=5):
    """Fallback fetcher using Playwright to bypass Cloudflare"""
    try:
        # Special handling for Ben's Bites which uses custom Homepage Scraper
        if "Ben's Bites" in source_name:
             script_path = os.path.join(os.path.dirname(__file__), "fetch_bensbites.py")
             # No arguments needed, script hardcodes URL
             cmd = [sys.executable, script_path]
             
             
             result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
             
             if result.returncode == 0:
                 try:
                    data = json.loads(result.stdout)
                    if not data: raise ValueError("Empty JSON")
                    return data
                 except Exception:
                    # Fallback for Ben's Bites if parsing fails
                    return [{
                        "source": "Ben's Bites",
                        "title": "Ben's Bites (Visit Site)",
                        "url": "https://bensbites.beehiiv.com/",
                        "time": "Today",
                        "summary": "Auto-fetch failed. Please verify on site.",
                    }]
             else:
                 return [{
                        "source": "Ben's Bites",
                        "title": "Ben's Bites (Check Site)",
                        "url": "https://bensbites.beehiiv.com/",
                        "time": "Today",
                        "summary": "Fetch process failed.",
                    }]

        # User generic Playwright script for all OTHER protected feeds
        
        if result.returncode == 0:
            from rss_parser import parse_rss_content
            # Result stdout should be the HTML/XML content
            return parse_rss_content(result.stdout, source_name, limit)
        else:
            print(f"Playwright fetch failed for {source_name}: {result.stderr}", file=sys.stderr)
            return []
    except Exception as e:
        print(f"Playwright exception for {source_name}: {e}", file=sys.stderr)
        return []


PODCAST_SOURCES = [
    ("Lex Fridman", "https://lexfridman.com/feed/podcast"),
    # Removed: Cognitive Rev (megaphone.fm feed 404)
    ("80000 Hours", "https://feeds.transistor.fm/80-000-hours-podcast"),
    ("Latent Space", "https://latent.space/feed"),
]

ESSAY_SOURCES = [
    ("Wait But Why", "https://waitbutwhy.com/feed"),
    ("James Clear", "https://jamesclear.com/feed"),
    ("Farnam Street", "https://fs.blog/feed"),
    ("Paul Graham", "http://www.aaronsw.com/2002/feeds/pgessays.rss"), 
    ("Scott Young", "https://www.scotthyoung.com/blog/feed/"),
    ("Dan Koe", "https://thedankoe.com/feed/"),
]

def fetch_ai_newsletters(limit=5, keyword=None):
    """Aggregate Fetcher for AI Newsletters"""
    all_items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_rss_feed, url, name, 3): name for name, url in AI_NEWSLETTER_SOURCES}
        for future in concurrent.futures.as_completed(futures):
            all_items.extend(future.result())
    return filter_items(all_items, keyword)[:limit]

def fetch_podcasts(limit=5, keyword=None):
    all_items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_rss_feed, url, name, 3): name for name, url in PODCAST_SOURCES}
        for future in concurrent.futures.as_completed(futures):
            all_items.extend(future.result())
    return filter_items(all_items, keyword)[:limit]

def fetch_essays(limit=5, keyword=None):
    all_items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_rss_feed, url, name, 3): name for name, url in ESSAY_SOURCES}
        for future in concurrent.futures.as_completed(futures):
            all_items.extend(future.result())
    return filter_items(all_items, keyword)[:limit]


# ════════════════════════════════════════════════════════════
# 全网搜索采集（Web Search）
# 5 路并行搜索渠道：行业新闻、竞品监控、技术社区、大会动态、腾讯云动态
# 使用 DuckDuckGo HTML 搜索（无需 API Key），按关键词搜索近 7 天内容
# ════════════════════════════════════════════════════════════

WEB_SEARCH_CHANNELS = [
    {
        "name": "行业新闻",
        "label": "📰 行业新闻",
        "queries": [
            "cloud computing news this week",
            "AI infrastructure news 2026",
            "serverless edge computing trend",
            "cloud native latest developments",
        ],
        "relevance_keywords": [
            "cloud", "serverless", "edge", "kubernetes", "AI", "GPU",
            "infrastructure", "deployment", "container", "microservice",
        ],
    },
    {
        "name": "竞品监控",
        "label": "🔍 竞品监控",
        "queries": [
            "AWS new service launch 2026",
            "Azure AI update 2026",
            "Google Cloud new feature",
            "Cloudflare workers pages update",
            "Vercel Netlify serverless news",
        ],
        "relevance_keywords": [
            "AWS", "Azure", "GCP", "Google Cloud", "Cloudflare", "Vercel",
            "Netlify", "DigitalOcean", "Lambda", "EC2", "Bedrock",
        ],
    },
    {
        "name": "技术社区",
        "label": "💻 技术社区",
        "queries": [
            "site:dev.to cloud AI tutorial",
            "site:medium.com serverless GPU training",
            "site:hashnode.com cloud native deployment",
        ],
        "relevance_keywords": [
            "tutorial", "guide", "deploy", "serverless", "GPU", "AI",
            "cloud", "docker", "kubernetes", "practice",
        ],
    },
    {
        "name": "大会动态",
        "label": "🎤 大会动态",
        "queries": [
            "tech conference AI cloud 2026",
            "KubeCon CloudNativeCon 2026",
            "AWS re:Invent Google Next Azure Build 2026",
        ],
        "relevance_keywords": [
            "conference", "summit", "keynote", "announcement",
            "launch", "preview", "GA", "release",
        ],
    },
    {
        "name": "腾讯云动态",
        "label": "☁️ 腾讯云动态",
        "queries": [
            "Tencent Cloud new product update",
            "Tencent Cloud international",
            "腾讯云 新功能 发布",
            "EdgeOne Lighthouse Hunyuan update",
        ],
        "relevance_keywords": [
            "Tencent", "腾讯", "EdgeOne", "Lighthouse", "Hunyuan",
            "TRTC", "COS", "TDSQL", "CodeBuddy", "SCF",
        ],
    },
]


def _duckduckgo_search(query, max_results=5):
    """使用 DuckDuckGo HTML 搜索获取结果，不依赖 API Key。
    
    返回 [{title, url, snippet}] 列表。
    """
    items = []
    try:
        # DuckDuckGo HTML 搜索
        params = {"q": query, "t": "h_", "ia": "web"}
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params=params,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            },
            timeout=15,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        for result in soup.select(".result"):
            title_el = result.select_one(".result__a")
            snippet_el = result.select_one(".result__snippet")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")
            # DuckDuckGo 的链接可能是重定向链接，提取实际 URL
            if "uddg=" in href:
                import urllib.parse
                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                href = parsed.get("uddg", [href])[0]
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""
            if title and href and href.startswith("http"):
                items.append({
                    "title": title,
                    "url": href,
                    "snippet": snippet,
                })
            if len(items) >= max_results:
                break
    except Exception as e:
        import sys
        print(f"[web_search] DuckDuckGo 搜索失败 ({query[:30]}...): {e}", file=sys.stderr)
    return items


def _is_relevant(item, relevance_keywords):
    """检查搜索结果是否与相关关键词匹配（至少命中 1 个）"""
    text = f"{item.get('title', '')} {item.get('snippet', '')}".lower()
    for kw in relevance_keywords:
        if kw.lower() in text:
            return True
    return False


def _search_channel(channel, per_query_limit=3):
    """搜索单个渠道的所有查询词"""
    channel_items = []
    seen_urls = set()
    for query in channel["queries"]:
        results = _duckduckgo_search(query, max_results=per_query_limit)
        for item in results:
            url = item["url"]
            if url in seen_urls:
                continue
            # 相关性过滤
            if not _is_relevant(item, channel["relevance_keywords"]):
                continue
            seen_urls.add(url)
            channel_items.append({
                "title": item["title"],
                "url": item["url"],
                "summary": item["snippet"],
                "heat": f"web_search:{channel['name']}",
                "time": datetime.now().strftime("%Y-%m-%d"),
                "source": f"web_search_{channel['name']}",
                "search_channel": channel["name"],
            })
        # 避免搜索过快被限流
        time.sleep(0.5)
    return channel_items


def fetch_web_search(limit=10, keyword=None):
    """全网搜索采集：5 路并行搜索，聚合结果后按相关性排序返回。
    
    每个渠道独立搜索 + 过滤，最终合并去重。
    """
    all_items = []
    seen_urls = set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(_search_channel, ch, 3): ch["name"]
            for ch in WEB_SEARCH_CHANNELS
        }
        for future in concurrent.futures.as_completed(futures):
            channel_name = futures[future]
            try:
                items = future.result()
                for item in items:
                    url = item["url"]
                    if url not in seen_urls:
                        seen_urls.add(url)
                        all_items.append(item)
                import sys
                print(f"[web_search] {channel_name}: {len(items)} items", file=sys.stderr)
            except Exception as e:
                import sys
                print(f"[web_search] {channel_name} 失败: {e}", file=sys.stderr)

    # 应用关键词过滤
    if keyword:
        all_items = filter_items(all_items, keyword)

    return all_items[:limit]


# ════════════════════════════════════════════════════════════
# 增强版 GitHub 开发者画像（KOC 建联基础数据）
# 从 GitHub API + 主页 HTML 抓取开发者完整画像
# ════════════════════════════════════════════════════════════

def _fetch_github_developer_profile(username: str) -> dict:
    """抓取单个 GitHub 用户的完整画像信息，用于 KOC 建联评估。

    返回 {name, bio, company, location, blog, twitter, email, followers,
          following, public_repos, avatar_url, social_links, profile_url}
    """
    profile = {
        "github_username": username,
        "profile_url": f"https://github.com/{username}",
        "name": "",
        "bio": "",
        "company": "",
        "location": "",
        "blog": "",
        "twitter_username": "",
        "email": "",
        "followers": 0,
        "following": 0,
        "public_repos": 0,
        "avatar_url": "",
        "social_links": [],  # [{platform, url}]
        "is_org": False,
    }
    if not username:
        return profile

    gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
    headers = dict(HEADERS)
    headers["Accept"] = "application/vnd.github.v3+json"
    if gh_token:
        headers["Authorization"] = f"token {gh_token}"

    # 1. GitHub REST API
    try:
        resp = requests.get(
            f"https://api.github.com/users/{username}",
            headers=headers, timeout=8,
        )
        if resp.status_code == 200:
            data = resp.json()
            profile["name"] = data.get("name") or ""
            profile["bio"] = data.get("bio") or ""
            profile["company"] = data.get("company") or ""
            profile["location"] = data.get("location") or ""
            profile["blog"] = data.get("blog") or ""
            profile["twitter_username"] = data.get("twitter_username") or ""
            profile["email"] = data.get("email") or ""
            profile["followers"] = data.get("followers") or 0
            profile["following"] = data.get("following") or 0
            profile["public_repos"] = data.get("public_repos") or 0
            profile["avatar_url"] = data.get("avatar_url") or ""
            profile["is_org"] = data.get("type", "").lower() == "organization"

            # 从 social_accounts API 获取社交链接（GitHub 2022+ 新功能）
            try:
                social_resp = requests.get(
                    f"https://api.github.com/users/{username}/social_accounts",
                    headers=headers, timeout=5,
                )
                if social_resp.status_code == 200:
                    for acc in social_resp.json():
                        provider = acc.get("provider", "").lower()
                        url = acc.get("url", "")
                        if url:
                            profile["social_links"].append({"platform": provider, "url": url})
                            # 补充 Twitter
                            if provider == "twitter" and not profile["twitter_username"]:
                                # 从 URL 提取用户名
                                tw_parts = url.rstrip("/").split("/")
                                if tw_parts:
                                    profile["twitter_username"] = tw_parts[-1].lstrip("@")
            except Exception:
                pass
    except Exception:
        pass

    # 2. HTML 补充（如果 API 缺信息）
    if not profile["email"] or not profile["bio"]:
        try:
            resp = requests.get(
                f"https://github.com/{username}",
                headers=HEADERS, timeout=8,
            )
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                # bio
                if not profile["bio"]:
                    bio_el = soup.select_one(".user-profile-bio, [data-bio-text]")
                    if bio_el:
                        profile["bio"] = bio_el.get_text(strip=True)[:300]
                # email
                if not profile["email"]:
                    for a_tag in soup.find_all("a", href=True):
                        href = a_tag["href"]
                        if href.startswith("mailto:"):
                            email = href.replace("mailto:", "").strip()
                            if "@" in email and "noreply" not in email.lower():
                                profile["email"] = email
                                break
                # social links from HTML
                for a_tag in soup.select('a[rel="nofollow me"]'):
                    url = a_tag.get("href", "")
                    if url and url.startswith("http"):
                        platform = "unknown"
                        if "twitter.com" in url or "x.com" in url:
                            platform = "twitter"
                        elif "linkedin.com" in url:
                            platform = "linkedin"
                        elif "youtube.com" in url:
                            platform = "youtube"
                        elif "mastodon" in url:
                            platform = "mastodon"
                        elif "reddit.com" in url:
                            platform = "reddit"
                        elif "discord" in url:
                            platform = "discord"
                        # 避免重复
                        existing_urls = {s["url"] for s in profile["social_links"]}
                        if url not in existing_urls:
                            profile["social_links"].append({"platform": platform, "url": url})
        except Exception:
            pass

    # 3. 如果还没有 email，尝试 events API
    if not profile["email"]:
        try:
            resp = requests.get(
                f"https://api.github.com/users/{username}/events/public",
                headers=headers, timeout=5,
            )
            if resp.status_code == 200:
                for event in resp.json()[:10]:
                    if event.get("type") == "PushEvent":
                        for commit in event.get("payload", {}).get("commits", []):
                            email = commit.get("author", {}).get("email", "")
                            if email and "@" in email and "noreply" not in email.lower():
                                profile["email"] = email
                                break
                    if profile["email"]:
                        break
        except Exception:
            pass

    return profile


def enrich_github_developer_profiles(items: list) -> list:
    """批量增强 GitHub 开发者画像，返回 KOC 候选列表。

    为每个有 developer_name 的条目抓取完整画像，
    只返回有一定影响力的开发者（followers >= 10 或有明确联系方式）。
    """
    seen_users = set()
    koc_candidates = []

    for item in items:
        dev_name = item.get("developer_name", "")
        if not dev_name or dev_name in seen_users:
            continue
        seen_users.add(dev_name)

        profile = _fetch_github_developer_profile(dev_name)
        item["developer_profile"] = profile

        # 补充已有字段
        if profile["email"] and not item.get("developer_email"):
            item["developer_email"] = profile["email"]

        # KOC 候选判断：有一定影响力或有联系方式
        has_contact = bool(profile["email"] or profile["twitter_username"] or profile["social_links"])
        has_influence = profile["followers"] >= 10 or profile["public_repos"] >= 5
        if has_contact or has_influence:
            koc_candidates.append({
                "source": "github",
                "username": dev_name,
                "profile": profile,
                "associated_item": {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "heat": item.get("heat", ""),
                },
            })

        time.sleep(0.3)  # 避免 API 限流

    return koc_candidates


# ════════════════════════════════════════════════════════════
# 社交平台帖子搜索：找到热点相关的 Twitter/Reddit 讨论帖
# 目标：提供可直接互动的原帖链接，而非转发 GitHub
# ════════════════════════════════════════════════════════════

def search_social_posts_for_item(item: dict, source_key: str = "") -> list:
    """为单条热点搜索 Twitter/Reddit 上的相关讨论帖。

    返回 [{platform, url, title, author, author_url, snippet, search_query}]
    """
    title = item.get("title", "")
    url = item.get("url", "")
    dev_name = item.get("developer_name", "")

    if not title:
        return []

    # 构建搜索词：项目名 / 标题关键词
    search_terms = []
    if source_key == "github" and "/" in title:
        # GitHub 项目：用 repo 名
        repo_name = title.split(" - ")[0].strip() if " - " in title else title.strip()
        search_terms.append(repo_name)
        # 也搜索 repo 的短名
        if "/" in repo_name:
            short_name = repo_name.split("/")[-1]
            search_terms.append(short_name)
    else:
        # 通用：取标题前 6 个单词
        words = title.split()[:6]
        search_terms.append(" ".join(words))

    if dev_name and dev_name not in str(search_terms):
        search_terms.append(dev_name)

    posts = []
    seen_urls = set()

    for term in search_terms[:2]:
        # --- 搜索 Twitter/X 帖子 (via DuckDuckGo site:twitter.com OR site:x.com) ---
        for site in ["site:twitter.com", "site:x.com"]:
            query = f"{term} {site}"
            ddg_results = _duckduckgo_search(query, max_results=3)
            for r in ddg_results:
                r_url = r["url"]
                if r_url in seen_urls:
                    continue
                # 过滤非推文链接
                if not any(d in r_url for d in ["twitter.com/", "x.com/"]):
                    continue
                if "/status/" not in r_url and "/i/" not in r_url:
                    # 可能是用户主页，也可以作为 KOC 线索
                    pass
                seen_urls.add(r_url)
                # 提取作者
                author = ""
                author_url = ""
                try:
                    import urllib.parse as _up
                    path = _up.urlparse(r_url).path.strip("/")
                    parts = path.split("/")
                    if parts:
                        author = parts[0]
                        author_url = f"https://x.com/{author}"
                except Exception:
                    pass
                posts.append({
                    "platform": "twitter",
                    "url": r_url,
                    "title": r["title"],
                    "author": author,
                    "author_url": author_url,
                    "snippet": r.get("snippet", ""),
                    "search_query": query,
                })

        # --- 搜索 Reddit 帖子 (via DuckDuckGo site:reddit.com) ---
        query = f"{term} site:reddit.com"
        ddg_results = _duckduckgo_search(query, max_results=3)
        for r in ddg_results:
            r_url = r["url"]
            if r_url in seen_urls:
                continue
            if "reddit.com" not in r_url:
                continue
            seen_urls.add(r_url)
            # 提取 subreddit 和作者
            author = ""
            subreddit = ""
            try:
                import urllib.parse as _up
                path = _up.urlparse(r_url).path.strip("/")
                parts = path.split("/")
                if len(parts) >= 2 and parts[0] == "r":
                    subreddit = parts[1]
                if "user" in parts:
                    idx = parts.index("user")
                    if idx + 1 < len(parts):
                        author = parts[idx + 1]
            except Exception:
                pass
            posts.append({
                "platform": "reddit",
                "url": r_url,
                "title": r["title"],
                "author": author,
                "author_url": f"https://reddit.com/user/{author}" if author else "",
                "subreddit": subreddit,
                "snippet": r.get("snippet", ""),
                "search_query": query,
            })

        time.sleep(0.5)  # 限流

    return posts


def batch_search_social_posts(results: list) -> dict:
    """批量为高分热点搜索社交帖子。

    返回 {item_url: [social_posts]} 映射。
    同时从帖子中收集 KOC 候选人。
    """
    social_map = {}
    koc_from_social = []
    tasks = []

    for result in results:
        if result.get("error"):
            continue
        source_key = result.get("key", "")
        for item in result.get("items", []):
            analysis = item.get("analysis", {})
            score = analysis.get("composite_score", 0)
            # 只为高分条目搜索社交帖子
            if score < 5.0:
                continue
            item_url = item.get("url", "")
            if not item_url:
                continue
            tasks.append({"item": item, "source_key": source_key, "item_url": item_url})

    print(f"[social_search] searching social posts for {len(tasks)} items...", file=sys.stderr)

    for task in tasks[:15]:  # 限制最多 15 条，避免过多搜索
        try:
            posts = search_social_posts_for_item(task["item"], task["source_key"])
            if posts:
                social_map[task["item_url"]] = posts
                # 收集 KOC 候选（来自社交帖子的作者）
                for post in posts:
                    if post.get("author") and post.get("author_url"):
                        koc_from_social.append({
                            "source": post["platform"],
                            "username": post["author"],
                            "profile_url": post["author_url"],
                            "associated_post": {
                                "title": post["title"],
                                "url": post["url"],
                                "snippet": post.get("snippet", ""),
                            },
                            "associated_hotspot": {
                                "title": task["item"].get("title", ""),
                                "url": task["item_url"],
                            },
                        })
        except Exception as e:
            print(f"[social_search] error for {task['item_url'][:60]}: {e}", file=sys.stderr)

    print(f"[social_search] found social posts for {len(social_map)} items, {len(koc_from_social)} KOC candidates", file=sys.stderr)
    return {"social_map": social_map, "koc_from_social": koc_from_social}


# ════════════════════════════════════════════════════════════
# 公开平台 KOC 发现：搜索云/AI 话题讨论者
# 从 Twitter/Reddit 搜索云产品、腾讯云、AI 项目的活跃讨论者
# ════════════════════════════════════════════════════════════

KOC_DISCOVERY_QUERIES = [
    # 腾讯云相关
    {"query": "Tencent Cloud site:twitter.com", "focus": "腾讯云用户"},
    {"query": "Tencent Cloud site:reddit.com", "focus": "腾讯云用户"},
    {"query": "EdgeOne CDN site:twitter.com", "focus": "EdgeOne用户"},
    {"query": "Lighthouse server deploy site:twitter.com", "focus": "Lighthouse用户"},
    # AI 项目相关
    {"query": "self-hosted AI deploy site:twitter.com", "focus": "AI自部署开发者"},
    {"query": "cloud GPU training site:reddit.com", "focus": "GPU云用户"},
    # 云产品对比/评测
    {"query": "cloud provider comparison 2026 site:twitter.com", "focus": "云产品评测者"},
    {"query": "AWS vs alternatives site:reddit.com", "focus": "云迁移讨论者"},
]


def discover_koc_from_platforms() -> list:
    """从公开平台搜索云/AI 话题的活跃讨论者，建立 KOC 候选库。

    返回 [{source, username, profile_url, focus, post_title, post_url, snippet}]
    """
    koc_list = []
    seen_authors = set()

    for q in KOC_DISCOVERY_QUERIES:
        try:
            results = _duckduckgo_search(q["query"], max_results=5)
            for r in results:
                r_url = r["url"]
                author = ""
                author_url = ""
                platform = ""

                if "twitter.com" in r_url or "x.com" in r_url:
                    platform = "twitter"
                    try:
                        import urllib.parse as _up
                        path = _up.urlparse(r_url).path.strip("/")
                        parts = path.split("/")
                        if parts:
                            author = parts[0]
                            author_url = f"https://x.com/{author}"
                    except Exception:
                        pass
                elif "reddit.com" in r_url:
                    platform = "reddit"
                    try:
                        import urllib.parse as _up
                        path = _up.urlparse(r_url).path.strip("/")
                        parts = path.split("/")
                        # 尝试提取帖子作者（需要爬取页面或从URL中推断）
                        if len(parts) >= 2 and parts[0] == "r":
                            # subreddit 级别，作者需从页面获取
                            pass
                        if "user" in parts:
                            idx = parts.index("user")
                            if idx + 1 < len(parts):
                                author = parts[idx + 1]
                                author_url = f"https://reddit.com/user/{author}"
                    except Exception:
                        pass

                if not author:
                    continue
                author_key = f"{platform}:{author.lower()}"
                if author_key in seen_authors:
                    continue
                seen_authors.add(author_key)

                koc_list.append({
                    "source": platform,
                    "username": author,
                    "profile_url": author_url,
                    "focus": q["focus"],
                    "post_title": r["title"],
                    "post_url": r_url,
                    "snippet": r.get("snippet", ""),
                })
        except Exception as e:
            print(f"[koc_discovery] error for query '{q['query'][:30]}': {e}", file=sys.stderr)

        time.sleep(0.5)

    print(f"[koc_discovery] discovered {len(koc_list)} KOC candidates from platforms", file=sys.stderr)
    return koc_list


# ════════════════════════════════════════════════════════════
# Twitter/Reddit 作为独立数据源：采集云/AI相关的实时社媒讨论
# 三路并行：Reddit JSON API（实时）+ HN Algolia（实时）+ DuckDuckGo 兜底
# ════════════════════════════════════════════════════════════

# Reddit 搜索话题：通过 RSS 获取子版块最新帖子 + 本地关键词过滤
# 注意：RSS 不支持搜索，靠遍历多个子版块 + query 关键词本地匹配
REDDIT_SEARCH_QUERIES = [
    {"query": "cloud aws azure gcp serverless", "subreddits": ["aws", "googlecloud", "azure", "cloudcomputing", "devops"], "focus": "云服务讨论"},
    {"query": "serverless edge CDN deploy hosting", "subreddits": ["webdev", "selfhosted", "webhosting"], "focus": "Serverless/Edge讨论"},
    {"query": "AI GPU LLM deploy model hosting", "subreddits": ["MachineLearning", "LocalLLaMA", "artificial", "singularity"], "focus": "AI基础设施讨论"},
    {"query": "VPS cloud provider comparison migrate", "subreddits": ["selfhosted", "webhosting", "sysadmin"], "focus": "云产品对比"},
    {"query": "copilot cursor coding AI IDE assistant", "subreddits": ["programming", "webdev", "vim", "neovim"], "focus": "AI编码工具讨论"},
]

# HN Algolia 搜索话题（24h 内的实时讨论）
HN_SOCIAL_QUERIES = [
    {"query": "cloud computing deploy", "focus": "云服务讨论"},
    {"query": "serverless edge CDN", "focus": "边缘计算讨论"},
    {"query": "GPU cloud AI infrastructure", "focus": "AI基础设施讨论"},
    {"query": "AWS Azure Google Cloud", "focus": "竞品动态"},
    {"query": "self-hosted VPS alternative", "focus": "云产品对比"},
    {"query": "AI coding assistant", "focus": "AI编码工具讨论"},
]


def _fetch_reddit_rss(subreddit, sort="new", limit=5):
    """通过 Reddit RSS/Atom feed 获取子版块帖子（JSON API 已返回 403，RSS 仍可用）。

    RSS 返回 Atom XML，包含最新帖子的标题、链接、作者、更新时间。
    不支持关键词搜索，但可以遍历多个相关子版块获取实时讨论。
    """
    items = []
    try:
        url = f"https://www.reddit.com/r/{subreddit}/{sort}/.rss?limit={limit}"
        resp = requests.get(url, headers={
            "User-Agent": "HotspotTracker/1.0 (research bot)",
        }, timeout=10)
        if resp.status_code != 200:
            print(f"[reddit_rss] r/{subreddit}: status={resp.status_code}", file=sys.stderr)
            return items

        soup = BeautifulSoup(resp.text, "html.parser")
        for entry in soup.find_all("entry"):
            title_tag = entry.find("title")
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)
            if not title:
                continue

            # 链接
            link_tag = entry.find("link")
            post_url = link_tag.get("href", "") if link_tag else ""

            # 作者
            author_tag = entry.find("author")
            author_name = ""
            if author_tag:
                name_tag = author_tag.find("name")
                author_name = name_tag.get_text(strip=True).lstrip("/u/") if name_tag else ""

            # 时间
            updated_tag = entry.find("updated") or entry.find("published")
            time_str = ""
            age_hours = 999
            if updated_tag:
                try:
                    # Atom uses ISO 8601 format, e.g. 2026-03-23T10:30:00+00:00
                    raw_time = updated_tag.get_text(strip=True)
                    # Python 3.7+ fromisoformat doesn't handle trailing Z
                    raw_time = raw_time.replace("Z", "+00:00")
                    dt = datetime.fromisoformat(raw_time)
                    age_hours = (time.time() - dt.timestamp()) / 3600
                    if age_hours < 1:
                        time_str = f"{int(age_hours * 60)}分钟前"
                    elif age_hours < 24:
                        time_str = f"{int(age_hours)}小时前"
                    else:
                        time_str = f"{int(age_hours / 24)}天前"
                except Exception:
                    time_str = updated_tag.get_text(strip=True)[:16]

            # 过滤：只要最近 48h 的帖子
            if age_hours > 48:
                continue

            # 内容摘要
            content_tag = entry.find("content")
            summary = ""
            if content_tag:
                content_html = content_tag.get_text(strip=True)
                content_soup = BeautifulSoup(content_html, "html.parser")
                summary = content_soup.get_text(separator=" ", strip=True)[:200]

            items.append({
                "source": "Twitter/Reddit",
                "title": title,
                "url": post_url,
                "heat": f"r/{subreddit}",
                "time": time_str,
                "summary": summary,
                "platform": "reddit",
                "author": author_name,
                "author_url": f"https://reddit.com/user/{author_name}" if author_name else "",
                "developer_name": author_name or f"r/{subreddit}",
                "developer_url": f"https://reddit.com/user/{author_name}" if author_name else f"https://reddit.com/r/{subreddit}",
                "subreddit": subreddit,
            })
    except Exception as e:
        print(f"[reddit_rss] r/{subreddit} error: {e}", file=sys.stderr)
    return items


def _fetch_reddit_search(query, subreddits=None, sort="new", limit=5, time_filter="day"):
    """通过 Reddit RSS feed 获取子版块的实时帖子，并按关键词过滤。

    Reddit JSON API 返回 403（需 OAuth），改用 RSS + 本地关键词匹配。
    """
    items = []
    query_words = [w.lower().strip() for w in re.split(r'\s+|OR', query) if w.strip() and w.strip() != 'OR']

    if subreddits:
        for sub in subreddits[:4]:
            try:
                posts = _fetch_reddit_rss(sub, sort=sort, limit=limit * 2)
                # 关键词过滤
                for post in posts:
                    text = f"{post['title']} {post.get('summary', '')}".lower()
                    if any(w in text for w in query_words):
                        items.append(post)
            except Exception:
                continue
            time.sleep(0.3)
    else:
        # 无指定子版块时，搜索通用的技术子版块
        fallback_subs = ["technology", "programming", "devops", "cloudcomputing"]
        for sub in fallback_subs:
            try:
                posts = _fetch_reddit_rss(sub, sort=sort, limit=limit)
                for post in posts:
                    text = f"{post['title']} {post.get('summary', '')}".lower()
                    if any(w in text for w in query_words):
                        items.append(post)
            except Exception:
                continue
            time.sleep(0.3)
    return items[:limit]


def _fetch_hn_social_search(query, focus, limit=3):
    """通过 HN Algolia API 搜索最近 24h 的讨论（实时+可靠）"""
    items = []
    try:
        timestamp_24h = int(time.time() - 24 * 3600)
        api_url = (
            f"http://hn.algolia.com/api/v1/search_by_date"
            f"?tags=story&numericFilters=created_at_i>{timestamp_24h}"
            f"&hitsPerPage={limit * 2}&query={requests.utils.quote(query)}"
        )
        resp = requests.get(api_url, timeout=10)
        if resp.status_code != 200:
            return items
        data = resp.json()

        for hit in data.get("hits", []):
            title = hit.get("title", "")
            if not title:
                continue
            hn_url = f"https://news.ycombinator.com/item?id={hit['objectID']}"
            points = hit.get("points", 0) or 0
            num_comments = hit.get("num_comments", 0) or 0
            author = hit.get("author", "")
            # 时间计算
            created = hit.get("created_at_i", 0)
            age_hours = (time.time() - created) / 3600 if created else 0
            if age_hours < 1:
                time_str = f"{int(age_hours * 60)}分钟前"
            elif age_hours < 24:
                time_str = f"{int(age_hours)}小时前"
            else:
                time_str = f"{int(age_hours / 24)}天前"

            items.append({
                "source": "Twitter/Reddit",
                "title": title,
                "url": hit.get("url") or hn_url,
                "hn_url": hn_url,
                "heat": f"↑{points} · {num_comments} comments",
                "time": time_str,
                "summary": "",
                "platform": "hackernews",
                "author": author,
                "author_url": f"https://news.ycombinator.com/user?id={author}" if author else "",
                "developer_name": author,
                "developer_url": f"https://news.ycombinator.com/user?id={author}" if author else "",
                "subreddit": "",
                "tcloud_focus": focus,
            })
            if len(items) >= limit:
                break
    except Exception as e:
        print(f"[hn_social] error: {e}", file=sys.stderr)
    return items


def fetch_twitter_reddit_tcloud(limit: int = 10, keyword=None) -> list:
    """采集实时社媒讨论：Reddit JSON API + HN Algolia + DuckDuckGo 兜底。

    三路并行，确保即使某个渠道不可用也能获取到数据。
    聚焦当下：所有内容限制在最近 24-48h 内。
    """
    all_items = []
    seen_urls = set()

    # ── 路线 1: Reddit JSON API（实时，高可用）──
    print("[twitter_reddit] fetching Reddit posts...", file=sys.stderr)
    for q_spec in REDDIT_SEARCH_QUERIES:
        try:
            posts = _fetch_reddit_search(
                q_spec["query"],
                subreddits=q_spec.get("subreddits"),
                limit=3,
                time_filter="day",
            )
            for post in posts:
                url = post["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                post["tcloud_focus"] = q_spec["focus"]
                all_items.append(post)
        except Exception as e:
            print(f"[reddit] error for '{q_spec['query'][:30]}': {e}", file=sys.stderr)
        time.sleep(0.3)

    reddit_count = len(all_items)
    print(f"[twitter_reddit] Reddit: {reddit_count} posts", file=sys.stderr)

    # ── 路线 2: HN Algolia 24h 实时讨论 ──
    print("[twitter_reddit] fetching HN discussions...", file=sys.stderr)
    for q_spec in HN_SOCIAL_QUERIES:
        try:
            posts = _fetch_hn_social_search(q_spec["query"], q_spec["focus"], limit=2)
            for post in posts:
                url = post.get("hn_url") or post["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                all_items.append(post)
        except Exception as e:
            print(f"[hn_social] error for '{q_spec['query'][:30]}': {e}", file=sys.stderr)

    hn_count = len(all_items) - reddit_count
    print(f"[twitter_reddit] HN: {hn_count} posts", file=sys.stderr)

    # ── 路线 3: DuckDuckGo 兜底（如果前两路数据不够） ──
    if len(all_items) < limit // 2:
        print("[twitter_reddit] DDG fallback...", file=sys.stderr)
        ddg_queries = [
            {"query": "Tencent Cloud site:x.com", "focus": "腾讯云品牌讨论"},
            {"query": "cloud computing site:x.com", "focus": "云服务讨论"},
            {"query": "AI deploy site:reddit.com", "focus": "AI基础设施讨论"},
        ]
        for q_spec in ddg_queries:
            try:
                results = _duckduckgo_search(q_spec["query"], max_results=3)
                for r in results:
                    url = r["url"]
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    platform = "twitter" if any(d in url for d in ["twitter.com", "x.com"]) else "reddit"
                    author = ""
                    try:
                        import urllib.parse as _up
                        path = _up.urlparse(url).path.strip("/")
                        parts = path.split("/")
                        if parts:
                            author = parts[0]
                    except Exception:
                        pass
                    all_items.append({
                        "source": "Twitter/Reddit",
                        "title": r["title"],
                        "url": url,
                        "heat": f"social:{q_spec['focus']}",
                        "time": datetime.now().strftime("%Y-%m-%d"),
                        "summary": r.get("snippet", ""),
                        "platform": platform,
                        "author": author,
                        "author_url": f"https://x.com/{author}" if platform == "twitter" else "",
                        "developer_name": author,
                        "developer_url": f"https://x.com/{author}" if platform == "twitter" else "",
                        "subreddit": "",
                        "tcloud_focus": q_spec["focus"],
                    })
            except Exception:
                pass
            time.sleep(0.3)

    # 按时效排序：最新的排前面
    def _sort_key(item):
        t = item.get("time", "")
        if "分钟前" in t:
            return 0
        if "小时前" in t:
            try:
                return int(t.replace("小时前", ""))
            except Exception:
                return 50
        if "天前" in t:
            return 100
        return 200
    all_items.sort(key=_sort_key)

    if keyword:
        all_items = filter_items(all_items, keyword)

    print(f"[twitter_reddit_tcloud] total: {len(all_items)} social posts (Reddit:{reddit_count}, HN:{hn_count})", file=sys.stderr)
    return all_items[:limit]


def create_single_rss_fetcher(url, name):
    def fetcher(limit=5, keyword=None):
        return filter_items(fetch_rss_feed(url, name, limit), keyword)[:limit]
    return fetcher


def save_report(data, source_name, out_dir):
    """
    Saves JSON and generates a simple Markdown report.
    """
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
        
    # Sanitize source name for filename
    safe_name = "".join([c if c.isalnum() else "_" for c in source_name]).lower()
    timestamp = datetime.now().strftime("%H%M")
    
    # 1. Save JSON
    json_path = os.path.join(out_dir, f"{safe_name}_{timestamp}.json")
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        
    return json_path

def main():
    parser = argparse.ArgumentParser()
    sources_map = {
        'hackernews': fetch_hackernews, 'weibo': fetch_weibo, 'github': fetch_github,
        '36kr': fetch_36kr, 'v2ex': fetch_v2ex, 'tencent': fetch_tencent,
        'wallstreetcn': fetch_wallstreetcn, 'producthunt': fetch_producthunt,
        # Aggregates
        'huggingface': fetch_huggingface_papers,
        'ai_newsletters': fetch_ai_newsletters, 'podcasts': fetch_podcasts,
        'essays': fetch_essays,
        # Web Search (5-channel parallel)
        'web_search': fetch_web_search,
        # Twitter/Reddit Tencent Cloud discussions
        'twitter_reddit': fetch_twitter_reddit_tcloud,
        # Standalone AI Sources
        'latentspace_ainews': fetch_latentspace_ainews,
    }

    # Dynamic Registration of Sub-sources
    # AI Newsletters
    for name, url in AI_NEWSLETTER_SOURCES:
        key = name.lower().replace(' ', '').replace("'", "")
        # Check if this source needs Playwright
        if "Ben's Bites" in name or "The Rundown" in name:
             sources_map[key] = lambda limit=10, k=None, u=url, n=name: filter_items(fetch_rss_with_playwright(u, n, limit), k)[:limit]
        else:
             sources_map[key] = create_single_rss_fetcher(url, name)
        
    # Podcasts
    for name, url in PODCAST_SOURCES:
        key = name.lower().replace(' ', '')
        sources_map[key] = create_single_rss_fetcher(url, name)

    # Essays
    for name, url in ESSAY_SOURCES:
        key = name.lower().replace(' ', '')
        sources_map[key] = create_single_rss_fetcher(url, name)
    
    parser.add_argument('--source', default='all', help='Source(s) to fetch from (comma-separated). Now supports sub-sources like "chinai", "paulgraham"')
    parser.add_argument('--limit', type=int, default=10, help='Limit per source. Default 10')
    parser.add_argument('--keyword', help='Comma-sep keyword filter')
    parser.add_argument('--deep', action='store_true', help='Download article content for detailed summarization')
    parser.add_argument('--save', action='store_true', help='Save output to reports directory (JSON + MD)')
    parser.add_argument('--no-save', action='store_true', dest='no_save', help='Skip saving JSON files to disk (only output to stdout)')
    parser.add_argument('--outdir', help='Custom output directory for saved reports')
    parser.add_argument('--list-sources', action='store_true', help='List all available source keys')
    
    args = parser.parse_args()

    if args.list_sources:
        print(f"{'Source Key':<20} | {'Source Name'}")
        print("-" * 40)
        for key in sorted(sources_map.keys()):
            print(f"{key:<20}")
        return
    
    to_run = []
    if args.source == 'all':
        to_run = list(sources_map.values())
    else:
        requested_sources = [s.strip() for s in args.source.split(',')]
        for s in requested_sources:
            if s in sources_map: to_run.append(sources_map[s])
            
    results = []
    
    def run_fetchers(fetchers, limit, kw):
        res = []
        for func in fetchers:
            try:
                res.extend(func(limit, kw))
            except: pass
        return res

    # Primary Fetch
    results = run_fetchers(to_run, args.limit, args.keyword)
        
    # Smart Fill Logic (Only if keyword is used and results are sparse)
    MIN_ITEMS = 5
    if args.keyword and len(results) < MIN_ITEMS:
        sys.stderr.write(f"Smart Fill triggered: Found {len(results)} items, filling gaps...\n")
        
        # Secondary Fetch (Broad, no keyword)
        # We fetch enough to potentially fill the gap, limit=MIN_ITEMS is a safe bet for each source
        fill_limit = MIN_ITEMS 
        fill_results = run_fetchers(to_run, limit=fill_limit, kw=None)
        
        # Deduplicate and Append
        existing_urls = {item.get('url') for item in results}
        existing_titles = {item.get('title') for item in results}
        
        for item in fill_results:
            if len(results) >= MIN_ITEMS:
                break
                
            u = item.get('url')
            t = item.get('title')
            
            if u not in existing_urls and t not in existing_titles:
                # Mark as smart fill
                item['smart_fill'] = True
                
                # Add warning to time field as per SKILL.md
                if 'time' in item:
                    item['time'] = f"⚠️ {item['time']}"
                
                results.append(item)
                existing_urls.add(u)
                existing_titles.add(t)

    if args.deep and results:
        sys.stderr.write(f"Deep fetching content for {len(results)} items...\n")
        results = enrich_items_with_content(results)
        
    print(json.dumps(results, indent=2, ensure_ascii=False))
    
    # Save Report if requested or if running a single source (implicit convenience)
    # Skip saving when --no-save is set (agent reads from stdout)
    if not getattr(args, 'no_save', False) and (args.save or args.source != 'all'):
        if args.outdir:
            out_dir = args.outdir
        else:
            today = datetime.now().strftime('%Y-%m-%d')
            out_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'reports', today)
            
        md_file = save_report(results, args.source, out_dir)
        sys.stderr.write(f"\n[Saved] Raw Data: {md_file} (Agent to process)\n")

if __name__ == "__main__":
    main()
