"""
simulator.py - 텍스트 파일 업로드 기반 백테스트/시뮬레이션
실제 주문 없이 과거 일봉 데이터로 전략 성과를 계산
"""

import json
import logging
from collections import defaultdict
from typing import Callable, Optional

import pandas as pd

from kiwoom_api import KiwoomAPI
from strategy import simulate_strategy
from config import resolve_data_days, SETTLEMENT_TRADING_DAYS

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str, Optional[pd.DataFrame]], None]


class Simulator:
    def __init__(self, api: KiwoomAPI, params: dict):
        self.api    = api
        self.params = params

    def run(self, ticker_map: dict[str, str],
            progress_callback: Optional[ProgressCallback] = None) -> pd.DataFrame:
        """
        ticker_map: {code: name}
        progress_callback: fn(done, total, message, partial_df)
            - done: 완료된 종목 수 (0 ~ total)
            - partial_df: 지금까지 누적된 결과 DataFrame
        """
        records: list[dict] = []
        total   = len(ticker_map)

        for i, (code, name) in enumerate(ticker_map.items()):
            if progress_callback:
                progress_callback(
                    i, total,
                    f"[{i + 1}/{total}] {name}({code}) 분석 중...",
                    _records_to_df(records),
                )

            batch = self._simulate_one(code, name)
            records.extend(batch)

            if progress_callback:
                progress_callback(
                    i + 1, total,
                    f"[{i + 1}/{total}] {name}({code}) 완료",
                    _records_to_df(records),
                )

        if progress_callback:
            progress_callback(total, total, "시뮬레이션 완료", _records_to_df(records))

        return _records_to_df(records)

    def _simulate_one(self, code: str, name: str) -> list[dict]:
        """단일 종목 시뮬 → 레코드 리스트 (0건이면 빈 레코드 1행)"""
        try:
            candles = self.api.get_daily_candles(
                code, count=resolve_data_days(self.params),
                price_source=self.params.get("price_source", "KRX"),
            )
            if not candles:
                logger.warning("일봉 데이터 없음: %s", code)
                return [_empty_record(code, name, "데이터 없음")]

            trades = simulate_strategy(candles, self.params)
            if not trades:
                return [_empty_record(code, name, "매매조건 없음")]

            return [_trade_record(code, name, t) for t in trades]

        except Exception as e:
            logger.error("시뮬레이션 오류 [%s]: %s", code, e)
            return [_empty_record(code, name, f"오류: {e}")]


