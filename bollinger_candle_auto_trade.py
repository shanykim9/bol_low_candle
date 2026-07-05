"""
bollinger_candle_auto_trade.py
볼린저밴드 + 저점캔들 돌파 양봉 자동매매 시스템
Streamlit UI
"""

import io
import time
import logging
import threading
from datetime import datetime

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

import config
from config import validate_env, calc_budgets, DEFAULT_PARAMS, APP_VERSION
from config import (
    resolve_scan_days, resolve_data_days,
    scan_month_options, format_scan_months, calc_data_days,
    price_source_options, format_price_source, parse_stock_list,
    ma_filter_mode_options, ma_trend_days_options, format_ma_filter,
    SETTLEMENT_TRADING_DAYS,
)
from kiwoom_api import KiwoomAPI
from state_manager import StateManager, IDLE, WAIT_BUY, PARTIALLY_BOUGHT, FULLY_BOUGHT, TAKING_PROFIT
from strategy import (
    calc_indicators, find_pivot_candle, check_base_candle,
    calc_buy_prices, calc_stop_price, SignalChecker
)
from simulator import Simulator, summarize_by_stock, analyze_portfolio_capital

# ── 로깅 설정 ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── 페이지 설정 ──────────────────────────────────────────────
st.set_page_config(
    page_title="볼린저밴드 자동매매",
    page_icon="📈",
    layout="wide",
)

# ── 세션 상태 초기화 ─────────────────────────────────────────
def init_session():
    # 코드 업데이트 후에도 예전 세션이 남지 않도록 버전 확인
    if st.session_state.get("_app_version") != APP_VERSION:
        st.session_state.clear()
        st.session_state["_app_version"] = APP_VERSION

    defaults = {
        "running"       : False,
        "log_messages"  : [],
        "sim_result"    : None,
        "ticker_map"    : {},    # {code: name}
        "api"           : None,
        "state_manager" : None,
        "params"        : dict(DEFAULT_PARAMS),
        "worker_thread" : None,
        "input_method"  : "직접 입력",
        "uploaded_names_raw": None,
        "uploaded_file_name": "",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

    # params를 최신 DEFAULT_PARAMS와 동기화 + data_days 재계산
    p = st.session_state.params
    for key, val in DEFAULT_PARAMS.items():
        if key not in p:
            p[key] = val
    if "scan_days" in p:
        del p["scan_days"]
    if "scan_months" not in p:
        p["scan_months"] = DEFAULT_PARAMS["scan_months"]
    p["data_days"] = calc_data_days(p["scan_months"], p["ma_period"])

init_session()


# ── 로그 기록 ─────────────────────────────────────────────────
def log(msg: str, level: str = "INFO"):
    ts  = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] [{level}] {msg}"
    st.session_state.log_messages.insert(0, entry)
    if len(st.session_state.log_messages) > 50:
        st.session_state.log_messages = st.session_state.log_messages[:50]
    getattr(logger, level.lower(), logger.info)(msg)


# ── 장 운영시간 확인 ─────────────────────────────────────────
def is_market_open() -> bool:
    now  = datetime.now()
    if now.weekday() >= 5:
        return False
    t     = now.strftime("%H:%M")
    return config.MARKET_OPEN <= t <= config.MARKET_CLOSE


# ── 종목명 → 종목코드 변환 (파일 업로드) ─────────────────────
def resolve_names_to_codes(api: KiwoomAPI, names: list[str]) -> tuple[dict, list[str]]:
    """
    반환: (성공 {code: name}, 실패 name 리스트)
    """
    success = {}
    failed  = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        code = api.search_stock_code(name)
        if code:
            if code not in success:
                success[code] = api.get_stock_name(code)
        else:
            failed.append(name)
    return success, failed


def _prepare_tickers_for_sim() -> tuple[dict[str, str], list[str]]:
    """
    시뮬레이션 실행 직전 종목 목록 준비.
    파일 업로드 모드: 업로드 텍스트 → 종목코드 자동 변환
    직접 입력 모드: session ticker_map 사용
    """
    method = st.session_state.get("input_method", "직접 입력")
    if method == "파일 업로드":
        raw = st.session_state.get("uploaded_names_raw")
        if not raw:
            return {}, []
        names = parse_stock_list(raw)
        if not names:
            return {}, []
        api = KiwoomAPI()
        success, failed = resolve_names_to_codes(api, names)
        st.session_state.ticker_map = success
        return success, failed
    return dict(st.session_state.get("ticker_map", {})), []


