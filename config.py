"""
config.py - .env 파일 로딩 및 전역 설정 관리
"""

import os
from dotenv import load_dotenv

# 코드 업데이트 시 Streamlit 세션 초기화용 (값 변경 시 자동 리셋)
APP_VERSION = "20260705-v12"

# .env 파일 로딩
load_dotenv()

# ── API 인증정보 ──────────────────────────────────────────────
APP_KEY    = os.getenv("APP_KEY", "").strip()
APP_SECRET = os.getenv("APP_SECRET", "").strip()
ACCOUNT_NO = os.getenv("ACCOUNT_NO", "").strip()

# ── 수정된 코드 (실제 키움 REST API 기준) ────────────
BASE_URL = "https://api.kiwoom.com"          # 실전투자
# BASE_URL = "https://mockapi.kiwoom.com"    # 모의투자 시 이 줄 사용

ENDPOINTS = {
    "token"      : "/oauth2/token",          # 토큰 발급
    "current"    : "/api/dostk/stkinfo",     # 주식 기본정보 (현재가 포함)
    "daily"      : "/api/dostk/chart",       # 일봉차트
    "order"      : "/api/dostk/ordr",        # 주문
    "cancel"     : "/api/dostk/ordr",        # 취소주문 (같은 경로)
    "balance"    : "/api/dostk/acnt",        # 계좌/잔고
    "stock_info" : "/api/dostk/stkinfo",     # 종목정보
}

TR_IDS = {
    "current"  : "ka10001",   # 주식기본정보요청 (현재가 포함)
    "daily"    : "ka10081",   # 주식일봉차트조회요청
    "buy"      : "kt10000",   # 주식 매수주문
    "sell"     : "kt10001",   # 주식 매도주문
    "cancel"   : "kt10003",   # 주식 취소주문
    "balance"  : "kt00005",   # 체결잔고요청
    "unfilled" : "ka10075",   # 미체결요청
    "stock_info": "ka10001",  # 주식기본정보요청
}

# ── 시세 구분 (KRX / 통합시세) ─────────────────────────────────
PRICE_SOURCES = {
    "KRX" : {"label": "KRX (정규장)",           "suffix": ""},
    "통합": {"label": "통합시세 (KRX+NXT)",     "suffix": "_AL"},
}

# ── 저점캔들 탐색 기간 (6개월 단위, 최대 5년) ─────────────────
SCAN_MONTH_STEP = 6
SCAN_MONTH_MIN  = 6
SCAN_MONTH_MAX  = 60
TRADING_DAYS_PER_MONTH = 21   # 연간 약 252거래일 기준
MAX_CANDLE_FETCH = 1500        # 키움 API 일봉 최대 수집량
MIN_CANDLE_FETCH = 600         # MA240 정확도를 위한 최소 수집량

# ── 240일선 필터 ───────────────────────────────────────────────
MA_FILTER_MODES = ["디폴트", "240선추세"]
MA_TREND_DAYS_OPTIONS = [3, 7, 10, 20]

# ── 기본 전략 파라미터 ────────────────────────────────────────
DEFAULT_PARAMS = {
    "bb_period"        : 20,          # 볼린저밴드 기간
    "bb_multiplier"    : 2.0,         # 볼린저밴드 승수 (D1)
    "ma_period"        : 240,         # 이동평균선 기간
    "scan_months"      : 6,           # 저점캔들 탐색 기간 (6개월 단위, 최대 60=5년)
    "total_budget"     : 7_000_000,   # 총 투자금액
    "buy1_ratio"       : 1,           # 1차 비율
    "buy2_ratio"       : 2,           # 2차 비율
    "buy3_ratio"       : 4,           # 3차 비율
    "abandon_pct"      : 30,          # 매수포기 기준 (저점캔들 저가 대비 %)
    "stop_mode"        : "기준저가",   # 손절 방식
    "stop_pct"         : 10,          # 1차 매수가 대비 손절 % (옵션 모드)
    "profit1_pct"      : 20,          # 1차 익절 비율 %
    "profit2_pct"      : 25,          # 2차 익절 비율 %
    "trailing_pct"     : 5,           # 트레일링 스탑 하락 허용 %
    "volume_ratio"     : 3.0,         # 거래량 배수 기준
    "data_days"        : 0,           # 일봉 수집 일수 (scan_months+ma_period 기준 자동 계산)
    "poll_interval"    : 5,           # 현재가 폴링 간격(초)
    "price_source"     : "KRX",       # 시세 구분: KRX | 통합
    "ma_filter_mode"   : "디폴트",     # 240일선 조건: 디폴트 | 240선추세
    "ma_trend_days"    : 3,           # 240선추세 모드 시 연속 상승 일수 (3/7/10/20)
}

