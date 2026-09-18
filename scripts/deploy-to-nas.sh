#!/usr/bin/env bash
# StockLab · 一键部署到群晖 NAS
#
# 用法：
#   ./scripts/deploy-to-nas.sh                    # 用默认 NAS 地址
#   NAS=192.168.1.100 ./scripts/deploy-to-nas.sh  # 指定地址
#
# 前置：NAS 已开启 SSH，且已安装 Container Manager。
# 本脚本会把当前目录同步到 NAS 并远程启动容器。

set -euo pipefail

NAS="${NAS:-}"          # 用法: NAS=192.168.1.100 ./scripts/deploy-to-nas.sh
NAS_USER="${NAS_USER:-}"
REMOTE_DIR="${REMOTE_DIR:-/volume1/docker/stock-lab}"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -z "$NAS" ]; then
  read -r -p "NAS 地址（IP 或主机名）: " NAS
fi
if [ -z "$NAS" ]; then
  echo "✗ 必须提供 NAS 地址"; exit 1
fi
if [ -z "$NAS_USER" ]; then
  read -r -p "NAS 用户名: " NAS_USER
fi

echo "=============================================="
echo "  StockLab 部署到 NAS"
echo "  源目录  : $LOCAL_DIR"
echo "  目标    : ${NAS_USER}@${NAS}:${REMOTE_DIR}"
echo "=============================================="
echo

# 1) 连通性检查
echo "[1/5] 检查 NAS 连通性..."
if ! ping -c 2 -W 2000 "$NAS" >/dev/null 2>&1; then
  echo "  ✗ ping 不通 $NAS"
  echo "    请确认 NAS 已开机、与本机同一网段，且未开启 ICMP 屏蔽。"
  exit 1
fi
if ! ssh -o ConnectTimeout=8 -o BatchMode=yes "${NAS_USER}@${NAS}" true 2>/dev/null; then
  echo "  ! SSH 免密未配置，稍后会提示输入密码。"
fi
echo "  ✓ NAS 可达"
echo

# 2) 本地自检
echo "[2/5] 本地文件自检..."
for f in Dockerfile docker-compose.yml requirements.txt app/main.py; do
  if [ ! -e "$LOCAL_DIR/$f" ]; then
    echo "  ✗ 缺少 $f"
    exit 1
  fi
done
echo "  ✓ 关键文件齐全"

if [ ! -f "$LOCAL_DIR/.env" ]; then
  echo "  ! 未找到 .env，将用 .env.example 作为模板上传"
  ENV_FROM_EXAMPLE=1
else
  ENV_FROM_EXAMPLE=0
fi
echo

# 3) 同步代码（排除虚拟环境与本地数据）
echo "[3/5] 同步代码到 NAS..."
ssh "${NAS_USER}@${NAS}" "mkdir -p '$REMOTE_DIR'"
rsync -az --delete \
  --exclude='.venv/' \
  --exclude='data/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.git/' \
  --exclude='.DS_Store' \
  -e ssh \
  "$LOCAL_DIR/" "${NAS_USER}@${NAS}:${REMOTE_DIR}/"
echo "  ✓ 同步完成"
echo

# 4) 首次部署时准备 .env 与数据目录
echo "[4/5] 准备配置与数据目录..."
if [ "$ENV_FROM_EXAMPLE" = "1" ]; then
  rsync -az -e ssh "$LOCAL_DIR/.env.example" "${NAS_USER}@${NAS}:${REMOTE_DIR}/.env"
  echo "  ✓ 已上传 .env（默认配置，可之后在 NAS 上修改）"
fi
ssh "${NAS_USER}@${NAS}" "mkdir -p '$REMOTE_DIR/data'"
echo "  ✓ 数据目录就绪"
echo

# 5) 构建并启动
echo "[5/5] 构建并启动容器（首次约 3-8 分钟）..."
ssh -t "${NAS_USER}@${NAS}" "cd '$REMOTE_DIR' && \
  (docker compose version >/dev/null 2>&1 && docker compose up -d --build || \
   docker-compose up -d --build)"
echo

# 状态
echo "=============================================="
ssh "${NAS_USER}@${NAS}" "cd '$REMOTE_DIR' && \
  (docker compose ps 2>/dev/null || docker-compose ps 2>/dev/null)" || true
echo "=============================================="
echo
echo "部署完成。访问地址： http://${NAS}:8787"
echo
echo "后续步骤："
echo "  1. 打开上面的地址，进入「设置」页"
echo "  2. 点击「刷新全市场快照」，等待 1-3 分钟"
echo "  3. 到「自选」页添加你关注的股票"
echo
echo "查看日志： ssh ${NAS_USER}@${NAS} \"cd $REMOTE_DIR && docker compose logs -f\""