def _sim_ready() -> bool:
    """시뮬레이션 실행 가능 여부"""
    if st.session_state.get("input_method") == "파일 업로드":
        return bool(st.session_state.get("uploaded_names_raw"))
    return bool(st.session_state.get("ticker_map"))


# ── 매매 엔진 (별도 스레드) ───────────────────────────────────
def trading_worker(api: KiwoomAPI, sm: StateManager, params: dict):
    """장중 폴링 루프. running 플래그가 False가 되면 종료."""
    checker = SignalChecker()

    while st.session_state.get("running", False):
        if not is_market_open():
            time.sleep(30)
            continue

        all_states = sm.get_all()

        for code, s in all_states.items():
            try:
                _process_ticker(api, sm, checker, code, s, params)
            except Exception as e:
                log(f"[{code}] 처리 오류: {e}", "ERROR")

        time.sleep(params.get("poll_interval", 5))

    log("자동매매 종료")


def _process_ticker(api: KiwoomAPI, sm: StateManager,
                    checker: SignalChecker, code: str,
                    s: dict, params: dict):
    """단일 종목 매매 처리"""
    state = s["state"]

    # ── 현재가 조회 ──────────────────────────────────────────
    price_data = api.get_current_price(code, params.get("price_source", "KRX"))
    if not price_data:
        return
    current = price_data["current"]
    sm.update_price(code, current)

    budgets = calc_budgets(params["total_budget"])

    # ── IDLE: 조건 탐색 ─────────────────────────────────────
    if state == IDLE:
        candles = api.get_daily_candles(
            code, count=resolve_data_days(params),
            price_source=params.get("price_source", "KRX"),
        )
        if not candles:
            return
        df = calc_indicators(candles, params["ma_period"],
                             params["bb_period"], params["bb_multiplier"])
        pivot = find_pivot_candle(df, resolve_scan_days(params), params)
        if not pivot:
            return
        base = check_base_candle(df, pivot)
        if not base:
            return

        prices     = calc_buy_prices(pivot, base, params["volume_ratio"])
        stop_price = calc_stop_price(pivot, base, params["stop_mode"],
                                     params["stop_pct"], prices["buy1"])
        abandon_th = pivot["low"] * (1 + params["abandon_pct"] / 100)

        sm.to_wait_buy(
            code, pivot, base, prices, stop_price, abandon_th,
            params["profit1_pct"], params["profit2_pct"], budgets
        )
        log(f"[{code}] 매수대기 진입 | Case {prices['case']} "
            f"1차:{prices['buy1']:,} 2차:{prices['buy2']:,} 3차:{prices['buy3']:,}")
        return

    # 최신 상태 재조회
    s = sm.get(code)
    if not s:
        return

    # ── WAIT_BUY: 매수 발동 또는 매수포기 ───────────────────
    if state == WAIT_BUY:
        # 매수포기 체크
        if checker.check_abandon(current, s["pivot"]["low"],
                                  params["abandon_pct"], s["total_qty"]):
            api.cancel_all_orders(code)
            sm.to_idle(code, "매수포기")
            log(f"[{code}] 매수포기 (현재가:{current:,} > 기준:{s['abandon_threshold']:,})", "WARN")
            return

        # 각 차수 매수 발동
        for nth in [1, 2, 3]:
            if not s[f"buy{nth}_filled"]:
                bp  = s[f"buy{nth}_price"]
                qty = s[f"buy{nth}_qty"]
                if qty > 0 and checker.check_buy_trigger(current, bp, False):
                    result = api.place_order(code, "buy", qty, bp)
                    if result:
                        sm.record_buy(code, nth, qty, bp)
                        log(f"[{code}] {nth}차 매수 체결 | {qty}주 @{bp:,}원")

    # ── PARTIALLY_BOUGHT / FULLY_BOUGHT: 손절 + 잔여 매수 ──
    elif state in (PARTIALLY_BOUGHT, FULLY_BOUGHT):
        s = sm.get(code)

        # 잔여 매수 발동 (PARTIALLY_BOUGHT)
        if state == PARTIALLY_BOUGHT:
            for nth in [1, 2, 3]:
                if not s[f"buy{nth}_filled"]:
                    bp  = s[f"buy{nth}_price"]
                    qty = s[f"buy{nth}_qty"]
                    if qty > 0 and checker.check_buy_trigger(current, bp, False):
                        result = api.place_order(code, "buy", qty, bp)
                        if result:
                            sm.record_buy(code, nth, qty, bp)
                            log(f"[{code}] {nth}차 매수 체결 | {qty}주 @{bp:,}원")
            s = sm.get(code)  # 갱신

        # 손절
        if checker.check_stop(current, s["stop_price"]):
            remaining = s["total_qty"] - s["profit1_qty"] - s["profit2_qty"]
            if remaining > 0:
                api.place_order(code, "sell_market", remaining, 0)
            sm.to_idle(code, "손절")
            log(f"[{code}] 손절 | 기준가:{s['stop_price']:,} 현재가:{current:,}", "WARN")
            return

        # 1차 익절
        if s["avg_price"] > 0 and checker.check_profit1(
                current, s["avg_price"], s["profit1_pct"], s["profit1_done"]):
            qty = s["total_qty"] // 2
            if qty > 0:
                api.place_order(code, "sell_limit", qty, current)
                sm.record_profit1(code, current, qty)
                log(f"[{code}] 1차 익절 | {qty}주 @{current:,}원")

    # ── TAKING_PROFIT: 2차 익절 + 트레일링 스탑 ────────────
    elif state == TAKING_PROFIT:
        s = sm.get(code)

        # 2차 익절
        if checker.check_profit2(current, s["avg_price"], s["profit2_pct"],
                                  s["profit1_done"], s["profit2_done"]):
            remaining = s["total_qty"] - s["profit1_qty"]
            qty = remaining // 2
            if qty > 0:
                api.place_order(code, "sell_limit", qty, current)
                sm.record_profit2(code, current, qty)
                log(f"[{code}] 2차 익절 | {qty}주 @{current:,}원")
            s = sm.get(code)

        # 트레일링 스탑
        if checker.check_trailing_stop(current, s["last_profit_price"],
                                        params.get("trailing_pct", 5)):
            remaining = s["total_qty"] - s["profit1_qty"] - s["profit2_qty"]
            if remaining > 0:
                api.place_order(code, "sell_market", remaining, 0)
            sm.to_idle(code, "트레일링스탑")
            log(f"[{code}] 트레일링 스탑 익절 | 기준가:{s['last_profit_price']:,}", "WARN")


            log(f"[{code}] 트레일링 스탑 익절 | 기준가:{s['last_profit_price']:,}", "WARN")


