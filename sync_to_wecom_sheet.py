#!/usr/bin/env python3
"""
腾讯文档多维表格自动写入脚本
================================
从 CSV 汇总表读取数据，通过企业微信 Webhook 写入腾讯文档多维表格。

功能：
1. 全量热点写入 —— 所有热点数据同步到「全量热点」表
2. 精选话题写入 —— 筛选 P0/P1 且腾讯云结合点有具体信息的记录写入「精选话题」表
3. 支持按日去重，同一天多次运行不会重复写入（可用 --force 强制）
4. 自动字段映射，从 CSV 列名匹配到腾讯文档字段 ID

用法：
    python3 sync_to_wecom_sheet.py                           # 写入今天的数据
    python3 sync_to_wecom_sheet.py --date 2026-03-22         # 写入指定日期
    python3 sync_to_wecom_sheet.py --force                   # 强制重写（跳过去重）
    python3 sync_to_wecom_sheet.py --dry-run                 # 预览不写入
    python3 sync_to_wecom_sheet.py --output-dir /path/to/dir # 指定数据目录
"""

import csv
import json
import os
import sys
import time
import warnings
import argparse
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

try:
    from urllib3.exceptions import NotOpenSSLWarning
except Exception:
    NotOpenSSLWarning = None

if NotOpenSSLWarning is not None:
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)

try:
    import requests
except ImportError:
    print("❌ requests 库未安装，请运行: pip3 install requests", file=sys.stderr)
    sys.exit(1)

# ═══════════════════════════════════════════════════════════
# 配置区 —— Webhook URL 和字段映射
# ═══════════════════════════════════════════════════════════

# ── 全量热点表 ──────────────────────────────────────────
FULL_TABLE_WEBHOOK = (
    "https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet/webhook"
    "?key=5Qa7X6F1SyUzSP4AZDhJLsINOvzNJgthba1Od1m5AaIIyQbWm8A2CE1mO8WdptClLwyTQOQcVvhHyZNPsE9LiEQjkxrAk6g2am0mW4ZnzHV0"
)

# 全量热点表 schema: 字段ID → 中文名（32 个字段）
FULL_TABLE_SCHEMA = {
    "fn8TJd": "日期",
    "ftQMc5": "栏目",
    "fU847k": "来源Key",
    "f04Gwj": "标题",
    "f72tuP": "链接",
    "fzdEUO": "热度",
    "ftk5Tx": "中文简介",
    "feoNtf": "具体内容摘要",
    "fzBepF": "开发者/作者",
    "f6rfc5": "开发者链接",
    "fldXDs": "开发者邮箱",
    "f9Q1BZ": "话题分类",
    "fsrOz8": "是否AI相关",
    "fOWysA": "是否云行业",
    "faYWoO": "内容标注",
    "fFY7I6": "综合评分",
    "foXiXl": "热度评分",
    "fUK6I7": "时效评分",
    "fZEzfE": "与腾讯云结合度",
    "fz0Ryk": "产品标签",
    "f2Lmq6": "竞品标签",
    "fQ5BVR": "友商标签",
    "fTTREn": "技术标签",
    "fREjLF": "官方号主推产品",
    "f6PfQr": "国际站关联类型",
    "fxpDad": "腾讯云结合点",
    "fLB8Xs": "官方号写作角度",
    "fQK4Vz": "配图建议",
    "fbWHnD": "可用角度",
    "fMxAv9": "发布优先级",
    "flrcgP": "社媒结论",
    "f6gxJX": "备注",
}

# ── 精选话题表 ──────────────────────────────────────────
SELECTED_TABLE_WEBHOOK = (
    "https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet/webhook"
    "?key=4EUmbakwMUKH0uhzWk2qyQhHVuY9LmgfnItinAuJvpUI4urNHsG7RqRfbkuZrJgCXQI3KWB7qX2Qc5s4z09mkPCc3q1RjvQFuhCJlOQtMOCh"
)

