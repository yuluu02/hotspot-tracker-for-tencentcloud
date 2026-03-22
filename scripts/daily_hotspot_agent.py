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
        "# 每日热点内容写作包",
        "",
        f"> 生成时间：{generated_at.strftime('%Y-%m-%d %H:%M')}",
        "> 外部依据仅使用：`https://www.tencentcloud.com/act/pro/intl-openclaw` 与 `https://www.tencentcloud.com/zh`",
        "> 目的：把热点直接转成腾讯云国际站官方号可发的英文内容包，而不是停留在资讯整理。",
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

        lines.extend([
            f"## {idx}. {strip_title_prefix(item.get('title', '未命名条目'), candidate['key'])}",
            "",
            f"**来源**：{candidate['label']} / `{candidate['key']}`",
            "",
            f"**具体内容**：{analysis.get('detail_brief', '')}",
            "",
            "### 📋 精选话题结论",
            "",
        ])

        # 输出结构化结论
        editorial_brief = analysis.get("editorial_brief", "")
        if editorial_brief:
            for brief_line in editorial_brief.split("\n"):
                if brief_line.strip():
                    lines.append(f"- {brief_line.strip()}")
        else:
            lines.append(f"- 话题类型：{analysis.get('topic_type', '')}")
            lines.append(f"- 可用角度：{analysis.get('editorial_angles_text', '')}")
            lines.append(f"- 优先级：{analysis.get('publish_priority', '')}")
            lines.append(f"- 适合平台：{analysis.get('recommended_platforms_text', '')}")

        lines.extend([
            "",
            "### 🔗 腾讯云结合点",
            "",
            f"- 主推产品：{analysis.get('official_product_focus', '') or '待人工判断'}",
            f"- 关联类型：{analysis.get('tcloud_relation_type', '') or '待人工判断'}",
            f"- 结合说明：{analysis.get('tcloud_integration', '')}",
            f"- 配图建议：{analysis.get('visual_brief', '')}",
            "",
            "---",
            "",
        ])

    return "\n".join(lines)



def write_outputs(
    output_dir: Path,
    results: List[Dict[str, Any]],
    generated_at: datetime,
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

    return {
        "csv_report": archive_csv,
        "latest_csv": latest_csv,
        "latest_json": latest_json,
        "dated_csv": dated_csv,
        "dated_json": dated_json,
        "latest_writing_md": latest_writing_md,
        "dated_writing_md": dated_writing_md,
    }


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

    # 输出文件
    output_paths = write_outputs(output_dir, ordered_results, generated_at)
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
