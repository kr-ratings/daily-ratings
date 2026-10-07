"""금융투자협회 채권정보센터(KOFIA) 단기금융 거래내역.

- ABCP 거래내역공시   : BISABCPTrdAnnSrchSO  (조회일 1일 단위)
- 전자단기사채 정보   : BISITShortPMnyInfoSrchSO (기간 조회)
둘 다 ProFrame XML 서비스(/proframeWeb/XMLSERVICES/)로 호출한다.
"""
from __future__ import annotations

import datetime as dt
import re
from xml.sax.saxutils import escape

from .util import norm_name, request, session

URL = "https://www.kofiabond.or.kr/proframeWeb/XMLSERVICES/"
_ROW = re.compile(r"<BISComDspDatDTO>(.*?)</BISComDspDatDTO>", re.S)
_VAL = re.compile(r"<(val\d+)>([^<]*)</\1>")


def _call(sess, svc: str, fields: dict) -> list[dict]:
    body = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in fields.items())
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n<message><proframeHeader>'
        f"<pfmAppName>BIS-KOFIABOND</pfmAppName><pfmSvcName>{svc}</pfmSvcName><pfmFnName>list</pfmFnName>"
        f"</proframeHeader><systemHeader></systemHeader><BISComDspDatDTO>{body}</BISComDspDatDTO></message>"
    )
    r = request(sess, "POST", URL, data=xml.encode("utf-8"), timeout=180,
                headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    text = r.content.decode("utf-8", errors="replace")
    return [dict(_VAL.findall(m)) for m in _ROW.findall(text)]


def _d(s: str) -> str | None:
    s = re.sub(r"\D", "", s or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 else None


def _f(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


class Trades:
    """요청한 날짜의 거래만 받아 메모리에 보관(실행 중 캐시)."""

    def __init__(self):
        self.sess = session()
        self.abcp: dict[str, list[dict]] = {}   # 거래일 → rows
        self.stb: dict[str, list[dict]] = {}
        self.errors: list[str] = []

    # ABCP: 하루 단위 조회
    def abcp_on(self, day: dt.date) -> list[dict]:
        k = day.isoformat()
        if k not in self.abcp:
            try:
                raw = _call(self.sess, "BISABCPTrdAnnSrchSO", {"val21": day.strftime("%Y%m%d"), "val23": "1"})
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"KOFIA ABCP {k}: {e}")
                raw = []
            self.abcp[k] = [{
                "trade": _d(r.get("val1")), "name": r.get("val2", ""), "key": norm_name(r.get("val2", "")),
                "type": r.get("val4", ""), "issue": _d(r.get("val6")), "maturity": _d(r.get("val7")),
                "rate": _f(r.get("val9")), "grade": r.get("val11", ""),
            } for r in raw if r.get("val4") in ("직접매입(할인)", "매출")]
        return self.abcp[k]

    # 전자단기사채: 기간 조회 → 거래일별로 나눠 담는다
    def stb_range(self, start: dt.date, end: dt.date) -> None:
        need = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
        need = [d for d in need if d.isoformat() not in self.stb]
        if not need:
            return
        s, e = min(need), max(need)
        try:
            raw = _call(self.sess, "BISITShortPMnyInfoSrchSO",
                        {"val1": s.strftime("%Y%m%d"), "val2": e.strftime("%Y%m%d"), "val3": "1"})
        except Exception as ex:  # noqa: BLE001
            self.errors.append(f"KOFIA 전단채 {s}~{e}: {ex}")
            raw = []
        for d in need:
            self.stb.setdefault(d.isoformat(), [])
        for r in raw:
            if r.get("val5") not in ("매출", "직접매입(할인)"):
                continue
            t = _d(r.get("val1"))
            self.stb.setdefault(t, []).append({
                "trade": t, "ab": r.get("val2", ""), "name": r.get("val3", ""), "key": norm_name(r.get("val3", "")),
                "asset": r.get("val4", ""), "type": r.get("val5", ""), "issue": _d(r.get("val7")),
                "maturity": _d(r.get("val8")), "rate": _f(r.get("val9")), "grade": r.get("val11", ""),
            })

    def stb_on(self, day: dt.date) -> list[dict]:
        self.stb_range(day, day)
        return self.stb.get(day.isoformat(), [])
