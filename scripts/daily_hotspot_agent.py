from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fetch_news import (
    fetch_36kr,
    fetch_ai_newsletters,
    fetch_github,
    fetch_hackernews,
    fetch_huggingface_papers,
    fetch_producthunt,
    fetch_v2ex,
    fetch_url_content,
    fetch_web_search,
    fetch_twitter_reddit_tcloud,
    enrich_github_developer_profiles,
    batch_search_social_posts,
    discover_koc_from_platforms,
)
from content_analyzer import analyze_results
try:
    from iwiki_writer import update_iwiki_page, save_iwiki_html_locally
except ImportError:  # pragma: no cover - optional dependency path
    def update_iwiki_page(*args, **kwargs):
        return {
            "success": False,
            "message": "iwiki_writer.py 未提供，已跳过 iWiki 写入。",
            "wiki_url": "",
        }

    def save_iwiki_html_locally(*args, **kwargs):
        return ""

from wecom_doc_writer import write_to_wecom_doc, write_selected_topics

Fetcher = Callable[[int, Optional[str]], List[Dict[str, Any]]]

# 每个渠道最低抓取量 = 6
MIN_ITEMS_PER_SOURCE = 6

SOURCE_SPECS: List[Dict[str, Any]] = [
    {"index": 1, "label": "🦄 硅谷热点", "key": "hackernews", "fetcher": fetch_hackernews, "limit": 8, "top_n": 3},
    {"index": 2, "label": "🐙 开源趋势", "key": "github", "fetcher": fetch_github, "limit": 8, "top_n": 3},
    {"index": 3, "label": "🚀 创投快讯", "key": "36kr", "fetcher": fetch_36kr, "limit": 8, "top_n": 3},
    {"index": 4, "label": "🐱 产品猎人", "key": "producthunt", "fetcher": fetch_producthunt, "limit": 8, "top_n": 3},
    {"index": 5, "label": "🤓 极客社区", "key": "v2ex", "fetcher": fetch_v2ex, "limit": 8, "top_n": 3},
    {"index": 6, "label": "🤗 HF 每日论文", "key": "huggingface", "fetcher": fetch_huggingface_papers, "limit": 6, "top_n": 3},
    {"index": 7, "label": "🧠 AI 内参热点", "key": "ai_newsletters", "fetcher": fetch_ai_newsletters, "limit": 10, "top_n": 5},
    {"index": 8, "label": "🌐 全网搜索", "key": "web_search", "fetcher": fetch_web_search, "limit": 10, "top_n": 3},
    {"index": 9, "label": "🐦 社媒讨论", "key": "twitter_reddit", "fetcher": fetch_twitter_reddit_tcloud, "limit": 10, "top_n": 5},
]


def clean_text(text: Optional[str], limit: int = 120) -> str:
    if not text:
        return ""
    compact = re.sub(r"\s+", " ", str(text)).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def strip_title_prefix(title: str, source_key: str) -> str:
    if source_key == "github" and " - " in title:
        return title.split(" - ", 1)[0].strip()
    return title.strip()


def extract_inline_description(item: Dict[str, Any], source_key: str) -> str:
    if item.get("summary"):
        return clean_text(item["summary"], 120)
    title = item.get("title", "")
    if source_key == "github" and " - " in title:
        return clean_text(title.split(" - ", 1)[1], 120)
    if item.get("content"):
        content = clean_text(item["content"], 120)
        return content
    return ""


def build_item_line(item: Dict[str, Any], source_key: str) -> str:
    title = strip_title_prefix(item.get("title", "未命名条目"), source_key)
    url = item.get("url") or ""
    time_text = item.get("time") or "未知时间"
    heat_text = item.get("heat") or "热度未提供"
    desc = extract_inline_description(item, source_key)

    meta = f"时间：{time_text}；热度：{heat_text}"
    if desc:
        meta = f"{meta}。一句话：{desc}"

    if url.startswith("http"):
        return f"- [{title}]({url})：{meta}"
    return f"- {title}：{meta}"


def fetch_source(spec: Dict[str, Any]) -> Dict[str, Any]:
    """抓取单个来源，如果不足 MIN_ITEMS_PER_SOURCE 则尝试扩大 limit 重抓一次"""
    fetcher: Fetcher = spec["fetcher"]
    key = spec["key"]
    base_limit = spec.get("limit", 6)

    # 确保 limit 至少为 MIN_ITEMS_PER_SOURCE
    limit = max(base_limit, MIN_ITEMS_PER_SOURCE)

    try:
        items = fetcher(limit, spec.get("keyword"))
        if not isinstance(items, list):
            items = []

        # 如果首次抓取不够 MIN_ITEMS_PER_SOURCE，尝试扩大 limit 再抓一次
        if len(items) < MIN_ITEMS_PER_SOURCE and len(items) > 0:
            try:
                expanded_limit = min(limit * 3, 30)
                extra_items = fetcher(expanded_limit, spec.get("keyword"))
                if isinstance(extra_items, list) and len(extra_items) > len(items):
                    items = extra_items
            except Exception:
                pass  # 保持首次结果

        return {
            "key": key,
            "label": spec["label"],
            "index": spec["index"],
            "items": items,
            "error": None,
        }
    except Exception as exc:  # pragma: no cover - network path
        return {
            "key": key,
            "label": spec["label"],
            "index": spec["index"],
            "items": [],
            "error": str(exc),
        }


def _fetch_github_readme(repo_url: str) -> str:
    """从 GitHub 仓库页面抓取 README 内容，返回纯文本（截断到 3000 字符）。"""
    import requests
    from bs4 import BeautifulSoup
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    # 优先尝试 raw README（API 不需要 token 的方式）
    # 从 repo_url 中提取 owner/repo
    parts = repo_url.rstrip("/").split("github.com/")
    if len(parts) < 2:
        return ""
    repo_path = parts[1]  # e.g. "langchain-ai/open-swe"

    # 尝试几种常见的 README 路径
    for branch in ["main", "master"]:
        for readme_name in ["README.md", "readme.md", "README.rst", "README"]:
            raw_url = f"https://raw.githubusercontent.com/{repo_path}/{branch}/{readme_name}"
            try:
                resp = requests.get(raw_url, headers=headers, timeout=8)
                if resp.status_code == 200 and len(resp.text.strip()) > 50:
                    text = resp.text.strip()
                    # 简单清理 markdown 标记以提取纯文本
                    # 去掉图片 ![...](...)
                    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
                    # 去掉链接标记但保留文本 [text](url) -> text
                    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
                    # 去掉 HTML 标签
                    text = re.sub(r"<[^>]+>", " ", text)
                    # 去掉连续空行
                    text = re.sub(r"\n{3,}", "\n\n", text)
                    return text[:3000]
            except Exception:
                continue
    return ""


def _fetch_page_content(url: str) -> str:
    """通用页面内容抓取，返回纯文本。"""
    if not url or not url.startswith("http"):
        return ""
    return fetch_url_content(url) or ""


def enrich_items_with_deep_content(results: List[Dict[str, Any]], max_workers: int = 8) -> None:
    """为每条热点条目深度抓取原始内容，填充 content 字段。

    - GitHub：抓取 README.md 全文
    - V2EX / HN / 36kr / producthunt / ai_newsletters：抓取原文页面
    - HuggingFace papers：已有 summary，不需要额外抓取
    """
    tasks: List[Dict[str, Any]] = []  # (item, url, fetch_func)

    for result in results:
        if result.get("error"):
            continue
        source_key = result.get("key", "")
        for item in result.get("items", []):
            # 如果已经有足够长的 content，跳过
            existing_content = item.get("content", "")
            if len(existing_content) > 200:
                continue

            url = item.get("url", "")
            if not url or not url.startswith("http"):
                continue

            if source_key == "github":
                tasks.append({"item": item, "url": url, "type": "github"})
            elif source_key in {"hackernews", "v2ex", "36kr", "producthunt", "ai_newsletters"}:
                tasks.append({"item": item, "url": url, "type": "page"})

    if not tasks:
        return

    print(f"[daily_hotspot_agent] enriching {len(tasks)} items with deep content...", file=sys.stderr)

    def _do_fetch(task: Dict[str, Any]) -> None:
        try:
            if task["type"] == "github":
                content = _fetch_github_readme(task["url"])
            else:
                content = _fetch_page_content(task["url"])
            if content and len(content) > 50:
                task["item"]["content"] = content
        except Exception:
            pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_do_fetch, task) for task in tasks]
        concurrent.futures.wait(futures, timeout=60)

    enriched_count = sum(1 for t in tasks if len(t["item"].get("content", "")) > 200)
    print(f"[daily_hotspot_agent] enriched {enriched_count}/{len(tasks)} items", file=sys.stderr)