# ── 시뮬레이션 결과 렌더링 ────────────────────────────────────
def _render_sim_results(df_sim: pd.DataFrame, done: int, total: int,
                        running: bool = False):
    """시뮬레이션 결과 패널 (진행 중·완료 공통)"""
    if running:
        st.info(f"⏳ 분석 진행 중… **{done}/{total}** 종목 완료")
    elif done >= total and total > 0:
        st.success(f"✅ 시뮬레이션 완료 ({total}개 종목)")

    if df_sim.empty:
        if running and done == 0:
            st.caption("첫 번째 종목 분석을 기다리는 중…")
        else:
            st.warning("아직 결과가 없습니다.")
        return

    df_summary = summarize_by_stock(df_sim)
    trades_with_buy = df_sim[df_sim["1차매수체결"] == "O"]
    stocks_with_buy = df_summary[df_summary["매수 발생"] > 0]

    total_profit = int(df_summary.get("수익금액", pd.Series(dtype=int)).sum())
    total_loss   = int(df_summary.get("손실금액", pd.Series(dtype=int)).sum())
    total_net    = int(df_summary.get("순손익금액", pd.Series(dtype=int)).sum())

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("분석 완료",    f"{done}/{total}")
    col2.metric("누적 신호",   len(df_sim))
    col3.metric("매수 발생",   len(trades_with_buy))
    if len(stocks_with_buy) > 0:
        avg_pnl = stocks_with_buy["평균 손익률(%)"].mean()
        win_stocks = (stocks_with_buy["평균 손익률(%)"] > 0).sum()
        col4.metric("종목 평균 손익률", f"{avg_pnl:.2f}%")
        col5.metric("수익 종목", f"{win_stocks}/{len(stocks_with_buy)}")
    elif not running and "비고" in df_sim.columns:
        notes = ", ".join(sorted(set(
            df_sim["비고"].astype(str).replace("", pd.NA).dropna()
        )))
        if notes:
            st.warning(f"매수 발생 0건 사유: {notes}")

    # ── 손익 금액 합계 ────────────────────────────────────────
    m1, m2, m3 = st.columns(3)
    m1.metric("총 수익금액", f"+{total_profit:,}원")
    m2.metric("총 손실금액", f"{total_loss:,}원")
    m3.metric("순손익 합계", f"{total_net:+,}원",
              delta=f"{total_net:+,}원")

    # ── 포트폴리오 최대 동시 투입자금 ─────────────────────────
    cap = analyze_portfolio_capital(df_sim)
    if cap["peak_tied"] > 0:
        st.markdown("**💰 포트폴리오 자금 분석**")
        peak_dt = cap["peak_date"]
        if len(peak_dt) == 8:
            peak_dt = f"{peak_dt[:4]}-{peak_dt[4:6]}-{peak_dt[6:]}"
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("최대 동시 투입자금", f"{cap['peak_tied']:,}원")
        c2.metric("발생일", peak_dt)
        c3.metric("동시 보유 종목", f"{cap['peak_overlap_count']}개")
        roi_pct = total_net / cap["peak_tied"] * 100
        c4.metric("자금투입대비 순손익률", f"{roi_pct:+.2f}%")
        if cap["recommended_account"] > cap["peak_tied"]:
            st.caption(
                f"※ T+{SETTLEMENT_TRADING_DAYS} 결제 대기 반영 권장 계좌자금: "
                f"{cap['recommended_account']:,}원"
            )
        if cap["peak_overlap_codes"]:
            st.caption(f"해당일 보유 종목: {', '.join(cap['peak_overlap_codes'])}")

    st.markdown("**🏆 종목별 요약** (완료된 종목부터 표시)")
    st.dataframe(
        df_summary,
        use_container_width=True,
        hide_index=True,
        column_config={
            "수익금액"  : st.column_config.NumberColumn("수익금액", format="%d 원"),
            "손실금액"  : st.column_config.NumberColumn("손실금액", format="%d 원"),
            "순손익금액": st.column_config.NumberColumn("순손익금액", format="%d 원"),
        },
    )

    if running:
        st.caption("↑ 완료된 종목 결과 · 나머지 종목은 분석 후 자동 추가됩니다.")
    else:
        with st.expander("📋 거래 상세 보기", expanded=False):
            detail = df_sim.drop(columns=["flow_events"], errors="ignore")
            st.dataframe(detail, use_container_width=True, hide_index=True)


