#!/usr/bin/env python3
"""
热点追踪 Web UI 后端
====================
基于 Flask 的轻量 Web 应用，提供：
1. 一键运行数据采集
2. 查看/筛选热点数据
3. 同步到腾讯文档
4. 查看每日总结

启动：python3 web_app.py
访问：http://localhost:5088
"""

import csv
import json
import os
import subprocess
import sys
import threading
import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path

try:
    from urllib3.exceptions import NotOpenSSLWarning
except Exception:
    NotOpenSSLWarning = None

if NotOpenSSLWarning is not None:
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)

try:
    from flask import Flask, jsonify, request, send_from_directory
except ImportError:
    print("❌ Flask 未安装，正在安装...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "flask"])
    from flask import Flask, jsonify, request, send_from_directory

# ════════════════════════════════════════════
# 配置
# ════════════════════════════════════════════

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")

app = Flask(__name__, static_folder=None)

# 全局运行状态
run_state = {
    "running": False,
    "progress": "",
    "log": [],
    "last_run": None,
    "last_result": None,
}
run_lock = threading.Lock()


# ════════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════════


def get_available_dates():
    """获取所有可用的日期（有 CSV 数据的）"""
    dates = set()
    if not os.path.isdir(OUTPUT_DIR):
        return []
    for f in os.listdir(OUTPUT_DIR):
        if f.startswith("每日热点汇总表_") and f.endswith(".csv"):
            # 每日热点汇总表_2026-03-22.csv 或 每日热点汇总表_2026-03-22_1519.csv
            parts = f.replace("每日热点汇总表_", "").replace(".csv", "")
            date_part = parts[:10]  # YYYY-MM-DD
            if len(date_part) == 10:
                dates.add(date_part)
    # 也检查 archive
    archive_dir = os.path.join(OUTPUT_DIR, "archive")
    if os.path.isdir(archive_dir):
        for d in os.listdir(archive_dir):
            if len(d) == 10 and d[4] == "-":
                dates.add(d)
    return sorted(dates, reverse=True)


def find_csv(date_str):
    """找到指定日期的 CSV 文件"""
    import glob
    # 优先 archive
    pattern = os.path.join(OUTPUT_DIR, "archive", date_str, f"每日热点汇总表_{date_str}_*.csv")
    files = sorted(glob.glob(pattern), reverse=True)
    if files:
        return files[0]
    # dated
    pattern2 = os.path.join(OUTPUT_DIR, f"每日热点汇总表_{date_str}*.csv")
    files2 = sorted(glob.glob(pattern2), reverse=True)
    if files2:
        return files2[0]
    # 今日
    latest = os.path.join(OUTPUT_DIR, "今日热点汇总表.csv")
    if os.path.exists(latest):
        return latest
    return None


def read_csv_rows(csv_path):
    """读取 CSV 为字典列表"""
    rows = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(dict(row))
    return rows


def read_summary(date_str):
    """读取指定日期的总结"""
    for name in [f"每日热点趋势总结_{date_str}.md", "今日热点趋势总结.md"]:
        path = os.path.join(OUTPUT_DIR, name)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    return None


# ════════════════════════════════════════════
# API 路由
# ════════════════════════════════════════════


@app.route("/")
def index():
    """主页"""
    return send_from_directory(BASE_DIR, "web_ui.html")


@app.route("/api/status")
def api_status():
    """获取系统状态"""
    dates = get_available_dates()
    today = datetime.now().strftime("%Y-%m-%d")
    has_today = today in dates

    return jsonify({
        "ok": True,
        "today": today,
        "has_today_data": has_today,
        "available_dates": dates[:30],
        "running": run_state["running"],
        "last_run": run_state["last_run"],
        "progress": run_state["progress"],
    })


@app.route("/api/run", methods=["POST"])
def api_run():
    """触发一次完整的数据采集 + 同步"""
    with run_lock:
        if run_state["running"]:
            return jsonify({"ok": False, "error": "已有任务正在运行"}), 409

        run_state["running"] = True
        run_state["progress"] = "启动中..."
        run_state["log"] = []

    def _run_task():
        try:
            steps = [
                ("📡 数据采集中...", [
                    sys.executable, os.path.join(SCRIPTS_DIR, "daily_hotspot_agent.py"),
                    "--output-dir", OUTPUT_DIR,
                ]),
                ("📊 合并历史总表...", [
                    sys.executable, os.path.join(BASE_DIR, "merge_master_table.py"),
                    "--output-dir", OUTPUT_DIR,
                ]),
                ("📤 同步到腾讯文档...", [
                    sys.executable, os.path.join(BASE_DIR, "sync_to_wecom_sheet.py"),
                    "--output-dir", OUTPUT_DIR,
                    "--date", datetime.now().strftime("%Y-%m-%d"),
                    "--force",
                ]),
            ]

            for i, (desc, cmd) in enumerate(steps):
                run_state["progress"] = f"[{i+1}/{len(steps)}] {desc}"
                run_state["log"].append(f"\n{'='*50}\n{desc}\n{'='*50}")

                try:
                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=600,
                        cwd=SCRIPTS_DIR if i == 0 else BASE_DIR,
                    )
                    if result.stdout:
                        run_state["log"].append(result.stdout[-2000:])
                    if result.returncode != 0:
                        run_state["log"].append(f"⚠️ 退出码: {result.returncode}")
                        if result.stderr:
                            run_state["log"].append(result.stderr[-1000:])
                except subprocess.TimeoutExpired:
                    run_state["log"].append("⏰ 超时（10分钟限制）")
                except Exception as e:
                    run_state["log"].append(f"❌ 错误: {e}")

            run_state["progress"] = "✅ 完成！"
            run_state["last_run"] = datetime.now().isoformat()
            run_state["last_result"] = "success"

        except Exception as e:
            run_state["progress"] = f"❌ 失败: {e}"
            run_state["last_result"] = "error"
        finally:
            run_state["running"] = False

    thread = threading.Thread(target=_run_task, daemon=True)
    thread.start()

    return jsonify({"ok": True, "message": "任务已启动"})


