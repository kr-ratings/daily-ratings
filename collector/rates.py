"""한국은행 ECOS: 시장금리(일별) 817Y002 → CP 91일, CD 91일, 국고채 3년."""
from __future__ import annotations

import datetime as dt
import os

from .util import request, session, today_kst

STAT = "817Y002"
ITEMS = {
    "CP91": ("010503000", "CP 91일", "A1 기준"),
    "CD91": ("010502000", "CD 91일", "AAA 은행"),
    "KTB3": ("010200000", "국고채 3년", "지표물"),
}
BASE = "https://ecos.bok.or.kr/api/StatisticSearch"


def _fetch_item(sess, key: str, code: str, start: str, end: str) -> list[dict]:
    """ECOS는 'sample' 키일 때 한 번에 10건까지만 준다. 실제 키면 100건씩."""
    page = 100 if key != "sample" else 10
    out, s = [], 1
    while True:
        url = f"{BASE}/{key}/json/kr/{s}/{s + page - 1}/{STAT}/D/{start}/{end}/{code}"
        js = request(sess, "GET", url, timeout=30).json()
        body = js.get("StatisticSearch")
        if not body:  # 오류 응답 {"RESULT": {...}}
            msg = js.get("RESULT", {}).get("MESSAGE", js)
            if out:
                break
            raise RuntimeError(f"ECOS 오류: {msg}")
        rows = body.get("row", [])
        out += [{"d": f"{r['TIME'][:4]}-{r['TIME'][4:6]}-{r['TIME'][6:]}", "v": float(r["DATA_VALUE"])}
                for r in rows if r.get("DATA_VALUE") not in (None, "")]
        total = int(body.get("list_total_count", 0))
        s += page
        if s > total:
            break
    out.sort(key=lambda x: x["d"])
    return out


def collect(days: int = 40) -> dict:
    key = os.environ.get("ECOS_API_KEY") or "sample"
    sess = session()
    end = today_kst()
    start = end - dt.timedelta(days=days)
    series = {}
    for k, (code, name, tag) in ITEMS.items():
        pts = _fetch_item(sess, key, code, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        series[k] = {"name": name, "tag": tag, "points": pts}
    asof = max((s["points"][-1]["d"] for s in series.values() if s["points"]), default=None)
    return {"asof": asof, "source": "한국은행 ECOS 시장금리(일별)", "key": "sample" if key == "sample" else "user", "series": series}