# 精选话题表 schema（与全量表字段ID和名称完全相同）
SELECTED_TABLE_SCHEMA = {
    "fn8TJd": "日期",
    "ftQMc5": "栏目",
    "fU847k": "来源Key",
    "f04Gwj": "标题",
    "f72tuP": "链接",
    "fzdEUO": "热度",
    "ftk5Tx": "中文简介",
    "feoNtf": "具体内容摘要",
    "fzBepF": "开发者/作者",
    "f6rfc5": "开发者链接",
    "fldXDs": "开发者邮箱",
    "f9Q1BZ": "话题分类",
    "fsrOz8": "是否AI相关",
    "fOWysA": "是否云行业",
    "faYWoO": "内容标注",
    "fFY7I6": "综合评分",
    "foXiXl": "热度评分",
    "fUK6I7": "时效评分",
    "fZEzfE": "与腾讯云结合度",
    "fz0Ryk": "产品标签",
    "f2Lmq6": "竞品标签",
    "fQ5BVR": "友商标签",
    "fTTREn": "技术标签",
    "fREjLF": "官方号主推产品",
    "f6PfQr": "国际站关联类型",
    "fxpDad": "腾讯云结合点",
    "fLB8Xs": "官方号写作角度",
    "fQK4Vz": "配图建议",
    "fbWHnD": "可用角度",
    "fMxAv9": "发布优先级",
    "flrcgP": "社媒结论",
    "f6gxJX": "备注",
}


# ═══════════════════════════════════════════════════════════
# CSV 列名 → 腾讯文档字段名 的映射
# ═══════════════════════════════════════════════════════════

# CSV 列名可能与腾讯文档字段名不同，这里做映射
# 格式: "CSV 列名" → "腾讯文档字段名" (None 表示跳过)
CSV_TO_DOC_FIELD_MAP = {
    # CSV中的"竞品产品标签" → 腾讯文档的"竞品标签"
    "竞品产品标签": "竞品标签",
    # 以下字段在腾讯文档表中没有对应列，跳过
    "话题类型": None,           # 腾讯文档表中没有这列
    "栏目序号": None,           # 不需要写入
    "时间": None,               # 腾讯文档表中没有这列
    "适合平台": None,           # 腾讯文档表中没有这列（信息合并到社媒结论）
    "是否渲染焦虑": None,       # 腾讯文档表中没有这列
}

# 每个字段的数据类型 (text / number / date / select)
FIELD_TYPES = {
    "日期": "date",
    "栏目": "select",
    "来源Key": "select",
    "标题": "text",
    "链接": "text",
    "热度": "text",
    "中文简介": "text",
    "具体内容摘要": "text",
    "开发者/作者": "text",
    "开发者链接": "text",
    "开发者邮箱": "text",
    "话题分类": "text",
    "是否AI相关": "select",
    "是否云行业": "select",
    "内容标注": "text",
    "综合评分": "number",
    "热度评分": "number",
    "时效评分": "number",
    "与腾讯云结合度": "number",
    "产品标签": "text",
    "竞品标签": "text",
    "友商标签": "text",
    "技术标签": "text",
    "官方号主推产品": "text",
    "国际站关联类型": "text",
    "腾讯云结合点": "text",
    "官方号写作角度": "text",
    "配图建议": "text",
    "可用角度": "text",
    "发布优先级": "text",
    "社媒结论": "text",
    "备注": "text",
}


# ═══════════════════════════════════════════════════════════
# 栏目名称映射（CSV中有emoji，腾讯文档中需要clean）
# ═══════════════════════════════════════════════════════════

COLUMN_LABEL_MAP = {
    "🦄 硅谷热点": "硅谷热点",
    "🐙 开源趋势": "开源趋势",
    "💰 创投快讯": "创投快讯",
    "🏹 产品猎人": "产品猎人",
    "🤓 极客社区": "极客社区",
    "📰 HF 每日论文": "HF每日论文",
    "📬 AI 内参热点": "AI内参热点",
}


# ═══════════════════════════════════════════════════════════
# 写入去重日志
# ═══════════════════════════════════════════════════════════

WRITE_LOG_PATH = Path.home() / ".wecom_sheet_sync_log.json"


