"""금리만 빠르게 갱신 — 장 마감 후 30분 간격으로 실행(.github/workflows/rates.yml).

    python -m collector.rates_job          # 영업일에만 동작
    FORCE=1 python -m collector.rates_job  # 휴일에도 실행
"""
from __future__ import annotations

import os
import sys

from . import rates as rates_mod
from .util import DATA_DIR, is_bday, now_kst_str, read_json, today_kst, write_json


def main() -> int:
    if not is_bday(today_kst()) and not os.environ.get("FORCE"):
        print("영업일 아님 → 건너뜀", file=sys.stderr)
        return 0
    old = read_json(DATA_DIR / "rates.json", {})
    try:
        new = rates_mod.collect(days=40, base=old)
    except Exception as e:  # noqa: BLE001  일시 장애면 다음 실행에서 다시 시도
        print("금리 수집 실패:", e, file=sys.stderr)
        return 0
    # 기간·최신값·보강 여부가 같으면 그대로 둔다
    sig = lambda d: (d.get("asof"), d.get("prelim"),  # noqa: E731
                     {k: (s["points"][0]["d"], len(s["points"]), s["points"][-1]) for k, s in d.get("series", {}).items() if s.get("points")})
    if sig(new) == sig(old):
        print("금리 변동 없음", new.get("asof"), file=sys.stderr)
        return 0
    now = now_kst_str()
    new["generated"] = now
    write_json(DATA_DIR / "rates.json", new)
    st = read_json(DATA_DIR / "status.json", {})
    if st:
        st.setdefault("sources", {})["ECOS"] = {"ok": True, "asof": new["asof"], "key": new["key"],
                                                **({"prelim": new["prelim"]["date"]} if new.get("prelim") else {})}
        st["rates_updated"] = now
        write_json(DATA_DIR / "status.json", st)
    print("금리 갱신", new["asof"], "(KOFIA 보강)" if new.get("prelim") else "", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
