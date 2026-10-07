"""전체 수집 → site/data/*.json 생성.

표시 기준: 최근 EVAL_LOOKBACK_DAYS(기본 120일) 안에 본평가를 받은 SPC의 발행일정 중,
발행일이 최근 ISSUE_WINDOW_DAYS(기본 30일) 안인 회차를 한 줄씩 보여준다.

    python -m collector.build
    EVAL_LOOKBACK_DAYS=150 ISSUE_WINDOW_DAYS=45 python -m collector.build
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys

from . import opinions as op
from . import rates as rates_mod
from . import ratings as rt
from .kofia import Trades
from .util import (CACHE_DIR, DATA_DIR, add_bdays, display_name, is_bday, norm_name, now_kst_str,
                   parse_date, read_json, today_kst, write_json)

AGENCY_PRIORITY = ["NICE", "한기평", "한신평"]  # 상세정보(주관사·신용보강) 우선순위


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def series_key(s: str) -> str:
    m = re.search(r"\d+(?:\s?-\s?\d+)*", s or "")
    return m.group(0).replace(" ", "") if m else (s or "").strip()


_SUP_RULES = [
    (r"인수\s?확약|사모사채\s?인수|인수\s?의무", "사모사채 인수확약"),
    (r"매입\s?보장", "매입보장"),
    (r"매입\s?(?:확약|의무|약정)|매입", "매입확약"),
    (r"자금\s?보충", "자금보충"),
    (r"연대\s?보증", "연대보증"),
    (r"책임\s?준공", "책임준공"),
    (r"채무\s?인수", "채무인수"),
    (r"정기\s?예금|예금", "정기예금"),
    (r"환매", "RP 환매"),
    (r"신용\s?공여|유동성", "신용공여"),
    (r"하자\s?담보", "하자담보"),
    (r"참가\s?계약|카드\s?이용대금|카드대금", "카드대금 참가"),
    (r"보증", "보증"),
    (r"신용도", "신용도 연계"),
]


def summarize_support(detail: str) -> str:
    """'메리츠증권㈜의 사모사채 매입의무로 통제' → '매입확약' 처럼 핵심 구조만 남긴다."""
    t = detail or ""
    tags = []
    for pat, label in _SUP_RULES:
        if re.search(pat, t) and label not in tags:
            if label == "보증" and any(x in tags for x in ("연대보증",)):
                continue
            if label == "신용도 연계" and tags:
                continue
            tags.append(label)
    if "매입보장" in tags and "매입확약" in tags:
        tags.remove("매입확약")
    if "카드대금 참가" in tags and "하자담보" in tags:
        tags.remove("하자담보")
    if tags:
        return "·".join(tags[:3])
    t = re.sub(r"[^\s]*(?:㈜|\(주\))[^\s]*", "", t)
    t = re.sub(r"(?:으로|로)\s?통제$|\s+", " ", t).strip()
    return t[:18] + ("…" if len(t) > 18 else "")


def is_pf(*texts) -> bool:
    t = " ".join(x for x in texts if x)
    return bool(re.search(r"PF|Project\s?Finance|프로젝트\s?파이낸스|부동산\s?개발", t, re.I))


# ------------------------------------------------------------------ 본평가 이벤트
def group_events(rows: list[dict]) -> list[dict]:
    ev: dict[tuple, dict] = {}
    for r in rows:
        if not r["eval_date"] or r["eval_type"] not in ("본", "정기", "수시"):
            continue
        k = (r["agency"], norm_name(r["spc"]), r["eval_date"])
        e = ev.setdefault(k, {"agency": r["agency"], "key": k[1], "spc": display_name(r["spc"]),
                              "eval_date": r["eval_date"], "rows": [], "doc": None, "ref": r.get("ref"),
                              "types": set()})
        e["rows"].append(r)
        e["types"].add(r["eval_type"])
        if not e["doc"] and r.get("doc"):
            e["doc"] = r["doc"]
    return list(ev.values())


# ------------------------------------------------------------------ KOFIA 매칭(회차 단위)
def match_kofia(kind: str, key: str, issue: dt.date, tr: Trades, today: dt.date) -> dict:
    if kind == "ABCP":
        if issue > today:
            return {"status": "pending"}
        rows = [r for r in tr.abcp_on(issue)
                if r["key"] == key and r["type"] == "직접매입(할인)" and r["issue"] == issue.isoformat()]
        if rows:
            b = max(rows, key=lambda r: r["rate"] or 0)
            return {"status": "matched", "rate": b["rate"], "trade_date": b["trade"], "source": "직접매입(할인)"}
        # 당일 공시가 늦게 올라오는 경우를 위해 발행 다음 영업일까지는 대기
        return {"status": "pending" if today <= add_bdays(issue, 1) else "none"}
    found, pf = [], False
    last = add_bdays(issue, 2)
    T = issue
    while T <= min(last, today):
        if is_bday(T):
            for r in tr.stb_on(T):
                if r["key"] != key:
                    continue
                pf = pf or r["ab"] == "PF AB"
                if r["type"] == "매출" and r["issue"] == issue.isoformat():
                    found.append(r)
        T += dt.timedelta(days=1)
    if found:
        b = max(found, key=lambda r: r["rate"] or 0)
        return {"status": "matched", "rate": b["rate"], "trade_date": b["trade"], "source": "매출", "pf": pf}
    return {"status": "pending" if today <= add_bdays(last, 1) else "none", "pf": pf}


# ------------------------------------------------------------------ main
def main() -> int:
    today = today_kst()
    lookback = int(os.environ.get("EVAL_LOOKBACK_DAYS", "120"))
    window = int(os.environ.get("ISSUE_WINDOW_DAYS", "30"))
    eval_start = today - dt.timedelta(days=lookback)
    win_start = today - dt.timedelta(days=window)
    status = {"generated": now_kst_str(), "eval_window": [eval_start.isoformat(), today.isoformat()],
              "issue_window": [win_start.isoformat(), today.isoformat()], "sources": {}}

    # 1) 시장금리 ----------------------------------------------------------
    try:
        rates = rates_mod.collect(days=60)
        rates["generated"] = status["generated"]
        write_json(DATA_DIR / "rates.json", rates)
        status["sources"]["ECOS"] = {"ok": True, "asof": rates["asof"], "key": rates["key"]}
        log("ECOS ok", rates["asof"])
    except Exception as e:  # noqa: BLE001
        status["sources"]["ECOS"] = {"ok": False, "error": str(e)[:300]}
        log("ECOS FAIL", e)

    # 2) 등급공시 목록 -----------------------------------------------------
    rows = []
    for name, fn in rt.AGENCIES.items():
        lpath = CACHE_DIR / f"list_{name}.json"
        try:
            got = fn(eval_start, today)
            write_json(lpath, got)
            rows += got
            status["sources"][name] = {"ok": True, "rows": len(got)}
            log(name, "ok", len(got))
        except Exception as e:  # noqa: BLE001
            prev = read_json(lpath, [])  # 실패 시 직전 성공 목록으로 대체
            rows += prev
            status["sources"][name] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}",
                                       "fallback_rows": len(prev)}
            log(name, "FAIL", e, f"→ 직전 목록 {len(prev)}건 사용")
    if not any(status["sources"].get(a, {}).get("ok") for a in ("NICE", "한신평", "한기평")):
        log("모든 평가사 수집 실패 → 기존 데이터 유지")
        write_json(DATA_DIR / "status.json", status)
        return 1

    events = group_events(rows)
    in_win = lambda d: d is not None and win_start.isoformat() <= d <= today.isoformat()  # noqa: E731

    # NICE 목록에 이미 발행일이 있는 (SPC, 회차) → 다른 평가사 회차의 발행일 보완용
    nice_sched = {}
    for e in events:
        if e["agency"] == "NICE":
            for r in e["rows"]:
                if r.get("issue_date"):
                    nice_sched[(e["key"], series_key(r["series"]))] = r

    # 3) 평가의견서가 필요한 이벤트 선별 ------------------------------------
    need = []
    for e in events:
        if not e["doc"]:
            continue
        if e["agency"] == "NICE":
            if any(in_win(r.get("issue_date")) for r in e["rows"]):
                need.append(e)
        elif e["agency"] == "한기평" and "본" not in e["types"]:
            continue  # 한기평은 요청 제한이 엄격해 정기·수시평가 의견서는 받지 않음
        else:
            known = [nice_sched.get((e["key"], series_key(r["series"]))) for r in e["rows"]]
            if not all(known) or any(k and in_win(k["issue_date"]) for k in known):
                need.append(e)
    # 우선순위: NICE 일정으로 대체할 수 없는 한기평·한신평 건 → 최근 평가일 순
    def prio(e):
        uncovered = e["agency"] != "NICE" and not all(
            nice_sched.get((e["key"], series_key(r["series"]))) for r in e["rows"])
        return (0 if uncovered else 1, "".join(chr(0x10FFFF - ord(c)) for c in e["eval_date"]))
    need.sort(key=prio)
    try:
        parsed = op.load_all([e["doc"] for e in need], log=log)
        st = getattr(op.load_all, "stats", {})
        missing = sum(1 for e in need if op.doc_key(e["doc"]) not in parsed)
        status["sources"]["평가의견서"] = {"ok": missing == 0, "docs": len(need), "missing": missing,
                                       "kr_blocked": st.get("kr_blocked", False)}
    except Exception as ex:  # noqa: BLE001
        parsed = {}
        status["sources"]["평가의견서"] = {"ok": False, "error": str(ex)[:200]}
    opinion = lambda e: parsed.get(op.doc_key(e["doc"])) if e["doc"] else None  # noqa: E731

    try:
        nice_detail = rt.nice_details({e["ref"] for e in events if e["agency"] == "NICE" and e["ref"]
                                       and any(in_win(r.get("issue_date")) for r in e["rows"])})
    except Exception:  # noqa: BLE001
        nice_detail = {}

    # 4) 회차 레코드 ------------------------------------------------------
    recs = []  # 평가사별 회차
    for e in events:
        p = opinion(e) or {}
        grade_by_series = {series_key(r["series"]): r["grade"] for r in e["rows"]}
        kind_by_series = {series_key(r["series"]): r["kind"] for r in e["rows"]}
        items = []
        if e["agency"] == "NICE":
            for r in e["rows"]:
                items.append({"series": r["series"], "kind": r["kind"], "issue": r["issue_date"], "maturity": r["maturity"],
                              "amount": r["amount"], "grade": r["grade"]})
        elif p.get("schedule"):
            for x in p["schedule"]:
                sk = series_key(x["series"])
                items.append({**x, "kind": kind_by_series.get(sk, x.get("kind")), "grade": grade_by_series.get(sk, x["grade"])})
        else:
            for r in e["rows"]:
                k = nice_sched.get((e["key"], series_key(r["series"])))
                if k:
                    items.append({"series": r["series"], "kind": r["kind"], "issue": k["issue_date"],
                                  "maturity": k["maturity"], "amount": r["amount"], "grade": r["grade"]})
        for it in items:
            if not in_win(it.get("issue")) or it.get("kind") not in ("ABSTB", "ABCP"):
                continue
            recs.append({"event": e, "op": p, **it})

    merged: dict[tuple, dict] = {}
    for r in sorted(recs, key=lambda x: (AGENCY_PRIORITY.index(x["event"]["agency"]), x["event"]["eval_date"])):
        e = r["event"]
        k = (e["key"], series_key(r["series"]), r["issue"])
        m = merged.get(k)
        if m is None:
            merged[k] = m = {"key": e["key"], "spc": e["spc"], "series": r["series"], "kind": r["kind"],
                             "issue": r["issue"], "maturity": r["maturity"], "amount": r["amount"],
                             "ratings": {}, "eval_dates": {}, "docs": {}, "src_event": e, "src_op": r["op"]}
        if e["agency"] not in m["ratings"] or e["eval_date"] > m["eval_dates"][e["agency"]]:
            m["ratings"][e["agency"]] = r["grade"]
            m["eval_dates"][e["agency"]] = e["eval_date"]
            if e["doc"]:
                m["docs"][e["agency"]] = e["doc"]
        if not m["maturity"] and r.get("maturity"):
            m["maturity"] = r["maturity"]
        if not m["src_op"].get("supports") and r["op"].get("supports"):
            m["src_op"] = r["op"]

    # 5) KOFIA 금리 --------------------------------------------------------
    mpath = CACHE_DIR / "kofia_rows.json"
    mcache = read_json(mpath, {})
    tr = Trades()
    pending_keys = []
    for m in merged.values():
        ck = f'{m["kind"]}|{m["key"]}|{m["issue"]}'
        m["ck"] = ck
        if mcache.get(ck, {}).get("status") not in ("matched", "none"):
            pending_keys.append(m)
    stb_need = [parse_date(m["issue"]) for m in pending_keys if m["kind"] == "ABSTB"]
    if stb_need:
        s = min(stb_need)
        while s <= today:
            e_ = min(today, s + dt.timedelta(days=6))
            tr.stb_range(s, e_)
            s = e_ + dt.timedelta(days=1)
    for i, m in enumerate(pending_keys, 1):
        mcache[m["ck"]] = match_kofia(m["kind"], m["key"], parse_date(m["issue"]), tr, today)
        if i % 50 == 0:
            log(f"KOFIA 매칭 {i}/{len(pending_keys)}")
    keep = {m["ck"] for m in merged.values()}
    mcache = {k: v for k, v in mcache.items() if k in keep}
    write_json(mpath, mcache)
    status["sources"]["KOFIA"] = {"ok": not tr.errors, "errors": tr.errors[:5],
                                 "matched": sum(1 for v in mcache.values() if v.get("status") == "matched")}

    # 6) 출력 --------------------------------------------------------------
    out = []
    for m in merged.values():
        e, p = m["src_event"], m["src_op"] or {}
        nd = nice_detail.get(e["ref"] or "", {}) if e["agency"] == "NICE" else {}
        km = mcache.get(m["ck"], {})
        arranger = p.get("arranger") or nd.get("주관사")
        manager = p.get("asset_manager")
        asset = p.get("asset") or nd.get("기초자산")
        supports = p.get("supports") or ([{"company": nd["신용공여기관"], "detail": "신용공여기관"}] if nd.get("신용공여기관") else [])
        pf = is_pf(p.get("deal_type"), asset) or bool(km.get("pf"))
        issue, mat = parse_date(m["issue"]), parse_date(m["maturity"])
        agencies = sorted(m["ratings"], key=AGENCY_PRIORITY.index)
        grades = sorted(set(m["ratings"].values()))
        out.append({
            "spc": m["spc"], "kind": m["kind"], "series": m["series"],
            "grade": grades[0] if len(grades) == 1 else "/".join(grades),
            "ratings": [{"agency": a, "grade": m["ratings"][a], "eval_date": m["eval_dates"][a]} for a in agencies],
            "agencies": agencies, "issue_date": m["issue"], "maturity": m["maturity"],
            "days": (mat - issue).days if issue and mat else None, "amount": m["amount"],
            "arranger": re.sub(r"㈜|\(주\)", "", arranger).strip() if arranger else None,
            "asset_manager": re.sub(r"㈜|\(주\)", "", manager).strip() if manager else None,
            "supports": [{"company": s["company"], "summary": summarize_support(s["detail"]), "detail": s["detail"]}
                         for s in supports],
            "pf": "PF" if pf else "일반", "deal_type": p.get("deal_type"), "asset": asset,
            "detail_source": e["agency"],
            "reports": [{"agency": a, "url": op.public_url(m["docs"][a])} for a in agencies if a in m["docs"]],
            "kofia": ({"rate": km["rate"], "trade_date": km["trade_date"], "source": km["source"]}
                      if km.get("status") == "matched" else {"status": km.get("status", "pending")}),
        })
    out.sort(key=lambda x: (x["issue_date"], x["spc"], x["series"]), reverse=True)

    write_json(DATA_DIR / "abs.json", {"generated": status["generated"], "eval_window": status["eval_window"],
                                       "issue_window": status["issue_window"], "rows": out})
    write_json(DATA_DIR / "status.json", status)
    log(f"완료: 회차 {len(out)}건, KOFIA 매칭 {status['sources']['KOFIA']['matched']}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