def filter_non_ai_non_cloud_items(results: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    过滤掉 analysis 中同时满足 is_ai=False 且 is_cloud=False 的条目。

    这样最终输出到 CSV / JSON / iWiki / 企业微信多维表格的数据，
    只保留 AI 相关或云行业相关的内容。
    """
    removed_count = 0
    kept_count = 0

    for result in results:
        if result.get("error"):
            continue

        items = result.get("items", [])
        filtered_items: List[Dict[str, Any]] = []
        for item in items:
            analysis = item.get("analysis", {})
            if analysis.get("is_ai") or analysis.get("is_cloud"):
                filtered_items.append(item)

        removed_count += len(items) - len(filtered_items)
        kept_count += len(filtered_items)
        result["items"] = filtered_items

    return {
        "removed_count": removed_count,
        "kept_count": kept_count,
    }


def build_report(results: List[Dict[str, Any]], generated_at: datetime) -> str:
    total_items = sum(len(result["items"]) for result in results)
    success_count = sum(1 for result in results if not result["error"])
    failed = [result for result in results if result["error"]]

    lines: List[str] = [
        "# 每日热点追踪",
        "",
        f"> **生成时间**：{generated_at.strftime('%Y-%m-%d %H:%M')}",
        f"> **覆盖栏目**：{len(results)} 个",
        f"> **抓取条数**：{total_items} 条",
        f"> **成功栏目**：{success_count} 个",
        "",
        "这份日报按你指定的信源顺序整理，默认仅保留 AI 相关或云行业相关的结果，减少无关噪音。",
        "",
    ]
    if failed:
        failed_names = "、".join(f"{item['label']}（{item['key']}）" for item in failed)
        lines.extend([
            f"> **抓取异常**：{failed_names}",
            "",
        ])

    for result in sorted(results, key=lambda item: item["index"]):
        key = result["key"]
        label = result["label"]
        items = result["items"]

        lines.append(f"## {result['index']}. {label} `{key}`")
        lines.append("")

        if result["error"]:
            lines.append(f"今天这一栏抓取失败了，错误信息：`{clean_text(result['error'], 160)}`")
            lines.append("")
            continue

        if not items:
            lines.append("今天这一栏暂时没有抓到可用结果，你可以稍后手动再跑一次。")
            lines.append("")
            continue

        lines.append(f"今天共抓到 **{len(items)}** 条，下面按抓取顺序完整列出：")
        lines.append("")
        for item in items:
            lines.append(build_item_line(item, key))
        lines.append("")

    lines.extend([
        "---",
        "",
        "- **说明**：本报告完全基于抓取结果生成，不额外编造新闻事实。",
        "",
    ])
    return "\n".join(lines)


def escape_table_text(text: Optional[str], limit: int = 80) -> str:
    cleaned = clean_text(text, limit)
    return cleaned.replace("|", "\\|").replace("\n", " ")


def ensure_directories(output_dir: Path, generated_at: datetime) -> Dict[str, Path]:
    date_str = generated_at.strftime("%Y-%m-%d")
    archive_dir = output_dir / "archive" / date_str
    raw_dir = archive_dir / "raw"
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    return {
        "archive_dir": archive_dir,
        "raw_dir": raw_dir,
    }


def build_csv_rows(results: List[Dict[str, Any]]) -> List[List[str]]:
    """构建包含热点到写作全链路结论的 CSV 行，同渠道内按综合评分降序排列。"""
    headers = [
        "栏目序号",
        "栏目",
        "来源Key",
        "标题",
        "链接",
        "时间",
        "热度",
        "中文简介",
        "具体内容摘要",
        "开发者/作者",
        "开发者链接",
        "开发者邮箱",
        "话题分类",
        "话题类型",
        "是否AI相关",
        "是否云行业",
        "内容标注",
        "综合评分",
        "热度评分",
        "时效评分",
        "与腾讯云结合度",
        "产品标签",
        "友商标签",
        "竞品产品标签",
        "技术标签",
        "官方号主推产品",
        "国际站关联类型",
        "腾讯云结合点",
        "官方号写作角度",
        "配图建议",
        "可用角度",
        "发布优先级",
        "适合平台",
        "社媒结论",
        "备注",
    ]
    rows: List[List[str]] = [headers]

    for result in sorted(results, key=lambda item: item["index"]):
        key = result["key"]
        label = result["label"]
        index_text = str(result["index"])

        if result["error"]:
            row = [""] * len(headers)
            row[0:3] = [index_text, label, key]
            row[-1] = f"抓取失败: {clean_text(result['error'], 200)}"
            rows.append(row)
            continue

        items = result["items"]
        if not items:
            if result.get("filtered_out_count", 0) > 0 and result.get("original_item_count", 0) > 0:
                continue
            row = [""] * len(headers)
            row[0:3] = [index_text, label, key]
            row[-1] = "暂无数据，可稍后重跑"
            rows.append(row)
            continue

        sorted_items = sorted(
            items,
            key=lambda it: it.get("analysis", {}).get("composite_score", 0),
            reverse=True,
        )

        for item in sorted_items:
            analysis = item.get("analysis", {})
            rows.append([
                index_text,
                label,
                key,
                strip_title_prefix(item.get("title", "未命名条目"), key),
                item.get("url") or "",
                item.get("time") or "未知时间",
                item.get("heat") or "热度未提供",
                item.get("summary_cn") or extract_inline_description(item, key) or "",
                analysis.get("detail_brief", ""),
                item.get("developer_name") or "",
                item.get("developer_url") or "",
                item.get("developer_email") or "",
                analysis.get("topic", ""),
                analysis.get("topic_type", ""),
                "是" if analysis.get("is_ai") else "否",
                "是" if analysis.get("is_cloud") else "否",
                analysis.get("tone", ""),
                str(analysis.get("composite_score", "")),
                str(analysis.get("heat_score", "")),
                str(analysis.get("timeliness_score", "")),
                str(analysis.get("tcloud_relevance", "")),
                ", ".join(analysis.get("products", [])),
                ", ".join(analysis.get("competitors", [])),
                ", ".join(analysis.get("competitor_products", [])),
                ", ".join(analysis.get("techs", [])),
                analysis.get("official_product_focus", ""),
                analysis.get("tcloud_relation_type", ""),
                analysis.get("tcloud_integration", ""),
                analysis.get("official_angle_cn", ""),
                analysis.get("visual_brief", ""),
                analysis.get("editorial_angles_text", ""),
                analysis.get("publish_priority", ""),
                analysis.get("recommended_platforms_text", ""),
                analysis.get("social_recommendation", ""),
                "",
            ])

    return rows


def write_csv_file(target_path: Path, rows: List[List[str]]) -> None:
    with target_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerows(rows)


def collect_writing_candidates(results: List[Dict[str, Any]], limit: int = 8) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for result in results:
        for item in result.get("items", []):
            analysis = item.get("analysis", {})
            if not analysis:
                continue
            if analysis.get("tcloud_primary_product") or analysis.get("tcloud_relevance", 0) >= 2.0:
                candidates.append({
                    "label": result.get("label", ""),
                    "key": result.get("key", ""),
                    "item": item,
                    "analysis": analysis,
                })

    candidates.sort(
        key=lambda entry: (
            entry["analysis"].get("tcloud_relevance", 0),
            entry["analysis"].get("composite_score", 0),
            entry["analysis"].get("timeliness_score", 0),
        ),
        reverse=True,
    )
    return candidates[:limit]



def build_writing_pack_markdown(results: List[Dict[str, Any]], generated_at: datetime) -> str:
    candidates = collect_writing_candidates(results)
    lines: List[str] = [
        "# ✍️ 每日写作包",
        "",
        f"> {generated_at.strftime('%Y-%m-%d %H:%M')} · 共 {len(candidates)} 条可写热点",
        "",
    ]

    if not candidates:
        lines.extend([
            "今天没有命中足够明确的腾讯云国际站产品锚点，建议先人工挑选再写。",
            "",
        ])
        return "\n".join(lines)

    for idx, candidate in enumerate(candidates, start=1):
        item = candidate["item"]
        analysis = candidate["analysis"]
        title = strip_title_prefix(item.get('title', '未命名条目'), candidate['key'])
        link = item.get('url', '') or item.get('link', '') or ''
        product = analysis.get('official_product_focus', '') or '待人工判断'
        priority = analysis.get('publish_priority', '')
        platforms = analysis.get('recommended_platforms_text', '')
        integration = analysis.get('tcloud_integration', '')
        angle = analysis.get('official_angle_cn', '')
        visual = analysis.get('visual_brief', '')

        # 标题行（带链接）
        if link:
            lines.append(f"## {idx}. [{title}]({link})")
        else:
            lines.append(f"## {idx}. {title}")
        lines.append("")

        # 一行摘要：来源 + 产品 + 优先级
        meta_parts = [f"`{candidate['key']}`"]
        if product and product != '待人工判断':
            meta_parts.append(f"→ **{product}**")
        if priority:
            prio_short = priority.split("（")[0] if "（" in priority else priority
            meta_parts.append(prio_short)
        if platforms:
            meta_parts.append(platforms)
        lines.append(" · ".join(meta_parts))
        lines.append("")

        # 结合点（最核心的信息）
        if integration:
            # 取第一句话，不超过 150 字
            integration_short = integration.split("\n")[0][:150]
            lines.append(f"**结合点**：{integration_short}")
            lines.append("")

        # 写作角度
        if angle and "暂不建议" not in angle:
            lines.append(f"**写作角度**：{angle}")
            lines.append("")

        # 配图建议（简短）
        if visual:
            lines.append(f"**配图**：{visual}")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# 内容生产管线：分产品生成 Draft Markdown
# 按产品维度聚合高优热点，每个产品输出一份 Draft，供审核使用
# ════════════════════════════════════════════════════════════════

def build_product_drafts(results: List[Dict[str, Any]], generated_at: datetime) -> Dict[str, str]:
    """按腾讯云产品维度聚合热点，为每个关联产品生成精简 Draft。

    输出风格：直接告诉你「发什么、在哪发、文案是什么」，最多附一句参考热点。
    返回 {product_name: markdown_content} 字典。
    """
    product_items: Dict[str, List[Dict[str, Any]]] = {}

    for result in results:
        source_key = result.get("key", "")
        for item in result.get("items", []):
            analysis = item.get("analysis", {})
            primary_product = analysis.get("tcloud_primary_product", "")
            priority = analysis.get("publish_priority", "")
            if not primary_product or not priority:
                continue
            if not (priority.startswith("P0") or priority.startswith("P1")):
                continue
            if primary_product not in product_items:
                product_items[primary_product] = []
            product_items[primary_product].append({
                "item": item,
                "analysis": analysis,
                "source_key": source_key,
            })

    drafts: Dict[str, str] = {}
    date_str = generated_at.strftime("%Y-%m-%d")

    for product, entries in sorted(product_items.items()):
        lines: List[str] = [
            f"# {product} — 今日内容 ({len(entries)} 条)",
            "",
        ]

        for idx, entry in enumerate(entries, start=1):
            item = entry["item"]
            analysis = entry["analysis"]
            title = strip_title_prefix(item.get("title", ""), entry["source_key"])
            link = item.get("url", "") or item.get("link", "")
            priority = analysis.get("publish_priority", "")
            prio_short = priority.split("（")[0] if "（" in priority else priority
            integration = analysis.get("tcloud_integration", "")
            angle = analysis.get("official_angle_cn", "")
            social_rec = analysis.get("social_recommendation", "")
            platforms_text = analysis.get("recommended_platforms_text", "")

            # ── 标题行（精简）──
            lines.append(f"## {idx}. {prio_short} · {title[:60]}")
            lines.append("")

            # ── 核心信息：一句话说清怎么发 ──
            # 写作切入（精简到一句话）
            if angle and "暂不建议" not in angle:
                angle_short = angle.split("，")[0][:80] if "，" in angle else angle[:80]
                lines.append(f"**写什么**: {angle_short}")
            elif integration:
                integration_short = integration.split("\n")[0][:80]
                lines.append(f"**写什么**: {integration_short}")
            lines.append("")

            # 发布平台
            if platforms_text:
                lines.append(f"**发到哪**: {platforms_text}")
            else:
                lines.append("**发到哪**: X(Twitter) + Discord")
            lines.append("")

            # 建议文案（直接可复制）
            draft_text = _generate_product_social_copy(product, title, integration, entry["source_key"])
            if draft_text:
                lines.append(f"**参考文案**:")
                lines.append(f"> {draft_text}")
                lines.append("")

            # 参考热点（一行带走）
            if link:
                lines.append(f"*参考: [{title[:50]}]({link})*")
            lines.append("")
            lines.append("---")
            lines.append("")

        drafts[product] = "\n".join(lines)

    return drafts


def _generate_product_social_copy(product: str, title: str, integration: str, source_key: str) -> str:
    """为特定产品生成社媒发布文案（英文，可直接复制发X/LinkedIn）"""
    title_short = title[:40]

    # 产品特定文案模板
    product_templates = {
        "Lighthouse": f"🚀 \"{title_short}\" caught our eye! Deploy it in minutes on Tencent Cloud Lighthouse — Docker pre-installed, global nodes, from $3.5/mo. Try it: https://www.tencentcloud.com/products/lighthouse #CloudComputing #DevOps",
        "EdgeOne": f"⚡ Interesting project: \"{title_short}\". For teams needing global acceleration + DDoS protection, check out EdgeOne — 3,200+ PoP nodes worldwide. #CDN #WebSecurity #EdgeComputing",
        "CodeBuddy": f"🤖 \"{title_short}\" shows the power of AI coding! We built CodeBuddy with similar vision — AI-powered IDE covering requirements→design→code→test. Try the Agent mode: https://www.tencentcloud.com/products/codebuddy #AICoding #DevTools",
        "Hunyuan": f"🧠 \"{title_short}\" — great to see AI innovation! Tencent Hunyuan offers enterprise-grade LLM + multimodal capabilities. Explore: https://www.tencentcloud.com/products/hunyuan #AI #LLM",
        "TDSQL-C": f"🗃️ \"{title_short}\" highlights the need for reliable databases. TDSQL-C offers 100% PostgreSQL/MySQL compatibility with cloud-native performance. #Database #CloudNative",
        "IM": f"💬 \"{title_short}\" — real-time communication matters! Tencent Cloud IM supports 100M+ concurrent users with global coverage. #RealTime #Messaging",
    }

    if product in product_templates:
        return product_templates[product]

    # 通用模板
    if integration:
        hook = integration.split("\n")[0][:60]
        return f"🔥 \"{title_short}\" — {hook}. Check out Tencent Cloud {product} for a production-ready solution. #TencentCloud #CloudComputing"

    return f"🔥 \"{title_short}\" is trending! For teams looking to build on this, Tencent Cloud {product} offers enterprise-grade infrastructure. #TencentCloud"


# ════════════════════════════════════════════════════════════════
# 社媒互动推荐（含原帖链接）：找到可直接互动的 Twitter/Reddit 帖子
# 不再推荐"转发 GitHub"，而是提供平台原帖供直接评论/互动
# ════════════════════════════════════════════════════════════════

def build_social_quick_actions(
    results: List[Dict[str, Any]],
    generated_at: datetime,
    social_posts_map: Optional[Dict[str, list]] = None,
) -> str:
    """生成社媒快速互动推荐列表（含原帖链接版）。

    改进：不再推荐"转发 GitHub 链接"，而是搜索 Twitter/Reddit 上的
    相关讨论帖，提供可直接互动的原帖链接。
    """
    date_str = generated_at.strftime("%Y-%m-%d")
    social_posts_map = social_posts_map or {}
    candidates: List[Dict[str, Any]] = []

    for result in results:
        source_key = result.get("key", "")
        for item in result.get("items", []):
            analysis = item.get("analysis", {})
            score = analysis.get("composite_score", 0)
            is_ai = analysis.get("is_ai", False)
            is_cloud = analysis.get("is_cloud", False)
            tcloud_products = analysis.get("tcloud_products", [])
            priority = analysis.get("publish_priority", "")
            title = item.get("title", "")
            link = item.get("url", "") or item.get("link", "")
            social_rec = analysis.get("social_recommendation", "")

            if score < 5.0 or not link:
                continue
            if not (is_ai or is_cloud):
                continue

            action_type = _classify_social_action(item, analysis, source_key)
            if not action_type:
                continue

            # 获取该热点对应的社交帖子
            item_social_posts = social_posts_map.get(link, [])

            candidates.append({
                "title": title,
                "link": link,
                "score": score,
                "source_key": source_key,
                "action_type": action_type,
                "products": tcloud_products,
                "priority": priority,
                "social_rec": social_rec,
                "is_ai": is_ai,
                "is_cloud": is_cloud,
                "analysis": analysis,
                "social_posts": item_social_posts,
            })

    candidates.sort(key=lambda x: -x["score"])

    lines: List[str] = [
        "# 📱 社媒快速互动推荐",
        "",
        f"> {date_str} · 共 {len(candidates)} 条可互动热点",
        "",
        "**💡 操作方式**：直接点击下方链接跳转到 **X(Twitter) / Reddit / HN** 的原帖，",
        "在帖子下方评论或引用即可。渠道优先级：**X > Discord > Reddit**。",
        "",
        "---",
        "",
    ]

    for idx, c in enumerate(candidates[:15], start=1):
        action_emoji = {"转发": "🔄", "评论": "💬", "引用": "📝", "恭喜": "🎉"}.get(c["action_type"], "📱")
        product_tag = f" → {','.join(c['products'][:2])}" if c["products"] else ""
        lines.append(f"## {idx}. {action_emoji} {c['action_type']}：{c['title'][:60]}")
        lines.append("")
        lines.append(f"- 📊 评分：{c['score']:.1f}{product_tag} · 来源 `{c['source_key']}`")
        lines.append(f"- 🔗 原始链接：{c['link']}")
        lines.append("")

        # 显示找到的社交帖子
        twitter_posts = [p for p in c["social_posts"] if p["platform"] == "twitter"]
        reddit_posts = [p for p in c["social_posts"] if p["platform"] == "reddit"]

        if twitter_posts:
            lines.append("**🐦 Twitter/X 上的相关讨论：**")
            for tp in twitter_posts[:3]:
                author_tag = f" (@{tp['author']})" if tp.get("author") else ""
                lines.append(f"  - [{tp['title'][:60]}]({tp['url']}){author_tag}")
            lines.append("")

        if reddit_posts:
            lines.append("**🔴 Reddit 上的相关讨论：**")
            for rp in reddit_posts[:3]:
                sub_tag = f" (r/{rp['subreddit']})" if rp.get("subreddit") else ""
                lines.append(f"  - [{rp['title'][:60]}]({rp['url']}){sub_tag}")
            lines.append("")

        if not twitter_posts and not reddit_posts:
            # 如果没搜到社交帖子，仍然给出建议文案
            draft_text = _generate_social_draft(c)
            if draft_text:
                lines.append("**📝 建议发帖文案（暂未找到可直接互动的帖子）：**")
                lines.append(f"  > {draft_text}")
                lines.append("")
        else:
            # 有社交帖子时，也附上建议互动文案
            draft_text = _generate_social_draft(c)
            if draft_text:
                lines.append(f"**💡 建议互动文案**：{draft_text[:150]}")
                lines.append("")

        lines.append("---")
        lines.append("")

    if not candidates:
        lines.append("今天暂无适合快速互动的热点，建议关注明天的数据。")
        lines.append("")

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# KOC 数据库构建：聚合 GitHub 开发者 + 社交平台讨论者
# 输出统一格式的 KOC 候选列表
# ════════════════════════════════════════════════════════════════

def build_koc_database(
    results: List[Dict[str, Any]],
    github_koc: List[Dict[str, Any]],
    social_koc: List[Dict[str, Any]],
    platform_koc: List[Dict[str, Any]],
    generated_at: datetime,
) -> Dict[str, Any]:
    """构建统一的 KOC 数据库。

    整合三个来源：
    1. github_koc: GitHub 开源项目开发者（来自 enrich_github_developer_profiles）
    2. social_koc: 热点相关的社交帖子作者（来自 batch_search_social_posts）
    3. platform_koc: 主动搜索发现的云/AI 话题讨论者（来自 discover_koc_from_platforms）

    返回 {koc_records: [...], markdown: str, stats: {...}}
    """
    date_str = generated_at.strftime("%Y-%m-%d")
    koc_records: List[Dict[str, Any]] = []
    seen_keys: set = set()

    # ── 1. GitHub 开发者 ──
    for gk in github_koc:
        profile = gk.get("profile", {})
        username = gk.get("username", "")
        key = f"github:{username.lower()}"
        if key in seen_keys:
            continue
        seen_keys.add(key)

        # 评估 KOC 质量
        followers = profile.get("followers", 0)
        has_email = bool(profile.get("email"))
        has_twitter = bool(profile.get("twitter_username"))
        has_social = bool(profile.get("social_links"))

        # 综合评分（0-10）
        koc_score = _compute_koc_score(
            followers=followers,
            has_email=has_email,
            has_twitter=has_twitter,
            has_social=has_social,
            source="github",
            repos=profile.get("public_repos", 0),
        )

        # 联系方式汇总
        contacts = []
        if profile.get("email"):
            contacts.append(f"📧 {profile['email']}")
        if profile.get("twitter_username"):
            contacts.append(f"🐦 @{profile['twitter_username']}")
        for sl in profile.get("social_links", []):
            emoji = {"linkedin": "💼", "youtube": "📺", "mastodon": "🦣", "reddit": "🔴"}.get(sl["platform"], "🔗")
            contacts.append(f"{emoji} {sl['url']}")

        koc_records.append({
            "koc_name": profile.get("name") or username,
            "username": username,
            "source_platform": "GitHub",
            "profile_url": profile.get("profile_url", f"https://github.com/{username}"),
            "koc_type": "开源开发者" if not profile.get("is_org") else "开源组织",
            "bio": profile.get("bio", "")[:200],
            "location": profile.get("location", ""),
            "followers": followers,
            "public_repos": profile.get("public_repos", 0),
            "contact_info": " | ".join(contacts),
            "email": profile.get("email", ""),
            "twitter": profile.get("twitter_username", ""),
            "social_links": profile.get("social_links", []),
            "associated_project": gk.get("associated_item", {}).get("title", ""),
            "associated_url": gk.get("associated_item", {}).get("url", ""),
            "discovery_source": "GitHub热点关联",
            "koc_score": koc_score,
            "cooperation_angle": _suggest_cooperation_angle(profile, gk.get("associated_item", {})),
            "discovery_date": date_str,
        })

    # ── 2. 社交帖子作者（热点关联） ──
    for sk in social_koc:
        username = sk.get("username", "")
        platform = sk.get("source", "")
        key = f"{platform}:{username.lower()}"
        if key in seen_keys or not username:
            continue
        seen_keys.add(key)

        koc_records.append({
            "koc_name": username,
            "username": username,
            "source_platform": platform.capitalize(),
            "profile_url": sk.get("profile_url", ""),
            "koc_type": "社媒讨论者",
            "bio": sk.get("associated_post", {}).get("snippet", "")[:200],
            "location": "",
            "followers": 0,  # 需要进一步抓取
            "public_repos": 0,
            "contact_info": f"{'🐦' if platform == 'twitter' else '🔴'} {sk.get('profile_url', '')}",
            "email": "",
            "twitter": username if platform == "twitter" else "",
            "social_links": [{"platform": platform, "url": sk.get("profile_url", "")}],
            "associated_project": sk.get("associated_hotspot", {}).get("title", ""),
            "associated_url": sk.get("associated_hotspot", {}).get("url", ""),
            "discovery_source": f"热点讨论帖({platform})",
            "koc_score": 3.0,  # 基础分，待后续丰富
            "cooperation_angle": f"在{platform}上讨论了相关热点，可作为社媒互动切入点",
            "discovery_date": date_str,
        })

    # ── 3. 平台主动发现的 KOC ──
    for pk in platform_koc:
        username = pk.get("username", "")
        platform = pk.get("source", "")
        key = f"{platform}:{username.lower()}"
        if key in seen_keys or not username:
            continue
        seen_keys.add(key)

        koc_records.append({
            "koc_name": username,
            "username": username,
            "source_platform": platform.capitalize(),
            "profile_url": pk.get("profile_url", ""),
            "koc_type": pk.get("focus", "行业讨论者"),
            "bio": pk.get("snippet", "")[:200],
            "location": "",
            "followers": 0,
            "public_repos": 0,
            "contact_info": f"{'🐦' if platform == 'twitter' else '🔴'} {pk.get('profile_url', '')}",
            "email": "",
            "twitter": username if platform == "twitter" else "",
            "social_links": [{"platform": platform, "url": pk.get("profile_url", "")}],
            "associated_project": pk.get("post_title", ""),
            "associated_url": pk.get("post_url", ""),
            "discovery_source": f"主动搜索({pk.get('focus', '')})",
            "koc_score": 2.5,
            "cooperation_angle": f"关注{pk.get('focus', '云/AI')}领域，可进一步评估合作价值",
            "discovery_date": date_str,
        })

    # 按 KOC 评分降序排列
    koc_records.sort(key=lambda x: -x["koc_score"])

    # 生成 Markdown 报告
    markdown = _build_koc_markdown(koc_records, date_str)

    stats = {
        "total": len(koc_records),
        "from_github": sum(1 for r in koc_records if r["source_platform"] == "GitHub"),
        "from_twitter": sum(1 for r in koc_records if r["source_platform"] == "Twitter"),
        "from_reddit": sum(1 for r in koc_records if r["source_platform"] == "Reddit"),
        "with_email": sum(1 for r in koc_records if r["email"]),
        "with_twitter": sum(1 for r in koc_records if r["twitter"]),
        "high_quality": sum(1 for r in koc_records if r["koc_score"] >= 6),
    }

    return {"koc_records": koc_records, "markdown": markdown, "stats": stats}


def _compute_koc_score(
    followers: int = 0,
    has_email: bool = False,
    has_twitter: bool = False,
    has_social: bool = False,
    source: str = "",
    repos: int = 0,
) -> float:
    """计算 KOC 综合评分 (0-10)"""
    score = 0.0

    # 影响力维度 (0-4)
    if followers >= 10000:
        score += 4.0
    elif followers >= 1000:
        score += 3.0
    elif followers >= 100:
        score += 2.0
    elif followers >= 10:
        score += 1.0

    # 可触达性维度 (0-3)
    if has_email:
        score += 1.5
    if has_twitter:
        score += 1.0
    if has_social:
        score += 0.5

    # 内容产出维度 (0-2)
    if repos >= 50:
        score += 2.0
    elif repos >= 20:
        score += 1.5
    elif repos >= 5:
        score += 1.0

    # 来源加成 (0-1)
    if source == "github":
        score += 1.0  # GitHub 开发者可验证度高

    return min(score, 10.0)


def _suggest_cooperation_angle(profile: dict, associated_item: dict) -> str:
    """根据开发者画像推荐合作切入角度"""
    name = profile.get("name") or profile.get("github_username", "")
    bio = (profile.get("bio") or "").lower()
    followers = profile.get("followers", 0)
    twitter = profile.get("twitter_username", "")
    project_title = associated_item.get("title", "")

    angles = []

    # 基于影响力
    if followers >= 1000:
        angles.append(f"高影响力开发者({followers} followers)，适合深度合作")
    elif followers >= 100:
        angles.append(f"活跃开发者({followers} followers)，适合技术内容共创")

    # 基于 bio 关键词
    if any(k in bio for k in ["cloud", "devops", "infrastructure", "serverless"]):
        angles.append("bio 显示云/DevOps 背景，可推荐腾讯云产品试用")
    if any(k in bio for k in ["ai", "ml", "machine learning", "deep learning", "llm"]):
        angles.append("AI/ML 背景，可推荐 GPU CVM / Hunyuan 试用")
    if any(k in bio for k in ["indie", "maker", "freelance", "solo"]):
        angles.append("独立开发者/Maker，适合 Lighthouse 推广合作")

    # 基于有 Twitter
    if twitter:
        angles.append(f"有 Twitter(@{twitter})，可直接 DM 建联")

    # 基于关联项目
    if project_title:
        angles.append(f"关联项目：{project_title[:40]}")

    if not angles:
        angles.append("建议进一步了解后判断合作方向")

    return " | ".join(angles[:3])


def _build_koc_markdown(koc_records: list, date_str: str) -> str:
    """生成 KOC 库的 Markdown 报告"""
    lines: List[str] = [
        "# 🤝 KOC 建联候选库",
        "",
        f"> {date_str} · 共发现 {len(koc_records)} 位 KOC 候选人",
        "",
        "**用途**：以下是从今日热点中自动提取的潜在合作对象，",
        "包括 GitHub 开源开发者、Twitter/Reddit 活跃讨论者。",
        "可根据评分和联系方式优先推进高价值 KOC 建联。",
        "",
        "---",
        "",
    ]

    # 高质量 KOC（评分 >= 6）
    high_quality = [r for r in koc_records if r["koc_score"] >= 6]
    if high_quality:
        lines.append("## ⭐ 高价值 KOC（评分 ≥ 6）")
        lines.append("")
        for idx, koc in enumerate(high_quality, 1):
            lines.append(f"### {idx}. {koc['koc_name']} ({koc['source_platform']})")
            lines.append("")
            lines.append(f"- 🔗 主页：{koc['profile_url']}")
            lines.append(f"- 📊 评分：{koc['koc_score']:.1f} · 类型：{koc['koc_type']}")
            if koc["followers"]:
                lines.append(f"- 👥 Followers：{koc['followers']}")
            if koc["bio"]:
                lines.append(f"- 📝 Bio：{koc['bio'][:100]}")
            if koc["contact_info"]:
                lines.append(f"- 📬 联系方式：{koc['contact_info']}")
            if koc["cooperation_angle"]:
                lines.append(f"- 💡 合作切入角度：{koc['cooperation_angle']}")
            if koc["associated_project"]:
                lines.append(f"- 🔗 关联项目：[{koc['associated_project'][:50]}]({koc['associated_url']})")
            lines.append("")
        lines.append("---")
        lines.append("")

    # GitHub 开发者
    github_kocs = [r for r in koc_records if r["source_platform"] == "GitHub" and r["koc_score"] < 6]
    if github_kocs:
        lines.append("## 🐙 GitHub 开源开发者")
        lines.append("")
        for koc in github_kocs[:10]:
            contact = koc["contact_info"][:60] if koc["contact_info"] else "暂无公开联系方式"
            lines.append(
                f"- **{koc['koc_name']}** ({koc['followers']} followers) "
                f"— [{koc['username']}]({koc['profile_url']}) · {contact}"
            )
        lines.append("")

    # 社交平台 KOC
    social_kocs = [r for r in koc_records if r["source_platform"] in ("Twitter", "Reddit")]
    if social_kocs:
        lines.append("## 🌐 社交平台活跃讨论者")
        lines.append("")
        for koc in social_kocs[:10]:
            lines.append(
                f"- **{koc['koc_name']}** ({koc['source_platform']}) "
                f"— [{koc['profile_url']}]({koc['profile_url']}) · {koc['discovery_source']}"
            )
        lines.append("")

    if not koc_records:
        lines.append("今日暂未发现 KOC 候选人。")
        lines.append("")

    return "\n".join(lines)


def _classify_social_action(item: Dict[str, Any], analysis: Dict[str, Any], source_key: str) -> str:
    """判断一条热点适合什么类型的社媒互动。

    核心原则：社媒互动 = 去社媒平台上互动，不是转发 GitHub 链接。
    只有来自社媒平台的帖子、或已找到社媒讨论帖的热点才推荐互动。
    """
    title = (item.get("title", "") or "").lower()
    topic_type = analysis.get("topic_type", "")
    platform = item.get("platform", "")

    # ── 来自社媒平台的原帖：直接推荐互动 ──
    if source_key == "twitter_reddit" or platform in ("twitter", "reddit", "hackernews"):
        # 产品发布 → 恭喜
        if topic_type == "产品发布" or any(kw in title for kw in ["launch", "release", "announce"]):
            return "恭喜"
        # 竞品动态 → 评论（抢评论区）
        if any(kw in title for kw in ["aws", "azure", "gcp", "cloudflare", "vercel"]):
            return "评论"
        # AI / 部署相关 → 引用
        if topic_type in ("技术分享", "部署教程") or any(kw in title for kw in ["deploy", "gpu", "llm", "ai"]):
            return "引用"
        # 默认评论
        return "评论"

    # ── ProductHunt 新品 → 恭喜（PH 本身就是社交平台）──
    if source_key == "producthunt":
        return "恭喜"

    # ── HN 帖子 → 评论（HN 有评论区可互动）──
    if source_key == "hackernews":
        return "评论"

    # ── GitHub / HuggingFace 等非社媒来源：不推荐"转发"
    # 只有当该热点已经有社交帖子讨论时才推荐（在 build_social_quick_actions 中处理）
    return ""


def _generate_social_draft(candidate: Dict[str, Any], platform: str = "x") -> str:
    """生成建议的社媒互动文案"""
    action = candidate["action_type"]
    title = candidate["title"][:50]
    products = candidate.get("products", [])
    product_str = products[0] if products else ""
    source = candidate["source_key"]

    if action == "恭喜":
        if product_str:
            return f"Congrats on the launch! 🎉 Great to see innovation in this space. We've been building similar capabilities with {product_str} at Tencent Cloud — would love to explore synergies!"
        return f"Congrats on the launch! 🎉 Exciting to see this kind of innovation. Looking forward to what comes next!"

    if action == "转发":
        if product_str:
            return f"Interesting project! 🔥 For those looking to deploy this, check out Tencent Cloud {product_str} for a seamless experience. #CloudComputing #AI"
        return f"Great find! 🔥 This is exactly the kind of innovation driving the cloud-native ecosystem forward. #TechTrends"

    if action == "引用":
        if product_str:
            return f"Great technical deep dive! At Tencent Cloud, we tackle similar challenges with {product_str}. Here's our take on this approach... #CloudNative"
        return f"Insightful perspective on this topic. The cloud infrastructure implications are significant. #CloudComputing"

    if action == "评论":
        if product_str:
            return f"Interesting move! At Tencent Cloud International, we offer {product_str} as a competitive alternative with unique advantages for global developers."
        return f"Watching this space closely. The competition drives better outcomes for developers worldwide. 🌍"

    if platform == "discord":
        return f"Hey everyone! Just came across this: {title}. Thoughts? 💭"

    return ""


def _drafts_to_iwiki_html(product_drafts: Dict[str, str], date_str: str) -> str:
    """将产品 Drafts 转换为 iWiki 可导入的 HTML 格式。

    本地版本：生成 HTML 文件，待有 iWiki Token 后可通过 API 推送。
    推送方式：POST https://iwiki.woa.com/api/v1/pages/{page_id}/children
    Headers: Authorization: Bearer {IWIKI_TOKEN}
    """
    lines = [
        "<!DOCTYPE html>",
        "<html><head><meta charset='utf-8'>",
        f"<title>产品 Draft 汇总 — {date_str}</title>",
        "<style>body{font-family:sans-serif;max-width:900px;margin:40px auto;padding:20px;line-height:1.6}",
        "h1{color:#1a73e8;border-bottom:2px solid #1a73e8;padding-bottom:8px}",
        "h2{color:#333;margin-top:32px;padding:8px 12px;background:#f0f4ff;border-left:4px solid #1a73e8}",
        "h3{color:#666;margin-top:16px}",
        ".meta{background:#f5f5f5;padding:8px 12px;border-radius:6px;font-size:14px;color:#666}",
        ".checklist{background:#fff3cd;padding:12px;border-radius:6px;border:1px solid #ffc107}",
        ".checklist li{margin:4px 0}",
        "hr{border:none;border-top:1px solid #ddd;margin:24px 0}",
        "</style></head><body>",
        f"<h1>📋 产品 Draft 汇总 — {date_str}</h1>",
        f"<div class='meta'>共 {len(product_drafts)} 个产品 · 状态：<strong>待审核</strong></div>",
        "<hr>",
    ]

    for product_name, draft_content in sorted(product_drafts.items()):
        # 简单 Markdown → HTML 转换
        html_content = draft_content
        html_content = re.sub(r"^### (.+)$", r"<h3>\1</h3>", html_content, flags=re.MULTILINE)
        html_content = re.sub(r"^## (.+)$", r"<h2>\1</h2>", html_content, flags=re.MULTILINE)
        html_content = re.sub(r"^# (.+)$", r"<h1>\1</h1>", html_content, flags=re.MULTILINE)
        html_content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html_content)
        html_content = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', html_content)
        html_content = re.sub(r"^- \[[ x]\] (.+)$", r"<li>\1</li>", html_content, flags=re.MULTILINE)
        html_content = re.sub(r"^- (.+)$", r"<li>\1</li>", html_content, flags=re.MULTILINE)
        html_content = re.sub(r"---", "<hr>", html_content)
        html_content = html_content.replace("\n\n", "</p><p>").replace("\n", "<br>")
        lines.append(html_content)

    lines.extend([
        "<hr>",
        "<p style='text-align:center;color:#999;font-size:12px'>",
        f"自动生成于 {date_str} · 待审核后可发布",
        "</p>",
        "</body></html>",
    ])

    return "\n".join(lines)


def _drafts_to_wecom_json(product_drafts: Dict[str, str], date_str: str) -> Dict[str, Any]:
    """将产品 Drafts 转换为腾讯文档（企业微信智能表格）可写入的 JSON 格式。

    本地版本：生成 JSON 文件，待有 Webhook URL 后可推送。
    推送方式：POST {WECOM_WEBHOOK_URL}
    Body: {"records": [...]}
    """
    records = []
    for product_name, draft_content in sorted(product_drafts.items()):
        # 提取关键信息
        item_count = draft_content.count("## ")
        has_p0 = "P0" in draft_content
        has_p1 = "P1" in draft_content

        records.append({
            "fields": {
                "产品名称": product_name,
                "日期": date_str,
                "Draft状态": "待审核",
                "优先级": "P0" if has_p0 else "P1",
                "关联热点数": item_count,
                "Draft内容": draft_content[:2000],  # 腾讯文档字段限制
                "审核意见": "",
                "审核人": "",
            }
        })

    return {
        "description": f"产品 Draft 汇总 — {date_str}",
        "record_count": len(records),
        "records": records,
        "integration_hints": {
            "iwiki": {
                "api": "POST https://iwiki.woa.com/api/v1/pages/{page_id}/children",
                "auth": "Bearer {IWIKI_TOKEN}",
                "note": "需要在环境变量中设置 IWIKI_TOKEN 和 IWIKI_PAGE_ID",
            },
            "wecom_doc": {
                "api": "POST {WECOM_WEBHOOK_URL}",
                "note": "需要在环境变量中设置 WECOM_DRAFT_WEBHOOK_URL",
            },
        },
    }


def write_outputs(
    output_dir: Path,
    results: List[Dict[str, Any]],
    generated_at: datetime,
    social_posts_map: Optional[Dict[str, list]] = None,
    koc_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Path]:
    paths = ensure_directories(output_dir, generated_at)
    date_str = generated_at.strftime("%Y-%m-%d")
    stamp = generated_at.strftime("%Y-%m-%d_%H%M")
    archive_csv = paths["archive_dir"] / f"每日热点汇总表_{stamp}.csv"
    latest_csv = output_dir / "今日热点汇总表.csv"
    dated_csv = output_dir / f"每日热点汇总表_{date_str}.csv"
    archive_json = paths["archive_dir"] / f"每日热点追踪_{stamp}.json"
    latest_json = output_dir / "今日热点追踪.json"
    dated_json = output_dir / f"每日热点追踪_{date_str}.json"
    archive_writing_md = paths["archive_dir"] / f"每日热点内容写作包_{stamp}.md"
    latest_writing_md = output_dir / "今日热点内容写作包.md"
    dated_writing_md = output_dir / f"每日热点内容写作包_{date_str}.md"

    payload = {
        "generated_at": generated_at.isoformat(),
        "results": results,
    }
    csv_rows = build_csv_rows(results)
    payload_text = json.dumps(payload, ensure_ascii=False, indent=2)
    writing_pack_text = build_writing_pack_markdown(results, generated_at)

    write_csv_file(archive_csv, csv_rows)
    write_csv_file(latest_csv, csv_rows)
    write_csv_file(dated_csv, csv_rows)
    archive_json.write_text(payload_text, encoding="utf-8")
    latest_json.write_text(payload_text, encoding="utf-8")
    dated_json.write_text(payload_text, encoding="utf-8")
    archive_writing_md.write_text(writing_pack_text, encoding="utf-8")
    latest_writing_md.write_text(writing_pack_text, encoding="utf-8")
    dated_writing_md.write_text(writing_pack_text, encoding="utf-8")

    for result in results:
        raw_path = paths["raw_dir"] / f"{result['key']}.json"
        raw_path.write_text(json.dumps(result["items"], ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 产品 Draft 输出（分产品的内容草稿，供审核） ──
    drafts_dir = output_dir / "drafts" / date_str
    drafts_dir.mkdir(parents=True, exist_ok=True)
    product_drafts = build_product_drafts(results, generated_at)
    for product_name, draft_content in product_drafts.items():
        safe_name = product_name.replace("/", "_").replace(" ", "_")
        draft_path = drafts_dir / f"Draft_{safe_name}_{date_str}.md"
        draft_path.write_text(draft_content, encoding="utf-8")
        print(f"  📝 Draft: {draft_path}", file=sys.stderr)

    # ── 合并版 Draft 审核文件（供本地审核 / iWiki / 腾讯文档） ──
    if product_drafts:
        combined_lines = [
            f"# 📋 产品 Draft 汇总 — {date_str}",
            "",
            f"> 共 {len(product_drafts)} 个产品的 P0/P1 内容草稿",
            f"> 状态：**待审核** · 审核后可推送至 iWiki 或腾讯文档",
            "",
            "---",
            "",
        ]
        for product_name, draft_content in sorted(product_drafts.items()):
            combined_lines.append(draft_content)
            combined_lines.append("")
        combined_text = "\n".join(combined_lines)

        # 本地审核文件
        review_dir = output_dir / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        review_path = review_dir / f"Draft_Review_{date_str}.md"
        review_path.write_text(combined_text, encoding="utf-8")
        latest_review = review_dir / "Draft_Review_Latest.md"
        latest_review.write_text(combined_text, encoding="utf-8")
        print(f"  📋 Review: {review_path}", file=sys.stderr)

        # iWiki HTML 版本（本地保存，待有 Token 后可推送）
        iwiki_review_dir = output_dir / "review" / "iwiki"
        iwiki_review_dir.mkdir(parents=True, exist_ok=True)
        iwiki_html = _drafts_to_iwiki_html(product_drafts, date_str)
        iwiki_path = iwiki_review_dir / f"Draft_iWiki_{date_str}.html"
        iwiki_path.write_text(iwiki_html, encoding="utf-8")
        print(f"  📄 iWiki HTML: {iwiki_path}", file=sys.stderr)

        # 腾讯文档 JSON 版本（本地保存，待有 Webhook 后可推送）
        wecom_review_dir = output_dir / "review" / "wecom"
        wecom_review_dir.mkdir(parents=True, exist_ok=True)
        wecom_json = _drafts_to_wecom_json(product_drafts, date_str)
        wecom_path = wecom_review_dir / f"Draft_WeCom_{date_str}.json"
        wecom_path.write_text(
            json.dumps(wecom_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  📊 WeCom JSON: {wecom_path}", file=sys.stderr)

    # ── 社媒快速互动推荐（含原帖链接） ──
    social_text = build_social_quick_actions(results, generated_at, social_posts_map)
    social_path = output_dir / f"社媒互动推荐_{date_str}.md"
    social_path.write_text(social_text, encoding="utf-8")
    latest_social = output_dir / "今日社媒互动推荐.md"
    latest_social.write_text(social_text, encoding="utf-8")
    print(f"  📱 Social: {social_path}", file=sys.stderr)

    # ── KOC 数据库输出 ──
    koc_paths = {}
    if koc_data and koc_data.get("koc_records"):
        koc_md_path = output_dir / f"KOC建联候选库_{date_str}.md"
        koc_md_path.write_text(koc_data["markdown"], encoding="utf-8")
        latest_koc_md = output_dir / "今日KOC建联候选库.md"
        latest_koc_md.write_text(koc_data["markdown"], encoding="utf-8")

        koc_json_path = output_dir / f"KOC建联候选库_{date_str}.json"
        koc_json_path.write_text(
            json.dumps(koc_data["koc_records"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        latest_koc_json = output_dir / "今日KOC建联候选库.json"
        latest_koc_json.write_text(
            json.dumps(koc_data["koc_records"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # KOC CSV（匹配用户现有维度）
        koc_csv_path = output_dir / f"KOC建联候选库_{date_str}.csv"
        _write_koc_csv(koc_csv_path, koc_data["koc_records"])

        koc_paths = {
            "koc_md": koc_md_path,
            "koc_json": koc_json_path,
            "koc_csv": koc_csv_path,
        }
        print(
            f"  🤝 KOC: {koc_md_path} ({koc_data['stats']['total']} candidates)",
            file=sys.stderr,
        )

    return {
        "csv_report": archive_csv,
        "latest_csv": latest_csv,
        "latest_json": latest_json,
        "dated_csv": dated_csv,
        "dated_json": dated_json,
        "latest_writing_md": latest_writing_md,
        "dated_writing_md": dated_writing_md,
        "drafts_dir": drafts_dir,
        "review_dir": output_dir / "review",
        "social_md": social_path,
        **koc_paths,
    }


def _write_koc_csv(path: Path, koc_records: list) -> None:
    """输出 KOC 候选库 CSV，维度对齐用户现有 KOC 管理表"""
    headers = [
        "站长/KOC名称",
        "链接",
        "站长类型",
        "内容定位",
        "覆盖地域",
        "覆盖平台",
        "核心受众",
        "社媒表现",
        "联系方式",
        "导流形式",
        "内容参考",
        "当前主要推广项目",
        "已知推广激励（市场参考）",
        "备注",
        "内容相关性",
        "用户质量",
        "导流意图",
        "合作可执行性",
        "综合评分",
    ]
    rows = [headers]
    for koc in koc_records:
        # 根据画像推断维度
        location = koc.get("location", "")
        platform = koc.get("source_platform", "")
        followers = koc.get("followers", 0)
        bio = koc.get("bio", "")

        # 社媒表现
        social_perf = ""
        if followers >= 1000:
            social_perf = f"{followers} followers"
        elif followers > 0:
            social_perf = f"{followers} followers"
        if koc.get("public_repos"):
            social_perf += f", {koc['public_repos']} repos"

        # 内容相关性评估
        content_relevance = "中"
        if koc.get("koc_score", 0) >= 7:
            content_relevance = "高"
        elif koc.get("koc_score", 0) >= 4:
            content_relevance = "中"
        else:
            content_relevance = "低"

        # 合作可执行性
        executability = "低"
        if koc.get("email"):
            executability = "高"
        elif koc.get("twitter"):
            executability = "中"

        rows.append([
            koc.get("koc_name", ""),
            koc.get("profile_url", ""),
            koc.get("koc_type", ""),
            bio[:80] if bio else "",
            location or "待确认",
            platform,
            "开发者" if platform == "GitHub" else "技术讨论者",
            social_perf,
            koc.get("contact_info", ""),
            "技术内容/教程" if platform == "GitHub" else "社媒讨论",
            koc.get("associated_project", "")[:50],
            "",
            "",
            koc.get("cooperation_angle", ""),
            content_relevance,
            "高" if followers >= 100 else "中" if followers >= 10 else "待评估",
            "高" if koc.get("email") else "中",
            executability,
            f"{koc.get('koc_score', 0):.1f}",
        ])

    write_csv_file(path, rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate daily hotspot report with content analysis.")
    parser.add_argument(
        "--output-dir",
        default=str(Path.home() / "Desktop" / "每日热点追踪"),
        help="Directory for reports and archives.",
    )
    parser.add_argument(
        "--iwiki",
        action="store_true",
        default=False,
        help="Enable iWiki auto-update (writes to iwiki.woa.com).",
    )
    parser.add_argument(
        "--iwiki-dry-run",
        action="store_true",
        default=False,
        help="Generate iWiki content but don't actually write (saves HTML locally).",
    )
    parser.add_argument(
        "--iwiki-page-id",
        default="4018874163",
        help="iWiki page ID to update.",
    )
    # 企业微信腾讯文档（智能表格）参数
    parser.add_argument(
        "--wecom-doc",
        action="store_true",
        default=False,
        help="Enable writing to WeCom (企业微信) Smart Sheet via Webhook.",
    )
    parser.add_argument(
        "--wecom-doc-dry-run",
        action="store_true",
        default=False,
        help="Generate WeCom doc data but don't actually write (saves JSON locally).",
    )
    parser.add_argument(
        "--wecom-doc-force",
        action="store_true",
        default=False,
        help="Force writing to WeCom Smart Sheet even if already written today (skip duplicate check).",
    )
    parser.add_argument(
        "--wecom-webhook-url",
        default=None,
        help="WeCom Smart Sheet Webhook URL (overrides env/config file).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    generated_at = datetime.now()
    output_dir = Path(args.output_dir).expanduser()

    print("[daily_hotspot_agent] start fetching sources...", file=sys.stderr)
    results: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(SOURCE_SPECS))) as executor:
        futures = [executor.submit(fetch_source, spec) for spec in SOURCE_SPECS]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            item_count = len(result["items"])
            status = "✅" if item_count >= MIN_ITEMS_PER_SOURCE else f"⚠️ ({item_count}<{MIN_ITEMS_PER_SOURCE})"
            print(
                f"[daily_hotspot_agent] {result['key']} => {item_count} items {status}",
                file=sys.stderr,
            )

    ordered_results = sorted(results, key=lambda item: item["index"])

    # 深度内容抓取：为每条热点抓取原始内容（GitHub README / 社区全文等）
    print("[daily_hotspot_agent] enriching items with deep content...", file=sys.stderr)
    enrich_items_with_deep_content(ordered_results)

    # 内容分析标注
    print("[daily_hotspot_agent] running content analysis...", file=sys.stderr)
    analyze_results(ordered_results)

    # 过滤：移除既非 AI 相关也非云行业相关的条目
    filter_stats = filter_non_ai_non_cloud_items(ordered_results)
    print(
        "[daily_hotspot_agent] filtered non-AI/non-cloud items: "
        f"removed={filter_stats['removed_count']}, kept={filter_stats['kept_count']}",
        file=sys.stderr,
    )

    # ── 社交帖子搜索：为高分热点搜索 Twitter/Reddit 上的相关讨论帖 ──
    print("[daily_hotspot_agent] searching social posts for hotspots...", file=sys.stderr)
    social_data = batch_search_social_posts(ordered_results)
    social_posts_map = social_data.get("social_map", {})
    social_koc = social_data.get("koc_from_social", [])

    # ── GitHub 开发者画像增强（KOC 建联基础） ──
    print("[daily_hotspot_agent] enriching GitHub developer profiles...", file=sys.stderr)
    github_koc: List[Dict[str, Any]] = []
    for result in ordered_results:
        if result.get("key") == "github" and not result.get("error"):
            koc_candidates = enrich_github_developer_profiles(result.get("items", []))
            github_koc.extend(koc_candidates)
    print(f"[daily_hotspot_agent] found {len(github_koc)} GitHub KOC candidates", file=sys.stderr)

    # ── 公开平台 KOC 发现 ──
    print("[daily_hotspot_agent] discovering KOC from public platforms...", file=sys.stderr)
    platform_koc = discover_koc_from_platforms()

    # ── 构建统一 KOC 数据库 ──
    koc_data = build_koc_database(
        ordered_results, github_koc, social_koc, platform_koc, generated_at
    )
    print(
        f"[daily_hotspot_agent] KOC database: {koc_data['stats']['total']} total "
        f"(GitHub={koc_data['stats']['from_github']}, "
        f"Twitter={koc_data['stats']['from_twitter']}, "
        f"Reddit={koc_data['stats']['from_reddit']})",
        file=sys.stderr,
    )

    # 输出文件
    output_paths = write_outputs(
        output_dir, ordered_results, generated_at,
        social_posts_map=social_posts_map,
        koc_data=koc_data,
    )
    print(str(output_paths["dated_csv"]))

    # iWiki 写入
    if args.iwiki or args.iwiki_dry_run:
        print("[daily_hotspot_agent] preparing iWiki content...", file=sys.stderr)
        if args.iwiki_dry_run:
            html_path = save_iwiki_html_locally(ordered_results, generated_at, str(output_dir))
            print(f"[daily_hotspot_agent] iWiki HTML saved to: {html_path}", file=sys.stderr)
        else:
            result = update_iwiki_page(
                ordered_results,
                generated_at,
                page_id=args.iwiki_page_id,
            )
            if result["success"]:
                print(f"[daily_hotspot_agent] ✅ iWiki updated: {result['wiki_url']}", file=sys.stderr)
            else:
                print(f"[daily_hotspot_agent] ⚠️ iWiki update: {result['message']}", file=sys.stderr)
                # 即使 API 失败，也保存本地 HTML
                html_path = save_iwiki_html_locally(ordered_results, generated_at, str(output_dir))
                print(f"[daily_hotspot_agent] iWiki HTML backup: {html_path}", file=sys.stderr)

    # 企业微信腾讯文档（智能表格）写入
    if args.wecom_doc or args.wecom_doc_dry_run:
        print("[daily_hotspot_agent] preparing WeCom Smart Sheet data...", file=sys.stderr)
        wecom_result = write_to_wecom_doc(
            ordered_results,
            generated_at,
            webhook_url=args.wecom_webhook_url,
            dry_run=args.wecom_doc_dry_run,
            force=args.wecom_doc_force,
        )
        if wecom_result["success"]:
            print(f"[daily_hotspot_agent] ✅ WeCom Doc: {wecom_result['message']}", file=sys.stderr)
        else:
            print(f"[daily_hotspot_agent] ⚠️ WeCom Doc: {wecom_result['message']}", file=sys.stderr)

        # 精选话题自动写入（如果已配置精选话题子表的 Webhook）
        print("[daily_hotspot_agent] checking selected topics...", file=sys.stderr)
        selected_result = write_selected_topics(
            ordered_results,
            generated_at,
            dry_run=args.wecom_doc_dry_run,
            force=args.wecom_doc_force,
        )
        if selected_result["success"]:
            print(f"[daily_hotspot_agent] ⭐ Selected: {selected_result['message']}", file=sys.stderr)
        else:
            print(f"[daily_hotspot_agent] ⚠️ Selected: {selected_result['message']}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
