#!/usr/bin/env bash
# StockLab · 接口健壮性测试
#
# 覆盖非法输入、边界值、注入尝试。这些都是界面上很容易触发的场景
# （手机端误触、书签带奇怪参数、手输代码打错）。
#
# 用法：
#   ./scripts/robustness.sh                    # 默认 http://127.0.0.1:8787
#   BASE=http://你的NAS地址:8787 ./scripts/robustness.sh
#
# 判定原则：接口**不应该 5xx**。4xx 是合理的参数拒绝；
# 关键是「不该崩」以及「不该静默忽略用户输入」。

set -uo pipefail
BASE="${BASE:-http://127.0.0.1:8787}"
PASS=0; FAIL=0; WARN=0

# 期望：不应出现 5xx
check() {
  local desc="$1" expect="$2"; shift 2
  local code
  code=$(curl -s -m 45 -o /tmp/rb.json -w "%{http_code}" "$@" 2>/dev/null)
  if [ "$code" -ge 500 ] 2>/dev/null; then
    printf "  \033[31m✗\033[0m %-38s %s (5xx!) %s\n" "$desc" "$code" "$(head -c 70 /tmp/rb.json 2>/dev/null)"
    FAIL=$((FAIL+1))
    return
  fi
  if [ "$code" = "$expect" ]; then
    printf "  \033[32m✓\033[0m %-38s %s\n" "$desc" "$code"
    PASS=$((PASS+1))
  else
    printf "  \033[33m○\033[0m %-38s %s (期望 %s)\n" "$desc" "$code" "$expect"
    WARN=$((WARN+1))
  fi
}

# 期望：响应体里必须包含某个关键词（用于验证"不静默"）
check_has() {
  local desc="$1" needle="$2"; shift 2
  curl -s -m 60 -o /tmp/rb.json "$@" >/dev/null 2>&1
  # 用 -F 做固定字符串匹配：needle 里可能含 [] 等正则元字符
  # （比如 "results":[] 会被 grep 当成字符类，导致误报失败）
  if grep -qF "$needle" /tmp/rb.json 2>/dev/null; then
    printf "  \033[32m✓\033[0m %-38s 含 '%s'\n" "$desc" "$needle"
    PASS=$((PASS+1))
  else
    printf "  \033[31m✗\033[0m %-38s 缺少 '%s' → %s\n" "$desc" "$needle" "$(head -c 80 /tmp/rb.json 2>/dev/null)"
    FAIL=$((FAIL+1))
  fi
}

echo "══════════════════════════════════════════════════════"
echo "  StockLab 接口健壮性测试  →  $BASE"
echo "══════════════════════════════════════════════════════"

echo
echo "── 非法标的 ──"
check "乱码代码"            400 "$BASE/api/quote/%E4%B9%B1%E7%A0%81"
check "超长代码"            400 "$BASE/api/quote/$(printf '9%.0s' $(seq 1 300))"
check "纯符号代码"          400 "$BASE/api/quote/----"
check "不存在的股票"        404 "$BASE/api/quote/999999.SH"
check "K线非法周期"         422 "$BASE/api/kline/600519.SH?period=bogus"
check "K线limit越界"        422 "$BASE/api/kline/600519.SH?limit=99999"
check "K线limit负数"        422 "$BASE/api/kline/600519.SH?limit=-5"

echo
echo "── 注入尝试（SQL 全部走参数化，应被安全忽略）──"
check "搜索注入"            200 "$BASE/api/search?q=%27%20OR%201%3D1%20--"
check "通配符 %"            200 "$BASE/api/search?q=%25"
check "通配符 _"            200 "$BASE/api/search?q=_"
check "超长搜索词"          200 "$BASE/api/search?q=$(printf 'a%.0s' $(seq 1 500))"
check "选股字段注入"        200 "$BASE/api/screen" -X POST -H 'Content-Type: application/json' \
      -d '{"conditions":[{"field":"DROP TABLE","op":"lt","value":1}],"limit":3}'
check "选股排序注入"        200 "$BASE/api/screen" -X POST -H 'Content-Type: application/json' \
      -d '{"order":"id; DROP TABLE watchlist","limit":3}'

echo
echo "── 不静默：非法输入必须回报，不能假装成功 ──"
check_has "搜索 % 返回空"           '"results":[]' "$BASE/api/search?q=%25"
check_has "未知选股字段有告警"      "未知字段" "$BASE/api/screen" -X POST \
      -H 'Content-Type: application/json' \
      -d '{"conditions":[{"field":"pe_ratio","op":"lt","value":20}],"limit":3}'
check_has "非法操作符有告警"        "未知操作符" "$BASE/api/screen" -X POST \
      -H 'Content-Type: application/json' \
      -d '{"conditions":[{"field":"pe","op":"LIKE","value":5}],"limit":3}'