def _sim_csv_download(df_sim: pd.DataFrame, key: str):
    """시뮬레이션 CSV 다운로드 버튼"""
    df_summary = summarize_by_stock(df_sim)
    csv_buf = io.StringIO()
    csv_buf.write("[종목별 요약]\n")
    df_summary.to_csv(csv_buf, index=False)
    csv_buf.write("\n[거래 상세]\n")
    df_sim.to_csv(csv_buf, index=False)
    st.download_button(
        label="📥 CSV 다운로드 (요약+상세)",
        data=csv_buf.getvalue().encode("utf-8-sig"),
        file_name=f"simulation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
        key=key,
    )


# ════════════════════════════════════════════════════════════
#  UI
# ════════════════════════════════════════════════════════════

# ── .env 검증 ────────────────────────────────────────────────
missing_env = validate_env()

# ── 사이드바 ─────────────────────────────────────────────────
with st.sidebar:
    st.title("⚙️ 설정")
    st.caption(f"엔진 버전: {APP_VERSION}")

    # .env 상태
    if missing_env:
        st.error(f"❌ .env 누락 항목: {', '.join(missing_env)}\n\n"
                 ".env 파일을 프로젝트 루트에 생성하고 값을 입력해주세요.")
    else:
        st.success("✅ API 인증정보 로드 완료")

    # ── 시세 구분 ────────────────────────────────────────────
    st.subheader("📡 시세 구분")
    p = st.session_state.params
    src_opts = price_source_options()
    if p.get("price_source") not in src_opts:
        p["price_source"] = src_opts[0]
    prev_source = p.get("price_source")
    p["price_source"] = st.selectbox(
        "시세 기준",
        src_opts,
        index=src_opts.index(p["price_source"]),
        format_func=format_price_source,
    )
    if p["price_source"] != prev_source:
        st.session_state.sim_result = None
    if p["price_source"] == "통합":
        st.caption("HTS 통합시세(KRX+NXT) 기준 · 주문은 KRX 정규장으로 실행")
    st.divider()

    # ── 운영 모드 ────────────────────────────────────────────
    mode = st.radio("운영 모드", ["실시간 자동매매", "시뮬레이션"], horizontal=True)
    st.divider()

    # ── 종목 입력 ────────────────────────────────────────────
    st.subheader("📋 종목 입력")
    input_method = st.radio("입력 방법", ["직접 입력", "파일 업로드"], horizontal=True)
    st.session_state.input_method = input_method

    if input_method == "직접 입력":
        code_input = st.text_input(
            "종목코드 또는 종목명 (쉼표 구분)",
            placeholder="005930,삼성전자,000660,SK하이닉스",
        )
        if st.button("종목 확인", key="btn_check"):
            if not missing_env:
                entries = parse_stock_list(code_input)
                if entries:
                    api_tmp = KiwoomAPI()
                    ticker_map = {}
                    failed = []
                    for entry in entries:
                        code = api_tmp.search_stock_code(entry)
                        if not code:
                            failed.append(entry)
                            continue
                        name = api_tmp.get_stock_name(code)
                        ticker_map[code] = name
                    st.session_state.ticker_map = ticker_map
                    st.session_state.sim_result = None
                    if ticker_map:
                        st.success(f"{len(ticker_map)}개 종목 확인 완료")
                    if failed:
                        st.warning(f"매핑 실패: {', '.join(failed)}")
            else:
                st.error(".env 설정을 먼저 완료해주세요.")

    else:  # 파일 업로드
        uploaded = st.file_uploader(
            "종목명 텍스트 파일 (.txt)\n한 줄에 1개, 또는 쉼표/줄바꿈으로 구분",
            type=["txt"],
        )
        if uploaded is not None:
            raw = uploaded.read().decode("utf-8", errors="ignore")
            if raw != st.session_state.get("uploaded_names_raw"):
                st.session_state.uploaded_names_raw = raw
                st.session_state.uploaded_file_name = uploaded.name
                st.session_state.ticker_map = {}
                st.session_state.sim_result = None
            n = len(parse_stock_list(raw))
            st.caption(
                f"📄 {uploaded.name} · {n}개 종목 "
                f"(시뮬레이션 실행 시 자동 변환)"
            )
        elif st.session_state.get("uploaded_names_raw"):
            n = len(parse_stock_list(st.session_state.uploaded_names_raw))
            st.caption(
                f"📄 {st.session_state.get('uploaded_file_name', '업로드됨')} "
                f"· {n}개 종목 (시뮬레이션 실행 시 자동 변환)"
            )

    # 현재 종목 목록 표시
    if st.session_state.ticker_map:
        st.caption("확인된 종목:")
        for code, name in st.session_state.ticker_map.items():
            st.caption(f"  {code} | {name}")

    st.divider()

    # ── 지표 파라미터 ────────────────────────────────────────
    st.subheader("📊 지표 설정")
    p = st.session_state.params
    p["bb_period"]     = st.number_input("볼린저밴드 기간 (Period)", 5, 60, p["bb_period"], step=1)
    p["bb_multiplier"] = st.number_input("볼린저밴드 승수 (D1)",    1.0, 5.0, p["bb_multiplier"], step=0.5)
    p["ma_period"]     = st.number_input("이동평균선 기간",          20, 480, p["ma_period"], step=10)

    # ── 240일선 조건 ───────────────────────────────────────────
    ma_modes = ma_filter_mode_options()
    if p.get("ma_filter_mode") not in ma_modes:
        p["ma_filter_mode"] = ma_modes[0]
    prev_ma_mode = p.get("ma_filter_mode")
    p["ma_filter_mode"] = st.radio(
        "240일선 조건",
        ma_modes,
        index=ma_modes.index(p["ma_filter_mode"]),
        horizontal=True,
    )
    if p["ma_filter_mode"] == "240선추세":
        trend_opts = ma_trend_days_options()
        if p.get("ma_trend_days") not in trend_opts:
            p["ma_trend_days"] = trend_opts[0]
        p["ma_trend_days"] = st.selectbox(
            "240일선 연속 상승 일수",
            trend_opts,
            index=trend_opts.index(int(p["ma_trend_days"])),
            format_func=lambda d: f"{d}일 이상",
        )
        st.caption(
            f"저가 ≥ {p['ma_period']}일선 + BB하단 + "
            f"MA {p['ma_trend_days']}일 연속 상승"
        )
    else:
        st.caption(f"저가 ≥ {p['ma_period']}일선 + BB하단 (추세 조건 없음)")
    if p.get("ma_filter_mode") != prev_ma_mode:
        st.session_state.sim_result = None

    scan_opts = scan_month_options()
    if p.get("scan_months") not in scan_opts:
        p["scan_months"] = scan_opts[0]
    p["scan_months"] = st.selectbox(
        "저점캔들 탐색 기간 (6개월 단위, 최대 5년)",
        scan_opts,
        index=scan_opts.index(p["scan_months"]),
        format_func=format_scan_months,
    )
    p["data_days"] = calc_data_days(p["scan_months"], p["ma_period"])
    st.caption(
        f"탐색 범위: 최근 {resolve_scan_days(p):,}거래일 "
        f"({format_scan_months(p['scan_months'])}) · "
        f"일봉 수집: {p['data_days']:,}일"
    )
    st.divider()

    # ── 투자금액 ────────────────────────────────────────────
    st.subheader("💰 투자금액")
    total_budget = st.number_input(
        "총 투자금액 (원)", 100_000, 100_000_000,
        p["total_budget"], step=500_000, format="%d"
    )
    p["total_budget"] = total_budget
    b = calc_budgets(total_budget)
    st.caption(f"1차: {b['buy1']:,}원 / 2차: {b['buy2']:,}원 / 3차: {b['buy3']:,}원")
    st.divider()

    # ── 손절 설정 ────────────────────────────────────────────
    st.subheader("🛡️ 손절 설정")
    stop_mode = st.radio(
        "손절 방식",
        ["기준저가 (디폴트)", "1차 매수가 대비"],
        index=0 if p["stop_mode"] == "기준저가" else 1,
    )
    p["stop_mode"] = "기준저가" if "기준저가" in stop_mode else "1차매수가대비"
    if p["stop_mode"] == "1차매수가대비":
        p["stop_pct"] = st.selectbox("손절 비율", [5, 10, 15],
                                      index=[5, 10, 15].index(p["stop_pct"]))
        st.caption(f"1차 매수가 대비 -{p['stop_pct']}% 이하 시 손절")
    st.divider()

    # ── 익절 설정 ────────────────────────────────────────────
    st.subheader("💹 익절 설정")
    profit1_options = [15, 20, 25, 30, 35, 40, 45, 50]
    profit2_options = [20, 25, 30, 35, 40, 45, 50, 55, 60]

    p["profit1_pct"] = st.selectbox(
        "1차 익절 비율 (%)",
        profit1_options,
        index=profit1_options.index(p["profit1_pct"]) if p["profit1_pct"] in profit1_options else 1,
    )
    # 2차 익절: 1차보다 높은 옵션만 허용
    valid_p2 = [x for x in profit2_options if x > p["profit1_pct"]]
    if not valid_p2:
        valid_p2 = profit2_options[-1:]
    if p["profit2_pct"] not in valid_p2:
        p["profit2_pct"] = valid_p2[0]
    p["profit2_pct"] = st.selectbox(
        "2차 익절 비율 (%)",
        valid_p2,
        index=valid_p2.index(p["profit2_pct"]) if p["profit2_pct"] in valid_p2 else 0,
    )
    if p["profit2_pct"] <= p["profit1_pct"]:
        st.error("⚠️ 2차 익절 비율은 1차보다 높아야 합니다.")
    st.caption(f"트레일링 스탑: 익절가 대비 -{p['trailing_pct']}% 하락 시 잔량 전량 매도")
    st.divider()

    # ── 자동매매 제어 ────────────────────────────────────────
    if mode == "실시간 자동매매":
        col1, col2 = st.columns(2)
        with col1:
            if st.button("▶ 시작", type="primary", disabled=bool(missing_env)):
                if not st.session_state.ticker_map:
                    st.warning("종목을 먼저 설정해주세요.")
                elif not st.session_state.running:
                    # API 및 StateManager 초기화
                    api = KiwoomAPI()
                    sm  = StateManager()
                    sm.set_tickers(st.session_state.ticker_map)

                    st.session_state.api           = api
                    st.session_state.state_manager = sm
                    st.session_state.running       = True

                    thread = threading.Thread(
                        target=trading_worker,
                        args=(api, sm, dict(p)),
                        daemon=True,
                    )
                    thread.start()
                    st.session_state.worker_thread = thread
                    log("자동매매 시작")
        with col2:
            if st.button("⏹ 중지", disabled=not st.session_state.running):
                st.session_state.running = False
                log("자동매매 중지 요청")


