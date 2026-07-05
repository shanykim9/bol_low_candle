"""
strategy.py - 볼린저밴드 + 저점캔들 돌파 양봉 전략 로직
- 지표 계산 (SMA, 볼린저밴드)
- 저점캔들 탐색
- 기준캔들 판단
- 매수가 계산
- 손절/익절/매수포기 신호 생성
"""

import math
import logging
from typing import Optional

import pandas as pd
import numpy as np

from config import resolve_scan_days

logger = logging.getLogger(__name__)


# ── 지표 계산 ─────────────────────────────────────────────────
def calc_indicators(candles: list[dict], ma_period: int = 240,
                    bb_period: int = 20, bb_mult: float = 2.0) -> pd.DataFrame:
    """
    일봉 리스트를 DataFrame으로 변환하고 지표 계산
    candles: 최신→과거 순 (API 응답 그대로)
    반환 df: 과거→최신 순으로 정렬됨
    """
    if not candles:
        return pd.DataFrame()

    df = pd.DataFrame(candles)
    df = df.sort_values("date").reset_index(drop=True)

    # 이동평균선
    df["ma"] = df["close"].rolling(window=ma_period, min_periods=ma_period).mean()

    # 볼린저밴드 (영웅문4: SMA ± D1 × 모집단표준편차, ddof=0)
    df["bb_mid"]   = df["close"].rolling(window=bb_period, min_periods=bb_period).mean()
    df["bb_std"]   = df["close"].rolling(window=bb_period, min_periods=bb_period).std(ddof=0)
    df["bb_upper"] = df["bb_mid"] + bb_mult * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - bb_mult * df["bb_std"]

    return df


# ── 240일선 추세 ──────────────────────────────────────────────
def count_ma_rising_days(df: pd.DataFrame, idx: int) -> int:
    """idx 기준 MA 연속 상승 일수 (당일 포함)"""
    streak = 0
    i = idx
    while i > 0:
        cur  = df.loc[i, "ma"]
        prev = df.loc[i - 1, "ma"]
        if pd.isna(cur) or pd.isna(prev):
            break
        if cur > prev:
            streak += 1
            i -= 1
        else:
            break
    return streak


def _is_pivot_row(df: pd.DataFrame, idx: int, params: Optional[dict] = None) -> bool:
    """
    저점캔들 행 조건:
      공통) 저가 >= MA AND 저가 <= BB하단
      240선추세) MA 연속 상승 N일 이상
    """
    row = df.loc[idx]
    if pd.isna(row["ma"]) or pd.isna(row["bb_lower"]):
        return False
    if row["low"] < row["ma"] or row["low"] > row["bb_lower"]:
        return False
    if params and params.get("ma_filter_mode") == "240선추세":
        required = int(params.get("ma_trend_days", 3))
        if count_ma_rising_days(df, idx) < required:
            return False
    return True


def _pivot_dict_from_row(df: pd.DataFrame, idx: int) -> dict:
    row = df.loc[idx]
    return {
        "date"          : row["date"],
        "open"          : int(row["open"]),
        "high"          : int(row["high"]),
        "low"           : int(row["low"]),
        "close"         : int(row["close"]),
        "volume"        : int(row["volume"]),
        "idx"           : idx,
        "ma_rising_days": count_ma_rising_days(df, idx),
    }


# ── 저점캔들 탐색 ─────────────────────────────────────────────
def find_pivot_candle(df: pd.DataFrame, scan_days: int = 126,
                      params: Optional[dict] = None) -> Optional[dict]:
    """
    최근 scan_days 거래일 이내에서 저점캔들 탐색
    조건:
      A) 저가 >= 240일 이동평균
      B) 저가 <= 볼린저밴드 하한선
      C) (240선추세 모드) MA 연속 상승 N일 이상
    """
    if df.empty or len(df) < 2:
        return None

    search_df = df.iloc[-(scan_days + 1):-1].copy()

    for idx in reversed(search_df.index.tolist()):
        if _is_pivot_row(df, idx, params):
            return _pivot_dict_from_row(df, idx)
    return None


