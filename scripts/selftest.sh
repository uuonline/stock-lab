#!/usr/bin/env bash
# StockLab · 部署自检（容器内执行）
#
# 用法：
#   ./scripts/selftest.sh                    # 自动找容器
#   ./scripts/selftest.sh stocklab           # 指定容器名

set -uo pipefail
CONTAINER="${1:-stocklab}"

if ! command -v docker >/dev/null 2>&1; then
  echo "✗ 未找到 docker 命令"
  exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "✗ 容器 '$CONTAINER' 未在运行"
  echo "  当前运行中的容器："
  docker ps --format '  {{.Names}}  ({{.Image}})' || true
  exit 1
fi

echo "在容器 $CONTAINER 内执行自检..."
echo
docker exec -i "$CONTAINER" python -m app.services.selftest