@app.route("/api/run/log")
def api_run_log():
    """获取运行日志"""
    return jsonify({
        "ok": True,
        "running": run_state["running"],
        "progress": run_state["progress"],
        "log": "\n".join(run_state["log"])[-5000:],
    })


@app.route("/api/data/<date_str>")
def api_data(date_str):
    """获取指定日期的热点数据"""
    csv_path = find_csv(date_str)
    if not csv_path:
        return jsonify({"ok": False, "error": f"未找到 {date_str} 的数据"}), 404

    rows = read_csv_rows(csv_path)

    # 可选过滤
    source = request.args.get("source")
    ai_only = request.args.get("ai_only")
    cloud_only = request.args.get("cloud_only")
    min_score = request.args.get("min_score")
    priority = request.args.get("priority")

    filtered = rows
    if source:
        filtered = [r for r in filtered if r.get("来源Key") == source]
    if ai_only == "true":
        filtered = [r for r in filtered if r.get("是否AI相关") == "是"]
    if cloud_only == "true":
        filtered = [r for r in filtered if r.get("是否云行业") == "是"]
    if min_score:
        try:
            ms = float(min_score)
            filtered = [r for r in filtered if float(r.get("综合评分", "0") or "0") >= ms]
        except ValueError:
            pass
    if priority:
        filtered = [r for r in filtered if r.get("发布优先级", "").startswith(priority)]

    # 统计
    stats = {
        "total": len(rows),
        "filtered": len(filtered),
        "ai_count": sum(1 for r in rows if r.get("是否AI相关") == "是"),
        "cloud_count": sum(1 for r in rows if r.get("是否云行业") == "是"),
        "sources": {},
    }
    for r in rows:
        src = r.get("来源Key", "unknown")
        stats["sources"][src] = stats["sources"].get(src, 0) + 1

    return jsonify({
        "ok": True,
        "date": date_str,
        "stats": stats,
        "records": filtered,
    })


@app.route("/api/summary/<date_str>")
def api_summary(date_str):
    """获取每日总结"""
    content = read_summary(date_str)
    if not content:
        return jsonify({"ok": False, "error": f"未找到 {date_str} 的总结"}), 404
    return jsonify({"ok": True, "date": date_str, "content": content})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    """手动同步指定日期到腾讯文档"""
    data = request.get_json() or {}
    date_str = data.get("date", datetime.now().strftime("%Y-%m-%d"))
    target = data.get("target", "all")  # all / full / selected

    cmd = [
        sys.executable,
        os.path.join(BASE_DIR, "sync_to_wecom_sheet.py"),
        "--output-dir", OUTPUT_DIR,
        "--date", date_str,
        "--force",
    ]
    if target == "full":
        cmd.append("--full-only")
    elif target == "selected":
        cmd.append("--selected-only")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return jsonify({
            "ok": result.returncode == 0,
            "output": result.stdout[-3000:],
            "error": result.stderr[-1000:] if result.returncode != 0 else None,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/writing-pack/<date_str>")
def api_writing_pack(date_str):
    """获取写作包"""
    for name in [f"每日热点内容写作包_{date_str}.md", "今日热点内容写作包.md"]:
        path = os.path.join(OUTPUT_DIR, name)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return jsonify({"ok": True, "date": date_str, "content": f.read()})
    return jsonify({"ok": False, "error": "未找到写作包"}), 404


# ════════════════════════════════════════════
# 启动
# ════════════════════════════════════════════

if __name__ == "__main__":
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print("🌐 热点追踪 Web UI 启动中...")
    print("📍 访问地址: http://localhost:5088")
    print(f"📂 数据目录: {OUTPUT_DIR}")
    app.run(host="0.0.0.0", port=5088, debug=False)
