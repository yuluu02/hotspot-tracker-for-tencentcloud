#!/usr/bin/env python3
"""
静态站点数据生成器
==================
将 output/ 目录中的 CSV / Markdown 数据转换为前端可直接加载的 JSON 文件，
输出到 docs/ 目录，可直接部署到 GitHub Pages / Vercel / Cloudflare Pages 等静态托管。

用法：
    python3 generate_static.py                     # 默认使用 ./output
    python3 generate_static.py --output-dir ./output --site-dir ./docs
"""

import csv
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTPUT_DIR = os.path.join(BASE_DIR, "output")
DEFAULT_SITE_DIR = os.path.join(BASE_DIR, "docs")


def read_csv_rows(csv_path):
    """读取 CSV 为字典列表"""
    rows = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(dict(row))
    return rows


def read_text_file(path):
    """读取文本文件"""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return None


def find_csvs(output_dir):
    """查找所有日期的 CSV 文件，返回 {date_str: csv_path}"""
    import glob
    result = {}

    # 直接在 output 目录中找
    for f in glob.glob(os.path.join(output_dir, "每日热点汇总表_*.csv")):
        basename = os.path.basename(f)
        date_str = basename.replace("每日热点汇总表_", "").replace(".csv", "")[:10]
        if len(date_str) == 10 and date_str[4] == "-":
            # 同一个日期可能有多个文件，取最新的
            if date_str not in result or f > result[date_str]:
                result[date_str] = f

    # 在 archive 子目录中找
    archive_dir = os.path.join(output_dir, "archive")
    if os.path.isdir(archive_dir):
        for d in os.listdir(archive_dir):
            date_dir = os.path.join(archive_dir, d)
            if not os.path.isdir(date_dir):
                continue
            for f in glob.glob(os.path.join(date_dir, "每日热点汇总表_*.csv")):
                date_str = d[:10]
                if date_str not in result or f > result[date_str]:
                    result[date_str] = f

    return result


def compute_stats(records):
    """计算统计数据"""
    total = len(records)
    ai_count = sum(1 for r in records if r.get("是否AI相关") == "是")
    cloud_count = sum(1 for r in records if r.get("是否云行业") == "是")
    high_score = sum(1 for r in records if safe_float(r.get("综合评分")) >= 7)
    p0_count = sum(1 for r in records if (r.get("发布优先级") or "").startswith("P0"))
    p1_count = sum(1 for r in records if (r.get("发布优先级") or "").startswith("P1"))

    # 来源分布
    sources = {}
    for r in records:
        src = r.get("来源Key", "unknown")
        sources[src] = sources.get(src, 0) + 1

    # 技术标签统计
    tech_counts = {}
    for r in records:
        for tag in (r.get("技术标签") or "").split(","):
            tag = tag.strip()
            if tag:
                tech_counts[tag] = tech_counts.get(tag, 0) + 1
    tech_top = sorted(tech_counts.items(), key=lambda x: -x[1])[:15]

    return {
        "total": total,
        "ai_count": ai_count,
        "cloud_count": cloud_count,
        "high_score_count": high_score,
        "p0_count": p0_count,
        "p1_count": p1_count,
        "sources": sources,
        "tech_top": tech_top,
    }


def safe_float(val):
    """安全转 float"""
    try:
        return float(val or 0)
    except (ValueError, TypeError):
        return 0.0


def filter_selected(records):
    """筛选精选话题"""
    selected = []
    for r in records:
        priority = r.get("发布优先级", "") or ""
        tc_point = (r.get("腾讯云结合点", "") or "").strip()
        score = safe_float(r.get("综合评分"))
        if (priority.startswith("P0") or priority.startswith("P1")) and len(tc_point) > 5 and tc_point != "无":
            selected.append(r)
    selected.sort(key=lambda r: -safe_float(r.get("综合评分")))
    return selected


def generate_site(output_dir, site_dir):
    """生成静态站点数据"""
    print(f"📂 数据源目录: {output_dir}")
    print(f"📁 静态站点目录: {site_dir}")

    # 创建 data 子目录
    data_dir = os.path.join(site_dir, "data")
    os.makedirs(data_dir, exist_ok=True)

    # 找到所有日期的 CSV
    csvs = find_csvs(output_dir)
    if not csvs:
        print("⚠️  未找到任何 CSV 数据文件")
        return

    dates = sorted(csvs.keys(), reverse=True)
    print(f"📅 找到 {len(dates)} 个日期的数据: {', '.join(dates[:5])}...")

    # 全局索引
    index = {
        "generated_at": datetime.now().isoformat(),
        "dates": dates,
        "latest": dates[0],
    }

    # 为每个日期生成 JSON
    for date_str in dates:
        csv_path = csvs[date_str]
        records = read_csv_rows(csv_path)
        stats = compute_stats(records)
        selected = filter_selected(records)

        # 读取总结和写作包
        summary = None
        writing_pack = None
        for name_pattern in [f"每日热点趋势总结_{date_str}.md", "今日热点趋势总结.md"]:
            content = read_text_file(os.path.join(output_dir, name_pattern))
            if content:
                summary = content
                break
        for name_pattern in [f"每日热点内容写作包_{date_str}.md", "今日热点内容写作包.md"]:
            content = read_text_file(os.path.join(output_dir, name_pattern))
            if content:
                writing_pack = content
                break

        # 写入日期数据文件
        date_data = {
            "date": date_str,
            "stats": stats,
            "records": records,
            "selected": selected,
            "summary": summary,
            "writing_pack": writing_pack,
        }

        date_file = os.path.join(data_dir, f"{date_str}.json")
        with open(date_file, "w", encoding="utf-8") as f:
            json.dump(date_data, f, ensure_ascii=False, indent=None)
        print(f"  ✅ {date_str}: {len(records)} 条记录, {len(selected)} 条精选")

    # 写入索引文件
    index_file = os.path.join(data_dir, "index.json")
    with open(index_file, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    print(f"\n📄 索引文件: {index_file}")

    # 复制前端 HTML
    src_html = os.path.join(BASE_DIR, "index.html")
    if os.path.exists(src_html):
        dst_html = os.path.join(site_dir, "index.html")
        shutil.copy2(src_html, dst_html)
        print(f"📋 已复制前端页面到 {dst_html}")
    else:
        print(f"⚠️  未找到 index.html，请确保 docs/index.html 存在")

    print(f"\n🎉 静态站点生成完成！")
    print(f"   本地预览: cd {site_dir} && python3 -m http.server 8080")
    print(f"   部署方式: 将 docs/ 目录推送到 GitHub，开启 GitHub Pages 即可")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="生成静态站点数据")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="数据输出目录")
    parser.add_argument("--site-dir", default=DEFAULT_SITE_DIR, help="静态站点目录")
    args = parser.parse_args()

    generate_site(args.output_dir, args.site_dir)