# ── 기준캔들 판단 ─────────────────────────────────────────────
def check_base_candle(df: pd.DataFrame, pivot: dict) -> Optional[dict]:
    """
    저점캔들 바로 다음 거래일의 캔들이 기준캔들 조건을 만족하는지 확인
    조건:
      A) 종가 > 시가 (양봉)
      B) 종가 >= 저점캔들의 고가
    """
    pivot_idx = pivot["idx"]
    next_idx  = pivot_idx + 1
    if next_idx not in df.index:
        return None
    if next_idx != df.index[-1]:
        # 기준캔들은 가장 최근(마지막) 일봉이어야 함
        return None

    row = df.loc[next_idx]
    is_bullish = row["close"] > row["open"]
    meets_high = row["close"] >= pivot["high"]

    if is_bullish and meets_high:
        return {
            "date"  : row["date"],
            "open"  : int(row["open"]),
            "high"  : int(row["high"]),
            "low"   : int(row["low"]),
            "close" : int(row["close"]),
            "volume": int(row["volume"]),
            "idx"   : next_idx,
        }
    return None


# ── 매수가 계산 ───────────────────────────────────────────────
def calc_buy_prices(pivot: dict, base: dict,
                    volume_ratio: float = 3.0) -> dict:
    """
    기준캔들과 저점캔들 거래량 비교 후 매수가 산출
    반환: {"case": "A"|"B", "buy1": int, "buy2": int, "buy3": int}
    """
    body_size = base["close"] - base["open"]   # 몸통크기
    gap = body_size / 4                        # 1단위

    is_case_a = base["volume"] > pivot["volume"] * volume_ratio

    if is_case_a:
        buy1 = base["open"] + gap * 3
        buy2 = base["open"] + gap * 2
        buy3 = base["open"] + gap * 1
        case = "A"
    else:
        buy1 = base["open"] + gap * 2
        buy2 = base["open"] + gap * 1
        buy3 = base["open"]
        case = "B"

    return {
        "case": case,
        "buy1": int(round(buy1)),
        "buy2": int(round(buy2)),
        "buy3": int(round(buy3)),
    }


# ── 매수 수량 계산 ────────────────────────────────────────────
def calc_qty(budget: int, price: int) -> int:
    """투자금액 / 매수가 → 수량 (floor, 0이면 스킵)"""
    if price <= 0:
        return 0
    return math.floor(budget / price)


# ── 손절기준가 계산 ───────────────────────────────────────────
def calc_stop_price(pivot: dict, base: dict,
                    stop_mode: str, stop_pct: int,
                    buy1_price: int) -> int:
    """
    stop_mode: "기준저가" | "1차매수가대비"
    stop_pct: 1차 매수가 대비 하락 % (옵션 모드)
    """
    if stop_mode == "기준저가":
        return min(pivot["low"], base["low"])
    else:
        return int(buy1_price * (1 - stop_pct / 100))


# ── 장중 신호 판단 ────────────────────────────────────────────
class SignalChecker:
    """
    장중 현재가를 받아 매수/손절/익절 신호를 판단하는 클래스.
    state_manager의 종목 상태 dict를 직접 수정하지 않고 signal 반환.
    """

    @staticmethod
    def check_abandon(current_price: int, pivot_low: int,
                      abandon_pct: float, bought_qty: int) -> bool:
        """매수포기: WAIT_BUY 상태에서 저점캔들 저가 대비 30% 초과 상승"""
        threshold = pivot_low * (1 + abandon_pct / 100)
        return current_price > threshold and bought_qty == 0

    @staticmethod
    def check_buy_trigger(current_price: int, buy_price: int,
                          already_bought: bool) -> bool:
        """n차 매수 발동: 현재가 <= n차 매수가이고 아직 미체결"""
        return (not already_bought) and (current_price <= buy_price)

    @staticmethod
    def check_stop(current_price: int, stop_price: int) -> bool:
        """손절: 현재가 < 손절기준가"""
        return current_price < stop_price

    @staticmethod
    def check_profit1(current_price: int, avg_price: int,
                      profit1_pct: float, profit1_done: bool) -> bool:
        """1차 익절: 현재가 >= 평균단가 × (1 + profit1_pct/100)"""
        if profit1_done:
            return False
        return current_price >= avg_price * (1 + profit1_pct / 100)

    @staticmethod
    def check_profit2(current_price: int, avg_price: int,
                      profit2_pct: float, profit1_done: bool,
                      profit2_done: bool) -> bool:
        """2차 익절: TAKING_PROFIT 상태에서 조건 충족"""
        if not profit1_done or profit2_done:
            return False
        return current_price >= avg_price * (1 + profit2_pct / 100)

    @staticmethod
    def check_trailing_stop(current_price: int, last_profit_price: int,
                             trailing_pct: float) -> bool:
        """트레일링 스탑: 가장 최근 익절가 대비 trailing_pct% 이상 하락"""
        if last_profit_price <= 0:
            return False
        threshold = last_profit_price * (1 - trailing_pct / 100)
        return current_price < threshold


