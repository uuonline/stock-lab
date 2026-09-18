"""标的代码规范化。

系统内部统一格式：  <code>.<market>   例如 600519.SH / 000001.SZ / 00700.HK / 000001.SH(指数)

坑点备忘：
  * 000001 既是深市平安银行(000001.SZ) 又是上证指数(000001.SH)，必须带市场后缀。
  * 港股代码在腾讯/东财之间补零规则不同，统一按 5 位处理。
  * 北交所在东财归到 market=0，腾讯用 bj 前缀。
"""
from __future__ import annotations

import re

# 常用指数白名单：用户只输入 6 位数字时据此推断是「指数」而非个股
INDEX_CODES = {
    "000001.SH": "上证指数",
    "000300.SH": "沪深300",
    "000016.SH": "上证50",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "000688.SH": "科创50",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "399005.SZ": "中小100",
    "399300.SZ": "沪深300",
    "000010.SH": "上证180",
}

# 只声明真正支持的市场。曾经把 US 列在这里，但代码校验只接受纯数字，
# 导致 AAPL.US 报「无法识别的代码」—— 宣称支持却用不了，比不支持更糟。
MARKETS = ("SH", "SZ", "BJ", "HK")

_CODE_RE = re.compile(r"^[0-9]{1,6}$")


class SymbolError(ValueError):
    pass


def is_index_code(code: str) -> bool:
    return code.upper() in INDEX_CODES


def _split(raw: str) -> tuple[str, str | None]:
    raw = (raw or "").strip().upper().replace(" ", "")
    if not raw:
        raise SymbolError("代码不能为空")
    if "." in raw:
        code, _, mkt = raw.rpartition(".")
        return code, mkt or None
    # 支持 sh600519 / sz000001 / hk00700 这类前缀写法
    m = re.match(r"^(SH|SZ|BJ|HK)(\d{1,6})$", raw)
    if m:
        return m.group(2), m.group(1)
    return raw, None


def normalize(raw: str, default_market: str | None = None) -> str:
    """把各种写法统一成 CODE.MARKET。"""
    code, mkt = _split(raw)
    if not _CODE_RE.match(code):
        raise SymbolError(f"无法识别的代码: {raw}")
    if mkt is None:
        mkt = default_market or infer_market(code)
    if mkt not in MARKETS:
        raise SymbolError(f"不支持的市场: {mkt}")
    if mkt == "HK":
        code = code.zfill(5)
    else:
        code = code.zfill(6)
    return f"{code}.{mkt}"


def infer_market(code: str) -> str:
    """按代码段推断市场。"""
    if len(code) <= 5:
        return "HK"
    if code.startswith(("60", "68", "51", "58", "56", "50", "11", "90", "13")):
        return "SH"
    if code.startswith(("00", "30", "15", "16", "12", "39", "18", "19")):
        return "SZ"
    if code.startswith(("83", "87", "88", "43", "82", "89")):
        return "BJ"
    return "SH"


def asset_type(symbol: str) -> str:
    code, _, mkt = symbol.rpartition(".")
    if mkt == "HK":
        return "hk"
    if symbol in INDEX_CODES or code.startswith(("000", "399")) and mkt in ("SH", "SZ") and symbol in INDEX_CODES:
        return "index"
    if code.startswith(("51", "58", "56", "50", "15", "16", "12")):
        return "etf"
    if code.startswith(("11", "13")):
        return "bond"
    return "stock"


# ---------------- 各数据源格式转换 ----------------

def to_eastmoney(symbol: str) -> str:
    """-> 1.600519 / 0.000001 / 116.00700"""
    code, _, mkt = symbol.rpartition(".")
    if mkt == "SH":
        return f"1.{code}"
    if mkt in ("SZ", "BJ"):
        return f"0.{code}"
    if mkt == "HK":
        return f"116.{code}"
    return f"1.{code}"


def to_tencent(symbol: str) -> str:
    """-> sh600519 / sz000001 / hk00700"""
    code, _, mkt = symbol.rpartition(".")
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj", "HK": "hk"}.get(mkt, "sh")
    return f"{prefix}{code}"


def to_sina(symbol: str) -> str:
    code, _, mkt = symbol.rpartition(".")
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj", "HK": "hk"}.get(mkt, "sh")
    return f"{prefix}{code}"


def from_eastmoney_secid(secid: str) -> str:
    mkt, _, code = secid.partition(".")
    if mkt == "1":
        return f"{code.zfill(6)}.SH"
    if mkt == "0":
        return f"{code.zfill(6)}.SZ"
    if mkt == "116":
        return f"{code.zfill(5)}.HK"
    return f"{code}.SH"


def display_name(symbol: str) -> str:
    return INDEX_CODES.get(symbol, symbol)


def board_of(symbol: str) -> str:
    code, _, mkt = symbol.rpartition(".")
    if mkt == "HK":
        return "港股"
    if mkt == "BJ":
        return "北交所"
    if code.startswith("688"):
        return "科创板"
    if code.startswith("300") or code.startswith("301"):
        return "创业板"
    if symbol in INDEX_CODES:
        return "指数"
    if code.startswith(("51", "58", "56", "50", "15", "16")):
        return "ETF"
    if code.startswith("60"):
        return "沪主板"
    if code.startswith("00"):
        return "深主板"
    return "其他"
