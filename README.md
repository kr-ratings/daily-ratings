# 유동화증권 등급 · 금리 현황 대시보드

신용평가사 본평가를 받은 유동화증권(ABSTB·ABCP)의 최근 발행 회차와 CP·CD·국고채 3년 금리를 자동 수집해 보여주는 정적 웹페이지입니다.

- 사이트: https://kr-ratings.github.io/daily-ratings/
- 갱신: GitHub Actions가 평일 12:05·18:05(KST)에 `collector`를 실행해 `site/data/*.json`을 갱신하고 GitHub Pages로 배포합니다.

## 데이터 출처

| 영역 | 출처 |
|---|---|
| CP 91일·CD 91일·국고채 3년 | 한국은행 ECOS 시장금리(일별) |
| 본평가 공시 목록 | NICE신용평가, 한국신용평가, 한국기업평가 |
| 회차 일정·주관사·신용보강·거래유형 | 각 평가사 평가의견서 PDF |
| 거래금리 | 금융투자협회 채권정보센터 ABCP·전자단기사채 거래내역 |

## 유지보수 메모

- ECOS 인증키: Settings → Secrets and variables → Actions → `ECOS_API_KEY` (없으면 체험용 `sample` 키 사용)
- 한국기업평가는 요청이 몰리면 IP를 일시 차단하므로 한기평 평가서는 실행당 최대 150건씩 나눠 받습니다.
- `collector/util.py`의 `HOLIDAYS`는 매년 초 다음 해 휴장일을 추가합니다.

본 페이지는 비공식 참고용이며 투자 판단의 근거로 사용할 수 없습니다.