# ── 시뮬레이션용 단일 종목 전략 실행 ─────────────────────────
def simulate_strategy(candles: list[dict], params: dict) -> list[dict]:
    """
    과거 일봉 데이터 전체를 순회하며 매매 시그널 시뮬레이션
    반환: 각 트레이드 결과 리스트
    """
    results = []
    df = calc_indicators(
        candles,
        ma_period = params["ma_period"],
        bb_period = params["bb_period"],
        bb_mult   = params["bb_multiplier"],
    )
    if df.empty or len(df) < params["ma_period"] + 10:
        return results

    budgets    = {
        "buy1": int(params["total_budget"] / 7 * 1),
        "buy2": int(params["total_budget"] / 7 * 2),
        "buy3": int(params["total_budget"] / 7 * 4),
    }
    scan_days = resolve_scan_days(params)
    ma_period = params["ma_period"]
    bb_period = params["bb_period"]

    # 지표 계산 가능 시점부터 시뮬레이션 (scan_days는 탐색 범위이지 시작 지연 아님)
    min_idx = max(ma_period, bb_period) + 1

    i = min_idx
    while i < len(df):
        window_df = df.iloc[:i + 1].copy().reset_index(drop=True)
        pivot = _find_pivot_in_window(window_df, scan_days, params)
        if pivot is None:
            i += 1
            continue

        base = _check_base_in_window(window_df, pivot)
        if base is None:
            i += 1
            continue

        # 기준캔들 확정 → 매수가 계산
        prices = calc_buy_prices(pivot, base, params["volume_ratio"])
        stop_p = calc_stop_price(
            pivot, base,
            params["stop_mode"], params["stop_pct"],
            prices["buy1"]
        )
        abandon_threshold = pivot["low"] * (1 + params["abandon_pct"] / 100)

        # 기준캔들 다음날부터 매매 시뮬 (원본 df 인덱스 기준)
        trade = _run_trade_simulation(
            df, i + 1, prices, stop_p,
            abandon_threshold, budgets, params
        )
        trade.update({
            "pivot_date"    : pivot["date"],
            "base_date"     : base["date"],
            "case"          : prices["case"],
            "buy1_price"    : prices["buy1"],
            "buy2_price"    : prices["buy2"],
            "buy3_price"    : prices["buy3"],
            "stop_price"    : stop_p,
            "ma_rising_days": pivot.get("ma_rising_days", 0),
        })
        results.append(trade)

        # 트레이드 종료 이후 인덱스로 이동 (겹치는 신호 방지)
        next_i = trade.get("end_idx", i) + 1
        i = max(i + 1, next_i)

    return results


def _find_pivot_in_window(df: pd.DataFrame, scan_days: int,
                          params: Optional[dict] = None) -> Optional[dict]:
    if len(df) < 2:
        return None
    search = df.iloc[-(scan_days + 1):-1]
    for idx in reversed(search.index.tolist()):
        if _is_pivot_row(df, idx, params):
            return _pivot_dict_from_row(df, idx)
    return None


