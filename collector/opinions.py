"""평가의견서 PDF 다운로드·파싱 (NICE·한기평·한신평).

추출 항목
  schedule : [{series, kind, amount, issue, maturity, grade}]  회차별 발행일정
  arranger : 주관회사
  supports : [{company, detail}]                                신용보강(통제방안·주요평정요인)
  deal_type, asset, obligor                                     PF 판단·기초자산 표시용
결과는 문서 키별로 cache/opinions.json 에 저장해 한 번만 받는다.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .util import CACHE_DIR, read_json, request, session, write_json

DATE = r"(\d{4}\.\s?\d{1,2}\.\s?\d{1,2})"
GRADE = r"([AB][123]?\s?[+-]?\s*\(\s*sf\s*\))"
_local = threading.local()
# 한기평은 동시 요청이 많으면 429를 돌려준다 → 평가사별 동시 다운로드 수 제한
_LIMIT = {"NICE": threading.Semaphore(3), "한기평": threading.Semaphore(1), "한신평": threading.Semaphore(3)}


def _sess():
    if not hasattr(_local, "s"):
        _local.s = session()
        _local.kr_ready = False
    return _local.s


def _iso(s: str) -> str | None:
    m = re.match(r"(\d{4})\.\s?(\d{1,2})\.\s?(\d{1,2})", s or "")
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None


def _num(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def doc_key(doc: dict) -> str:
    return f'{doc["agency"]}:{doc["id"]}'


# ------------------------------------------------------------------ 다운로드
def download(doc: dict) -> bytes:
    s = _sess()
    a = doc["agency"]
    if a == "NICE":
        r = request(s, "GET", "https://www.nicerating.com/common/fileDown.do", params={"docId": doc["id"]}, timeout=90)
    elif a == "한기평":
        if not _local.kr_ready:
            request(s, "GET", "https://www.korearatings.com/cms/frCmnCon/index.do?MENU_ID=360", timeout=60)
            _local.kr_ready = True
        r = request(s, "GET", "https://www.korearatings.com/ajaxa/fileCpnt/reportFileDown.do", timeout=90, params={
            "encFileNm": doc["id"], "encSvcSeqNo": doc.get("svc"), "evalNo": doc.get("seq"), "rptNo": "03",
            "fileName": "", "compCd": doc.get("comp_cd"), "compNm": doc.get("comp_nm")})
    elif a == "한신평":
        d = {"menuCd": doc.get("menuCd", "R8"), "gubun": doc.get("gubun", "2"), "fileName": doc["id"],
             "fileTitle": doc.get("title", ""), "writedate": doc.get("writedate", ""), "filePath": "", "fullFileName": ""}
        request(s, "POST", "https://www.kisrating.com/checkFeeFile.json", data=d, timeout=30)
        r = request(s, "POST", "https://www.kisrating.com/fileDown.do", data=d, timeout=90)
    else:
        raise ValueError(a)
    if not r.content.startswith(b"%PDF"):
        raise RuntimeError(f"{a} 의견서가 PDF가 아님({r.headers.get('Content-Type')})")
    return r.content


def pdf_text(data: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        out = subprocess.run(["pdftotext", "-layout", "-l", "4", f.name, "-"], capture_output=True, timeout=60)
    return out.stdout.decode("utf-8", errors="replace")


# ------------------------------------------------------------------ 공통 추출
_CO_SUFFIX = r"(?:증권|은행|캐피탈|건설|산업|금융지주|투자|보험|생명|화재|카드|공사|기금|보증|신탁|저축은행|자산운용|에셋|개발|이앤씨|E&C|중공업|엔지니어링|지주|홀딩스|리츠|물산|전자|화학|에너지|파트너스|인베스트먼트|글로벌|코리아)"
_W = r"[가-힣A-Za-z0-9&·]"
_CO_PATTERNS = [
    re.compile(rf"(?:㈜|\(주\))[ ]?({_W}+)"),                 # ㈜국민은행의 → 국민은행의
    re.compile(rf"({_W}+)(?:㈜|\(주\))"),                     # 메리츠증권㈜ → 메리츠증권
    re.compile(rf"({_W}{{2,}}(?:은행|기금|공사))(?=의|이|가|와|과|,)"),  # 한국산업은행의 (표기 없음)
]
_PARTICLE = re.compile(r"(?:에게|으로|의|와|과|는|을|를|에|로|및|가)$")
_CO_STOP = {"SPC", "유동화회사", "차주", "시행사", "당사", "회사", "동사", "대주", "본건", "신탁사", "주관회사", "업무수탁자",
            "예치은행", "거래상대방", "판매사", "시공사", "매입보장기관", "신용공여기관", "자산관리자", "수탁자", "참가대상자산"}


def companies(text: str) -> list[str]:
    found: list[str] = []
    for i, pat in enumerate(_CO_PATTERNS):
        for m in pat.finditer(text):
            name = m.group(1).strip("·, ")
            if i == 0:
                name = _PARTICLE.sub("", name)
            name = re.sub(r"^(?:및|또는|그리고)", "", name)
            if (len(name) < 2 or name in _CO_STOP or re.fullmatch(r"[\d\-]+", name) or name.endswith("신용평가")
                    or re.search(r"제[일이삼사오육칠팔구십백\d]+차$", name)):
                continue
            if not any(name == f or name in f or f in name for f in found):
                found.append(name)
    return found


def _clean(s: str) -> str:
    s = s.replace("\uf0a7", "").replace("▪", "").replace("•", "").replace("∙", "")
    return re.sub(r"\s+", " ", s).strip(" -·")


def _field(text: str, label: str) -> str | None:
    m = re.search(rf"(?:^|[ \t]{{2,}}){label}[ \t]{{2,}}(\S.*?)[ \t]*$", text, re.M)
    return _clean(m.group(1)) if m else None


def _supports_from_lines(lines: list[str]) -> list[dict]:
    out = []
    for ln in lines:
        ln = _clean(ln)
        if not ln or len(ln) < 4:
            continue
        cos = companies(ln)
        if not cos:
            continue
        for c in cos:
            if not any(o["company"] == c for o in out):
                out.append({"company": c, "detail": ln})
    return out


# ------------------------------------------------------------------ 평가사별 파서
def parse_nice(t: str) -> dict:
    kind = "ABCP" if "기업어음" in t[:1500] and "전자단기사채" not in t[:1500] else "ABSTB"
    sched = []
    for m in re.finditer(rf"^\s*(제?\s?[\d\-]+\s?회(?:차)?)\s+([\d,\.]+)\s+{DATE}\s+{DATE}\s+{GRADE}", t, re.M):
        sched.append({"series": m.group(1).replace(" ", ""), "kind": kind, "amount": _num(m.group(2)),
                      "issue": _iso(m.group(3)), "maturity": _iso(m.group(4)), "grade": m.group(5)})
    # 주요 위험요소 및 통제방안: 다음 섹션(주) 정보제공자 / 평가근거) 전까지
    sup_lines = []
    m = re.search(r"주요\s?위험요소\s?및\s?통제방안(.+?)(?:\n\s*주\)|\n\s*평가근거)", t, re.S)
    if m:
        for ln in m.group(1).splitlines():
            if not ln.strip():
                continue
            parts = re.split(r"\s{2,}", ln.strip(), maxsplit=1)
            indent = len(ln) - len(ln.lstrip())
            if len(parts) == 2 and indent < 20:
                sup_lines.append(parts[1])
            elif sup_lines:
                sup_lines[-1] += " " + ln.strip()
            else:
                sup_lines.append(ln.strip())
    m2 = re.search(r"주요\s?평가근거는\s?아래와\s?같다\.(.+?)\n\s*\n\s*\n", t, re.S)
    if m2:
        sup_lines += [ln for ln in m2.group(1).splitlines() if ln.strip().startswith(("▪", "•", "-"))]
    return {"schedule": sched, "arranger": _field(t, "주관회사"), "asset_manager": _field(t, "자산관리자"), "supports": _supports_from_lines(sup_lines),
            "deal_type": _field(t, "유동화구조"), "asset": _field(t, "기초자산"), "obligor": None}


def parse_kr(t: str) -> dict:
    sched = []
    for m in re.finditer(rf"(제\s?[\d\-]+\s?회(?:차)?)\s+(ABSTB|ABCP)\s+([\d,\.]+)\s*억원.*?{DATE}\s+{DATE}\s+{GRADE}", t):
        sched.append({"series": m.group(1).replace(" ", ""), "kind": m.group(2), "amount": _num(m.group(3)),
                      "issue": _iso(m.group(4)), "maturity": _iso(m.group(5)), "grade": m.group(6)})
    sup_lines = []
    m = re.search(r"주요\s?평정요인(.+?)(?:\n\s*\n\s*\n|■)", t, re.S)
    if m:
        sup_lines = m.group(1).splitlines()
    return {"schedule": sched, "arranger": _field(t, "주관회사"), "asset_manager": _field(t, "자산관리자"), "supports": _supports_from_lines(sup_lines),
            "deal_type": _field(t, "거래유형"), "asset": _field(t, "자산유형"), "obligor": _field(t, "차주")}


def parse_kis(t: str) -> dict:
    g = r"[AB][123]?\s?[+-]?\s*\(\s*sf\s*\)"
    tok = rf"(?:{g}|WR|-)"
    sched, last_kind = [], None
    pat = rf"(?:(ABSTB|ABCP)\s+)?(제\s?[\d\-]+\s?회(?:차)?)\s+({tok}(?:\s+{tok})?)\s+([\d,\.]+)\s*억원\s+{DATE}\s+{DATE}"
    for m in re.finditer(pat, t):
        last_kind = m.group(1) or last_kind
        grades = re.findall(rf"{g}|WR|-", m.group(3))
        cur = grades[-1] if grades else ""
        if cur in ("WR", "-"):
            continue  # 철회·미부여 회차 제외
        sched.append({"series": m.group(2).replace(" ", ""), "kind": last_kind, "amount": _num(m.group(4)),
                      "issue": _iso(m.group(5)), "maturity": _iso(m.group(6)), "grade": cur})
    if sched and not sched[0]["kind"]:
        k = "ABCP" if "ABCP" in t[:3000] and "ABSTB" not in t[:3000] else "ABSTB"
        for x in sched:
            x["kind"] = x["kind"] or k
    sup_lines = []
    m = re.search(r"주요\s?평가요소(.+?)(?:유동화\s?개요|위험요인과\s?통제방안)", t, re.S)
    if m:
        sup_lines = [ln for ln in m.group(1).splitlines() if any(b in ln for b in ("•", "\uf0a7", "▪"))]
    return {"schedule": sched, "arranger": _field(t, "주관회사"), "asset_manager": _field(t, "자산관리자"), "supports": _supports_from_lines(sup_lines),
            "deal_type": _field(t, r"유동화[ ]?유형"), "asset": _field(t, "기초자산"), "obligor": None}


PARSERS = {"NICE": parse_nice, "한기평": parse_kr, "한신평": parse_kis}


def fetch_parse(doc: dict) -> dict:
    with _LIMIT[doc["agency"]]:
        data = download(doc)
    return _parse(doc, data)


class Blocked(Exception):
    pass


def load_all(docs: list[dict], workers: int = 4, log=print) -> dict:
    """docs 중 캐시에 없는 것만 받아 파싱. docs는 우선순위 순서로 넘긴다. 반환: {doc_key: parsed}

    한기평은 평가서 다운로드에 IP 단위 요청 제한이 있어(초과 시 일정 시간 차단)
    한 건씩 KR_INTERVAL초 간격으로, 실행당 최대 KR_MAX_DOCS건만 받고, 차단되면 즉시 멈춘다.
    남은 건은 다음 실행 때 이어서 받는다.
    """
    import os
    path = CACHE_DIR / "opinions.json"
    cache = read_json(path, {})
    todo = {}
    for d in docs:
        k = doc_key(d)
        # asset_manager 키가 없는 예전 파싱 결과는 다시 받아 자산관리자를 채운다(실패 시 예전 결과 유지)
        if (k not in cache or "asset_manager" not in cache[k]) and k not in todo:
            todo[k] = d
    kr = [(k, d) for k, d in todo.items() if d["agency"] == "한기평"]
    rest = [(k, d) for k, d in todo.items() if d["agency"] != "한기평"]
    stats = {"fetched": 0, "failed": 0, "kr_left": 0, "kr_blocked": False}

    def store(k, res):
        if "error" not in res or "PDF가 아님" in res.get("error", ""):
            cache[k] = res
            stats["fetched"] += 1
        else:
            stats["failed"] += 1

    def work(item):
        k, d = item
        try:
            return k, fetch_parse(d)
        except Exception as e:  # noqa: BLE001
            return k, {"error": f"{type(e).__name__}: {str(e)[:120]}"}

    if rest:
        log(f"평가의견서(NICE·한신평) {len(rest)}건 수집")
        with ThreadPoolExecutor(workers) as ex:
            for i, (k, res) in enumerate(ex.map(work, rest), 1):
                store(k, res)
                if i % 50 == 0:
                    log(f"  {i}/{len(rest)}")
                    write_json(path, cache)
        write_json(path, cache)

    if kr:
        cap = int(os.environ.get("KR_MAX_DOCS", "150"))
        gap = float(os.environ.get("KR_INTERVAL", "6"))
        log(f"평가의견서(한기평) {len(kr)}건 중 최대 {cap}건 수집")
        for i, (k, d) in enumerate(kr[:cap], 1):
            try:
                data = _download_once(d)
                store(k, _parse(d, data))
            except Blocked:
                stats["kr_blocked"] = True
                log("  한기평 요청 제한으로 중단 — 다음 실행 때 이어서 받음")
                break
            except Exception as e:  # noqa: BLE001
                store(k, {"error": str(e)[:120]})
            if i % 20 == 0:
                log(f"  {i}/{min(cap, len(kr))}")
                write_json(path, cache)
            time.sleep(gap)
        stats["kr_left"] = sum(1 for k, _ in kr if k not in cache)
        write_json(path, cache)
    load_all.stats = stats
    return cache


def _download_once(doc: dict) -> bytes:
    """한기평 전용: 재시도 없이 1회 요청, 429면 Blocked."""
    s = _sess()
    if not _local.kr_ready:
        r0 = s.get("https://www.korearatings.com/cms/frCmnCon/index.do?MENU_ID=360", timeout=60)
        if r0.status_code == 429:
            raise Blocked()
        _local.kr_ready = True
    r = s.get("https://www.korearatings.com/ajaxa/fileCpnt/reportFileDown.do", timeout=90, params={
        "encFileNm": doc["id"], "encSvcSeqNo": doc.get("svc"), "evalNo": doc.get("seq"), "rptNo": "03",
        "fileName": "", "compCd": doc.get("comp_cd"), "compNm": doc.get("comp_nm")})
    if r.status_code == 429:
        raise Blocked()
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise RuntimeError("한기평 의견서가 PDF가 아님")
    return r.content


def _parse(doc: dict, data: bytes) -> dict:
    res = PARSERS[doc["agency"]](pdf_text(data))
    for x in res["schedule"]:
        x["grade"] = re.sub(r"\s|\(sf\)", "", x["grade"])
    return res


def public_url(doc: dict) -> str | None:
    """브라우저에서 바로 열리는 평가서 다운로드 주소."""
    a = doc.get("agency")
    if a == "NICE":
        return "https://www.nicerating.com/common/fileDown.do?" + urlencode({"docId": doc["id"]})
    if a == "한신평":
        return "https://www.kisrating.com/fileDown.do?" + urlencode({
            "menuCd": doc.get("menuCd", "R8"), "gubun": doc.get("gubun", "2"), "fileName": doc["id"],
            "fileTitle": doc.get("title", ""), "writedate": doc.get("writedate", "")})
    if a == "한기평":
        return "https://www.korearatings.com/ajaxa/fileCpnt/reportFileDown.do?" + urlencode({
            "encFileNm": doc["id"], "encSvcSeqNo": doc.get("svc") or "", "evalNo": doc.get("seq") or "",
            "rptNo": "03", "fileName": "", "compCd": doc.get("comp_cd") or "", "compNm": doc.get("comp_nm") or ""})
    return None
