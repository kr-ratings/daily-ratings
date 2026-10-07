"""공통 유틸: HTTP 세션, 날짜/영업일, 이름 정규화."""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "site" / "data"
CACHE_DIR = ROOT / "cache"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
    return s


def request(sess: requests.Session, method: str, url: str, *, tries: int = 3, timeout: int = 60, **kw):
    """재시도 포함 요청. 마지막 실패 시 예외를 그대로 올린다."""
    last = None
    for i in range(tries):
        try:
            r = sess.request(method, url, timeout=timeout, **kw)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            code = getattr(getattr(e, "response", None), "status_code", None)
            # 429(요청 과다)는 길게 쉬었다가 재시도
            time.sleep(20 * (i + 1) if code == 429 else 2 * (i + 1))
    raise last  # type: ignore[misc]


def today_kst() -> dt.date:
    return dt.datetime.now(KST).date()


def now_kst_str() -> str:
    return dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")


# 주말 외 휴장일(필요 시 추가). KOFIA 거래 매칭의 '2영업일' 계산에만 쓰이며,
# 휴일 데이터가 없어 거래 0건이 나오는 날은 자동으로 건너뛰므로 누락돼도 치명적이지 않다.
HOLIDAYS = {
    "2026-01-01", "2026-02-16", "2026-02-17", "2026-02-18", "2026-03-02",
    "2026-05-01", "2026-05-05", "2026-05-25", "2026-06-03", "2026-08-17",
    "2026-09-24", "2026-09-25", "2026-10-05", "2026-10-09", "2026-12-25",
    "2026-12-31",
    "2027-01-01", "2027-02-08", "2027-02-09", "2027-03-01", "2027-05-05",
    "2027-05-13", "2027-08-16", "2027-09-14", "2027-09-15", "2027-09-16",
    "2027-10-04", "2027-10-11", "2027-12-27", "2027-12-31",
}


def is_bday(d: dt.date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def add_bdays(d: dt.date, n: int) -> dt.date:
    while n > 0:
        d += dt.timedelta(days=1)
        if is_bday(d):
            n -= 1
    return d


def parse_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) != 8:
        return None
    try:
        return dt.date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
    except ValueError:
        return None


_NAME_DROP = re.compile(
    r"\(주\)|㈜|주식회사|\(유\)|유한회사|유동화전문|유동화전문유한회사|\s+"
)


def norm_name(name: str) -> str:
    """SPC명 비교용 키. '(주)', '유동화전문(유)' 등과 공백을 제거."""
    return _NAME_DROP.sub("", name or "").strip()


def display_name(name: str) -> str:
    return re.sub(r"\(주\)|㈜|주식회사", "", name or "").strip()


def clean_grade(g: str | None) -> str:
    """'A1 (sf)' / 'A1(sf)' → 'A1'. 화살표·감시 기호는 유지."""
    g = (g or "").replace("(sf)", "").replace(" ", "").strip()
    return g


def to_num(s) -> float | None:
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
