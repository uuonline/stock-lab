#!/usr/bin/env bash
# StockLab · 前端冒烟测试
#
# 在 Node + jsdom 里真实执行前端 JS，驱动界面走一遍主要流程。
# 目的：避免"接口全通但页面白屏"——一个 JS 运行时错误就能让整个界面不可用，
# 而纯接口测试完全发现不了。
#
# 用法：
#   ./scripts/frontend-test.sh
#   BASE=http://你的NAS地址:8787 ./scripts/frontend-test.sh
#
# 依赖 jsdom。脚本会把依赖装到临时目录，不污染项目。

set -uo pipefail
BASE="${BASE:-http://127.0.0.1:8787}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_JS="$HERE/frontend-test.js"

if ! command -v node >/dev/null 2>&1; then
  echo "✗ 未找到 node（前端测试需要 Node.js 18+）"
  exit 1
fi

NODE_MAJOR=$(node -v | sed 's/^v//' | cut -d. -f1)
if [ "$NODE_MAJOR" -lt 18 ] 2>/dev/null; then
  echo "✗ Node 版本过低（需要 18+ 才有内置 fetch），当前 $(node -v)"
  exit 1
fi

# 服务可达性
if ! curl -s -m 8 -o /dev/null "$BASE/api/health"; then
  echo "✗ 无法访问 $BASE —— 请先启动服务"
  exit 1
fi

# 准备 jsdom（装在临时目录，避免往项目里塞 node_modules）
DEPS="${TMPDIR:-/tmp}/stocklab-fe-deps"
if [ ! -d "$DEPS/node_modules/jsdom" ]; then
  echo "首次运行：安装 jsdom 到 $DEPS ..."
  mkdir -p "$DEPS"
  ( cd "$DEPS" && npm init -y >/dev/null 2>&1 && \
    npm install jsdom --no-audit --no-fund --cache "$DEPS/.npmcache" >/dev/null 2>&1 )
  if [ ! -d "$DEPS/node_modules/jsdom" ]; then
    echo "✗ jsdom 安装失败。可手动执行："
    echo "    mkdir -p $DEPS && cd $DEPS && npm install jsdom"
    exit 1
  fi
fi

NODE_PATH="$DEPS/node_modules" BASE="$BASE" node "$TEST_JS"
