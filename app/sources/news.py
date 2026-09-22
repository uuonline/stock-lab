"""消息面数据源：公司公告 + 个股新闻。

**为什么需要它**：A股异动很大比例由消息驱动。没有消息面，
「大盘平、板块平、个股放量涨 3%」这种独立异动就只能说「不知道原因」——
而这恰恰是最需要解释的情形。

**能做到什么、不能做到什么**（这条边界比代码本身重要）：

  能做到：取到「某天有哪些公告、哪些新闻」——结构化的**时间 + 标题 + 来源**。
  做不到：解读消息是利好还是利空。标题不等于原因。

所以本模块只输出**事实**（什么时候发了什么），由上层去判断时间是否吻合，
绝不做「股价上涨是因为这条公告」这种因果断言。时间上吻合 ≠ 因果，
一篇收盘后发布的调研纪要解释不了盘中那波拉升。

> 实测来源：
>   · 公告 —— np-anotice-stock.eastmoney.com（东财公告库）
>   · 新闻 —— search-api-web.eastmoney.com（东财站内搜索，含媒体名和发布时间）
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .base import FetchError, fetch_json

log = logging.getLogger("stocklab.news")

TTL = 900          # 消息面变化快，15 分钟

HEADERS = {
    "Referer": "https://so.eastmoney.com/",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
}

_TAG = re.compile(r"<[^>]+>")


def _clean(s: Any) -> str:
    """去掉搜索接口返回的高亮标签。"""
    return _TAG.sub("", str(s or "")).strip()


def announcements(symbol: str, pages: int = 2) -> dict[str, Any]:
    """公司公告列表（按时间倒序）。

    返回 [{date, title, url, type}]，date 是公告日（YYYY-MM-DD）。
    """
    code = symbol.split(".")[0]
    out: list[dict] = []
    err = None
    for page in range(1, max(1, pages) + 1):
        try:
            d = fetch_json(
                "https://np-anotice-stock.eastmoney.com/api/security/ann",
                params={
                    "sr": "-1", "page_size": "50", "page_index": str(page),
                    "ann_type": "A", "client_source": "web",
                    "stock_list": code, "f_node": "0", "s_node": "0",
                },
                headers=HEADERS, retries=2, timeout=15, cache_ttl=TTL,
            )
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            break
        rows = ((d.get("data") or {}).get("list")) or []
        if not rows:
            break
        for r in rows:
            out.append({
                "date": str(r.get("notice_date") or "")[:10],
                "title": _clean(r.get("title")),
                "url": (f"https://data.eastmoney.com/notices/detail/{code}/"
                        f"{r.get('art_code')}.html" if r.get("art_code") else None),
                "type": ((r.get("columns") or [{}])[0] or {}).get("column_name"),
            })
    if not out and err:
        raise FetchError(f"公告获取失败: {err}")
    return {"ok": bool(out), "items": out, "count": len(out)}


def news(symbol: str, name: str | None = None, size: int = 20) -> dict[str, Any]:
    """个股新闻（按发布时间倒序）。

    搜索用**公司名**而不是代码 —— 用代码搜出来的多是行情页，不是报道。
    """
    kw = (name or "").strip()
    if not kw:
        return {"ok": False, "items": [], "reason": "缺少公司名，无法搜索新闻"}
    param = {
        "uid": "", "keyword": kw, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default", "sort": "time",
            "pageIndex": 1, "pageSize": max(1, min(size, 50)),
            "preTag": "", "postTag": "",
        }},
    }
    try:
        d = fetch_json(
            "https://search-api-web.eastmoney.com/search/jsonp",
            params={"cb": "", "param": json.dumps(param, ensure_ascii=False), "client": "web"},
            headers=HEADERS, retries=2, timeout=15, cache_ttl=TTL,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "items": [], "reason": f"新闻获取失败: {exc}"}

    arts = (((d.get("result") or {}).get("cmsArticleWebOld")) or [])
    items = []
    for a in arts:
        items.append({
            # 搜索接口给的是"发布时间"（到秒），和公告日口径不同，要单独标注
            "time": str(a.get("date") or ""),
            "date": str(a.get("date") or "")[:10],
            "title": _clean(a.get("title")),
            "source": a.get("mediaName"),
            "url": a.get("url"),
        })
    return {"ok": bool(items), "items": items, "count": len(items)}


def digest(symbol: str, name: str | None, days: int = 3) -> dict[str, Any]:
    """把公告与新闻合并成一个「最近消息面」摘要。"""
    import datetime as dt
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out: dict[str, Any] = {"ok": False, "announcements": [], "news": [],
                           "errors": [], "window_days": days}
    try:
        ann = announcements(symbol)
        out["announcements"] = [x for x in ann["items"] if x["date"] >= cutoff]
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(str(exc))
    try:
        nw = news(symbol, name)
        out["news"] = [x for x in nw["items"] if x["date"] >= cutoff]
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(str(exc))
    out["ok"] = bool(out["announcements"] or out["news"])
    return out