check_has "非数字取值有告警"        "不是数字" "$BASE/api/screen" -X POST \
      -H 'Content-Type: application/json' \
      -d '{"conditions":[{"field":"pb","op":"lt","value":"abc"}],"limit":3}'
check_has "非法排序有告警"          "不受支持" "$BASE/api/screen" -X POST \
      -H 'Content-Type: application/json' -d '{"order":"evil","limit":3}'
check_has "未知技术条件有告警"      "未知技术条件" "$BASE/api/screen" -X POST \
      -H 'Content-Type: application/json' \
      -d '{"tech":["__import__(\"os\")"],"conditions":[{"field":"amount","op":"gt","value":500000000}],"limit":3}'

echo
echo "── 回测参数边界 ──"
check "未知策略"            400 "$BASE/api/backtest" -X POST -H 'Content-Type: application/json' \
      -d '{"symbol":"000001.SZ","strategy":"nonexistent"}'
check "非法标的"            400 "$BASE/api/backtest" -X POST -H 'Content-Type: application/json' \
      -d '{"symbol":"BOGUS","strategy":"macd"}'
check "资金为0"             422 "$BASE/api/backtest" -X POST -H 'Content-Type: application/json' \
      -d '{"symbol":"000001.SZ","strategy":"macd","initial_cash":0}'
check "资金为负"            422 "$BASE/api/backtest" -X POST -H 'Content-Type: application/json' \
      -d '{"symbol":"000001.SZ","strategy":"macd","initial_cash":-5000}'
check "天数过小"            422 "$BASE/api/backtest" -X POST -H 'Content-Type: application/json' \
      -d '{"symbol":"000001.SZ","strategy":"macd","days":5}'
check "仓位>1"              422 "$BASE/api/backtest" -X POST -H 'Content-Type: application/json' \
      -d '{"symbol":"000001.SZ","strategy":"macd","position_size":5}'
check "参数类型错误"        400 "$BASE/api/backtest" -X POST -H 'Content-Type: application/json' \
      -d '{"symbol":"000001.SZ","strategy":"ma_cross","params":{"fast":"abc","slow":20}}'

echo
echo "── 提醒参数校验（缺参数会创建出永不触发的死规则）──"
check "缺阈值被拒"          400 "$BASE/api/alerts" -X POST -H 'Content-Type: application/json' \
      -d '{"symbol":"600519.SH","rule_type":"price_above","params":{}}'
check "阈值非数字被拒"      400 "$BASE/api/alerts" -X POST -H 'Content-Type: application/json' \
      -d '{"symbol":"600519.SH","rule_type":"price_above","params":{"value":"abc"}}'
check "非法规则类型被拒"    400 "$BASE/api/alerts" -X POST -H 'Content-Type: application/json' \
      -d '{"symbol":"600519.SH","rule_type":"evil","params":{}}'
check "冷却负数被拒"        422 "$BASE/api/alerts" -X POST -H 'Content-Type: application/json' \
      -d '{"symbol":"600519.SH","rule_type":"price_above","params":{"value":1},"cooldown":-9}'
check "合法规则可创建"      200 "$BASE/api/alerts" -X POST -H 'Content-Type: application/json' \
      -d '{"symbol":"600519.SH","rule_type":"vol_surge","params":{}}'

echo
echo "── 批量接口边界 ──"
check "空批量"              400 "$BASE/api/quote?symbols="
check "批量含空项"          200 "$BASE/api/quote?symbols=600519.SH,,000001.SZ"
check "批量含非法项"        200 "$BASE/api/quote?symbols=600519.SH,BOGUS,000001.SZ"
check "批量超 200"          400 "$BASE/api/quote?symbols=$(python3 -c 'print(",".join(["600519.SH"]*201))')"

echo
echo "── 404 / 越界 ──"
check "不存在的报告"        404 "$BASE/api/ai/reports/999999"
check "AI空symbol"          400 "$BASE/api/ai/report" -X POST -H 'Content-Type: application/json' \
      -d '{"scope":"single"}'
check "回测历史limit越界"   422 "$BASE/api/backtest/history?limit=99999"

echo
echo "── 箱体自适应窗口 ──"
check "自适应箱体正常"      200 "$BASE/api/box/002241.SZ?adaptive=1"
check "自适应+非法标的"     400 "$BASE/api/box/BOGUS?adaptive=1"
check "自适应参数越界"      422 "$BASE/api/box/002241.SZ?adaptive=9"
check_has "返回推荐窗口"    '"recommended_window"' "$BASE/api/box/002241.SZ?adaptive=1"
check_has "返回节奏测量"    '"levels"' "$BASE/api/box/002241.SZ?adaptive=1"
check_has "返回扫描曲线"    '"curve"' "$BASE/api/box/002241.SZ?adaptive=1"
check_has "返回可信度标记"  '"trustworthy"' "$BASE/api/box/002241.SZ?adaptive=1"
# 趋势股应当被如实标成「没有可信箱体」，而不是硬塞一个最不难看的窗口
check_has "趋势股如实拒绝"  '"trustworthy":false' "$BASE/api/box/000001.SZ?adaptive=1"

