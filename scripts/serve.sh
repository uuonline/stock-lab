#!/usr/bin/env bash
# StockLab · 本地服务管理
#
# 用法：
#   ./scripts/serve.sh start     后台启动
#   ./scripts/serve.sh stop      停止
#   ./scripts/serve.sh restart   重启
#   ./scripts/serve.sh status    查看状态
#   ./scripts/serve.sh log       跟踪日志（Ctrl-C 退出）
#   ./scripts/serve.sh fg        前台运行（Ctrl-C 停止，方便看报错）
#
# 说明：之前用 `nohup ... &` 起的进程在终端会话结束后容易被回收，
# 表现出来就是"服务莫名其妙没了"。这里用 setsid 让进程脱离控制终端，
# 并把 PID 记到文件里，停止/重启才可靠。

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

PIDFILE="$HERE/data/stocklab.pid"
LOGFILE="$HERE/data/logs/stocklab.log"
PY="$HERE/.venv/bin/python"

mkdir -p "$HERE/data/logs"

if [ ! -x "$PY" ]; then
  echo "✗ 找不到虚拟环境: $PY"
  echo "  先创建： python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

# 读取 .env（若存在）
if [ -f "$HERE/.env" ]; then
  set -a; . "$HERE/.env"; set +a
fi
PORT="${SL_PORT:-8787}"

running_pid() {
  [ -f "$PIDFILE" ] || return 1
  local p; p="$(cat "$PIDFILE" 2>/dev/null)"
  [ -n "$p" ] || return 1
  kill -0 "$p" 2>/dev/null || return 1
  echo "$p"
}

lan_ip() {
  for i in en0 en1 en8 en4 en5; do
    local ip; ip="$(ipconfig getifaddr "$i" 2>/dev/null)"
    [ -n "$ip" ] && { echo "$ip"; return; }
  done
  echo "127.0.0.1"
}

cmd_status() {
  local p
  if p="$(running_pid)"; then
    echo "✓ 运行中  PID=$p  端口=$PORT"
    echo "  本机:   http://127.0.0.1:$PORT"
    echo "  局域网: http://$(lan_ip):$PORT"
    local code
    code="$(curl -s -m 6 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/health" 2>/dev/null)"
    echo "  健康检查: HTTP ${code:-无响应}"
  else
    echo "✗ 未运行（端口 $PORT）"
    return 1
  fi
}

cmd_start() {
  if running_pid >/dev/null; then
    echo "已在运行："
    cmd_status
    return 0
  fi
  echo "启动中 ..."
  # setsid 让进程脱离当前终端，终端关闭不会连带杀掉它
  if command -v setsid >/dev/null 2>&1; then
    setsid "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" \
      >>"$LOGFILE" 2>&1 < /dev/null &
  else
    nohup "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" \
      >>"$LOGFILE" 2>&1 < /dev/null &
  fi
  echo $! > "$PIDFILE"

  for _ in $(seq 1 30); do
    sleep 1
    if curl -s -m 3 -o /dev/null "http://127.0.0.1:$PORT/api/health" 2>/dev/null; then
      echo
      cmd_status
      echo "  日志: $LOGFILE"
      return 0
    fi
  done
  echo "✗ 启动超时，查看日志："
  tail -20 "$LOGFILE"
  return 1
}

cmd_stop() {
  local p
  if ! p="$(running_pid)"; then
    echo "未在运行"
    rm -f "$PIDFILE"
    return 0
  fi
  echo "停止 PID=$p ..."
  kill "$p" 2>/dev/null
  for _ in $(seq 1 15); do
    sleep 1
    kill -0 "$p" 2>/dev/null || break
  done
  if kill -0 "$p" 2>/dev/null; then
    echo "  未响应 TERM，强制结束"
    kill -9 "$p" 2>/dev/null
  fi
  rm -f "$PIDFILE"
  echo "✓ 已停止"
}

case "${1:-status}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_stop; sleep 1; cmd_start ;;
  status)  cmd_status ;;
  log)     tail -f "$LOGFILE" ;;
  fg)      exec "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" ;;
  *)       echo "用法: $0 {start|stop|restart|status|log|fg}"; exit 1 ;;
esac