def _load_write_log():
    # type: () -> Dict[str, Any]
    if WRITE_LOG_PATH.exists():
        try:
            return json.loads(WRITE_LOG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_write_log(log):
    # type: (Dict[str, Any]) -> None
    WRITE_LOG_PATH.write_text(
        json.dumps(log, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _check_already_written(date_str, table_type="full"):
    # type: (str, str) -> Optional[Dict[str, Any]]
    """检查某天某表是否已成功写入过"""
    log = _load_write_log()
    key = f"{date_str}_{table_type}"
    entry = log.get(key)
    if entry and entry.get("success"):
        return entry
    return None


def _record_write(date_str, table_type, records_count, success):
    # type: (str, str, int, bool) -> None
    log = _load_write_log()
    key = f"{date_str}_{table_type}"
    log[key] = {
        "success": success,
        "records_count": records_count,
        "written_at": datetime.now().isoformat(),
    }
    # 保留最近 60 天
    if len(log) > 120:
        sorted_keys = sorted(log.keys())
        for old_key in sorted_keys[:-120]:
            del log[old_key]
    _save_write_log(log)


# ═══════════════════════════════════════════════════════════
# 核心函数
# ═══════════════════════════════════════════════════════════


def find_csv_for_date(output_dir, date_str):
    # type: (str, str) -> Optional[str]
    """找到某天的 CSV 文件"""
    import glob

    # 优先: archive 中带时间戳的最新文件
    archive_pattern = os.path.join(output_dir, "archive", date_str, f"每日热点汇总表_{date_str}_*.csv")
    archive_files = sorted(glob.glob(archive_pattern), reverse=True)
    if archive_files:
        return archive_files[0]

    # 其次: dated 文件
    dated_file = os.path.join(output_dir, f"每日热点汇总表_{date_str}.csv")
    if os.path.exists(dated_file):
        return dated_file

    # 最后: 今日文件
    latest_file = os.path.join(output_dir, "今日热点汇总表.csv")
    if os.path.exists(latest_file):
        return latest_file

    return None


def read_csv_data(csv_path):
    # type: (str) -> List[Dict[str, str]]
    """读取 CSV 为 dict 列表"""
    rows = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(dict(row))
    return rows


def format_value(value, data_type):
    # type: (Any, str) -> Any
    """格式化值为腾讯文档 Webhook 要求的格式"""
    if value is None or str(value).strip() == "":
        return None

    val_str = str(value).strip()

    if data_type == "number":
        try:
            num = float(val_str)
            if num == int(num):
                return int(num)
            return round(num, 1)
        except (ValueError, TypeError):
            return None

    elif data_type == "select":
        return [{"text": val_str}]

    elif data_type == "date":
        # 日期需要毫秒时间戳
        if val_str.isdigit() and len(val_str) >= 10:
            return val_str
        try:
            if "T" in val_str:
                dt = datetime.fromisoformat(val_str)
            else:
                dt = datetime.strptime(val_str, "%Y-%m-%d")
            return str(int(dt.timestamp() * 1000))
        except Exception:
            return None

    else:
        # text
        return val_str


def build_field_mapping(schema):
    # type: (Dict[str, str]) -> Dict[str, Tuple[str, str]]
    """
    构建 中文字段名 → (字段ID, 数据类型) 的映射

    Returns: {"标题": ("f04Gwj", "text"), "综合评分": ("fFY7I6", "number"), ...}
    """
    # 反转 schema: field_title → field_id
    title_to_id = {}  # type: Dict[str, str]
    for fid, ftitle in schema.items():
        title_to_id[ftitle] = fid

    mapping = {}  # type: Dict[str, Tuple[str, str]]
    for field_name, fid in title_to_id.items():
        dtype = FIELD_TYPES.get(field_name, "text")
        mapping[field_name] = (fid, dtype)

    return mapping


def clean_column_label(label):
    # type: (str) -> str
    """清理栏目名（去 emoji）"""
    if label in COLUMN_LABEL_MAP:
        return COLUMN_LABEL_MAP[label]
    # 通用处理：去掉前导 emoji
    clean = label.strip()
    if clean and not clean[0].isalnum() and not '\u4e00' <= clean[0] <= '\u9fff':
        clean = clean.lstrip().split(" ", 1)[-1] if " " in clean else clean[1:].strip()
    return clean


def csv_row_to_record(row, date_str, field_mapping):
    # type: (Dict[str, str], str, Dict[str, Tuple[str, str]]) -> Optional[Dict[str, Any]]
    """
    将一行 CSV 数据转换为腾讯文档的 record 格式。
    自动遍历 field_mapping 中所有字段，从 CSV 行中取值。

    Returns: {"values": {field_id: formatted_value, ...}} 或 None
    """
    values = {}  # type: Dict[str, Any]

    def add(doc_field_name, raw_value):
        # type: (str, Any) -> None
        if doc_field_name not in field_mapping:
            return
        fid, dtype = field_mapping[doc_field_name]
        formatted = format_value(raw_value, dtype)
        if formatted is not None:
            values[fid] = formatted

    # 标题必须存在
    title = row.get("标题", "").strip()
    if not title or title == "暂无数据":
        return None

    # 日期
    add("日期", date_str)

    # 栏目 (去 emoji)
    add("栏目", clean_column_label(row.get("栏目", "")))

    # 来源Key
    add("来源Key", row.get("来源Key", ""))

    # 标题（截断）
    add("标题", title[:200])

    # 链接
    add("链接", row.get("链接", ""))

    # 热度
    add("热度", row.get("热度", ""))

    # 中文简介（截断）
    add("中文简介", (row.get("中文简介", "") or "")[:500])

    # 具体内容摘要（截断）
    add("具体内容摘要", (row.get("具体内容摘要", "") or "")[:2000])

    # 开发者信息
    add("开发者/作者", row.get("开发者/作者", ""))
    add("开发者链接", row.get("开发者链接", ""))
    add("开发者邮箱", row.get("开发者邮箱", ""))

    # 分析标签
    add("话题分类", row.get("话题分类", ""))
    add("是否AI相关", row.get("是否AI相关", ""))
    add("是否云行业", row.get("是否云行业", ""))
    add("内容标注", row.get("内容标注", ""))

    # 评分 (数字)
    add("综合评分", row.get("综合评分", ""))
    add("热度评分", row.get("热度评分", ""))
    add("时效评分", row.get("时效评分", ""))
    add("与腾讯云结合度", row.get("与腾讯云结合度", ""))

    # 产品标签
    add("产品标签", row.get("产品标签", ""))

    # 竞品标签（CSV中叫"竞品产品标签"或"竞品标签"）
    competitor_tag = row.get("竞品标签", "") or row.get("竞品产品标签", "")
    add("竞品标签", competitor_tag)

    # 友商标签
    add("友商标签", row.get("友商标签", ""))

    # 技术标签
    add("技术标签", row.get("技术标签", ""))

    # 官方号主推产品
    add("官方号主推产品", row.get("官方号主推产品", ""))

    # 国际站关联类型
    add("国际站关联类型", row.get("国际站关联类型", ""))

    # 腾讯云结合点
    add("腾讯云结合点", (row.get("腾讯云结合点", "") or "")[:2000])

    # 官方号写作角度
    add("官方号写作角度", (row.get("官方号写作角度", "") or "")[:1000])

    # 配图建议
    add("配图建议", (row.get("配图建议", "") or "")[:500])

    # 可用角度
    add("可用角度", (row.get("可用角度", "") or "")[:1000])

    # 发布优先级
    add("发布优先级", row.get("发布优先级", ""))

    # 社媒结论
    add("社媒结论", (row.get("社媒结论", "") or "")[:2000])

    # 备注
    notes = row.get("备注", "").strip()
    # 把适合平台也加到备注里（如果有的话）
    platforms = row.get("适合平台", "").strip()
    if platforms and not notes:
        notes = f"适合平台: {platforms}"
    elif platforms:
        notes = f"{notes} | 适合平台: {platforms}"
    add("备注", notes)

    if len(values) < 3:  # 太少字段说明数据有问题
        return None

    return {"values": values}


def filter_selected_topics(rows):
    # type: (List[Dict[str, str]]) -> List[Dict[str, str]]
    """
    筛选精选话题：
    - 发布优先级为 P0 或 P1
    - 腾讯云结合点有具体信息（非空）
    """
    selected = []
    for row in rows:
        priority = row.get("发布优先级", "").strip()
        tcloud_point = row.get("腾讯云结合点", "").strip()

        # 必须是 P0 或 P1
        is_p0_p1 = priority.startswith("P0") or priority.startswith("P1")
        # 腾讯云结合点必须有具体信息
        has_tcloud_point = bool(tcloud_point) and tcloud_point != "无" and len(tcloud_point) > 5

        if is_p0_p1 and has_tcloud_point:
            selected.append(row)

    # 按综合评分降序
    def sort_key(r):
        try:
            return float(r.get("综合评分", "0") or "0")
        except ValueError:
            return 0
    selected.sort(key=sort_key, reverse=True)

    return selected


def send_records_to_webhook(webhook_url, records, batch_size=50):
    # type: (str, List[Dict[str, Any]], int) -> Tuple[int, List[str]]
    """
    分批发送记录到 Webhook

    Returns: (成功写入数, 错误列表)
    """
    # 禁用 SSL 警告
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass

    total_written = 0
    errors = []  # type: List[str]

    for i in range(0, len(records), batch_size):
        batch = records[i: i + batch_size]
        payload = {"add_records": batch}

        try:
            resp = requests.post(
                webhook_url,
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=30,
                verify=False,
            )
            if resp.status_code == 200:
                try:
                    body = resp.json()
                    if body.get("errcode", 0) != 0:
                        errors.append(
                            f"批次 {i // batch_size + 1}: errcode={body.get('errcode')}: {body.get('errmsg', '')}"
                        )
                        continue
                except Exception:
                    pass
                total_written += len(batch)
            else:
                errors.append(f"批次 {i // batch_size + 1}: HTTP {resp.status_code}")
        except Exception as exc:
            errors.append(f"批次 {i // batch_size + 1}: {exc}")

        # 批次间间隔
        if i + batch_size < len(records):
            time.sleep(0.5)

    return total_written, errors


def sync_full_table(output_dir, date_str, dry_run=False, force=False):
    # type: (str, str, bool, bool) -> Dict[str, Any]
    """同步全量热点到腾讯文档"""
    print(f"\n{'='*55}")
    print(f"  📊 全量热点 → 腾讯文档多维表格")
    print(f"{'='*55}")
    print(f"  日期: {date_str}")

    # 去重检查
    if not force and not dry_run:
        prev = _check_already_written(date_str, "full")
        if prev:
            msg = (
                f"  ⏭️  今天已于 {prev.get('written_at', '?')} 写入过 "
                f"{prev.get('records_count', '?')} 条记录，跳过。"
                f"\n  如需重写请加 --force 参数。"
            )
            print(msg)
            return {"success": True, "skipped": True, "message": msg}

    # 找 CSV
    csv_path = find_csv_for_date(output_dir, date_str)
    if not csv_path:
        msg = f"  ❌ 未找到 {date_str} 的 CSV 数据文件"
        print(msg)
        return {"success": False, "message": msg}

    print(f"  数据源: {os.path.basename(csv_path)}")

    # 读取数据
    rows = read_csv_data(csv_path)
    if not rows:
        msg = "  ❌ CSV 文件为空"
        print(msg)
        return {"success": False, "message": msg}

    print(f"  CSV 行数: {len(rows)}")

    # 构建字段映射
    field_mapping = build_field_mapping(FULL_TABLE_SCHEMA)
    print(f"  字段映射: {len(field_mapping)} 个字段")

    # 转换记录
    records = []
    for row in rows:
        rec = csv_row_to_record(row, date_str, field_mapping)
        if rec:
            records.append(rec)

    if not records:
        msg = "  ❌ 没有有效记录可写入"
        print(msg)
        return {"success": False, "message": msg}

    print(f"  有效记录: {len(records)} 条")

    if dry_run:
        print(f"\n  🔍 DRY RUN - 预览前 3 条:")
        for i, rec in enumerate(records[:3]):
            print(f"    [{i+1}] {json.dumps(rec, ensure_ascii=False)[:200]}...")
        return {"success": True, "dry_run": True, "records_count": len(records)}

    # 写入
    print(f"\n  📤 正在写入...")
    written, errors = send_records_to_webhook(FULL_TABLE_WEBHOOK, records)

    if written > 0:
        _record_write(date_str, "full", written, True)
        msg = f"  ✅ 全量热点: 成功写入 {written}/{len(records)} 条"
        if errors:
            msg += f"\n  ⚠️  部分失败:\n    " + "\n    ".join(errors)
        print(msg)
        return {"success": True, "records_count": written, "total": len(records)}
    else:
        msg = f"  ❌ 写入失败 (0/{len(records)} 条)\n    " + "\n    ".join(errors)
        print(msg)
        return {"success": False, "message": msg, "errors": errors}


def sync_selected_table(output_dir, date_str, dry_run=False, force=False):
    # type: (str, str, bool, bool) -> Dict[str, Any]
    """同步精选话题到腾讯文档"""
    print(f"\n{'='*55}")
    print(f"  ⭐ 精选话题 → 腾讯文档多维表格")
    print(f"{'='*55}")
    print(f"  日期: {date_str}")
    print(f"  筛选条件: 发布优先级 P0/P1 且 腾讯云结合点有具体信息")

    # 去重检查
    if not force and not dry_run:
        prev = _check_already_written(date_str, "selected")
        if prev:
            msg = (
                f"  ⏭️  精选话题已于 {prev.get('written_at', '?')} 写入过 "
                f"{prev.get('records_count', '?')} 条，跳过。"
            )
            print(msg)
            return {"success": True, "skipped": True, "message": msg}

    # 找 CSV
    csv_path = find_csv_for_date(output_dir, date_str)
    if not csv_path:
        msg = f"  ❌ 未找到 {date_str} 的 CSV 数据文件"
        print(msg)
        return {"success": False, "message": msg}

    # 读取数据
    rows = read_csv_data(csv_path)

    # 筛选精选话题
    selected_rows = filter_selected_topics(rows)
    if not selected_rows:
        msg = f"  ℹ️  今日 {len(rows)} 条中没有满足精选条件的话题"
        print(msg)
        return {"success": True, "records_count": 0, "message": msg}

    print(f"  全量: {len(rows)} 条 → 精选: {len(selected_rows)} 条")

    # 列出精选的标题
    for i, row in enumerate(selected_rows):
        priority = row.get("发布优先级", "").strip()
        score = row.get("综合评分", "")
        tcloud = row.get("与腾讯云结合度", "")
        title = row.get("标题", "")[:50]
        print(f"    [{priority}] {title}  (评分:{score}, 腾讯云:{tcloud})")

    # 构建字段映射
    field_mapping = build_field_mapping(SELECTED_TABLE_SCHEMA)

    # 转换记录
    records = []
    for row in selected_rows:
        rec = csv_row_to_record(row, date_str, field_mapping)
        if rec:
            records.append(rec)

    if not records:
        msg = "  ❌ 没有有效记录可写入"
        print(msg)
        return {"success": False, "message": msg}

    if dry_run:
        print(f"\n  🔍 DRY RUN - 预览前 3 条:")
        for i, rec in enumerate(records[:3]):
            print(f"    [{i+1}] {json.dumps(rec, ensure_ascii=False)[:200]}...")
        return {"success": True, "dry_run": True, "records_count": len(records)}

    # 写入
    print(f"\n  📤 正在写入精选话题...")
    written, errors = send_records_to_webhook(SELECTED_TABLE_WEBHOOK, records)

    if written > 0:
        _record_write(date_str, "selected", written, True)
        msg = f"  ✅ 精选话题: 成功写入 {written}/{len(records)} 条"
        if errors:
            msg += f"\n  ⚠️  部分失败:\n    " + "\n    ".join(errors)
        print(msg)
        return {"success": True, "records_count": written, "total": len(records)}
    else:
        msg = f"  ❌ 写入失败 (0/{len(records)} 条)\n    " + "\n    ".join(errors)
        print(msg)
        return {"success": False, "message": msg, "errors": errors}


# ═══════════════════════════════════════════════════════════
# 每日总结功能
# ═══════════════════════════════════════════════════════════


def generate_daily_summary(output_dir, date_str):
    # type: (str, str) -> str
    """
    生成每日热点总结报告：
    - 市面上有哪些很火的趋势
    - 与腾讯云国际站产品的关联
    """
    csv_path = find_csv_for_date(output_dir, date_str)
    if not csv_path:
        return f"❌ 未找到 {date_str} 的数据"

    rows = read_csv_data(csv_path)
    if not rows:
        return "❌ 数据为空"

    # 统计数据
    total = len(rows)
    ai_count = sum(1 for r in rows if r.get("是否AI相关") == "是")
    cloud_count = sum(1 for r in rows if r.get("是否云行业") == "是")

    # 按来源分组
    by_source = {}  # type: Dict[str, List[Dict[str, str]]]
    for row in rows:
        src = row.get("来源Key", "unknown")
        by_source.setdefault(src, []).append(row)

    # 按话题分类分组
    by_topic = {}  # type: Dict[str, int]
    for row in rows:
        topic = row.get("话题分类", "").strip()
        if topic:
            by_topic[topic] = by_topic.get(topic, 0) + 1

    # 高分内容（综合评分 >= 7）
    high_score = []
    for row in rows:
        try:
            score = float(row.get("综合评分", "0") or "0")
        except ValueError:
            score = 0
        if score >= 7.0:
            high_score.append(row)
    high_score.sort(key=lambda r: float(r.get("综合评分", "0") or "0"), reverse=True)

    # 腾讯云高关联内容
    tcloud_related = []
    for row in rows:
        try:
            relevance = float(row.get("与腾讯云结合度", "0") or "0")
        except ValueError:
            relevance = 0
        if relevance >= 6.0:
            tcloud_related.append(row)
    tcloud_related.sort(key=lambda r: float(r.get("与腾讯云结合度", "0") or "0"), reverse=True)

    # 产品标签统计
    product_counts = {}  # type: Dict[str, int]
    for row in rows:
        tags = row.get("产品标签", "").strip()
        if tags:
            for tag in tags.split(","):
                tag = tag.strip()
                if tag:
                    product_counts[tag] = product_counts.get(tag, 0) + 1

    # 友商标签统计
    competitor_counts = {}  # type: Dict[str, int]
    for row in rows:
        tags = row.get("友商标签", "").strip()
        if tags:
            for tag in tags.split(","):
                tag = tag.strip()
                if tag:
                    competitor_counts[tag] = competitor_counts.get(tag, 0) + 1

    # 技术标签统计
    tech_counts = {}  # type: Dict[str, int]
    for row in rows:
        tags = row.get("技术标签", "").strip()
        if tags:
            for tag in tags.split(","):
                tag = tag.strip()
                if tag:
                    tech_counts[tag] = tech_counts.get(tag, 0) + 1

    # 构建总结
    lines = [
        f"# 📊 每日热点趋势总结 — {date_str}",
        "",
        "---",
        "",
        "## 一、今日数据概览",
        "",
        f"| 指标 | 数值 |",
        f"|------|------|",
        f"| 采集总量 | {total} 条 |",
        f"| AI 相关 | {ai_count} 条 ({round(ai_count/total*100)}%) |",
        f"| 云行业相关 | {cloud_count} 条 ({round(cloud_count/total*100)}%) |",
        f"| 高分内容 (≥7.0) | {len(high_score)} 条 |",
        f"| 腾讯云高关联 (≥6.0) | {len(tcloud_related)} 条 |",
        "",
        "### 来源分布",
        "",
    ]

    source_display = {
        "hackernews": "🦄 HackerNews",
        "github": "🐙 GitHub",
        "producthunt": "🏹 ProductHunt",
        "huggingface": "📰 HuggingFace",
        "v2ex": "🤓 V2EX",
        "36kr": "💰 36氪",
        "ai_newsletters": "📬 AI Newsletter",
    }

    for src in ["hackernews", "github", "producthunt", "huggingface", "v2ex", "36kr", "ai_newsletters"]:
        items = by_source.get(src, [])
        display = source_display.get(src, src)
        lines.append(f"- {display}: {len(items)} 条")

    # 话题分类
    if by_topic:
        lines.extend([
            "",
            "### 话题分类分布",
            "",
        ])
        for topic, count in sorted(by_topic.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"- {topic}: {count} 条")

    # 热门技术趋势
    if tech_counts:
        lines.extend([
            "",
            "## 二、🔥 热门技术趋势",
            "",
            "今日被提及最多的技术方向：",
            "",
        ])
        for tech, count in sorted(tech_counts.items(), key=lambda x: x[1], reverse=True)[:10]:
            bar = "█" * min(count, 20)
            lines.append(f"- **{tech}**: {count} 次 {bar}")

    # 友商动态
    if competitor_counts:
        lines.extend([
            "",
            "## 三、👀 友商动态",
            "",
            "今日被提及的友商/竞品：",
            "",
        ])
        for comp, count in sorted(competitor_counts.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"- **{comp}**: {count} 次提及")

    # 高分内容
    if high_score:
        lines.extend([
            "",
            "## 四、⭐ 高分热点 TOP10",
            "",
            "综合评分 ≥ 7.0 的最值得关注内容：",
            "",
        ])
        for i, row in enumerate(high_score[:10]):
            score = row.get("综合评分", "")
            tcloud = row.get("与腾讯云结合度", "")
            title = row.get("标题", "")[:80]
            src = row.get("来源Key", "")
            priority = row.get("发布优先级", "")
            lines.append(f"{i+1}. **{title}**")
            lines.append(f"   - 来源: {src} | 综合: {score} | 腾讯云: {tcloud} | {priority}")
            brief = row.get("中文简介", "")[:120]
            if brief:
                lines.append(f"   - {brief}")
            lines.append("")

    # 腾讯云关联分析
    if tcloud_related:
        lines.extend([
            "",
            "## 五、☁️ 腾讯云国际站关联分析",
            "",
            "与腾讯云国际站产品高度关联的热点：",
            "",
        ])
        for i, row in enumerate(tcloud_related[:10]):
            title = row.get("标题", "")[:60]
            relevance = row.get("与腾讯云结合度", "")
            product = row.get("产品标签", "")
            tcloud_point = row.get("腾讯云结合点", "")[:200]
            lines.append(f"{i+1}. **{title}**")
            lines.append(f"   - 腾讯云结合度: {relevance}/10")
            if product:
                lines.append(f"   - 关联产品: {product}")
            if tcloud_point:
                lines.append(f"   - 💡 结合点: {tcloud_point}")
            lines.append("")

    # 产品标签热度
    if product_counts:
        lines.extend([
            "",
            "### 腾讯云产品被提及频次",
            "",
        ])
        for prod, count in sorted(product_counts.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"- **{prod}**: {count} 次")

    # 行动建议
    p0_items = [r for r in rows if r.get("发布优先级", "").startswith("P0")]
    p1_items = [r for r in rows if r.get("发布优先级", "").startswith("P1")]

    lines.extend([
        "",
        "## 六、📋 行动建议",
        "",
        f"### 🔴 P0 今天发 ({len(p0_items)} 条)",
        "",
    ])
    if p0_items:
        for row in p0_items:
            title = row.get("标题", "")[:60]
            product = row.get("产品标签", "")
            lines.append(f"- {title}")
            if product:
                lines.append(f"  - 关联产品: {product}")
    else:
        lines.append("- （无 P0 内容）")

    lines.extend([
        "",
        f"### 🟡 P1 本周发 ({len(p1_items)} 条)",
        "",
    ])
    if p1_items:
        for row in p1_items:
            title = row.get("标题", "")[:60]
            product = row.get("产品标签", "")
            lines.append(f"- {title}")
            if product:
                lines.append(f"  - 关联产品: {product}")
    else:
        lines.append("- （无 P1 内容）")

    lines.extend([
        "",
        "---",
        "",
        f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"> 数据源: {os.path.basename(csv_path)}",
    ])

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description="腾讯文档多维表格自动写入",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 sync_to_wecom_sheet.py                           # 写入今天的数据
  python3 sync_to_wecom_sheet.py --date 2026-03-22         # 写入指定日期
  python3 sync_to_wecom_sheet.py --force                   # 强制重写
  python3 sync_to_wecom_sheet.py --dry-run                 # 预览不写入
  python3 sync_to_wecom_sheet.py --summary-only            # 只生成总结
  python3 sync_to_wecom_sheet.py --full-only               # 只写全量表
  python3 sync_to_wecom_sheet.py --selected-only           # 只写精选表
        """,
    )
    parser.add_argument(
        "--output-dir",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "output"),
        help="数据输出目录（默认: ./output）",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="指定日期 (YYYY-MM-DD)，默认今天",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="预览模式，不实际写入",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="强制写入（跳过去重检查）",
    )
    parser.add_argument(
        "--full-only",
        action="store_true",
        default=False,
        help="只写入全量热点表",
    )
    parser.add_argument(
        "--selected-only",
        action="store_true",
        default=False,
        help="只写入精选话题表",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        default=False,
        help="只生成每日总结（不写入表格）",
    )
    parser.add_argument(
        "--no-summary",
        action="store_true",
        default=False,
        help="不生成每日总结",
    )

    args = parser.parse_args()

    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    output_dir = os.path.expanduser(args.output_dir)

    if not os.path.isdir(output_dir):
        print(f"❌ 输出目录不存在: {output_dir}", file=sys.stderr)
        sys.exit(1)

    print("╔═══════════════════════════════════════════════════╗")
    print("║   📊 热点追踪 → 腾讯文档多维表格同步               ║")
    print("╚═══════════════════════════════════════════════════╝")
    print(f"  日期: {date_str}")
    print(f"  数据目录: {output_dir}")
    if args.dry_run:
        print("  ⚠️  DRY RUN 模式（不实际写入）")

    results = {}

    # 生成每日总结
    if not args.no_summary:
        summary_text = generate_daily_summary(output_dir, date_str)
        summary_path = os.path.join(output_dir, f"每日热点趋势总结_{date_str}.md")
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(summary_text)
        print(f"\n  📝 每日总结已生成: {summary_path}")
        results["summary"] = summary_path

        # 也写一份固定名称的
        latest_summary = os.path.join(output_dir, "今日热点趋势总结.md")
        with open(latest_summary, "w", encoding="utf-8") as f:
            f.write(summary_text)

    if args.summary_only:
        print("\n✅ 总结生成完毕（--summary-only 模式）")
        return

    # 写入全量热点表
    if not args.selected_only:
        full_result = sync_full_table(
            output_dir, date_str,
            dry_run=args.dry_run,
            force=args.force,
        )
        results["full"] = full_result

    # 写入精选话题表
    if not args.full_only:
        selected_result = sync_selected_table(
            output_dir, date_str,
            dry_run=args.dry_run,
            force=args.force,
        )
        results["selected"] = selected_result

    # 总结
    print(f"\n{'='*55}")
    print(f"  🏁 同步完成")
    print(f"{'='*55}")

    return results


if __name__ == "__main__":
    main()
