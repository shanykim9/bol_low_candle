"""
kiwoom_api.py - 키움증권 REST API 래퍼 클래스
- 토큰 자동 발급/갱신
- 일봉 데이터 조회
- 현재가 조회
- 매수/매도/취소 주문
- 잔고 조회
- Rate Limiter (초당 5회)
"""

import io
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import requests

import config

logger = logging.getLogger(__name__)

CONTENT_TYPE = "application/json;charset=UTF-8"


# ── Rate Limiter ──────────────────────────────────────────────
class RateLimiter:
    """초당 최대 max_calls 회 호출 제한"""
    def __init__(self, max_calls: int = 5, period: float = 1.0):
        self.max_calls = max_calls
        self.period    = period
        self.calls: list[float] = []
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.time()
            self.calls = [t for t in self.calls if now - t < self.period]
            if len(self.calls) >= self.max_calls:
                sleep_time = self.period - (now - self.calls[0])
                if sleep_time > 0:
                    time.sleep(sleep_time)
            self.calls.append(time.time())


def _parse_int(value) -> int:
    """키움 API 가격/수량 문자열 파싱 (+/- 부호 제거)"""
    if value is None:
        return 0
    s = str(value).strip().replace(",", "")
    if not s or s in ("-", ""):
        return 0
    if s[0] in "+-":
        s = s[1:]
    try:
        return int(float(s))
    except ValueError:
        return 0