def _check_base_in_window(df: pd.DataFrame, pivot: dict) -> Optional[dict]:
    next_idx = pivot["idx"] + 1
    if next_idx not in df.index or next_idx != df.index[-1]:
        return None
    row = df.loc[next_idx]
    if row["close"] > row["open"] and row["close"] >= pivot["high"]:
        return {"date": row["date"], "open": int(row["open"]),
                "high": int(row["high"]), "low": int(row["low"]),
                "close": int(row["close"]), "volume": int(row["volume"]),
                "idx": next_idx}
    return None


def _run_trade_simulation(df: pd.DataFrame, start_idx: int,
                          prices: dict, stop_p: int,
                          abandon_threshold: float,
                          budgets: dict, params: dict) -> dict:
    """기준캔들 확정 후 미래 일봉 순회하며 매매 시뮬레이션"""
    result = {
        "buy1_filled": False, "buy2_filled": False, "buy3_filled": False,
        "buy1_qty": 0, "buy2_qty": 0, "buy3_qty": 0,
        "stop_triggered": False, "stop_price_actual": 0,
        "profit1_triggered": False, "profit1_price": 0,
        "profit2_triggered": False, "profit2_price": 0,
        "trailing_triggered": False, "trailing_price": 0,
        "final_pnl_pct": 0.0, "end_idx": start_idx,
        "invest_amount": 0, "pnl_amount": 0,
        "flow_events": [],
    }

    bought_qty   = 0
    total_cost   = 0
    profit1_done = False
    profit2_done = False
    last_profit_price = 0
    profit1_qty  = 0
    profit2_qty  = 0

    checker = SignalChecker()

    def _flow(date_val, tied_delta: int, proceeds: int = 0):
        if tied_delta == 0 and proceeds == 0:
            return
        result["flow_events"].append({
            "date"       : str(date_val),
            "tied_delta" : int(tied_delta),
            "proceeds"   : int(proceeds),
        })

    for idx in range(start_idx, min(start_idx + 60, len(df))):
        row      = df.iloc[idx]
        dt       = row["date"]
        low_p    = int(row["low"])
        high_p   = int(row["high"])
        close_p  = int(row["close"])

        # ── 매수포기 체크 (아직 매수 없음) ──────────────────
        if bought_qty == 0:
            if close_p > abandon_threshold:
                result["end_idx"] = idx
                break

        # ── 매수 체크 ───────────────────────────────────────
        for nth, key in enumerate(["buy1", "buy2", "buy3"], 1):
            if result[f"buy{nth}_filled"]:
                continue
            bp = prices[key]
            if low_p <= bp:
                qty = calc_qty(budgets[f"buy{nth}"], bp)
                if qty > 0:
                    amt = qty * bp
                    result[f"buy{nth}_filled"] = True
                    result[f"buy{nth}_qty"]    = qty
                    bought_qty += qty
                    total_cost += amt
                    _flow(dt, +amt, 0)

        avg_price = int(total_cost / bought_qty) if bought_qty > 0 else 0

        # ── 손절 ────────────────────────────────────────────
        if bought_qty > 0 and not profit1_done:
            if checker.check_stop(low_p, stop_p):
                proceeds = bought_qty * stop_p
                pnl = (stop_p - avg_price) / avg_price * 100
                pnl_amt = int(round(bought_qty * (stop_p - avg_price)))
                _flow(dt, -total_cost, proceeds)
                result.update({
                    "stop_triggered"    : True,
                    "stop_price_actual" : stop_p,
                    "final_pnl_pct"     : round(pnl, 2),
                    "invest_amount"     : int(total_cost),
                    "pnl_amount"        : pnl_amt,
                    "end_idx"           : idx,
                })
                return result

        # ── 1차 익절 ─────────────────────────────────────────
        if bought_qty > 0 and not profit1_done and avg_price > 0:
            p1_threshold = int(avg_price * (1 + params["profit1_pct"] / 100))
            if high_p >= p1_threshold:
                profit1_done  = True
                last_profit_price = p1_threshold
                profit1_qty   = bought_qty // 2
                cost_rel = profit1_qty * avg_price
                _flow(dt, -cost_rel, profit1_qty * p1_threshold)
                result.update({
                    "profit1_triggered": True,
                    "profit1_price"    : p1_threshold,
                })

        # ── 2차 익절 ─────────────────────────────────────────
        if profit1_done and not profit2_done and avg_price > 0:
            p2_threshold = int(avg_price * (1 + params["profit2_pct"] / 100))
            if high_p >= p2_threshold:
                profit2_done  = True
                last_profit_price = p2_threshold
                profit2_qty   = (bought_qty - profit1_qty) // 2
                cost_rel = profit2_qty * avg_price
                _flow(dt, -cost_rel, profit2_qty * p2_threshold)
                result.update({
                    "profit2_triggered": True,
                    "profit2_price"    : p2_threshold,
                })

        # ── 트레일링 스탑 ─────────────────────────────────────
        if profit1_done and last_profit_price > 0:
            trailing_threshold = int(last_profit_price * (1 - params["trailing_pct"] / 100))
            if low_p < trailing_threshold:
                remaining_qty = bought_qty - profit1_qty - profit2_qty
                cost_rel = remaining_qty * avg_price
                proceeds = remaining_qty * trailing_threshold
                pnl = _calc_sim_pnl(
                    avg_price, profit1_qty, result["profit1_price"],
                    profit2_qty, result.get("profit2_price", 0),
                    remaining_qty, trailing_threshold, total_cost
                )
                pnl_amt = _calc_sim_pnl_amount(
                    profit1_qty, result["profit1_price"],
                    profit2_qty, result.get("profit2_price", 0),
                    remaining_qty, trailing_threshold, total_cost
                )
                _flow(dt, -cost_rel, proceeds)
                result.update({
                    "trailing_triggered": True,
                    "trailing_price"    : trailing_threshold,
                    "final_pnl_pct"     : round(pnl, 2),
                    "invest_amount"     : int(total_cost),
                    "pnl_amount"        : pnl_amt,
                    "end_idx"           : idx,
                })
                return result

    # 마지막 종가로 정리
    if bought_qty > 0:
        avg_price = int(total_cost / bought_qty)
        end_idx = min(result.get("end_idx", start_idx), len(df) - 1)
        if end_idx < start_idx:
            end_idx = min(start_idx + 59, len(df) - 1)
        result["end_idx"] = end_idx
        last_row   = df.iloc[end_idx]
        last_price = int(last_row["close"])
        last_dt    = last_row["date"]
        remaining  = bought_qty - profit1_qty - profit2_qty
        if remaining > 0:
            cost_rel = remaining * avg_price
            _flow(last_dt, -cost_rel, remaining * last_price)
        pnl = _calc_sim_pnl(
            avg_price, profit1_qty, result.get("profit1_price", 0),
            profit2_qty, result.get("profit2_price", 0),
            remaining, last_price, total_cost
        )
        result["final_pnl_pct"] = round(pnl, 2)
        result["invest_amount"] = int(total_cost)
        result["pnl_amount"]    = _calc_sim_pnl_amount(
            profit1_qty, result.get("profit1_price", 0),
            profit2_qty, result.get("profit2_price", 0),
            remaining, last_price, total_cost
        )
    else:
        result["end_idx"] = min(start_idx + 59, len(df) - 1)

    return result


def _calc_sim_pnl(avg_price, q1, p1, q2, p2, qr, pr, total_cost) -> float:
    if total_cost <= 0:
        return 0.0
    revenue = q1 * p1 + q2 * p2 + qr * pr
    cost    = total_cost
    return (revenue - cost) / cost * 100


def _calc_sim_pnl_amount(q1, p1, q2, p2, qr, pr, total_cost) -> int:
    """손익 금액(원) = 매도수익 합 - 투자원금"""
    if total_cost <= 0:
        return 0
    revenue = q1 * p1 + q2 * p2 + qr * pr
    return int(round(revenue - total_cost))
