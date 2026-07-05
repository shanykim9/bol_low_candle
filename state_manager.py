"""
state_manager.py - 종목별 매매 상태 머신 관리
상태: IDLE → WAIT_BUY → PARTIALLY_BOUGHT → FULLY_BOUGHT → TAKING_PROFIT → IDLE
JSON 파일에 실시간 저장하여 재시작 시 복원
"""

import json
import logging
import threading
from pathlib import Path
from typing import Optional
from copy import deepcopy

logger = logging.getLogger(__name__)

STATE_FILE = Path("trade_state.json")

# ── 상태 상수 ────────────────────────────────────────────────
IDLE             = "IDLE"
WAIT_BUY         = "WAIT_BUY"
PARTIALLY_BOUGHT = "PARTIALLY_BOUGHT"
FULLY_BOUGHT     = "FULLY_BOUGHT"
TAKING_PROFIT    = "TAKING_PROFIT"

ALL_STATES = [IDLE, WAIT_BUY, PARTIALLY_BOUGHT, FULLY_BOUGHT, TAKING_PROFIT]


def _default_ticker_state(code: str, name: str = "") -> dict:
    return {
        "code"              : code,
        "name"              : name,
        "state"             : IDLE,
        "current_price"     : 0,

        # 캔들 정보
        "pivot"             : None,   # 저점캔들
        "base"              : None,   # 기준캔들

        # 매수가
        "buy_case"          : "",
        "buy1_price"        : 0,
        "buy2_price"        : 0,
        "buy3_price"        : 0,
        "buy1_qty"          : 0,
        "buy2_qty"          : 0,
        "buy3_qty"          : 0,
        "buy1_filled"       : False,
        "buy2_filled"       : False,
        "buy3_filled"       : False,

        # 포지션
        "total_qty"         : 0,
        "total_cost"        : 0,
        "avg_price"         : 0,

        # 손절
        "stop_price"        : 0,

        # 익절
        "profit1_pct"       : 20,
        "profit2_pct"       : 25,
        "profit1_done"      : False,
        "profit2_done"      : False,
        "profit1_price"     : 0,
        "profit2_price"     : 0,
        "last_profit_price" : 0,
        "profit1_qty"       : 0,
        "profit2_qty"       : 0,

        # 매수포기 기준
        "abandon_threshold" : 0,

        # 손익
        "realized_pnl"      : 0,
    }


