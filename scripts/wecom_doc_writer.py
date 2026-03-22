"""
企业微信腾讯文档（智能表格）自动写入模块

通过企业微信智能表格的 Webhook「接收外部数据」功能，
将每日热点追踪内容自动写入到企业微信的腾讯文档智能表格中。

核心机制：
- Webhook 使用「字段ID」（如 f04Gwj）而非「字段标题」（如 文本）
- 需要在配置文件中保存 schema（字段ID → 字段标题 的映射）
- 代码内部用中文字段名写逻辑，运行时自动转换成字段ID

配置方式（一次性）：
1. 在企业微信中创建智能表格，添加需要的列
2. 开启「接收外部数据」，复制 Webhook 地址和示例数据
3. 运行: python wecom_doc_writer.py --init
   按提示粘贴 Webhook URL 和示例数据的 schema 部分
4. 以后就可以直接用了

按日分区：
- 每条记录带「日期」字段（如 2026-03-20）
- 在智能表格中按「日期」列分组即可实现按日查看

API 文档：https://developer.work.weixin.qq.com/document/path/101239
"""
from __future__ import annotations

import json
import os
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from urllib3.exceptions import NotOpenSSLWarning
except Exception:  # pragma: no cover - environment-specific
    NotOpenSSLWarning = None  # type: ignore

if NotOpenSSLWarning is not None:
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)

try:
    import requests
except ImportError:
    requests = None  # type: ignore

# ── 配置文件路径 ──────────────────────────────────────
CONFIG_PATH = Path.home() / ".wecom_doc_config.json"

# ── 写入记录文件（去重用）──────────────────────────────
WRITE_LOG_PATH = Path.home() / ".wecom_doc_write_log.json"


# ── 所有可用的字段定义（中文名 → 数据类型）──────────────
# 数据类型: text=文本, number=数字, date=日期时间戳(ms), select=单选
FIELD_DEFINITIONS: Dict[str, str] = {
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
    "话题类型": "text",
    "是否AI相关": "select",
    "是否云行业": "select",
    "是否渲染焦虑": "select",
    "内容标注": "text",
    "综合评分": "number",
    "热度评分": "number",
    "时效评分": "number",
    "与腾讯云结合度": "number",
    "产品标签": "text",
    "友商标签": "text",
    "竞品产品标签": "text",
    "技术标签": "text",
    "腾讯云结合点": "text",
    "可用角度": "text",
    "发布优先级": "text",
    "适合平台": "text",
    "社媒结论": "text",
    "备注": "text",
}


# ── 精选话题字段定义（用于第二个子表）──────────────────────
SELECTED_FIELD_DEFINITIONS: Dict[str, str] = {
    "日期": "date",
    "栏目": "select",
    "来源Key": "select",
    "标题": "text",
    "链接": "text",
    "中文简介": "text",
    "具体内容摘要": "text",
    "开发者/作者": "text",
    "综合评分": "number",
    "与腾讯云结合度": "number",
    "腾讯云结合点": "text",
    "产品标签": "text",
    "友商标签": "text",
    "竞品产品标签": "text",
    "技术标签": "text",
    "话题分类": "text",
    "话题类型": "text",
    "内容标注": "text",
    "可用角度": "text",
    "发布优先级": "text",
    "适合平台": "text",
    "社媒结论": "text",
    "精选理由": "text",
    "备注": "text",
}

# ── 精选筛选阈值 ──────────────────────────────────────
SELECTED_COMPOSITE_THRESHOLD = 7.0    # 综合评分 >= 7.0 入选
SELECTED_TCLOUD_THRESHOLD = 6.0       # 或 腾讯云结合度 >= 6.0 入选


# ── 配置管理 ──────────────────────────────────────────


def load_config() -> Dict[str, Any]:
    """加载配置文件 ~/.wecom_doc_config.json"""
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠️ 配置文件读取失败: {e}", file=sys.stderr)
    return {}


def save_config(config: Dict[str, Any]):
    """保存配置文件"""
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"✅ 配置已保存到 {CONFIG_PATH}", file=sys.stderr)


def _load_webhook_url(cli_url: Optional[str] = None) -> Optional[str]:
    """
    按优先级加载 Webhook URL：
    1. 命令行参数传入
    2. 环境变量 WECOM_WEBHOOK_URL
    3. 配置文件 ~/.wecom_doc_config.json
    """
    if cli_url:
        return cli_url.strip()

    env_url = os.environ.get("WECOM_WEBHOOK_URL", "").strip()
    if env_url:
        return env_url

    config = load_config()
    url = config.get("webhook_url", "").strip()
    return url if url else None


def _load_schema(key: str = "schema") -> Dict[str, str]:
    """
    从配置文件加载 schema（字段ID → 字段标题 映射）。

    Args:
        key: 配置中的 schema key，默认 "schema"（全量表），
             精选话题用 "selected_schema"

    返回: {field_id: field_title}，如 {"f04Gwj": "标题", "fMAfWQ": "综合评分"}
    """
    config = load_config()
    return config.get(key, {})


def _load_field_types(key: str = "field_types") -> Dict[str, str]:
    """
    从配置文件加载每个字段ID的实际列类型（由智能表格定义）。

    Args:
        key: 配置中的 field_types key，默认 "field_types"（全量表），
             精选话题用 "selected_field_types"

    返回: {field_id: actual_type}，如 {"fn8TJd": "date", "fMAfWQ": "number"}
    如果没有配置则返回空字典。
    """
    config = load_config()
    return config.get(key, {})