# 请求更多历史必须真的拿到更多。
# 各家历史深度不同（腾讯在请求 >800 根时上游只回 641 根，新浪能回 1000+），
# 曾经 limit=1000 拿到的比 limit=800 还少。
check_more_bars() {
  local short long
  short=$(curl -s -m 60 "$BASE/api/kline/002241.SZ?period=day&limit=400" | grep -o '"date"' | wc -l)
  long=$(curl -s -m 60 "$BASE/api/kline/002241.SZ?period=day&limit=1000" | grep -o '"date"' | wc -l)
  if [ "$long" -gt 0 ] && [ "$long" -ge "$short" ]; then
    printf "  \033[32m✓\033[0m %-38s %s → %s 根\n" "请求更多历史不倒退" "$short" "$long"
    PASS=$((PASS+1))
  else
    printf "  \033[31m✗\033[0m %-38s limit=400 得 %s 根，limit=1000 只得 %s 根\n" \
      "请求更多历史不倒退" "$short" "$long"
    FAIL=$((FAIL+1))
  fi
}
check_more_bars

echo
echo "── AI 全流程 ──"
check "全流程正常"          200 "$BASE/api/flow/002241.SZ"
check "全流程+非法标的"     400 "$BASE/api/flow/BOGUS"
check "数据包正常"          200 "$BASE/api/flow/002241.SZ/pack"
check "ETF 无财报不崩"      200 "$BASE/api/flow/510300.SH"
check "港股无财报不崩"      200 "$BASE/api/flow/00700.HK"
check_has "返回六步结构"    '"steps"' "$BASE/api/flow/002241.SZ"
check_has "标注无数据项"    '"no_data"' "$BASE/api/flow/002241.SZ"
check_has "带免责声明"      '不构成投资建议' "$BASE/api/flow/002241.SZ"
# 补上 F10 数据源后，A股的「无数据」项应当只剩 1 项（客户供应商，确实取不到）
check_has "A股已接入主营构成"   '东财 F10 经营分析' "$BASE/api/flow/002241.SZ"
check_has "A股已接入行业对比"   'F10 行业分析' "$BASE/api/flow/002241.SZ"
check_has "A股已接入增减持"     '高管持股变动' "$BASE/api/flow/002241.SZ"
check "ETF 全流程不崩"          200 "$BASE/api/flow/510300.SH"

echo
echo "── 异动归因 ──"
check "异动分析正常"        200 "$BASE/api/anomaly/002241.SZ"
check "异动+非法标的"       400 "$BASE/api/anomaly/BOGUS"
check "异动+ETF"            200 "$BASE/api/anomaly/510300.SH"
check "异动+港股"           200 "$BASE/api/anomaly/00700.HK"
check "异动关消息面"        200 "$BASE/api/anomaly/002241.SZ?news=0"
check_has "返回异动判定"    '"detect"' "$BASE/api/anomaly/002241.SZ"
check_has "返回归因分层"    '"attribution"' "$BASE/api/anomaly/002241.SZ"
check_has "返回状态与条件"  '"condition"' "$BASE/api/anomaly/002241.SZ"

echo
echo "── 历史类比 ──"
check "历史类比正常"        200 "$BASE/api/analogs/002241.SZ"
check "类比+非法标的"       400 "$BASE/api/analogs/BOGUS"
check "类比+样本少的标的"   200 "$BASE/api/analogs/000001.SH"
check_has "类比返回持有期"  '"horizons"' "$BASE/api/analogs/002241.SZ"
check_has "类比返回独立事件数" '"independent"' "$BASE/api/analogs/002241.SZ"
check_has "类比返回样本内外" '"out_sample"' "$BASE/api/analogs/002241.SZ"
check_has "类比声明非预测"  '不是预测' "$BASE/api/analogs/002241.SZ"
check_has "数据包含提示词原文" \
  '请用一句话概括这家公司的核心商业模式并列出它最主要的收入来源是什么。' \
  "$BASE/api/flow/002241.SZ/pack"

echo
echo "══════════════════════════════════════════════════════"
printf "  通过 %d   失败 %d   警告 %d\n" "$PASS" "$FAIL" "$WARN"
echo "══════════════════════════════════════════════════════"
[ "$FAIL" -eq 0 ] || exit 1