# ── 메인 화면 ────────────────────────────────────────────────
st.title("📈 볼린저밴드 + 저점캔들 돌파 양봉 자동매매")

if missing_env:
    st.error(
        "### ⚠️ .env 파일 설정 필요\n\n"
        "프로젝트 루트에 `.env` 파일을 생성하고 아래 내용을 입력해주세요:\n\n"
        "```\nAPP_KEY=여기에_앱키_입력\n"
        "APP_SECRET=여기에_앱시크릿_입력\n"
        "ACCOUNT_NO=여기에_계좌번호_입력\n```"
    )
    st.stop()

# ── 실시간 자동매매 화면 ─────────────────────────────────────
if mode == "실시간 자동매매":
    # 5초마다 자동 갱신
    st_autorefresh(interval=5000, key="autorefresh")

    status_color = "🟢" if st.session_state.running else "🔴"
    st.subheader(f"{status_color} 종목별 매매 현황")

    sm = st.session_state.get("state_manager")
    if sm:
        all_s = sm.get_all()
        if all_s:
            rows = []
            for code, s in all_s.items():
                pnl_amt, pnl_pct = StateManager.eval_pnl(s)
                rows.append({
                    "종목코드"  : code,
                    "종목명"    : s["name"],
                    "상태"      : s["state"],
                    "현재가"    : f"{s['current_price']:,}",
                    "평균단가"  : f"{s['avg_price']:,}" if s["avg_price"] else "-",
                    "보유수량"  : s["total_qty"] - s["profit1_qty"] - s["profit2_qty"],
                    "평가손익"  : f"{pnl_amt:+,}원 ({pnl_pct:+.2f}%)" if s["avg_price"] else "-",
                    "1차매수가" : f"{s['buy1_price']:,}" if s["buy1_price"] else "-",
                    "2차매수가" : f"{s['buy2_price']:,}" if s["buy2_price"] else "-",
                    "3차매수가" : f"{s['buy3_price']:,}" if s["buy3_price"] else "-",
                    "손절기준가": f"{s['stop_price']:,}" if s["stop_price"] else "-",
                    "1차익절"   : f"✅ {s['profit1_price']:,}" if s["profit1_done"] else "대기",
                    "2차익절"   : f"✅ {s['profit2_price']:,}" if s["profit2_done"] else "대기",
                })
            df_display = pd.DataFrame(rows)
            st.dataframe(df_display, use_container_width=True, hide_index=True)
        else:
            st.info("등록된 종목이 없습니다.")
    else:
        st.info("자동매매를 시작하면 현황이 표시됩니다.")

    # ── 이벤트 로그 ──────────────────────────────────────────
    st.subheader("📋 실시간 이벤트 로그")
    log_text = "\n".join(st.session_state.log_messages) if st.session_state.log_messages else "이벤트 없음"
    st.text_area("", value=log_text, height=300, key="log_area")