def _records_to_df(records: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(records) if records else pd.DataFrame()


def _trade_record(code: str, name: str, t: dict) -> dict:
    return {
        "종목코드"        : code,
        "종목명"          : name,
        "저점캔들발생일"   : t.get("pivot_date", ""),
        "기준캔들발생일"   : t.get("base_date",  ""),
        "거래량케이스"     : t.get("case", ""),
        "1차매수가"        : t.get("buy1_price", 0),
        "2차매수가"        : t.get("buy2_price", 0),
        "3차매수가"        : t.get("buy3_price", 0),
        "1차매수체결"      : "O" if t.get("buy1_filled") else "X",
        "2차매수체결"      : "O" if t.get("buy2_filled") else "X",
        "3차매수체결"      : "O" if t.get("buy3_filled") else "X",
        "손절발동"         : "O" if t.get("stop_triggered") else "X",
        "손절가"           : t.get("stop_price_actual", 0),
        "1차익절발동"      : "O" if t.get("profit1_triggered") else "X",
        "1차익절가"        : t.get("profit1_price", 0),
        "2차익절발동"      : "O" if t.get("profit2_triggered") else "X",
        "2차익절가"        : t.get("profit2_price", 0),
        "트레일링스탑발동" : "O" if t.get("trailing_triggered") else "X",
        "최종손익률(%)"    : t.get("final_pnl_pct", 0.0),
        "투자금액"         : t.get("invest_amount", 0),
        "손익금액"         : t.get("pnl_amount", 0),
        "240선상승일"      : t.get("ma_rising_days", 0),
        "flow_events"      : json.dumps(t.get("flow_events", []), ensure_ascii=False),
        "비고"             : "",
    }


def summarize_by_stock(df: pd.DataFrame) -> pd.DataFrame:
    """종목별 시뮬레이션 요약 (신호 수, 매수 건수, 평균 손익률, 승률)"""
    if df.empty:
        return pd.DataFrame()

    rows = []
    for code, group in df.groupby("종목코드", sort=False):
        name = str(group["종목명"].iloc[0])
        notes = group.get("비고", pd.Series(dtype=str)).astype(str).replace("nan", "").str.strip()
        note  = next((n for n in notes if n), "")

        signals = len(group)
        buys    = group[group["1차매수체결"] == "O"]
        buy_cnt = len(buys)

        pnl_amt_series = pd.to_numeric(
            group.get("손익금액", pd.Series(dtype=float)), errors="coerce"
        ).fillna(0)
        profit_amt = int(pnl_amt_series[pnl_amt_series > 0].sum())
        loss_amt   = int(pnl_amt_series[pnl_amt_series < 0].sum())
        net_amt    = int(pnl_amt_series.sum())

        if buy_cnt > 0:
            pnl_series = buys["최종손익률(%)"]
            avg_pnl    = round(float(pnl_series.mean()), 2)
            win_rate   = round(float((pnl_series > 0).mean() * 100), 1)
            best_pnl   = round(float(pnl_series.max()), 2)
            worst_pnl  = round(float(pnl_series.min()), 2)
            note       = ""
        else:
            avg_pnl = win_rate = best_pnl = worst_pnl = 0.0
            if not note:
                note = "매매조건 없음"

        rows.append({
            "종목코드"      : code,
            "종목명"        : name,
            "신호 수"       : signals,
            "매수 발생"     : buy_cnt,
            "평균 손익률(%)": avg_pnl,
            "승률(%)"       : win_rate,
            "수익금액"      : profit_amt,
            "손실금액"      : loss_amt,
            "순손익금액"    : net_amt,
            "최고 수익(%)": best_pnl,
            "최저 수익(%)": worst_pnl,
            "비고"          : note,
        })

    summary = pd.DataFrame(rows)
    return summary.sort_values(
        ["순손익금액", "매수 발생"], ascending=[False, False]
    ).reset_index(drop=True)


def _add_trading_days(date_str: str, n: int, calendar: list[str]) -> str:
    """거래일 기준 n일 후 날짜 (calendar: 정렬된 YYYYMMDD 목록)"""
    if date_str not in calendar:
        # 가장 가까운 다음 거래일
        idx = next((i for i, d in enumerate(calendar) if d >= date_str), len(calendar) - 1)
    else:
        idx = calendar.index(date_str)
    target = min(idx + n, len(calendar) - 1)
    return calendar[target]


def analyze_portfolio_capital(df: pd.DataFrame,
                              settlement_days: int = SETTLEMENT_TRADING_DAYS) -> dict:
    """
    포트폴리오 최대 동시 투입자금(피크) 분석
    - 단일 계좌 자금 풀, 매도 대금 T+N 거래일 후 재매수 가능
    - 동일 종목 2포지션 불가 (종목별 시뮬이 비중복 전제)
    """
    empty = {
        "peak_tied"            : 0,
        "peak_date"            : "",
        "peak_overlap_count"   : 0,
        "peak_overlap_codes"   : [],
        "recommended_account"  : 0,
    }
    if df.empty:
        return empty

    trades = df[df["1차매수체결"] == "O"].copy()
    if trades.empty:
        return empty

    # ── 이벤트 수집 ───────────────────────────────────────────
    raw_events: list[dict] = []
    for _, row in trades.iterrows():
        code = str(row["종목코드"])
        key  = f"{code}_{row['기준캔들발생일']}"
        try:
            flows = json.loads(row.get("flow_events", "[]") or "[]")
        except json.JSONDecodeError:
            flows = []
        for ev in flows:
            raw_events.append({
                "date"       : str(ev["date"]),
                "trade_key"  : key,
                "code"       : code,
                "tied_delta" : int(ev.get("tied_delta", 0)),
                "proceeds"   : int(ev.get("proceeds", 0)),
            })

    if not raw_events:
        return empty

    calendar = sorted({e["date"] for e in raw_events})

    # ── 동일 종목 포지션 중복 방지 검증 ───────────────────────
    by_code: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for _, row in trades.iterrows():
        code = str(row["종목코드"])
        flows = json.loads(row.get("flow_events", "[]") or "[]")
        if not flows:
            continue
        dates = [f["date"] for f in flows]
        by_code[code].append((min(dates), max(dates)))
    for code, ranges in by_code.items():
        ranges.sort()
        for i in range(1, len(ranges)):
            if ranges[i][0] <= ranges[i - 1][1]:
                logger.warning("동일 종목 포지션 겹침 무시: %s", code)

    # ── 피크 동시 투입자금 ────────────────────────────────────
    by_date: dict[str, list[dict]] = defaultdict(list)
    for ev in raw_events:
        by_date[ev["date"]].append(ev)

    positions: dict[str, dict] = {}   # trade_key -> {tied, code}
    peak_tied = 0
    peak_date = ""
    peak_codes: list[str] = []

    for dt in calendar:
        for ev in by_date.get(dt, []):
            key = ev["trade_key"]
            if key not in positions:
                positions[key] = {"tied": 0, "code": ev["code"]}
            positions[key]["tied"] += ev["tied_delta"]
            if positions[key]["tied"] <= 0:
                del positions[key]

        tied = sum(p["tied"] for p in positions.values())
        if tied > peak_tied:
            peak_tied = tied
            peak_date = dt
            peak_codes = sorted({p["code"] for p in positions.values()})

    # ── T+3 결제 대기 반영 권장 계좌자금 ──────────────────────
    cash = 0
    pending: dict[str, int] = defaultdict(int)
    positions3: dict[str, int] = {}
    wallet_injected = 0
    peak_account = 0

    for dt in calendar:
        cash += pending.pop(dt, 0)
        for ev in by_date.get(dt, []):
            key = ev["trade_key"]
            if ev["tied_delta"] > 0:
                need = ev["tied_delta"]
                if cash < need:
                    wallet_injected += need - cash
                    cash = 0
                else:
                    cash -= need
                positions3[key] = positions3.get(key, 0) + ev["tied_delta"]
            elif ev["tied_delta"] < 0:
                positions3[key] = positions3.get(key, 0) + ev["tied_delta"]
                if positions3.get(key, 0) <= 0:
                    positions3.pop(key, None)
            if ev["proceeds"] > 0:
                avail = _add_trading_days(dt, settlement_days, calendar)
                pending[avail] += ev["proceeds"]

        tied_now = sum(positions3.values())
        peak_account = max(peak_account, wallet_injected + tied_now)

    return {
        "peak_tied"           : int(peak_tied),
        "peak_date"           : peak_date,
        "peak_overlap_count"  : len(peak_codes),
        "peak_overlap_codes"  : peak_codes,
        "recommended_account" : int(peak_account),
    }


def _empty_record(code: str, name: str, note: str) -> dict:
    return {
        "종목코드": code, "종목명": name,
        "저점캔들발생일": "", "기준캔들발생일": "",
        "거래량케이스": "", "1차매수가": 0, "2차매수가": 0, "3차매수가": 0,
        "1차매수체결": "-", "2차매수체결": "-", "3차매수체결": "-",
        "손절발동": "-", "손절가": 0,
        "1차익절발동": "-", "1차익절가": 0,
        "2차익절발동": "-", "2차익절가": 0,
        "트레일링스탑발동": "-",
        "최종손익률(%)": 0.0, "투자금액": 0, "손익금액": 0, "240선상승일": 0,
        "비고": note,
    }
