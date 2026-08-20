# bollinger_candle_with20

기존 `bol_low_candle` 전략에 **기준캔들 20일선(SMA20) 하락 필터**를 추가한 변형 버전입니다.  
상위 폴더 원본과 동일한 저점·기준캔들·매매 규칙을 쓰며, 사이드바에서 SMA20 필터만 ON/OFF 할 수 있습니다.

**엔진 버전:** `20260726-with20-v2`

---

## 원본과의 차이

| 항목 | 원본 (루트) | with20 |
|------|-------------|--------|
| 20일선 하락반영 | 없음 | 사이드바 체크박스 (기본 **OFF**) |
| 그 외 전략·UI | — | 원본과 동일 (저점/기준캔들, 탐색기간, CSV·Excel 등) |

---

## 20일선 하락반영

| 상태 | 동작 |
|------|------|
| **체크 ON** | 기준캔들 당일 SMA20 **&lt;** 전일 SMA20 이면 기준캔들 **탈락** |
| **체크 OFF** (기본) | 20일선 무시 → 원본과 동일 |

- 지표: 종가 **단순이동평균 SMA20** (볼린저밴드 기간과 독립, 항상 20일)
- 같으면(`=`) 통과, 하락(`<`)만 제외
- **저점캔들**에는 적용하지 않음 (기준캔들만)
- **실시간 자동매매 + 시뮬레이션** 모두 적용

---

## 실행 방법

상위 저장소의 `.env`를 사용하거나, 이 폴더에 `.env`를 두어도 됩니다.

```powershell
cd C:\Users\USER\Documents\bol_low_candle\bollinger_candle_with20
pip install -r requirements.txt
streamlit run bollinger_candle_auto_trade.py
```

실행 파일: `bollinger_candle_auto_trade.py`  
브라우저: `http://localhost:8501`  
사이드바 엔진 버전이 `20260726-with20-v2` 이면 최신입니다.

---

## 전략 요약 (원본과 동일 + SMA20 옵션)

### 저점캔들

- 저가 ≥ 240일선
- 시·고·저·종 중 하나라도 BB하한 **이하** (양·음봉 무관)
- 탐색: 상대기간(최대 10년) 또는 특정 년·월

### 기준캔들

- 저점캔들 다음 거래일 양봉
- 종가 **>** 저점캔들 고가
- (옵션) 20일선 하락반영 ON 시 SMA20 하락이면 탈락

### 매매

분할 매수(Case A/B), 손절, 1·2차 익절, 트레일링 스탑 — 원본과 동일

---

## 시뮬레이션 결과

- **종목별 요약** 유지
- **거래 상세** 컬럼:  
  종목코드, 종목명, 저점/기준캔들발생일, 1·2·3차매수가, 매수평균가,  
  손절일·손절가, 1·2차익절일·익절가, 트레일링스탑발동, 최종손익률, 투자금액, 손익금액
- **CSV / Excel** 다운로드 (요약 + 상세)

---

## 파일 구성

```
bollinger_candle_with20/
├── bollinger_candle_auto_trade.py   # Streamlit UI (실행 파일)
├── strategy.py                      # SMA20 + 기준캔들 하락 필터
├── config.py                        # ma20_decline_filter 기본 False
├── simulator.py
├── kiwoom_api.py
├── state_manager.py                 # trade_state_with20.json 사용
├── requirements.txt
├── .env.example
└── README.md
```

원본 전체 설명은 상위 [`README.md`](../README.md)를 참고하세요.
