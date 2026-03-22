#!/bin/bash
# ==========================================
# 热点追踪工具 —— 一键安装配置
# ==========================================
# 新用户首次使用时运行此脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "╔═══════════════════════════════════════════════════╗"
echo "║   🔧 热点追踪工具 —— 初始化安装                    ║"
echo "╚═══════════════════════════════════════════════════╝"
echo ""

# 1. 检查 Python3
if ! command -v python3 &>/dev/null; then
    echo "❌ 未找到 python3，请先安装 Python 3.8+"
    exit 1
fi
echo "✅ Python3: $(python3 --version)"

# 2. 安装依赖
echo ""
echo "📦 安装 Python 依赖..."
pip3 install -r "${SCRIPT_DIR}/requirements.txt"
echo "✅ 依赖安装完成"

# 3. 创建输出目录
mkdir -p "${SCRIPT_DIR}/output"

# 4. 赋予运行权限
chmod +x "${SCRIPT_DIR}/run.sh"

echo ""
echo "=========================================="
echo "  ✅ 安装完成！"
echo "=========================================="
echo ""
echo "  使用方法："
echo ""
echo "  1. 运行数据采集:"
echo "     ./run.sh"
echo ""
echo "  2. 手动同步到腾讯文档（如果不想跑采集只想同步历史数据）:"
echo "     python3 sync_to_wecom_sheet.py --date 2026-03-22"
echo ""
echo "  3. 查看帮助:"
echo "     python3 sync_to_wecom_sheet.py --help"
echo ""
echo "  ⚙️  配置说明:"
echo "     腾讯文档 Webhook 地址在 sync_to_wecom_sheet.py 中配置"
echo "     如果要写入你自己的腾讯文档表格，请修改:"
echo "       FULL_TABLE_WEBHOOK    —— 全量热点表"
echo "       FULL_TABLE_SCHEMA     —— 全量热点表字段映射"
echo "       SELECTED_TABLE_WEBHOOK —— 精选话题表"
echo "       SELECTED_TABLE_SCHEMA  —— 精选话题表字段映射"
echo ""
echo "=========================================="