# ── KiwoomAPI ────────────────────────────────────────────────
class KiwoomAPI:
    def __init__(self):
        self.app_key    = config.APP_KEY
        self.app_secret = config.APP_SECRET
        # 하이픈 포함 형식(6220-0130-10)도 처리
        self.account_no = config.ACCOUNT_NO.replace("-", "").strip()
        self.base_url   = config.BASE_URL
        self.endpoints  = config.ENDPOINTS
        self.tr_ids     = config.TR_IDS

        self._token: Optional[str] = None
        self._token_expire: Optional[datetime] = None
        self._rate_limiter = RateLimiter(max_calls=5, period=1.0)
        self._lock = threading.Lock()
        self._name_code_map: Optional[dict[str, str]] = None

    # ── 토큰 관리 ─────────────────────────────────────────────
    def _is_token_valid(self) -> bool:
        if not self._token or not self._token_expire:
            return False
        return datetime.now() < self._token_expire - timedelta(minutes=5)

    def get_token(self) -> str:
        with self._lock:
            if self._is_token_valid():
                return self._token
            return self._issue_token()

    def _issue_token(self) -> str:
        url  = self.base_url + self.endpoints["token"]
        body = {
            "grant_type": "client_credentials",
            "appkey"    : self.app_key,
            "secretkey" : self.app_secret,
        }
        headers = {"Content-Type": CONTENT_TYPE}
        self._rate_limiter.wait()
        resp = requests.post(url, json=body, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if str(data.get("return_code", "0")) != "0":
            raise RuntimeError(f"토큰 발급 실패: {data}")

        token = data.get("token") or data.get("access_token")
        if not token:
            raise RuntimeError(f"토큰 발급 실패: {data}")

        self._token = token
        expires_dt = data.get("expires_dt")
        if expires_dt:
            self._token_expire = datetime.strptime(str(expires_dt), "%Y%m%d%H%M%S")
        else:
            expires_in = int(data.get("expires_in", 21600))
            self._token_expire = datetime.now() + timedelta(seconds=expires_in)

        logger.info("키움 토큰 발급 완료 (만료: %s)", self._token_expire.strftime("%H:%M:%S"))
        return self._token

    # ── HTTP 공통 요청 ────────────────────────────────────────
    def _headers(self, api_id: str = "", cont_yn: str = "", next_key: str = "") -> dict:
        headers = {
            "Content-Type" : CONTENT_TYPE,
            "authorization": f"Bearer {self.get_token()}",
        }
        if api_id:
            headers["api-id"] = api_id
        if cont_yn:
            headers["cont-yn"] = cont_yn
        if next_key:
            headers["next-key"] = next_key
        return headers

    def _api_post(self, api_id: str, path: str, body: dict,
                  cont_yn: str = "", next_key: str = "", retry: int = 3) -> dict:
        data, _ = self._api_post_with_headers(api_id, path, body, cont_yn, next_key, retry)
        return data

    def _api_post_with_headers(self, api_id: str, path: str, body: dict,
                               cont_yn: str = "", next_key: str = "",
                               retry: int = 3) -> tuple[dict, dict]:
        url = self.base_url + path
        for attempt in range(retry):
            try:
                self._rate_limiter.wait()
                resp = requests.post(
                    url, json=body,
                    headers=self._headers(api_id, cont_yn, next_key),
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json() if resp.text else {}

                if str(data.get("return_code", "0")) != "0":
                    msg = data.get("return_msg", "Unknown error")
                    if "인증" in str(msg) or str(data.get("return_code")) == "3":
                        with self._lock:
                            self._token = None
                            self._token_expire = None
                        if attempt < retry - 1:
                            continue
                    raise RuntimeError(f"[{api_id}] {msg}")

                return data, dict(resp.headers)
            except requests.RequestException as e:
                logger.warning("POST 실패 (%d/%d) [%s]: %s", attempt + 1, retry, api_id, e)
                if attempt < retry - 1:
                    time.sleep(1)
        raise RuntimeError(f"API POST 호출 최대 재시도 초과: {api_id}")

    # ── 종목명 → 종목코드 변환 ────────────────────────────────
    def _load_name_code_map(self) -> dict[str, str]:
        if self._name_code_map is not None:
            return self._name_code_map
        try:
            url = "http://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            df = pd.read_html(io.BytesIO(resp.content), header=0)[0]
            name_col = "회사명" if "회사명" in df.columns else df.columns[0]
            code_col = "종목코드" if "종목코드" in df.columns else df.columns[1]
            self._name_code_map = {
                str(row[name_col]).strip(): str(row[code_col]).zfill(6)
                for _, row in df.iterrows()
            }
            logger.info("KRX 종목 목록 로드: %d개", len(self._name_code_map))
        except Exception as e:
            logger.warning("KRX 종목 목록 로드 실패: %s", e)
            self._name_code_map = {}
        return self._name_code_map

    def search_stock_code(self, stock_name: str) -> Optional[str]:
        """종목명으로 종목코드 검색. 실패 시 None 반환."""
        name = stock_name.strip()
        if not name:
            return None
        if any(sep in name for sep in (",", ";", "\t", "\n")):
            return None
        # 6자리 숫자면 종목코드로 간주
        if name.isdigit() and len(name) <= 6:
            return name.zfill(6)
        try:
            name_map = self._load_name_code_map()
            if name in name_map:
                return name_map[name]
            # 부분 일치 검색
            for k, v in name_map.items():
                if name in k or k in name:
                    return v
            return None
        except Exception as e:
            logger.warning("종목코드 검색 실패 [%s]: %s", stock_name, e)
            return None

    def get_stock_name(self, stock_code: str) -> str:
        """종목코드로 종목명 조회. 실패 시 종목코드 반환."""
        try:
            data = self._api_post(
                self.tr_ids["stock_info"],
                self.endpoints["stock_info"],
                {"stk_cd": stock_code},
            )
            return data.get("stk_nm", stock_code)
        except Exception:
            return stock_code

    # ── 현재가 조회 ───────────────────────────────────────────
    def get_current_price(self, stock_code: str,
                          price_source: str = "KRX") -> Optional[dict]:
        """
        현재가 조회 (ka10001 - 주식기본정보)
        price_source: KRX | 통합 (config.PRICE_SOURCES)
        반환: {"current": int, "open": int, "high": int, "low": int, "volume": int}
        """
        try:
            query_code = config.resolve_price_code(stock_code, price_source)
            data = self._api_post(
                self.tr_ids["stock_info"],
                self.endpoints["stock_info"],
                {"stk_cd": query_code},
            )
            return {
                "current": _parse_int(data.get("cur_prc")),
                "open"   : _parse_int(data.get("open_pric")),
                "high"   : _parse_int(data.get("high_pric")),
                "low"    : _parse_int(data.get("low_pric")),
                "volume" : _parse_int(data.get("trde_qty")),
            }
        except Exception as e:
            logger.error("현재가 조회 실패 [%s]: %s", stock_code, e)
            return None

    # ── 일봉 데이터 조회 ──────────────────────────────────────
    def get_daily_candles(self, stock_code: str, count: int = 250,
                          price_source: str = "KRX") -> list[dict]:
        """
        일봉 데이터 조회 (ka10081, 연속조회 지원)
        price_source: KRX | 통합 (config.PRICE_SOURCES)
        반환: [{"date", "open", "high", "low", "close", "volume"}, ...]  최신→과거 순
        """
        try:
            query_code = config.resolve_price_code(stock_code, price_source)
            today    = datetime.now().strftime("%Y%m%d")
            body     = {
                "stk_cd"       : query_code,
                "base_dt"      : today,
                "upd_stkpc_tp" : "0",
            }
            all_items: list[dict] = []
            cont_yn  = ""
            next_key = ""

            while len(all_items) < count:
                data, headers = self._api_post_with_headers(
                    self.tr_ids["daily"],
                    self.endpoints["daily"],
                    body,
                    cont_yn=cont_yn,
                    next_key=next_key,
                )
                batch = data.get("stk_dt_pole_chart_qry", [])
                if not batch:
                    break
                all_items.extend(batch)

                cont = (headers.get("cont-yn") or headers.get("Cont-Yn") or "").upper()
                if cont != "Y":
                    break
                cont_yn  = "Y"
                next_key = headers.get("next-key") or headers.get("Next-Key") or ""

            result = []
            for item in all_items[:count]:
                result.append({
                    "date"  : str(item.get("dt", "")),
                    "open"  : _parse_int(item.get("open_pric")),
                    "high"  : _parse_int(item.get("high_pric")),
                    "low"   : _parse_int(item.get("low_pric")),
                    "close" : _parse_int(item.get("cur_prc")),
                    "volume": _parse_int(item.get("trde_qty")),
                })
            return result
        except Exception as e:
            logger.error("일봉 조회 실패 [%s]: %s", stock_code, e)
            return []

    # ── 주문 ──────────────────────────────────────────────────
    def place_order(self, stock_code: str, order_type: str,
                    quantity: int, price: int = 0) -> Optional[dict]:
        """
        주문 실행
        order_type: "buy" | "sell_limit" | "sell_market"
        price=0 이면 시장가
        """
        try:
            if order_type == "buy":
                api_id  = self.tr_ids["buy"]
                trde_tp = "0" if price > 0 else "3"   # 0=지정가, 3=시장가
            else:
                api_id  = self.tr_ids["sell"]
                trde_tp = "0" if price > 0 else "3"

            body = {
                "dmst_stex_tp": "KRX",
                "stk_cd"      : stock_code,
                "ord_qty"     : str(quantity),
                "ord_uv"      : str(price) if price > 0 else "0",
                "trde_tp"     : trde_tp,
                "cond_uv"     : "",
            }
            data = self._api_post(api_id, self.endpoints["order"], body)
            logger.info("주문 완료 [%s] %s %d주 @%d", stock_code, order_type, quantity, price)
            return data
        except Exception as e:
            logger.error("주문 실패 [%s] %s: %s", stock_code, order_type, e)
            return None

    def cancel_all_orders(self, stock_code: str) -> bool:
        """미체결 주문 전량 취소"""
        try:
            # 미체결 주문 조회 (ka10075)
            unfilled = self._api_post(
                self.tr_ids["unfilled"],
                self.endpoints["balance"],
                {"all_stk_tp": "1", "trde_tp": "0", "stk_cd": stock_code, "stex_tp": "1"},
            )
            orders = unfilled.get("oso", [])
            if not orders:
                return True

            ok = True
            for order in orders:
                orig_no = order.get("ord_no", "")
                if not orig_no:
                    continue
                try:
                    self._api_post(
                        self.tr_ids["cancel"],
                        self.endpoints["cancel"],
                        {
                            "dmst_stex_tp": "KRX",
                            "orig_ord_no" : orig_no,
                            "stk_cd"      : stock_code,
                            "cncl_qty"    : "0",
                        },
                    )
                except Exception as e:
                    logger.error("개별 주문 취소 실패 [%s] %s: %s", stock_code, orig_no, e)
                    ok = False

            logger.info("주문 취소 완료 [%s] (%d건)", stock_code, len(orders))
            return ok
        except Exception as e:
            logger.error("주문 취소 실패 [%s]: %s", stock_code, e)
            return False

    # ── 잔고 조회 ─────────────────────────────────────────────
    def get_balance(self) -> list[dict]:
        """보유 잔고 조회. 반환: [{"code", "name", "qty", "avg_price", "eval_price"}, ...]"""
        try:
            data  = self._api_post(
                self.tr_ids["balance"],
                self.endpoints["balance"],
                {"dmst_stex_tp": "KRX"},
            )
            items = data.get("stk_cntr_remn", [])
            result = []
            for item in items:
                qty = _parse_int(item.get("cur_qty"))
                if qty > 0:
                    result.append({
                        "code"      : item.get("stk_cd", ""),
                        "name"      : item.get("stk_nm", ""),
                        "qty"       : qty,
                        "avg_price" : _parse_int(item.get("buy_uv")),
                        "eval_price": _parse_int(item.get("evlt_amt")),
                    })
            return result
        except Exception as e:
            logger.error("잔고 조회 실패: %s", e)
            return []