class StateManager:
    """전체 종목 상태를 메모리에서 관리하고 JSON 파일에 동기화"""

    def __init__(self):
        self._states: dict[str, dict] = {}
        self._lock   = threading.Lock()
        self._load()

    # ── 파일 I/O ─────────────────────────────────────────────
    def _load(self):
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    self._states = json.load(f)
                logger.info("상태 파일 로드 완료: %d 종목", len(self._states))
            except Exception as e:
                logger.warning("상태 파일 로드 실패 (초기화): %s", e)
                self._states = {}

    def _save(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._states, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("상태 파일 저장 실패: %s", e)

    # ── 종목 등록/제거 ────────────────────────────────────────
    def register(self, code: str, name: str = ""):
        with self._lock:
            if code not in self._states:
                self._states[code] = _default_ticker_state(code, name)
                self._save()
            elif name and not self._states[code].get("name"):
                self._states[code]["name"] = name
                self._save()

    def unregister(self, code: str):
        with self._lock:
            if code in self._states:
                del self._states[code]
                self._save()

    def set_tickers(self, ticker_map: dict[str, str]):
        """ticker_map: {code: name} 형태. 기존 종목 중 없는 것은 제거"""
        with self._lock:
            # 신규 추가
            for code, name in ticker_map.items():
                if code not in self._states:
                    self._states[code] = _default_ticker_state(code, name)
                else:
                    self._states[code]["name"] = name
            # 제거 (IDLE 상태만)
            codes_to_remove = [
                c for c in self._states
                if c not in ticker_map and self._states[c]["state"] == IDLE
            ]
            for c in codes_to_remove:
                del self._states[c]
            self._save()

    # ── 상태 조회 ─────────────────────────────────────────────
    def get(self, code: str) -> Optional[dict]:
        with self._lock:
            return deepcopy(self._states.get(code))

    def get_all(self) -> dict[str, dict]:
        with self._lock:
            return deepcopy(self._states)

    def get_state(self, code: str) -> str:
        with self._lock:
            return self._states.get(code, {}).get("state", IDLE)

    # ── 상태 전이 메서드 ─────────────────────────────────────
    def to_wait_buy(self, code: str, pivot: dict, base: dict,
                    prices: dict, stop_price: int,
                    abandon_threshold: float,
                    profit1_pct: int, profit2_pct: int,
                    budgets: dict):
        from strategy import calc_qty
        with self._lock:
            s = self._states.get(code)
            if not s:
                return
            if s["state"] != IDLE:
                return  # 중복 방지

            s["state"]              = WAIT_BUY
            s["pivot"]              = pivot
            s["base"]               = base
            s["buy_case"]           = prices["case"]
            s["buy1_price"]         = prices["buy1"]
            s["buy2_price"]         = prices["buy2"]
            s["buy3_price"]         = prices["buy3"]
            s["buy1_qty"]           = calc_qty(budgets["buy1"], prices["buy1"])
            s["buy2_qty"]           = calc_qty(budgets["buy2"], prices["buy2"])
            s["buy3_qty"]           = calc_qty(budgets["buy3"], prices["buy3"])
            s["buy1_filled"]        = False
            s["buy2_filled"]        = False
            s["buy3_filled"]        = False
            s["total_qty"]          = 0
            s["total_cost"]         = 0
            s["avg_price"]          = 0
            s["stop_price"]         = stop_price
            s["profit1_pct"]        = profit1_pct
            s["profit2_pct"]        = profit2_pct
            s["profit1_done"]       = False
            s["profit2_done"]       = False
            s["profit1_price"]      = 0
            s["profit2_price"]      = 0
            s["last_profit_price"]  = 0
            s["profit1_qty"]        = 0
            s["profit2_qty"]        = 0
            s["abandon_threshold"]  = int(abandon_threshold)
            s["realized_pnl"]       = 0
            self._save()

    def record_buy(self, code: str, nth: int, qty: int, price: int):
        """n차 매수 체결 기록"""
        with self._lock:
            s = self._states.get(code)
            if not s:
                return
            s[f"buy{nth}_filled"] = True
            s["total_qty"]   += qty
            s["total_cost"]  += qty * price
            s["avg_price"]   = int(s["total_cost"] / s["total_qty"]) if s["total_qty"] > 0 else 0

            filled_count = sum(1 for n in [1, 2, 3] if s[f"buy{n}_filled"])
            if filled_count == 3:
                s["state"] = FULLY_BOUGHT
            elif filled_count >= 1:
                s["state"] = PARTIALLY_BOUGHT
            self._save()

    def record_profit1(self, code: str, price: int, qty: int):
        with self._lock:
            s = self._states.get(code)
            if not s:
                return
            s["profit1_done"]      = True
            s["profit1_price"]     = price
            s["profit1_qty"]       = qty
            s["last_profit_price"] = price
            s["realized_pnl"]     += qty * (price - s["avg_price"])
            s["state"]             = TAKING_PROFIT
            self._save()

    def record_profit2(self, code: str, price: int, qty: int):
        with self._lock:
            s = self._states.get(code)
            if not s:
                return
            s["profit2_done"]      = True
            s["profit2_price"]     = price
            s["profit2_qty"]       = qty
            s["last_profit_price"] = price
            s["realized_pnl"]     += qty * (price - s["avg_price"])
            self._save()

    def to_idle(self, code: str, reason: str = ""):
        with self._lock:
            s = self._states.get(code)
            if not s:
                return
            logger.info("[%s] IDLE 복귀: %s", code, reason)
            self._states[code] = _default_ticker_state(code, s.get("name", ""))
            self._save()

    def update_price(self, code: str, price: int):
        with self._lock:
            if code in self._states:
                self._states[code]["current_price"] = price
                # 평가손익은 표시 시 계산하므로 저장 생략 (빈번한 I/O 방지)

    # ── 편의 속성 계산 ────────────────────────────────────────
    @staticmethod
    def eval_pnl(s: dict) -> tuple[int, float]:
        """평가손익 (금액, %) 반환"""
        if s["avg_price"] <= 0 or s["total_qty"] <= 0:
            return 0, 0.0
        remaining = s["total_qty"] - s["profit1_qty"] - s["profit2_qty"]
        if remaining <= 0:
            return int(s["realized_pnl"]), 0.0
        unrealized = remaining * (s["current_price"] - s["avg_price"])
        total_pnl  = int(s["realized_pnl"]) + unrealized
        pnl_pct    = total_pnl / s["total_cost"] * 100 if s["total_cost"] > 0 else 0.0
        return int(total_pnl), round(pnl_pct, 2)
