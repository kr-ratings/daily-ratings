"""신용평가사 등급공시 수집 → ABSTB·ABCP '본평가' 건을 공통 레코드로 정규화.

공통 레코드 필드
  agency, spc, kind(ABSTB|ABCP), eval_type(본|정기|수시), grade, eval_date(YYYY-MM-DD),
  series(회차), issue_date, maturity, amount(억원), ref(평가사 내부 코드)
"""
from __future__ import annotations

import datetime as dt
import re

from bs4 import BeautifulSoup

from .util import CACHE_DIR, clean_grade, parse_date, read_json, request, session, to_num, write_json

KINDS = {"ABSTB", "ABCP"}


def _iso(s):
    d = parse_date(s)
    return d.isoformat() if d else None


# ---------------------------------------------------------------- NICE신용평가
def nice(start: dt.date, end: dt.date) -> list[dict]:
    sess = session()
    url = "https://www.nicerating.com/disclosure/dayRatingNews.do"
    params = {"today": end.isoformat(), "secuTyp": "ABS", "strDate": start.isoformat(), "endDate": end.isoformat()}
    html = request(sess, "GET", url, params=params, timeout=120).text
    soup = BeautifulSoup(html, "lxml")
    table = next((t for t in soup.find_all("table") if t.find(string=lambda x: x and "유동화회사명" in x)), None)
    if table is None:
        raise RuntimeError("NICE: 자산유동화증권 표를 찾지 못함(페이지 구조 변경 가능)")
    out = []
    for tr in table.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 11:
            continue
        c = [td.get_text(" ", strip=True) for td in tds]
        if c[2] not in KINDS:
            continue
        a = tds[0].find("a")
        ref = None
        if a and "goView" in (a.get("href") or ""):
            parts = a["href"].split("'")
            ref = parts[3] if len(parts) > 3 else None
        doc = None
        m = re.search(r"fncFileDown\('([^']+)'\)", str(tds[11]))
        if m:
            doc = {"agency": "NICE", "id": m.group(1)}
        out.append({
            "agency": "NICE", "spc": c[0], "kind": c[2], "eval_type": c[3], "grade": clean_grade(c[5]),
            "eval_date": _iso(c[6]), "series": c[1], "issue_date": _iso(c[8]), "maturity": _iso(c[9]),
            "amount": to_num(c[10]), "ref": ref, "doc": doc,
        })
    return out


def nice_details(refs: set[str]) -> dict:
    """NICE 유동화회사 상세(기초자산·주관사·시공사·신용공여기관). 코드별로 캐시."""
    cache_path = CACHE_DIR / "nice_spc.json"
    cache = read_json(cache_path, {})
    sess = session()
    for ref in sorted(r for r in refs if r and r not in cache):
        try:
            html = request(sess, "GET", "https://www.nicerating.com/disclosure/securitizationCompanyGrade.do",
                           params={"cmpCd": ref}, timeout=60).text
        except Exception:  # noqa: BLE001
            continue
        soup = BeautifulSoup(html, "lxml")
        info = {}
        for t in soup.find_all("table")[:3]:
            rows = t.find_all("tr")
            if len(rows) < 2:
                continue
            heads = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
            vals = [c.get_text(" ", strip=True) for c in rows[1].find_all(["th", "td"])]
            for h, v in zip(heads, vals):
                if h in ("기초자산", "자산보유자", "주관사", "시공사", "신용공여기관") and v:
                    info[h] = v
        cache[ref] = info
    write_json(cache_path, cache)
    return cache


# ---------------------------------------------------------------- 한국신용평가
def kis(start: dt.date, end: dt.date) -> list[dict]:
    sess = session()
    data = {"tabType": "0", "searchYn": "Y", "startDt": start.strftime("%Y.%m.%d"), "endDt": end.strftime("%Y.%m.%d")}
    html = request(sess, "POST", "https://www.kisrating.com/ratings/hot_disclosure.do", data=data, timeout=120).text
    soup = BeautifulSoup(html, "lxml")
    table = None
    for t in soup.find_all("table"):
        heads = [th.get_text(strip=True) for th in t.find_all("th")]
        if "회차" in heads and "종류" in heads and "발행액(억원)" in heads:
            table = t
            break
    if table is None:
        raise RuntimeError("한신평: 유동화증권 표를 찾지 못함(페이지 구조 변경 가능)")
    out = []
    for tr in table.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 10:
            continue
        c = [td.get_text(" ", strip=True) for td in tds]
        if c[2] not in KINDS:
            continue
        doc = None
        m = re.search(r"fn_file\('([^']*)',\s*'([^']*)',\s*'([^']*)',\s*'([^']*)',\s*'([^']*)',\s*'([^']*)'", str(tds[-1]))
        if m:
            doc = {"agency": "한신평", "id": m.group(4), "menuCd": m.group(1), "gubun": m.group(2),
                   "title": m.group(3), "writedate": m.group(6)}
        out.append({
            "agency": "한신평", "spc": c[1], "kind": c[2], "eval_type": c[6], "grade": clean_grade(c[8]),
            "eval_date": _iso(c[9]), "series": c[3], "issue_date": None, "maturity": _iso(c[5]),
            "amount": to_num(c[4]), "ref": None, "doc": doc,
        })
    return out


# ---------------------------------------------------------------- 한국기업평가
def kr(start: dt.date, end: dt.date) -> list[dict]:
    sess = session()
    page = "https://www.korearatings.com/cms/frCmnCon/index.do?MENU_ID=360"
    request(sess, "GET", page, timeout=60)  # 세션 쿠키
    out = []
    # 한 번에 최대 90일 조회 가능
    s = start
    while s <= end:
        e = min(end, s + dt.timedelta(days=89))
        data = [("MENU_ID", "360"), ("CONTENTS_NO", "1"), ("SITE_NO", "2"), ("COMP_CD", ""),
                ("STDT", s.isoformat()), ("ENDT", e.isoformat()), ("CHNG_ONLY_YN", "N"), ("SVCTY_CD", "05")]
        js = request(sess, "POST", "https://www.korearatings.com/ajaxf/frDisclosureSvc/getRatingDisclosureList.do",
                     data=data, headers={"X-Requested-With": "XMLHttpRequest", "Referer": page}, timeout=120).json()
        body = js.get("data") or {}
        rows = []
        for v in body.values():
            if isinstance(v, dict) and isinstance(v.get("Data"), list):
                rows += v["Data"]
        for r in rows:
            kind = (r.get("BOND_KIND_KRNM") or "").strip()
            if kind not in KINDS:
                continue
            out.append({
                "agency": "한기평", "spc": r.get("COMP_NM") or r.get("COMP_ABBR_NM") or "", "kind": kind,
                "eval_type": r.get("EVAL_DIV_NM") or "", "grade": clean_grade(r.get("GRD")),
                "eval_date": _iso(r.get("EVAL_DT")), "series": r.get("BOND_NM") or r.get("BOND_NO") or "",
                "issue_date": None, "maturity": None, "amount": to_num(r.get("ISSUE_AMT")), "ref": r.get("COMP_CD"),
                "doc": ({"agency": "한기평", "id": r["RTNG_OPN_FILE_NM"], "svc": r.get("SVC_ID"),
                         "seq": r.get("EVAL_SEQNO"), "comp_cd": r.get("COMP_CD"), "comp_nm": r.get("COMP_NM")}
                        if r.get("RTNG_OPN_FILE_NM") not in (None, "", "-") else None),
            })
        s = e + dt.timedelta(days=1)
    return out


AGENCIES = {"NICE": nice, "한신평": kis, "한기평": kr}
