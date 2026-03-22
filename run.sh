#!/bin/bash
# ==========================================
# 每日热点追踪 —— 一键运行脚本
# ==========================================
#
# 用法：
#   ./run.sh              # 默认输出到当前目录下 output/
#   ./run.sh ~/Desktop    # 指定输出目录
#
# 功能：
#   1. 抓取 7 个来源（HackerNews / GitHub / 36kr / ProductHunt / V2EX / HuggingFace / AI Newsletter）
#   2. 深度内容抓取（GitHub README 全文 / 社区帖子全文等）
#   3. 内容分析标注（产品关联 / 竞品识别 / 写作角度 / 社媒结论）
#   4. Google Translate 整句翻译英文摘要为中文
#   5. 输出 CSV 汇总表 + Markdown 写作包 + JSON 归档
#
# 依赖：
#   pip3 install googletrans==4.0.0-rc1
#

set -e

# 脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 核心代码目录（使用项目内的 scripts/ 目录）
CORE_DIR="${SCRIPT_DIR}/scripts"

# 输出目录（默认：当前目录下的 output/）
OUTPUT_DIR="${1:-${SCRIPT_DIR}/output}"

# 日期标记
TODAY=$(date +%Y-%m-%d)

echo "=========================================="
echo "  📡 每日热点追踪  ${TODAY}"
echo "=========================================="
echo ""
echo "  核心脚本: ${CORE_DIR}"
echo "  输出目录: ${OUTPUT_DIR}"
echo ""

# 检查核心脚本是否存在
if [ ! -f "${CORE_DIR}/daily_hotspot_agent.py" ]; then
    echo "❌ 核心脚本不存在: ${CORE_DIR}/daily_hotspot_agent.py"
    echo "   请检查路径是否正确。"
    exit 1
fi

# 检查 googletrans 依赖
if ! python3 -c "import googletrans" 2>/dev/null; then
    echo "⚠️  googletrans 未安装，正在安装..."
    pip3 install googletrans==4.0.0-rc1
    echo ""
fi

# 创建输出目录
mkdir -p "${OUTPUT_DIR}"

# 运行
echo "🚀 开始运行..."
echo ""

cd "${CORE_DIR}"
python3 daily_hotspot_agent.py --output-dir "${OUTPUT_DIR}"

echo ""
echo "=========================================="
echo "  ✅ 数据采集完成！正在合并历史总表..."
echo "=========================================="
echo ""

# 合并历史总表
python3 "${SCRIPT_DIR}/merge_master_table.py" --output-dir "${OUTPUT_DIR}"

echo ""
echo "=========================================="
echo "  📤 同步到腾讯文档多维表格 + 生成每日总结..."
echo "=========================================="
echo ""

# 同步到腾讯文档（全量热点 + 精选话题 + 每日总结）
python3 "${SCRIPT_DIR}/sync_to_wecom_sheet.py" --output-dir "${OUTPUT_DIR}" --date "${TODAY}" --force

echo ""
echo "=========================================="
echo "  🌐 生成静态站点数据..."
echo "=========================================="
echo ""

# 生成静态站点 JSON 数据
python3 "${SCRIPT_DIR}/generate_static.py" --output-dir "${OUTPUT_DIR}" --site-dir "${SCRIPT_DIR}/docs"

echo ""
echo "=========================================="
echo "  📁 输出文件一览："
echo "=========================================="
echo ""
echo "  📊 历史总表（跨天累计）:"
ls -la "${OUTPUT_DIR}/热点追踪历史总表.csv" 2>/dev/null || echo "    (暂无)"
echo ""
echo "  📋 今日数据:"
ls -la "${OUTPUT_DIR}/每日热点汇总表_${TODAY}.csv" 2>/dev/null || echo "    (暂无)"
ls -la "${OUTPUT_DIR}/每日热点内容写作包_${TODAY}.md" 2>/dev/null || echo "    (暂无)"
ls -la "${OUTPUT_DIR}/每日热点趋势总结_${TODAY}.md" 2>/dev/null || echo "    (暂无)"
echo ""
echo "  ☁️  腾讯文档同步:"
echo "    全量热点表 + 精选话题表 已自动写入"
echo ""
echo "  🌐 静态站点:"
echo "    docs/index.html + docs/data/*.json"
echo "    推送到 GitHub 后自动部署到 GitHub Pages"
echo ""
echo "  📂 完整输出目录: ${OUTPUT_DIR}"
echo "=========================================="
