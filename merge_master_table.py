#!/usr/bin/env python3
"""
历史总表合并脚本
================
将 output/ 目录下所有 "每日热点汇总表_{date}.csv" 合并成一张「热点追踪历史总表.csv」。

规则：
1. 按日期降序排列（最新的在最前面）
2. 同一天内按渠道（来源Key）分组排列
3. 同一天运行多次时，只保留最新一次的数据（用 archive 目录下带时间戳的文件判断）
4. 自动去重（基于 日期 + 来源Key + 标题 三元组）
5. 新增「采集日期」列，方便筛选

用法：
    python3 merge_master_table.py                           # 默认 output 目录
    python3 merge_master_table.py --output-dir /path/to/output
"""

import csv
import os
import sys
import glob
import argparse
from datetime import datetime
from typing import Optional, List, Dict, Tuple


# 渠道排序权重（越小越靠前）
SOURCE_ORDER = {
    "hackernews": 1,
    "github": 2,
    "producthunt": 3,
    "huggingface": 4,
    "v2ex": 5,
    "36kr": 6,
    "ai_newsletters": 7,
}


def get_source_sort_key(source_key: str) -> int:
    """获取渠道排序权重"""
    return SOURCE_ORDER.get(source_key.lower().strip(), 99)


def find_best_csv_for_date(output_dir: str, date_str: str) -> Optional[str]:
    """找到某一天最新的 CSV 文件。
    
    优先用 archive 目录下带时间戳的最新文件，否则用 dated 文件。
    """
    # 先看 archive 目录下有没有带时间戳的文件
    archive_pattern = os.path.join(output_dir, "archive", date_str, f"每日热点汇总表_{date_str}_*.csv")
    archive_files = sorted(glob.glob(archive_pattern), reverse=True)  # 时间戳越大越新
    if archive_files:
        return archive_files[0]  # 返回最新的

    # 否则用 dated 文件
    dated_file = os.path.join(output_dir, f"每日热点汇总表_{date_str}.csv")
    if os.path.exists(dated_file):
        return dated_file

    return None


def discover_dates(output_dir: str) -> List[str]:
    """发现所有有数据的日期，返回降序排列的日期列表"""
    dates = set()

    # 从 dated 文件名中提取日期
    for f in glob.glob(os.path.join(output_dir, "每日热点汇总表_????-??-??.csv")):
        basename = os.path.basename(f)
        # 格式: 每日热点汇总表_2026-03-22.csv
        date_part = basename.replace("每日热点汇总表_", "").replace(".csv", "")
        if len(date_part) == 10:  # YYYY-MM-DD
            dates.add(date_part)

    # 从 archive 目录中提取日期
    archive_dir = os.path.join(output_dir, "archive")
    if os.path.isdir(archive_dir):
        for d in os.listdir(archive_dir):
            if len(d) == 10 and d[4] == "-" and d[7] == "-":
                dates.add(d)

    return sorted(dates, reverse=True)


def read_csv_rows(csv_path: str) -> Tuple[List[str], List[Dict]]:
    """读取 CSV 文件，返回 (表头列表, 行字典列表)"""
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows = list(reader)
    return headers, rows


def merge_all(output_dir: str) -> Tuple[List[str], List[Dict]]:
    """合并所有日期的数据，返回 (表头, 合并后的行列表)"""
    dates = discover_dates(output_dir)
    if not dates:
        print("⚠️  没有找到任何历史数据", file=sys.stderr)
        return [], []

    print(f"📅 发现 {len(dates)} 天的数据: {', '.join(dates[:5])}{'...' if len(dates) > 5 else ''}")

    all_rows = []
    headers = []
    seen_keys = set()  # 用于去重: (日期, 来源Key, 标题)

    for date_str in dates:
        csv_path = find_best_csv_for_date(output_dir, date_str)
        if not csv_path:
            print(f"  ⏭️  {date_str}: 无数据文件，跳过")
            continue

        h, rows = read_csv_rows(csv_path)
        if not headers and h:
            headers = h
        
        # 按渠道排序
        rows.sort(key=lambda r: get_source_sort_key(r.get("来源Key", "")))

        day_count = 0
        for row in rows:
            # 去重键
            dedup_key = (date_str, row.get("来源Key", ""), row.get("标题", ""))
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            # 添加采集日期
            row["采集日期"] = date_str
            all_rows.append(row)
            day_count += 1

        print(f"  ✅ {date_str}: {day_count} 条 ← {os.path.basename(csv_path)}")

    return headers, all_rows


def write_master_csv(output_dir: str, headers: List[str], rows: List[Dict]):
    """写入历史总表"""
    if not rows:
        print("⚠️  没有数据可写", file=sys.stderr)
        return

    # 在表头最前面加上「采集日期」
    master_headers = ["采集日期"] + [h for h in headers if h != "采集日期"]

    master_path = os.path.join(output_dir, "热点追踪历史总表.csv")
    with open(master_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=master_headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n📊 历史总表已生成: {master_path}")
    print(f"   共 {len(rows)} 条记录，覆盖 {len(set(r['采集日期'] for r in rows))} 天")


def main():
    parser = argparse.ArgumentParser(description="合并每日热点 CSV 为历史总表")
    parser.add_argument("--output-dir", default=os.path.join(os.path.dirname(__file__), "output"),
                        help="输出目录路径（默认: ./output）")
    args = parser.parse_args()

    output_dir = os.path.expanduser(args.output_dir)
    if not os.path.isdir(output_dir):
        print(f"❌ 输出目录不存在: {output_dir}", file=sys.stderr)
        sys.exit(1)

    print("=" * 50)
    print("  📊 热点追踪历史总表合并")
    print("=" * 50)
    print(f"  数据目录: {output_dir}")
    print()

    headers, rows = merge_all(output_dir)
    write_master_csv(output_dir, headers, rows)


if __name__ == "__main__":
    main()