# ── 장 운영시간 ───────────────────────────────────────────────
MARKET_OPEN  = "09:00"
MARKET_CLOSE = "15:30"

# 매도 대금 재매수 가능까지의 결제 대기 (거래일)
SETTLEMENT_TRADING_DAYS = 3


def validate_env() -> list[str]:
    """필수 환경변수 누락 항목 반환. 빈 리스트이면 정상."""
    missing = []
    if not APP_KEY:
        missing.append("APP_KEY")
    if not APP_SECRET:
        missing.append("APP_SECRET")
    if not ACCOUNT_NO:
        missing.append("ACCOUNT_NO")
    return missing


def calc_budgets(total: int) -> dict:
    """총 투자금액을 100:200:400 비율로 분할"""
    unit = total / 7
    return {
        "buy1": int(unit * 1),
        "buy2": int(unit * 2),
        "buy3": int(unit * 4),
    }


def scan_months_to_days(months: int) -> int:
    """탐색 개월 수 → 거래일 수 변환"""
    return int(months * TRADING_DAYS_PER_MONTH)


def calc_data_days(scan_months: int, ma_period: int) -> int:
    """탐색기간 + MA 계산 + 시뮬레이션 여유를 포함한 일봉 수집 일수"""
    scan_days = scan_months_to_days(scan_months)
    needed = ma_period + scan_days + 252   # MA + 탐색 + 1년 시뮬 여유
    return min(MAX_CANDLE_FETCH, max(MIN_CANDLE_FETCH, needed))


def format_scan_months(months: int) -> str:
    """UI 표시용 라벨 (예: 6개월, 1년, 2년 6개월)"""
    if months < 12:
        return f"{months}개월"
    years, rem = divmod(months, 12)
    if rem == 0:
        return f"{years}년"
    return f"{years}년 {rem}개월"


def scan_month_options() -> list[int]:
    """6개월 단위 탐색 옵션 (6 ~ 60개월)"""
    return list(range(SCAN_MONTH_MIN, SCAN_MONTH_MAX + 1, SCAN_MONTH_STEP))


def resolve_scan_days(params: dict) -> int:
    """params에서 저점캔들 탐색 거래일 수 반환"""
    if "scan_months" in params:
        return scan_months_to_days(int(params["scan_months"]))
    return int(params.get("scan_days", scan_months_to_days(SCAN_MONTH_MIN)))


def normalize_stock_code(stock_code: str) -> str:
    """종목코드 접미사(_AL/_NX) 제거 후 6자리 정규화"""
    base = str(stock_code).split("_")[0].strip()
    return base.zfill(6) if base.isdigit() else base


def resolve_price_code(stock_code: str, price_source: str) -> str:
    """시세 조회용 종목코드 (KRX: 005930, 통합: 005930_AL)"""
    base = normalize_stock_code(stock_code)
    info = PRICE_SOURCES.get(price_source, PRICE_SOURCES["KRX"])
    return base + info["suffix"]


def price_source_options() -> list[str]:
    return list(PRICE_SOURCES.keys())


def format_price_source(source: str) -> str:
    return PRICE_SOURCES.get(source, PRICE_SOURCES["KRX"])["label"]


def ma_filter_mode_options() -> list[str]:
    return list(MA_FILTER_MODES)


def ma_trend_days_options() -> list[int]:
    return list(MA_TREND_DAYS_OPTIONS)


def format_ma_filter(params: dict) -> str:
    """UI/로그용 240일선 조건 설명"""
    if params.get("ma_filter_mode") == "240선추세":
        days = int(params.get("ma_trend_days", 3))
        return f"240선 {days}일 연속 상승"
    return "240선 위 (디폴트)"


def parse_stock_list(text: str) -> list[str]:
    """텍스트에서 종목명/코드 목록 추출 (줄바꿈·쉼표·탭·세미콜론 구분)"""
    import re
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = re.split(r"[\n,\t;]+", normalized)
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        entry = part.strip()
        if entry and entry not in seen:
            seen.add(entry)
            result.append(entry)
    return result


def resolve_data_days(params: dict) -> int:
    """params에서 일봉 수집 일수 반환 (미설정 시 자동 계산)"""
    scan_months = int(params.get("scan_months", SCAN_MONTH_MIN))
    ma_period   = int(params.get("ma_period", 240))
    explicit    = int(params.get("data_days", 0))
    auto        = calc_data_days(scan_months, ma_period)
    return max(auto, explicit) if explicit > 0 else auto


# 기본 data_days 자동 설정
DEFAULT_PARAMS["data_days"] = calc_data_days(
    DEFAULT_PARAMS["scan_months"], DEFAULT_PARAMS["ma_period"]
)