def _build_field_mapping(
    schema: Dict[str, str],
    field_defs: Optional[Dict[str, str]] = None,
    ft_key: str = "field_types",
) -> Dict[str, Tuple[str, str]]:
    """
    构建 中文字段名 → (字段ID, 数据类型) 的映射。

    从 schema 中读取 {field_id: field_title}，
    与 field_defs 中的 {field_title: data_type} 做匹配。

    Args:
        schema: {field_id: field_title} 映射
        field_defs: 字段定义，默认 FIELD_DEFINITIONS（全量表），
                    精选话题用 SELECTED_FIELD_DEFINITIONS
        ft_key: 配置中 field_types 的 key

    返回: {"标题": ("f04Gwj", "text"), "综合评分": ("fMAfWQ", "number"), ...}
    """
    if field_defs is None:
        field_defs = FIELD_DEFINITIONS

    field_types = _load_field_types(ft_key)

    # 反转 schema: field_title → field_id
    title_to_id: Dict[str, str] = {}
    for fid, ftitle in schema.items():
        title_to_id[ftitle] = fid

    mapping: Dict[str, Tuple[str, str]] = {}
    for field_name, default_type in field_defs.items():
        if field_name in title_to_id:
            fid = title_to_id[field_name]
            # 优先使用实际列类型（来自 field_types 配置）
            actual_type = field_types.get(fid, default_type)
            mapping[field_name] = (fid, actual_type)

    return mapping


def _format_value(value: Any, data_type: str) -> Any:
    """
    将值格式化为智能表格 Webhook 要求的格式。

    - text:   字符串
    - number: 数字
    - date:   毫秒时间戳字符串（如 "1742342400000"）
    - select: [{"text": "选项名"}]
    """
    if value is None or value == "":
        return None  # 跳过空值

    if data_type == "number":
        try:
            num = float(value) if isinstance(value, str) else value
            # 如果是整数，返回 int
            if isinstance(num, float) and num == int(num):
                return int(num)
            return num
        except (ValueError, TypeError):
            return None
    elif data_type == "select":
        return [{"text": str(value)}]
    elif data_type == "date":
        # 日期列需要毫秒时间戳字符串
        val_str = str(value)
        # 如果已经是时间戳格式（纯数字），直接返回
        if val_str.isdigit() and len(val_str) >= 10:
            return val_str
        # 如果是 YYYY-MM-DD 格式，转换为毫秒时间戳
        try:
            from datetime import datetime as _dt
            if "T" in val_str:
                dt = _dt.fromisoformat(val_str)
            else:
                dt = _dt.strptime(val_str, "%Y-%m-%d")
            return str(int(dt.timestamp() * 1000))
        except Exception:
            return None
    else:
        # text 类型
        return str(value)


# ── 构建记录 ──────────────────────────────────────────


def _build_records_from_results(
    results: List[Dict[str, Any]],
    generated_at: datetime,
    field_mapping: Dict[str, Tuple[str, str]],
) -> List[Dict[str, Any]]:
    """
    将爬取 + 分析结果转换为智能表格的记录格式。

    只写入 field_mapping 中存在的字段（即表格中实际有的列）。
    没有对应列的字段会被自动跳过。
    """
    records = []
    date_str = generated_at.strftime("%Y-%m-%d")

    def _add_field(values: dict, field_name: str, raw_value: Any):
        """如果字段存在于映射中，添加到 values"""
        if field_name not in field_mapping:
            return
        fid, dtype = field_mapping[field_name]
        formatted = _format_value(raw_value, dtype)
        if formatted is not None:
            values[fid] = formatted

    for result in sorted(results, key=lambda x: x.get("index", 0)):
        label = result.get("label", "")
        key = result.get("key", "")
        index = result.get("index", 0)

        # 去掉 emoji 前缀（如 "🦄 硅谷热点" → "硅谷热点"）
        clean_label = label.strip()
        if clean_label and not clean_label[0].isalnum() and not '\u4e00' <= clean_label[0] <= '\u9fff':
            clean_label = clean_label.lstrip().split(" ", 1)[-1] if " " in clean_label else clean_label[1:].strip()

        if result.get("error"):
            values: Dict[str, Any] = {}
            _add_field(values, "日期", date_str)
            _add_field(values, "栏目", clean_label)
            _add_field(values, "来源Key", key)
            _add_field(values, "标题", f"抓取失败: {result['error'][:100]}")
            _add_field(values, "备注", "抓取失败")
            if values:
                records.append({"values": values})
            continue

        items = result.get("items", [])
        if not items:
            if result.get("filtered_out_count", 0) > 0 and result.get("original_item_count", 0) > 0:
                continue
            values = {}
            _add_field(values, "日期", date_str)
            _add_field(values, "栏目", clean_label)
            _add_field(values, "来源Key", key)
            _add_field(values, "标题", "暂无数据")
            _add_field(values, "备注", "暂无数据")
            if values:
                records.append({"values": values})
            continue

        # 按综合评分降序
        sorted_items = sorted(
            items,
            key=lambda it: it.get("analysis", {}).get("composite_score", 0),
            reverse=True,
        )

        for item in sorted_items:
            analysis = item.get("analysis", {})
            values = {}

            _add_field(values, "日期", date_str)
            _add_field(values, "栏目", clean_label)
            _add_field(values, "来源Key", key)
            _add_field(values, "标题", (item.get("title") or "未命名")[:200])
            _add_field(values, "链接", item.get("url", ""))
            _add_field(values, "热度", str(item.get("heat", "")))
            _add_field(values, "中文简介", (
                item.get("summary_cn")
                or item.get("summary")
                or item.get("description")
                or item.get("content", "")
            )[:500])
            _add_field(values, "具体内容摘要", analysis.get("detail_brief", ""))
            _add_field(values, "开发者/作者", item.get("developer_name") or item.get("author", ""))
            _add_field(values, "开发者链接", item.get("developer_url") or item.get("author_url") or item.get("github", ""))
            _add_field(values, "开发者邮箱", item.get("developer_email") or item.get("author_email", ""))
            _add_field(values, "话题分类", analysis.get("topic", ""))
            _add_field(values, "话题类型", analysis.get("topic_type", ""))
            _add_field(values, "是否AI相关", "是" if analysis.get("is_ai") else "否")
            _add_field(values, "是否云行业", "是" if analysis.get("is_cloud") else "否")
            _add_field(values, "是否渲染焦虑", "是" if analysis.get("is_anxiety") else "否")
            _add_field(values, "内容标注", analysis.get("tone", ""))
            _add_field(values, "综合评分", analysis.get("composite_score"))
            _add_field(values, "热度评分", analysis.get("heat_score"))
            _add_field(values, "时效评分", analysis.get("timeliness_score"))
            _add_field(values, "与腾讯云结合度", analysis.get("tcloud_relevance"))
            _add_field(values, "产品标签", ", ".join(analysis.get("products", [])))
            _add_field(values, "友商标签", ", ".join(analysis.get("competitors", [])))
            _add_field(values, "竞品产品标签", ", ".join(analysis.get("competitor_products", [])))
            _add_field(values, "技术标签", ", ".join(analysis.get("techs", [])))
            _add_field(values, "腾讯云结合点", analysis.get("tcloud_integration", ""))
            _add_field(values, "可用角度", analysis.get("editorial_angles_text", ""))
            _add_field(values, "发布优先级", analysis.get("publish_priority", ""))
            _add_field(values, "适合平台", analysis.get("recommended_platforms_text", ""))
            _add_field(values, "社媒结论", analysis.get("social_recommendation", ""))
            _add_field(values, "备注", "")

            if values:
                records.append({"values": values})

    return records


