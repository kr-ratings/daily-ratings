"""한국은행 ECOS: 시장금리(일별) 817Y002 → CP 91일, CD 91일, 국고채 3년.

당일분은 ECOS보다 먼저 나오는 금융투자협회 최종호가수익률로 보강한다(kofia_topup).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys

from .util import is_bday, request, session, today_kst


def log(*a):
    print(*a, file=sys.stderr, flush=True)

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


# ------------------------------------------------------------------ KOFIA 당일 보강
# ECOS는 당일 금리를 저녁(대략 17~18시대)에야 올린다. 같은 값의 원천인 금융투자협회
# 최종호가수익률은 장 마감(15:30) 직후 고시되므로, 당일분이 ECOS에 아직 없으면 KOFIA 값으로
# 먼저 채운다. 응답 열 구성이 문서화돼 있지 않아, ECOS에 이미 있는 최근 2영업일 값과 정확히
# 일치하는 열을 찾아 그 열을 '15:30 최종호가'로 쓴다(맞는 열이 없으면 보강하지 않음).
KOFIA_URL = "https://www.kofiabond.or.kr/proframeWeb/XMLSERVICES/"
KOFIA_NAMES = {"KTB3": re.compile(r"국고채?권?\(?3년"), "CD91": re.compile(r"CD(수익률)?\(?91일"), "CP91": re.compile(r"CP\(?91일")}
_ROW = re.compile(r"<BISComDspDatDTO>(.*?)</BISComDspDatDTO>", re.S)
_VAL = re.compile(r"<(val\d+)>([^<]*)</\1>")


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _kofia_raw(sess, day: dt.date) -> str:
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n<message><proframeHeader>'
        "<pfmAppName>BIS-KOFIABOND</pfmAppName><pfmSvcName>BISLastAskPrcROPSrchSO</pfmSvcName>"
        "<pfmFnName>listDay</pfmFnName></proframeHeader><systemHeader></systemHeader>"
        f"<BISComDspDatDTO><val1>{day:%Y%m%d}</val1></BISComDspDatDTO></message>"
    )
    r = request(sess, "POST", KOFIA_URL, data=xml.encode("utf-8"), timeout=60,
                headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    return r.content.decode("utf-8", errors="replace")


def _kofia_day(sess, day: dt.date) -> dict[str, dict[str, float]]:
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n<message><proframeHeader>'
        "<pfmAppName>BIS-KOFIABOND</pfmAppName><pfmSvcName>BISLastAskPrcROPSrchSO</pfmSvcName>"
        "<pfmFnName>listDay</pfmFnName></proframeHeader><systemHeader></systemHeader>"
        f"<BISComDspDatDTO><val1>{day:%Y%m%d}</val1></BISComDspDatDTO></message>"
    )
    r = request(sess, "POST", KOFIA_URL, data=xml.encode("utf-8"), timeout=60,
                headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    out: dict[str, dict[str, float]] = {}
    for m in _ROW.findall(r.content.decode("utf-8", errors="replace")):
        vals = dict(_VAL.findall(m))
        for k, pat in KOFIA_NAMES.items():
            if k in out:
                continue
            if any(pat.search(re.sub(r"\s", "", v)) for v in vals.values()):
                nums = {f: _num(v) for f, v in vals.items()}
                out[k] = {f: v for f, v in nums.items() if v is not None}
    return out


def kofia_rows(sess, day: dt.date) -> list[tuple]:
    """진단용: 최종호가 응답의 (이름, 당일, 코드) 목록."""
    return [(v.get("val1"), v.get("val3"), v.get("val4"), v.get("val6"), v.get("val20"))
            for v in (dict(_VAL.findall(m)) for m in _ROW.findall(_kofia_raw(sess, day)))]


def kofia_topup(sess, series: dict, today: dt.date) -> dict | None:
    """당일분이 ECOS에 없는 금리마다 KOFIA 최종호가(15:30)로 하루치를 덧붙인다.
    응답 행: val1 이름, val3 오전(11:30), val4 당일 최종호가(15:30), val6 전일.
    금리마다 최근 2영업일 val4가 ECOS 값과 정확히 같을 때만 쓴다 — 국고3년·CD는 같고,
    CP(91일)는 ECOS와 다른 계열이라(수 bp 차이) 자동으로 제외되고 ECOS를 기다린다."""
    if not is_bday(today):
        return None
    t = today.isoformat()
    need = [k for k, s in series.items() if s["points"] and s["points"][-1]["d"] < t]
    if not need:
        return None
    pts = {k: {p["d"]: p["v"] for p in s["points"]} for k, s in series.items()}
    common = sorted(set.intersection(*(set(v) for v in pts.values())))[-2:]
    if len(common) < 2:
        return None
    ref = {d: _kofia_day(sess, dt.date.fromisoformat(d)) for d in common}
    cols = {}
    for k in need:
        cand = None
        for d in common:
            row = ref[d].get(k)
            ok = {f for f, v in (row or {}).items() if abs(v - pts[k][d]) < 0.0005}
            cand = ok if cand is None else cand & ok
        if cand and ("val4" in cand or len(cand) == 1):
            cols[k] = "val4" if "val4" in cand else next(iter(cand))
        else:
            log(f"KOFIA 최종호가: {k}는 ECOS 값과 맞는 열이 없어 사용 안 함")
    if not cols:
        return None
    got = _kofia_day(sess, today)
    added = []
    for k, col in cols.items():
        v = got.get(k, {}).get(col)
        if v is None:
            continue  # 이 금리는 아직 고시 전
        prev = series[k]["points"][-1]["v"]
        if abs(v - prev) > 0.5:  # 비정상값 방어
            log(f"KOFIA 최종호가: {k} {prev}→{v} 변동이 커서 사용 안 함")
            continue
        series[k]["points"].append({"d": t, "v": v})
        added.append(k)
    if not added:
        log("KOFIA 최종호가: 당일 고시 전")
        return None
    return {"date": t, "items": added, "source": "금융투자협회 최종호가수익률(15:30)"}


HIST_DAYS = 10 * 366 + 10  # 화면 최대 조회기간(10년) + 여유


def save(path, obj) -> None:
    """rates.json은 10년치(약 7,500점)라 줄바꿈 없이 압축해 저장(약 200KB)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def collect(days: int = 40, base: dict | None = None) -> dict:
    """ECOS 금리. base(직전 rates.json)가 있으면 최근 days일만 새로 받아 덧쓰고 10년치를 유지한다.
    base가 없거나 10년치가 모자라면 10년치를 한 번에 받는다(체험용 키는 10건씩이라 이때만 오래 걸림)."""
    key = os.environ.get("ECOS_API_KEY") or "sample"
    sess = session()
    end = today_kst()
    keep_from = (end - dt.timedelta(days=HIST_DAYS)).isoformat()
    old = (base or {}).get("series") or {}
    full = any(not old.get(k, {}).get("points") or old[k]["points"][0]["d"] > (end - dt.timedelta(days=HIST_DAYS - 40)).isoformat()
               for k in ITEMS)
    start = end - dt.timedelta(days=HIST_DAYS if full else days)
    series = {}
    for k, (code, name, tag) in ITEMS.items():
        fresh = _fetch_item(sess, key, code, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        merged = {}
        if fresh and not full:
            # 직전 파일의 값 중 이번 ECOS 최신일 이후분(지난번 KOFIA 보강분)은 버리고 다시 판단한다
            merged = {p["d"]: p["v"] for p in old.get(k, {}).get("points", []) if p["d"] <= fresh[-1]["d"]}
        merged.update({p["d"]: p["v"] for p in fresh})
        pts = [{"d": d, "v": v} for d, v in sorted(merged.items()) if d >= keep_from]
        series[k] = {"name": name, "tag": tag, "points": pts}
    prelim = None
    try:
        prelim = kofia_topup(sess, series, end)
    except Exception as e:  # noqa: BLE001  보강 실패는 ECOS 값만으로 진행
        log(f"KOFIA 최종호가 보강 실패: {e}")
    asof = max((s["points"][-1]["d"] for s in series.values() if s["points"]), default=None)
    out = {"asof": asof, "source": "한국은행 ECOS 시장금리(일별)", "key": "sample" if key == "sample" else "user", "series": series}
    if prelim:
        out["prelim"] = prelim
    return out