# ── 시뮬레이션 화면 ──────────────────────────────────────────
else:
    st.subheader("🔬 시뮬레이션 (백테스트)")

    if not _sim_ready():
        st.info(
            "사이드바에서 종목을 입력(직접 입력 → 종목 확인)하거나 "
            "txt 파일을 업로드한 뒤 **▶ 시뮬레이션 실행**을 클릭하세요."
        )
    else:
        pending_n = (
            len(parse_stock_list(st.session_state.uploaded_names_raw))
            if st.session_state.get("input_method") == "파일 업로드"
            and st.session_state.get("uploaded_names_raw")
            and not st.session_state.ticker_map
            else len(st.session_state.ticker_map)
        )
        label = f"**{pending_n}개** 종목 (실행 시 변환)" if (
            st.session_state.get("input_method") == "파일 업로드"
            and not st.session_state.ticker_map
        ) else f"**{len(st.session_state.ticker_map)}개** 종목"
        st.write(f"분석 대상: {label}")
        st.caption(
            f"시뮬레이션 조건: 탐색 {format_scan_months(p['scan_months'])} "
            f"({resolve_scan_days(p):,}거래일), 일봉 수집 {resolve_data_days(p):,}일 · "
            f"{format_ma_filter(p)}"
        )
        ticker_list_str = ", ".join(
            [f"{n}({c})" for c, n in st.session_state.ticker_map.items()]
        )
        if ticker_list_str:
            st.caption(ticker_list_str)

        sim_panel = st.empty()

        if st.button("▶ 시뮬레이션 실행", type="primary"):
            st.session_state.sim_result = None

            # ── 종목코드 변환 (파일 업로드 시 자동) ──────────
            if st.session_state.get("input_method") == "파일 업로드":
                with st.spinner("종목코드 변환 중..."):
                    ticker_map, failed = _prepare_tickers_for_sim()
            else:
                ticker_map, failed = _prepare_tickers_for_sim()

            if failed:
                st.warning(f"⚠️ 매핑 실패 {len(failed)}개: {', '.join(failed)}")
            if not ticker_map:
                st.error("변환된 종목이 없습니다. 파일 또는 입력을 확인해주세요.")
                st.stop()

            if (
                st.session_state.get("input_method") == "파일 업로드"
                and ticker_map
            ):
                st.success(
                    f"✅ {len(ticker_map)}개 종목 변환 완료 → 시뮬레이션 시작"
                )

            api   = KiwoomAPI()
            sim   = Simulator(api, dict(p))
            total = len(ticker_map)

            progress_bar = st.progress(0, text=f"0/{total} 종목")
            status_text  = st.empty()

            def progress_cb(done, total_cnt, msg, partial_df):
                pct = done / total_cnt if total_cnt > 0 else 0
                progress_bar.progress(pct, text=f"{done}/{total_cnt} 종목")
                status_text.caption(msg)
                if partial_df is not None:
                    with sim_panel.container():
                        st.subheader("📊 시뮬레이션 결과")
                        _render_sim_results(
                            partial_df, done, total_cnt,
                            running=(done < total_cnt),
                        )

            result_df = sim.run(
                ticker_map,
                progress_callback=progress_cb,
            )

            progress_bar.progress(1.0, text=f"{total}/{total} 종목")
            status_text.empty()
            st.session_state.sim_result = result_df

            with sim_panel.container():
                st.subheader("📊 시뮬레이션 결과")
                _render_sim_results(result_df, total, total, running=False)
                if not result_df.empty:
                    _sim_csv_download(result_df, key="sim_csv_after_run")

        elif st.session_state.sim_result is not None:
            with sim_panel.container():
                st.subheader("📊 시뮬레이션 결과")
                df_sim = st.session_state.sim_result
                n_done = len(summarize_by_stock(df_sim))
                n_total = len(st.session_state.ticker_map)
                _render_sim_results(df_sim, n_done, n_total, running=False)
                if not df_sim.empty:
                    _sim_csv_download(df_sim, key="sim_csv_download")

    # 로그 (시뮬레이션 모드)
    if st.session_state.log_messages:
        with st.expander("로그 보기"):
            st.text("\n".join(st.session_state.log_messages))