# ── 主写入函数 ────────────────────────────────────────


def _load_write_log() -> Dict[str, Any]:
    """加载写入记录日志"""
    if WRITE_LOG_PATH.exists():
        try:
            return json.loads(WRITE_LOG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_write_log(log: Dict[str, Any]):
    """保存写入记录日志"""
    WRITE_LOG_PATH.write_text(
        json.dumps(log, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _check_duplicate(generated_at: datetime) -> Optional[Dict[str, Any]]:
    """
    检查今天是否已经成功写入过。
    返回 None 表示没有重复；返回 dict 表示上次写入信息。
    """
    log = _load_write_log()
    date_key = generated_at.strftime("%Y-%m-%d")
    today_log = log.get(date_key)
    if today_log and today_log.get("success"):
        return today_log
    return None


def _record_write(generated_at: datetime, records_count: int, success: bool):
    """记录本次写入"""
    log = _load_write_log()
    date_key = generated_at.strftime("%Y-%m-%d")
    log[date_key] = {
        "success": success,
        "records_count": records_count,
        "written_at": datetime.now().isoformat(),
    }
    # 只保留最近 30 天的记录
    if len(log) > 30:
        sorted_keys = sorted(log.keys())
        for old_key in sorted_keys[:-30]:
            del log[old_key]
    _save_write_log(log)


def write_to_wecom_doc(
    results: List[Dict[str, Any]],
    generated_at: datetime,
    webhook_url: Optional[str] = None,
    dry_run: bool = False,
    batch_size: int = 50,
    force: bool = False,
) -> Dict[str, Any]:
    """
    将爬取结果写入企业微信腾讯文档智能表格。

    Args:
        results: 爬取结果列表（含 analysis 字段）
        generated_at: 生成时间
        webhook_url: Webhook URL（可选）
        dry_run: True 只生成数据不实际写入
        batch_size: 每批写入记录数（默认50）
        force: True 则跳过去重检查，强制写入

    Returns:
        {"success": bool, "message": str, "records_count": int, ...}
    """
    if requests is None:
        return {
            "success": False,
            "message": "requests 库未安装，请运行 pip install requests",
            "records_count": 0,
        }

    # 去重检查：同一天只写入一次（除非 force=True 或 dry_run）
    if not force and not dry_run:
        prev = _check_duplicate(generated_at)
        if prev:
            return {
                "success": True,
                "message": (
                    f"⏭️ 今天已于 {prev.get('written_at', '?')} 成功写入过 "
                    f"{prev.get('records_count', '?')} 条记录，跳过重复写入。\n"
                    f"如需强制重新写入，请加 --wecom-doc-force 参数。"
                ),
                "records_count": prev.get("records_count", 0),
                "skipped_duplicate": True,
            }

    # 加载 Webhook URL
    url = _load_webhook_url(webhook_url)
    if not url:
        return {
            "success": False,
            "message": (
                "未配置 Webhook URL。请先运行初始化：\n"
                "  python wecom_doc_writer.py --init\n"
            ),
            "records_count": 0,
        }

    # 加载 schema 并构建字段映射
    schema = _load_schema()
    if not schema:
        return {
            "success": False,
            "message": (
                "未配置字段映射 (schema)。请先运行初始化：\n"
                "  python wecom_doc_writer.py --init\n\n"
                "初始化时需要粘贴智能表格「接收外部数据」页面中的示例数据。"
            ),
            "records_count": 0,
        }

    field_mapping = _build_field_mapping(schema)
    if not field_mapping:
        return {
            "success": False,
            "message": (
                "字段映射为空——schema 中的字段标题与代码中定义的字段名没有匹配项。\n"
                "请检查智能表格中的列名是否与以下名称一致：\n"
                + ", ".join(FIELD_DEFINITIONS.keys())
            ),
            "records_count": 0,
        }

    mapped_fields = [name for name in field_mapping]
    print(
        f"[wecom_doc] 已映射 {len(mapped_fields)}/{len(FIELD_DEFINITIONS)} 个字段: "
        f"{', '.join(mapped_fields)}",
        file=sys.stderr,
    )

    # 构建记录
    records = _build_records_from_results(results, generated_at, field_mapping)

    if not records:
        return {
            "success": False,
            "message": "没有可写入的记录数据",
            "records_count": 0,
        }

    # 脱敏 URL
    masked_url = _mask_url(url)

    if dry_run:
        dry_run_path = (
            Path.home()
            / "Desktop"
            / "每日热点追踪"
            / f"wecom_doc_data_{generated_at.strftime('%Y-%m-%d')}.json"
        )
        dry_run_path.parent.mkdir(parents=True, exist_ok=True)
        dry_run_data = {
            "webhook_url": masked_url,
            "field_mapping": {k: v[0] for k, v in field_mapping.items()},
            "total_records": len(records),
            "sample_records": records[:3],
            "all_records": records,
        }
        dry_run_path.write_text(
            json.dumps(dry_run_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "success": True,
            "message": f"DRY RUN - 数据已生成但未写入（共 {len(records)} 条），预览: {dry_run_path}",
            "records_count": len(records),
            "dry_run_path": str(dry_run_path),
        }

    # 禁用 SSL 警告
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass

    # 分批写入
    total_written = 0
    errors = []

    for i in range(0, len(records), batch_size):
        batch = records[i : i + batch_size]
        payload = {"add_records": batch}

        try:
            resp = requests.post(
                url,
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
                            f"批次 {i // batch_size + 1}: "
                            f"errcode={body.get('errcode')}: {body.get('errmsg', '')}"
                        )
                        continue
                except Exception:
                    pass
                total_written += len(batch)
            else:
                errors.append(
                    f"批次 {i // batch_size + 1}: HTTP {resp.status_code}"
                )
        except Exception as exc:
            errors.append(f"批次 {i // batch_size + 1}: {exc}")

        if i + batch_size < len(records):
            time.sleep(0.5)

    if total_written > 0:
        _record_write(generated_at, total_written, success=True)
        msg = f"✅ 成功写入 {total_written}/{len(records)} 条记录到企业微信智能表格"
        if errors:
            msg += f"\n⚠️ 部分失败:\n" + "\n".join(errors)
        return {
            "success": True,
            "message": msg,
            "records_count": total_written,
            "total_records": len(records),
            "webhook_url": masked_url,
        }
    else:
        fallback_path = (
            Path.home()
            / "Desktop"
            / "每日热点追踪"
            / f"wecom_doc_fallback_{generated_at.strftime('%Y-%m-%d')}.json"
        )
        fallback_path.parent.mkdir(parents=True, exist_ok=True)
        fallback_path.write_text(
            json.dumps({"add_records": records}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "success": False,
            "message": (
                f"❌ 写入失败（0/{len(records)} 条）\n"
                + "\n".join(errors)
                + f"\n\n数据已备份到: {fallback_path}"
            ),
            "records_count": 0,
            "total_records": len(records),
            "fallback_path": str(fallback_path),
        }


# ── 精选话题功能 ──────────────────────────────────────


def _generate_selection_reason(item: Dict[str, Any], analysis: Dict[str, Any]) -> str:
    """生成能直接服务社媒运营的精选结论。"""
    topic_type = analysis.get("topic_type") or analysis.get("topic") or "行业观察"
    angles = analysis.get("editorial_angles_text") or "信息快讯"
    priority = analysis.get("publish_priority") or "P2 观察中"
    platforms = analysis.get("recommended_platforms_text") or "LinkedIn（行业观察）"

    reasons = [
        f"话题类型：{topic_type}",
        f"可用角度：{angles}",
        f"优先级：{priority}",
        f"适合平台：{platforms}",
    ]

    tcloud_point = analysis.get("tcloud_integration", "")
    if tcloud_point:
        reasons.append(f"腾讯云切入：{tcloud_point[:100]}")

    return "｜".join(reasons)


def _filter_selected_topics(
    results: List[Dict[str, Any]],
    composite_threshold: float = SELECTED_COMPOSITE_THRESHOLD,
    tcloud_threshold: float = SELECTED_TCLOUD_THRESHOLD,
) -> List[Dict[str, Any]]:
    """
    从全量结果中筛选精选话题。

    筛选条件（满足任一即入选）：
    1. 综合评分 >= composite_threshold（默认 7.0）
    2. 腾讯云结合度 >= tcloud_threshold（默认 6.0）

    返回: 按综合评分降序排列的 [{item, source_label, source_key, reason}, ...]
    """
    selected = []
    for result in results:
        label = result.get("label", "")
        key = result.get("key", "")
        for item in result.get("items", []):
            analysis = item.get("analysis", {})
            cs = analysis.get("composite_score", 0) or 0
            tc = analysis.get("tcloud_relevance", 0) or 0

            if cs >= composite_threshold or tc >= tcloud_threshold:
                reason = _generate_selection_reason(item, analysis)
                selected.append({
                    "item": item,
                    "analysis": analysis,
                    "source_label": label,
                    "source_key": key,
                    "reason": reason,
                    "composite_score": cs,
                })

    # 按综合评分降序
    selected.sort(key=lambda x: x["composite_score"], reverse=True)
    return selected


def _build_selected_records(
    selected: List[Dict[str, Any]],
    generated_at: datetime,
    field_mapping: Dict[str, Tuple[str, str]],
) -> List[Dict[str, Any]]:
    """
    将精选话题列表转换为智能表格记录格式。
    """
    records = []
    date_str = generated_at.strftime("%Y-%m-%d")

    def _add_field(values: dict, field_name: str, raw_value: Any):
        if field_name not in field_mapping:
            return
        fid, dtype = field_mapping[field_name]
        formatted = _format_value(raw_value, dtype)
        if formatted is not None:
            values[fid] = formatted

    for sel in selected:
        item = sel["item"]
        analysis = sel["analysis"]
        label = sel["source_label"]
        key = sel["source_key"]
        reason = sel["reason"]

        # 去掉 emoji 前缀
        clean_label = label.strip()
        if clean_label and not clean_label[0].isalnum() and not '\u4e00' <= clean_label[0] <= '\u9fff':
            clean_label = clean_label.lstrip().split(" ", 1)[-1] if " " in clean_label else clean_label[1:].strip()

        values: Dict[str, Any] = {}
        _add_field(values, "日期", date_str)
        _add_field(values, "栏目", clean_label)
        _add_field(values, "来源Key", key)
        _add_field(values, "标题", (item.get("title") or "未命名")[:200])
        _add_field(values, "链接", item.get("url", ""))
        _add_field(values, "中文简介", (
            item.get("summary_cn")
            or item.get("summary")
            or item.get("description")
            or item.get("content", "")
        )[:500])
        _add_field(values, "具体内容摘要", analysis.get("detail_brief", ""))
        _add_field(values, "开发者/作者", item.get("developer_name") or item.get("author", ""))
        _add_field(values, "综合评分", analysis.get("composite_score"))
        _add_field(values, "与腾讯云结合度", analysis.get("tcloud_relevance"))
        _add_field(values, "腾讯云结合点", analysis.get("tcloud_integration", ""))
        _add_field(values, "产品标签", ", ".join(analysis.get("products", [])))
        _add_field(values, "友商标签", ", ".join(analysis.get("competitors", [])))
        _add_field(values, "竞品产品标签", ", ".join(analysis.get("competitor_products", [])))
        _add_field(values, "技术标签", ", ".join(analysis.get("techs", [])))
        _add_field(values, "话题分类", analysis.get("topic", ""))
        _add_field(values, "话题类型", analysis.get("topic_type", ""))
        _add_field(values, "内容标注", analysis.get("tone", ""))
        _add_field(values, "可用角度", analysis.get("editorial_angles_text", ""))
        _add_field(values, "发布优先级", analysis.get("publish_priority", ""))
        _add_field(values, "适合平台", analysis.get("recommended_platforms_text", ""))
        _add_field(values, "社媒结论", analysis.get("social_recommendation", ""))
        _add_field(values, "精选理由", reason)
        _add_field(values, "备注", "")

        if values:
            records.append({"values": values})

    return records


def write_selected_topics(
    results: List[Dict[str, Any]],
    generated_at: datetime,
    webhook_url: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False,
    composite_threshold: float = SELECTED_COMPOSITE_THRESHOLD,
    tcloud_threshold: float = SELECTED_TCLOUD_THRESHOLD,
) -> Dict[str, Any]:
    """
    筛选精选话题并写入企业微信智能表格的「精选话题」子表。

    筛选条件（满足任一即入选）：
    - 综合评分 >= composite_threshold（默认 7.0）
    - 腾讯云结合度 >= tcloud_threshold（默认 6.0）

    Args:
        results: 爬取结果列表（含 analysis 字段）
        generated_at: 生成时间
        webhook_url: 精选话题子表的 Webhook URL（可选，否则从配置读取）
        dry_run: True 只生成数据不实际写入
        force: True 则跳过去重检查
        composite_threshold: 综合评分筛选阈值
        tcloud_threshold: 腾讯云结合度筛选阈值

    Returns:
        {"success": bool, "message": str, "records_count": int, "selected_count": int, ...}
    """
    if requests is None:
        return {
            "success": False,
            "message": "requests 库未安装，请运行 pip install requests",
            "records_count": 0,
        }

    # 筛选精选话题
    selected = _filter_selected_topics(results, composite_threshold, tcloud_threshold)
    total_items = sum(len(r.get("items", [])) for r in results)

    if not selected:
        return {
            "success": True,
            "message": (
                f"今日 {total_items} 条中没有满足精选条件的话题 "
                f"(综合评分≥{composite_threshold} 或 腾讯云结合度≥{tcloud_threshold})"
            ),
            "records_count": 0,
            "selected_count": 0,
        }

    print(
        f"[wecom_doc_selected] 精选话题: {len(selected)}/{total_items} 条 "
        f"(综合评分≥{composite_threshold} 或 腾讯云结合度≥{tcloud_threshold})",
        file=sys.stderr,
    )

    # 去重检查（精选话题用独立的 log key）
    if not force and not dry_run:
        log = _load_write_log()
        date_key = generated_at.strftime("%Y-%m-%d")
        sel_key = f"{date_key}_selected"
        sel_log = log.get(sel_key)
        if sel_log and sel_log.get("success"):
            return {
                "success": True,
                "message": (
                    f"⏭️ 精选话题已于 {sel_log.get('written_at', '?')} 成功写入过 "
                    f"{sel_log.get('records_count', '?')} 条，跳过重复写入。\n"
                    f"如需强制重新写入，请加 --wecom-doc-force 参数。"
                ),
                "records_count": sel_log.get("records_count", 0),
                "selected_count": sel_log.get("records_count", 0),
                "skipped_duplicate": True,
            }

    # 加载精选话题的 Webhook URL
    if not webhook_url:
        config = load_config()
        webhook_url = config.get("selected_webhook_url", "").strip()

    if not webhook_url:
        # 如果没有配置精选话题的独立 Webhook，生成提示
        return {
            "success": False,
            "message": (
                "❌ 未配置精选话题子表的 Webhook URL。\n\n"
                "请按以下步骤操作：\n"
                "1. 在企业微信智能表格中新建一个工作表，命名为「精选话题」\n"
                "2. 添加以下列：日期、栏目、来源Key、标题、链接、中文简介、\n"
                "   开发者/作者、综合评分、与腾讯云结合度、腾讯云结合点、\n"
                "   产品标签、技术标签、话题分类、内容标注、精选理由、备注\n"
                "3. 点击该工作表的 ... → 「接收外部数据」\n"
                "4. 运行: python wecom_doc_writer.py --init-selected\n"
                "   按提示粘贴 Webhook URL 和示例数据\n"
            ),
            "records_count": 0,
            "selected_count": len(selected),
        }

    # 加载精选话题的 schema
    schema = _load_schema("selected_schema")
    if not schema:
        return {
            "success": False,
            "message": (
                "❌ 未配置精选话题子表的字段映射。\n"
                "请运行: python wecom_doc_writer.py --init-selected\n"
            ),
            "records_count": 0,
            "selected_count": len(selected),
        }

    field_mapping = _build_field_mapping(
        schema,
        field_defs=SELECTED_FIELD_DEFINITIONS,
        ft_key="selected_field_types",
    )
    if not field_mapping:
        return {
            "success": False,
            "message": "精选话题字段映射为空，请检查子表列名是否正确。",
            "records_count": 0,
            "selected_count": len(selected),
        }

    mapped_fields = list(field_mapping.keys())
    print(
        f"[wecom_doc_selected] 已映射 {len(mapped_fields)}/{len(SELECTED_FIELD_DEFINITIONS)} 个字段: "
        f"{', '.join(mapped_fields)}",
        file=sys.stderr,
    )

    # 构建记录
    records = _build_selected_records(selected, generated_at, field_mapping)
    if not records:
        return {
            "success": False,
            "message": "没有可写入的精选记录",
            "records_count": 0,
            "selected_count": len(selected),
        }

    masked_url = _mask_url(webhook_url)

    if dry_run:
        dry_run_path = (
            Path.home()
            / "Desktop"
            / "每日热点追踪"
            / f"wecom_doc_selected_{generated_at.strftime('%Y-%m-%d')}.json"
        )
        dry_run_path.parent.mkdir(parents=True, exist_ok=True)
        dry_run_data = {
            "type": "精选话题",
            "webhook_url": masked_url,
            "filter_criteria": {
                "composite_threshold": composite_threshold,
                "tcloud_threshold": tcloud_threshold,
            },
            "field_mapping": {k: v[0] for k, v in field_mapping.items()},
            "selected_count": len(selected),
            "total_items": total_items,
            "records": records,
            "selected_details": [
                {
                    "title": s["item"].get("title", "")[:60],
                    "composite_score": s["composite_score"],
                    "tcloud_relevance": s["analysis"].get("tcloud_relevance", 0),
                    "reason": s["reason"],
                }
                for s in selected
            ],
        }
        dry_run_path.write_text(
            json.dumps(dry_run_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "success": True,
            "message": (
                f"DRY RUN - 精选话题 {len(records)}/{total_items} 条已生成但未写入，"
                f"预览: {dry_run_path}"
            ),
            "records_count": len(records),
            "selected_count": len(selected),
            "dry_run_path": str(dry_run_path),
        }

    # 禁用 SSL 警告
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass

    # 写入（精选话题通常不超过 50 条，一批搞定）
    payload = {"add_records": records}
    try:
        resp = requests.post(
            webhook_url,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=30,
            verify=False,
        )
        if resp.status_code == 200:
            body = resp.json()
            if body.get("errcode", 0) != 0:
                return {
                    "success": False,
                    "message": (
                        f"❌ 精选话题写入失败: errcode={body.get('errcode')}: "
                        f"{body.get('errmsg', '')}"
                    ),
                    "records_count": 0,
                    "selected_count": len(selected),
                }

            # 记录写入
            log = _load_write_log()
            date_key = generated_at.strftime("%Y-%m-%d")
            log[f"{date_key}_selected"] = {
                "success": True,
                "records_count": len(records),
                "written_at": datetime.now().isoformat(),
            }
            _save_write_log(log)

            return {
                "success": True,
                "message": (
                    f"✅ 精选话题 {len(records)}/{total_items} 条已写入智能表格「精选话题」子表"
                ),
                "records_count": len(records),
                "selected_count": len(selected),
                "webhook_url": masked_url,
            }
        else:
            return {
                "success": False,
                "message": f"❌ 精选话题写入失败: HTTP {resp.status_code}",
                "records_count": 0,
                "selected_count": len(selected),
            }
    except Exception as exc:
        return {
            "success": False,
            "message": f"❌ 精选话题写入异常: {exc}",
            "records_count": 0,
            "selected_count": len(selected),
        }


def _mask_url(url: str) -> str:
    """脱敏 Webhook URL"""
    if "key=" in url:
        parts = url.split("key=")
        if len(parts) > 1:
            key_value = parts[1].split("&")[0]
            if len(key_value) > 8:
                masked = key_value[:4] + "****" + key_value[-4:]
            else:
                masked = "****"
            return url.replace(key_value, masked)
    return url


# ── 交互式初始化 ──────────────────────────────────────


def interactive_init():
    """
    交互式初始化配置。
    从用户输入中提取 Webhook URL 和 schema。
    """
    print("""
╔══════════════════════════════════════════════════════════╗
║   📊 企业微信智能表格 - 一键初始化配置                     ║
╚══════════════════════════════════════════════════════════╝

请按照以下步骤操作：

1. 在企业微信中打开你的智能表格
2. 点击智能表旁边的 ... → 「接收外部数据」
3. 在弹出的面板中：
   - 复制 Webhook 地址
   - 复制「示例数据」（JSON 格式）
""")

    # 加载现有配置
    config = load_config()

    # 输入 Webhook URL
    existing_url = config.get("webhook_url", "")
    if existing_url:
        print(f"当前 Webhook URL: {_mask_url(existing_url)}")
        inp = input("输入新的 Webhook URL（直接回车保持不变）: ").strip()
        if inp:
            config["webhook_url"] = inp
    else:
        url = input("请粘贴 Webhook URL: ").strip()
        if not url:
            print("❌ URL 不能为空！")
            return
        config["webhook_url"] = url

    print()

    # 输入示例数据
    print("请粘贴「示例数据」JSON（粘贴完后按两次回车确认）:")
    print("（示例数据通常是 {\"schema\": {...}, \"add_records\": [...]} 格式）")
    print()

    lines = []
    empty_count = 0
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "":
            empty_count += 1
            if empty_count >= 2:
                break
        else:
            empty_count = 0
            lines.append(line)

    raw_json = "\n".join(lines).strip()
    if raw_json:
        try:
            sample_data = json.loads(raw_json)
            if "schema" in sample_data:
                config["schema"] = sample_data["schema"]
                schema = sample_data["schema"]
                print(f"\n✅ 解析到 {len(schema)} 个字段:")
                for fid, fname in schema.items():
                    matched = "✅" if fname in FIELD_DEFINITIONS else "⚠️ (未匹配)"
                    print(f"   {fid} → {fname} {matched}")

                # 检查哪些需要的字段还没有
                existing_titles = set(schema.values())
                missing = [
                    name for name in FIELD_DEFINITIONS
                    if name not in existing_titles
                ]
                if missing:
                    print(f"\n💡 以下字段在表格中还未创建（可选添加）:")
                    for m in missing:
                        print(f"   - {m} ({FIELD_DEFINITIONS[m]})")
                    print("   不创建也没关系，脚本会自动跳过这些字段。")
            else:
                print("⚠️ JSON 中没有找到 schema 字段。请确认粘贴的是「示例数据」。")
                return
        except json.JSONDecodeError as e:
            print(f"❌ JSON 解析失败: {e}")
            print("请确认粘贴的是完整的 JSON 格式数据。")
            return
    elif "schema" not in config:
        print("⚠️ 未输入示例数据且配置中无 schema，初始化未完成。")
        return

    save_config(config)

    # 显示映射结果
    field_mapping = _build_field_mapping(config.get("schema", {}))
    print(f"\n📋 字段映射结果 ({len(field_mapping)} 个可用):")
    for name, (fid, dtype) in field_mapping.items():
        print(f"   {name} → {fid} ({dtype})")

    print(f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ 初始化完成！

现在可以运行：
  # 预览数据（不写入）
  ./run_daily_hotspot.sh --wecom-doc-dry-run

  # 正式写入
  ./run_daily_hotspot.sh --wecom-doc

💡 按日分区：在智能表格中点击「分组」→ 选择「日期」列，
   即可按天查看每日爬取的数据。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")


def quick_init_from_json(json_str: str, webhook_url: Optional[str] = None):
    """
    非交互式初始化——直接传入 schema JSON 和 webhook_url。
    方便脚本调用。
    """
    config = load_config()

    if webhook_url:
        config["webhook_url"] = webhook_url

    try:
        data = json.loads(json_str)
        if "schema" in data:
            config["schema"] = data["schema"]
        elif all(isinstance(v, str) for v in data.values()):
            # 直接传入的就是 schema
            config["schema"] = data
    except json.JSONDecodeError as e:
        print(f"❌ JSON 解析失败: {e}", file=sys.stderr)
        return False

    save_config(config)
    return True


def interactive_init_selected():
    """
    交互式初始化「精选话题」子表配置。
    """
    print("""
╔══════════════════════════════════════════════════════════╗
║   ⭐ 精选话题子表 - 初始化配置                             ║
╚══════════════════════════════════════════════════════════╝

请按照以下步骤操作：

1. 在企业微信智能表格中新建一个工作表，命名为「精选话题」
2. 添加以下列（建议）：
   日期、栏目、来源Key、标题、链接、中文简介、具体内容摘要、
   开发者/作者、综合评分、与腾讯云结合度、腾讯云结合点、
   产品标签、友商标签、竞品产品标签、技术标签、话题分类、话题类型、
   可用角度、发布优先级、适合平台、社媒结论、内容标注、精选理由、备注
3. 点击该工作表的 ... → 「接收外部数据」
4. 复制 Webhook 地址和「示例数据」
""")

    config = load_config()

    # 输入 Webhook URL
    existing_url = config.get("selected_webhook_url", "")
    if existing_url:
        print(f"当前精选话题 Webhook URL: {_mask_url(existing_url)}")
        inp = input("输入新的 Webhook URL（直接回车保持不变）: ").strip()
        if inp:
            config["selected_webhook_url"] = inp
    else:
        url = input("请粘贴精选话题子表的 Webhook URL: ").strip()
        if not url:
            print("❌ URL 不能为空！")
            return
        config["selected_webhook_url"] = url

    print()

    # 输入示例数据
    print("请粘贴「示例数据」JSON（粘贴完后按两次回车确认）:")
    print()

    lines = []
    empty_count = 0
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "":
            empty_count += 1
            if empty_count >= 2:
                break
        else:
            empty_count = 0
            lines.append(line)

    raw_json = "\n".join(lines).strip()
    if raw_json:
        try:
            sample_data = json.loads(raw_json)
            if "schema" in sample_data:
                config["selected_schema"] = sample_data["schema"]
                schema = sample_data["schema"]

                # 自动推断 field_types
                field_types = {}
                title_to_type = {v: k for k, v in SELECTED_FIELD_DEFINITIONS.items()}
                for fid, ftitle in schema.items():
                    if ftitle in SELECTED_FIELD_DEFINITIONS:
                        field_types[fid] = SELECTED_FIELD_DEFINITIONS[ftitle]
                if field_types:
                    config["selected_field_types"] = field_types

                print(f"\n✅ 解析到 {len(schema)} 个字段:")
                for fid, fname in schema.items():
                    matched = "✅" if fname in SELECTED_FIELD_DEFINITIONS else "⚠️ (未匹配)"
                    print(f"   {fid} → {fname} {matched}")

                existing_titles = set(schema.values())
                missing = [
                    name for name in SELECTED_FIELD_DEFINITIONS
                    if name not in existing_titles
                ]
                if missing:
                    print(f"\n💡 以下字段在精选话题表中还未创建（可选添加）:")
                    for m in missing:
                        print(f"   - {m} ({SELECTED_FIELD_DEFINITIONS[m]})")
            else:
                print("⚠️ JSON 中没有找到 schema 字段。")
                return
        except json.JSONDecodeError as e:
            print(f"❌ JSON 解析失败: {e}")
            return
    elif "selected_schema" not in config:
        print("⚠️ 未输入示例数据且配置中无 selected_schema，初始化未完成。")
        return

    save_config(config)

    # 显示映射结果
    field_mapping = _build_field_mapping(
        config.get("selected_schema", {}),
        field_defs=SELECTED_FIELD_DEFINITIONS,
        ft_key="selected_field_types",
    )
    print(f"\n📋 精选话题字段映射 ({len(field_mapping)} 个可用):")
    for name, (fid, dtype) in field_mapping.items():
        print(f"   {name} → {fid} ({dtype})")

    print(f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ 精选话题子表初始化完成！

筛选条件：
  - 综合评分 ≥ {SELECTED_COMPOSITE_THRESHOLD}
  - 或 腾讯云结合度 ≥ {SELECTED_TCLOUD_THRESHOLD}

运行方式：
  ./run_daily_hotspot.sh --wecom-doc   # 会同时写入全量表 + 精选话题表
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")


# ── CLI 入口 ──────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "--init":
            interactive_init()
        elif cmd == "--init-selected":
            interactive_init_selected()
        elif cmd == "--show-config":
            config = load_config()
            if config:
                # 脱敏显示
                display = dict(config)
                if "webhook_url" in display:
                    display["webhook_url"] = _mask_url(display["webhook_url"])
                if "selected_webhook_url" in display:
                    display["selected_webhook_url"] = _mask_url(display["selected_webhook_url"])
                print(json.dumps(display, ensure_ascii=False, indent=2))
            else:
                print("未找到配置文件。请先运行: python wecom_doc_writer.py --init")
        elif cmd == "--show-fields":
            print("全量表字段:")
            for name, dtype in FIELD_DEFINITIONS.items():
                print(f"  {name:12s} ({dtype})")
            print(f"\n精选话题表字段:")
            for name, dtype in SELECTED_FIELD_DEFINITIONS.items():
                print(f"  {name:12s} ({dtype})")
            print(f"\n精选条件: 综合评分≥{SELECTED_COMPOSITE_THRESHOLD} 或 腾讯云结合度≥{SELECTED_TCLOUD_THRESHOLD}")
        else:
            print(f"未知命令: {cmd}")
            print("用法:")
            print("  python wecom_doc_writer.py --init              # 初始化全量表配置")
            print("  python wecom_doc_writer.py --init-selected     # 初始化精选话题子表配置")
            print("  python wecom_doc_writer.py --show-config       # 查看当前配置")
            print("  python wecom_doc_writer.py --show-fields       # 查看所有字段定义")
    else:
        print("用法:")
        print("  python wecom_doc_writer.py --init              # 初始化全量表配置")
        print("  python wecom_doc_writer.py --init-selected     # 初始化精选话题子表配置")
        print("  python wecom_doc_writer.py --show-config       # 查看当前配置")
        print("  python wecom_doc_writer.py --show-fields       # 查看所有字段定义")
        print("  # 通常通过 daily_hotspot_agent.py --wecom-doc 调用")
