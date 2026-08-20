"""
bollinger_lowcandle_case.py
파라미터 전수 조합 시뮬레이션 → 전체 베스트 5 + 종목별 베스트 3

조합:
  1) 1차 익절: 30, 40, 50%
  2) 2차 익절: 40, 50, 60% (1차보다 큰 값만)
  3) 손절: 기준저가 / 1차매수가대비 -5% · -10%
  4) 시세: KRX(정규장) / 통합
  5) 저점캔들 탐색: 1년, 2년, 3년

순위: 자금투입대비 순손익률 → 순손익합계 → (동일 시 짧은 탐색기간만 표시)
     → 승률 → 평균손익률

실행:
  streamlit run bollinger_lowcandle_case.py
"""

from __future__ import annotations

import io
import itertools
import logging
from copy import deepcopy
from datetime import datetime

import pandas as pd
import streamlit as st

from config import (
    APP_VERSION,
    DEFAULT_PARAMS,
    validate_env,
    calc_budgets,
    calc_data_days,
    format_scan_months,
    format_price_source,
    parse_stock_list,
    ma_filter_mode_options,
    ma_trend_days_options,
    format_ma_filter,
)
from kiwoom_api import KiwoomAPI
from strategy import simulate_strategy
from simulator import (
    _trade_record,
    _empty_record,
    _records_to_df,
    summarize_by_stock,
    analyze_portfolio_capital,
    detail_trades_df,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── 조합 정의 ─────────────────────────────────────────────────
PROFIT1_OPTS = [30, 40, 50]
PROFIT2_OPTS = [40, 50, 60]
STOP_OPTS = [
    ("기준저가", 0),
    ("1차매수가대비", 5),
    ("1차매수가대비", 10),
]
PRICE_OPTS = ["KRX", "통합"]
SCAN_MONTH_OPTS = [12, 24, 36]  # 1년, 2년, 3년
TOP_N = 5
TOP_N_STOCK = 3  # 종목별 베스트


def build_case_grid() -> list[dict]:
    """유효한 파라미터 조합 목록 (2차 익절 > 1차 익절)"""
    cases = []
    for p1, p2, (stop_mode, stop_pct), src, months in itertools.product(
        PROFIT1_OPTS, PROFIT2_OPTS, STOP_OPTS, PRICE_OPTS, SCAN_MONTH_OPTS
    ):
        if p2 <= p1:
            continue
        cases.append({
            "profit1_pct": p1,
            "profit2_pct": p2,
            "stop_mode": stop_mode,
            "stop_pct": stop_pct,
            "price_source": src,
            "scan_months": months,
            "scan_mode": "상대기간",
        })
    return cases


def _stop_label(stop_mode: str, stop_pct: int) -> str:
    if stop_mode == "기준저가":
        return "기준저가"
    return f"1차매수가대비 -{stop_pct}%"


def _case_label(case: dict) -> str:
    return (
        f"1차익절 {case['profit1_pct']}% / "
        f"2차익절 {case['profit2_pct']}% / "
        f"손절 {_stop_label(case['stop_mode'], case['stop_pct'])} / "
        f"{format_price_source(case['price_source'])} / "
        f"탐색 {format_scan_months(case['scan_months'])}"
    )


def resolve_names_to_codes(api: KiwoomAPI, names: list[str]) -> tuple[dict, list[str]]:
    success, failed = {}, []
    for name in names:
        name = name.strip()
        if not name:
            continue
        code = api.search_stock_code(name)
        if code:
            if code not in success:
                # 종목명은 입력값 그대로 사용 (키움 인증 불필요)
                if name.isdigit() and len(name.strip()) <= 6:
                    success[code] = api.get_stock_name(code)
                else:
                    success[code] = name
        else:
            failed.append(name)
    return success, failed


def fetch_candle_cache(
    api: KiwoomAPI,
    ticker_map: dict[str, str],
    price_sources: list[str],
    data_days: int,
    progress_cb=None,
) -> dict[tuple[str, str], list[dict]]:
    """(종목코드, 시세)별 일봉을 한 번씩만 수집."""
    cache: dict[tuple[str, str], list[dict]] = {}
    jobs = [(code, name, src) for code, name in ticker_map.items() for src in price_sources]
    total = len(jobs)
    for i, (code, name, src) in enumerate(jobs):
        if progress_cb:
            progress_cb(
                i, total,
                f"일봉 수집 [{i + 1}/{total}] {name}({code}) · {format_price_source(src)}",
            )
        try:
            candles = api.get_daily_candles(code, count=data_days, price_source=src)
            cache[(code, src)] = candles or []
        except Exception as e:
            logger.error("일봉 수집 실패 %s/%s: %s", code, src, e)
            cache[(code, src)] = []
    if progress_cb:
        progress_cb(total, total, "일봉 수집 완료")
    return cache


def run_one_case(
    case: dict,
    base_params: dict,
    ticker_map: dict[str, str],
    candle_cache: dict[tuple[str, str], list[dict]],
) -> tuple[dict, pd.DataFrame]:
    """단일 조합 시뮬 → (요약 metrics, 거래 DataFrame)"""
    params = deepcopy(base_params)
    params.update(case)
    # auto_trade와 동일: 조합별 탐색기간에 맞는 일봉 길이만 사용
    # (캐시는 max 탐색기간으로 모아 두므로 여기서 잘라야 함)
    needed = calc_data_days(params["scan_months"], params["ma_period"])
    params["data_days"] = needed

    records: list[dict] = []
    src = case["price_source"]
    for code, name in ticker_map.items():
        raw = candle_cache.get((code, src), [])
        # API 일봉은 최신→과거 순 → [:needed] 가 count=needed 조회와 동일
        candles = raw[:needed] if raw else []
        if not candles:
            records.append(_empty_record(code, name, "데이터 없음"))
            continue
        trades = simulate_strategy(candles, params)
        if not trades:
            records.append(_empty_record(code, name, "매매조건 없음"))
            continue
        records.extend(_trade_record(code, name, t) for t in trades)

    df = _records_to_df(records)
    metrics = _metrics_from_df(case, df)
    return metrics, df


def _metrics_from_df(case: dict, df: pd.DataFrame) -> dict:
    """거래 DataFrame → 조합 성과 지표"""
    summary = summarize_by_stock(df) if df is not None and not df.empty else pd.DataFrame()
    buys = (
        df[df["1차매수체결"] == "O"]
        if df is not None and not df.empty and "1차매수체결" in df.columns
        else pd.DataFrame()
    )
    cap = analyze_portfolio_capital(df) if df is not None else {
        "peak_tied": 0, "recommended_account": 0,
    }

    total_net = int(summary["순손익금액"].sum()) if not summary.empty else 0
    buy_cnt = len(buys)
    peak_tied = int(cap.get("peak_tied", 0) or 0)
    roi = (total_net / peak_tied * 100) if peak_tied > 0 else 0.0

    if buy_cnt > 0 and "최종손익률" in buys.columns:
        avg_pnl = round(float(buys["최종손익률"].mean()), 2)
        win_rate = round(float((buys["최종손익률"] > 0).mean() * 100), 1)
    else:
        avg_pnl, win_rate = 0.0, 0.0

    return {
        "순위": 0,
        "조합설명": _case_label(case),
        "1차익절(%)": case["profit1_pct"],
        "2차익절(%)": case["profit2_pct"],
        "손절": _stop_label(case["stop_mode"], case["stop_pct"]),
        "시세": format_price_source(case["price_source"]),
        "탐색기간": format_scan_months(case["scan_months"]),
        "매수발생": buy_cnt,
        "신호수": len(df) if df is not None and not df.empty else 0,
        "순손익합계": total_net,
        "최대동시투입자금": peak_tied,
        "자금투입대비순손익률(%)": round(roi, 2),
        "평균손익률(%)": avg_pnl,
        "승률(%)": win_rate,
        "권장계좌자금": int(cap.get("recommended_account", 0) or 0),
        "_case": case,
        "_df": df,
    }


def _scan_months_of(row: dict) -> int:
    case = row.get("_case") or {}
    return int(case.get("scan_months", 9999))


def _rank_key(r: dict) -> tuple:
    """
    높을수록 좋음 (reverse=True).
    순손익합계가 같으면 탐색기간이 짧을수록 우선 (-months 가 큼).
    """
    return (
        r["자금투입대비순손익률(%)"],
        r["순손익합계"],
        -_scan_months_of(r),
        r["승률(%)"],
        r["평균손익률(%)"],
        r["매수발생"],
    )


def rank_results(rows: list[dict], top_n: int = TOP_N) -> list[dict]:
    """
    베스트 선정:
      자금투입대비 순손익률 → 순손익합계
      → 순손익합계가 동일하면 탐색기간이 짧은 경우만 남김
      → 승률 → 평균손익률
    """
    ranked = sorted(rows, key=_rank_key, reverse=True)

    # 동일 순손익합계 → 이미 정렬상 짧은 탐색이 앞 → 첫 것만 채택
    seen_net: set = set()
    picked: list[dict] = []
    for r in ranked:
        net = r["순손익합계"]
        if net in seen_net:
            continue
        seen_net.add(net)
        picked.append(r)
        if len(picked) >= top_n:
            break

    for i, r in enumerate(picked, 1):
        r["순위"] = i
    return picked


def build_per_stock_best(
    all_metrics: list[dict],
    ticker_map: dict[str, str],
    top_n: int = TOP_N_STOCK,
) -> dict[str, list[dict]]:
    """종목별로 조합 성과를 모아 베스트 top_n 선정"""
    per: dict[str, list[dict]] = {code: [] for code in ticker_map}
    for m in all_metrics:
        df = m.get("_df")
        case = m.get("_case")
        if case is None or df is None or df.empty or "종목코드" not in df.columns:
            continue
        for code, name in ticker_map.items():
            sdf = df[df["종목코드"].astype(str) == str(code)].copy()
            if sdf.empty:
                continue
            sm = _metrics_from_df(case, sdf)
            sm["종목코드"] = code
            sm["종목명"] = name
            per[code].append(sm)

    result = {}
    for code, name in ticker_map.items():
        result[code] = {
            "name": name,
            "top": rank_results(per.get(code, []), top_n),
        }
    return result


def metrics_table(rows: list[dict]) -> pd.DataFrame:
    cols = [
        "순위", "조합설명", "1차익절(%)", "2차익절(%)", "손절", "시세", "탐색기간",
        "매수발생", "신호수", "순손익합계", "최대동시투입자금",
        "자금투입대비순손익률(%)", "평균손익률(%)", "승률(%)", "권장계좌자금",
    ]
    return pd.DataFrame([{k: r[k] for k in cols} for r in rows])


def _assign_ranks(rows: list[dict]) -> list[dict]:
    """현재까지 완료된 결과에 임시 순위 부여 (복사본, 동일 순손익→짧은 탐색만)"""
    ranked = sorted(rows, key=_rank_key, reverse=True)
    seen_net: set = set()
    out = []
    for r in ranked:
        net = r["순손익합계"]
        if net in seen_net:
            continue
        seen_net.add(net)
        item = {k: v for k, v in r.items() if k not in ("_case", "_df")}
        item["순위"] = len(out) + 1
        out.append(item)
    return out


LIVE_COLS = [
    "순위", "1차익절(%)", "2차익절(%)", "손절", "시세", "탐색기간",
    "매수발생", "순손익합계", "자금투입대비순손익률(%)", "승률(%)", "평균손익률(%)",
]


def _live_df(rows: list[dict], limit=None) -> pd.DataFrame:
    ranked = _assign_ranks(rows)
    if limit is not None:
        ranked = ranked[:limit]
    return pd.DataFrame([{k: r.get(k) for k in LIVE_COLS} for r in ranked])


def _recent_df(rows: list[dict], n: int = 15) -> pd.DataFrame:
    """최근 완료된 n개 (실행 순서 역순, 순위 없이)"""
    recent = rows[-n:][::-1]
    cols = [
        "진행번호", "1차익절(%)", "2차익절(%)", "손절", "시세", "탐색기간",
        "매수발생", "순손익합계", "자금투입대비순손익률(%)", "승률(%)", "평균손익률(%)",
    ]
    data = []
    for r in recent:
        data.append({
            "진행번호": r.get("_seq", ""),
            "1차익절(%)": r["1차익절(%)"],
            "2차익절(%)": r["2차익절(%)"],
            "손절": r["손절"],
            "시세": r["시세"],
            "탐색기간": r["탐색기간"],
            "매수발생": r["매수발생"],
            "순손익합계": r["순손익합계"],
            "자금투입대비순손익률(%)": r["자금투입대비순손익률(%)"],
            "승률(%)": r["승률(%)"],
            "평균손익률(%)": r["평균손익률(%)"],
        })
    return pd.DataFrame(data, columns=cols)


def _metric_col_config() -> dict:
    return {
        "순손익합계": st.column_config.NumberColumn(format="%d 원"),
        "최대동시투입자금": st.column_config.NumberColumn(format="%d 원"),
        "권장계좌자금": st.column_config.NumberColumn(format="%d 원"),
        "자금투입대비순손익률(%)": st.column_config.NumberColumn(format="%.2f %%"),
        "평균_자금대비순손익률(%)": st.column_config.NumberColumn(format="%.2f %%"),
        "최고_자금대비순손익률(%)": st.column_config.NumberColumn(format="%.2f %%"),
        "평균_순손익합계": st.column_config.NumberColumn(format="%d 원"),
    }


FACTOR_SPECS = [
    ("1차익절(%)", "1차 익절률"),
    ("2차익절(%)", "2차 익절률"),
    ("손절", "손절 설정"),
    ("시세", "시세 기준"),
    ("탐색기간", "저점캔들 탐색 기간"),
]


def _factor_stats(rows: list[dict], col: str) -> pd.DataFrame:
    """요인별 평균/최고 자금대비 순손익률 집계"""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if col not in df.columns:
        return pd.DataFrame()
    g = df.groupby(col, dropna=False)
    out = g.agg(
        조합수=("자금투입대비순손익률(%)", "count"),
        평균_자금대비순손익률=("자금투입대비순손익률(%)", "mean"),
        최고_자금대비순손익률=("자금투입대비순손익률(%)", "max"),
        평균_순손익합계=("순손익합계", "mean"),
        평균_승률=("승률(%)", "mean"),
        평균_매수발생=("매수발생", "mean"),
    ).reset_index()
    out = out.rename(columns={col: "설정값"})
    out["평균_자금대비순손익률"] = out["평균_자금대비순손익률"].round(2)
    out["최고_자금대비순손익률"] = out["최고_자금대비순손익률"].round(2)
    out["평균_순손익합계"] = out["평균_순손익합계"].round(0).astype(int)
    out["평균_승률"] = out["평균_승률"].round(1)
    out["평균_매수발생"] = out["평균_매수발생"].round(1)
    out = out.sort_values(
        ["평균_자금대비순손익률", "최고_자금대비순손익률", "평균_순손익합계"],
        ascending=False,
    ).reset_index(drop=True)
    out.insert(0, "요인내순위", range(1, len(out) + 1))
    return out


def build_factor_report(rows: list[dict]) -> dict:
    """
    자금대비 순손익률 기준 요인별 분석 리포트.
    반환: {
      summary_lines: list[str],
      markdown: str,
      tables: {요인명: DataFrame},
      winners: {요인키: {value, avg_roi, max_roi, ...}},
      best_case: dict (전체 1위 조합),
    }
    """
    empty = {
        "summary_lines": [],
        "markdown": "",
        "tables": {},
        "winners": {},
        "best_case": None,
    }
    if not rows:
        return empty

    ranked = sorted(
        rows,
        key=lambda r: (
            r["자금투입대비순손익률(%)"],
            r["순손익합계"],
            r["승률(%)"],
            r["평균손익률(%)"],
        ),
        reverse=True,
    )
    best = ranked[0]
    winners = {}
    tables = {}
    lines = []

    for col, label in FACTOR_SPECS:
        stats = _factor_stats(rows, col)
        tables[label] = stats
        if stats.empty:
            continue
        top_row = stats.iloc[0]
        val = top_row["설정값"]
        # 표시용: 익절은 % 붙이기
        if col in ("1차익절(%)", "2차익절(%)"):
            try:
                num = float(val)
                val_disp = f"{int(num)}%" if num == int(num) else f"{num}%"
            except (TypeError, ValueError):
                val_disp = f"{val}%"
        else:
            val_disp = str(val)
        winners[col] = {
            "label": label,
            "value": val,
            "value_display": val_disp,
            "avg_roi": float(top_row["평균_자금대비순손익률"]),
            "max_roi": float(top_row["최고_자금대비순손익률"]),
            "avg_net": int(top_row["평균_순손익합계"]),
            "n": int(top_row["조합수"]),
        }
        lines.append(
            f"- **{label}**: `{val_disp}` 이 가장 유리 "
            f"(평균 자금대비 순손익률 **{top_row['평균_자금대비순손익률']:+.2f}%**, "
            f"최고 **{top_row['최고_자금대비순손익률']:+.2f}%**, "
            f"해당 설정 조합 {int(top_row['조합수'])}개)"
        )

    md = [
        "## 파라미터 요인 분석 리포트",
        "",
        f"분석 대상 조합 수: **{len(rows)}개**",
        "",
        "### 1. 전체 1위 조합 (자금대비 순손익률 최고)",
        "",
        f"- **조합**: {best.get('조합설명', '')}",
        f"- **1차 익절**: {best['1차익절(%)']}%",
        f"- **2차 익절**: {best['2차익절(%)']}%",
        f"- **손절**: {best['손절']}",
        f"- **시세**: {best['시세']}",
        f"- **탐색 기간**: {best['탐색기간']}",
        f"- **자금대비 순손익률**: **{best['자금투입대비순손익률(%)']:+.2f}%**",
        f"- **순손익 합계**: {best['순손익합계']:+,}원",
        f"- **승률**: {best['승률(%)']}% · 평균손익률 {best['평균손익률(%)']:+.2f}%",
        "",
        "### 2. 요인별 「평균 자금대비 순손익률」이 가장 높은 설정",
        "",
        "각 요인의 설정값별로 해당 값이 들어간 모든 조합의 평균을 비교했습니다.",
        "",
    ]
    md.extend(lines)
    md.extend([
        "",
        "### 3. 종합 권장 (요인별 최적값 요약)",
        "",
    ])
    if winners:
        md.append(
            f"| 요인 | 최적 설정 | 평균 자금대비 순손익률 | 최고 자금대비 순손익률 |\n"
            f"|------|----------|------------------------|------------------------|\n"
            + "\n".join(
                f"| {w['label']} | **{w['value_display']}** | "
                f"{w['avg_roi']:+.2f}% | {w['max_roi']:+.2f}% |"
                for w in winners.values()
            )
        )
        md.append("")
        md.append(
            "> ※ 요인별 최적값은 **다른 요인을 섞은 평균**이므로, "
            "전체 1위 조합의 설정과 다를 수 있습니다. "
            "실전 적용 시에는 **전체 1위 조합** 또는 **베스트 5**를 우선 참고하세요."
        )

    return {
        "summary_lines": lines,
        "markdown": "\n".join(md),
        "tables": tables,
        "winners": winners,
        "best_case": best,
    }


def render_factor_report(report: dict):
    """Streamlit에 요인 분석 리포트 표시"""
    if not report or not report.get("markdown"):
        st.info("분석할 조합 결과가 없습니다.")
        return

    st.subheader("📑 파라미터 요인 분석 리포트")
    st.markdown(report["markdown"])

    st.markdown("#### 요인별 상세 통계")
    for label, df in report.get("tables", {}).items():
        if df is None or df.empty:
            continue
        with st.expander(f"{label} — 설정값별 성과", expanded=False):
            st.caption(
                "정렬: 평균 자금대비 순손익률 → 최고 자금대비 순손익률 → 평균 순손익"
            )
            show = df.rename(columns={
                "평균_자금대비순손익률": "평균_자금대비순손익률(%)",
                "최고_자금대비순손익률": "최고_자금대비순손익률(%)",
            })
            st.dataframe(
                show,
                use_container_width=True,
                hide_index=True,
                column_config=_metric_col_config(),
            )


def main():
    st.set_page_config(
        page_title="파라미터 조합 시뮬레이션 (베스트5)",
        page_icon="🔬",
        layout="wide",
    )

    st.title("🔬 파라미터 전수 조합 시뮬레이션")
    st.caption(
        f"엔진 {APP_VERSION} · 1·2차 익절 × 손절 × 시세 × 탐색기간 전 조합 → "
        f"**자금투입대비 순손익률** 기준 베스트 {TOP_N}"
    )

    missing_env = validate_env()
    if missing_env:
        st.error(f".env 설정 필요: {', '.join(missing_env)}")
        st.stop()

    if "case_ticker_map" not in st.session_state:
        st.session_state.case_ticker_map = {}
    if "case_result_top" not in st.session_state:
        st.session_state.case_result_top = None
    if "case_all_metrics" not in st.session_state:
        st.session_state.case_all_metrics = None
    if "case_factor_report" not in st.session_state:
        st.session_state.case_factor_report = None
    if "case_per_stock_best" not in st.session_state:
        st.session_state.case_per_stock_best = None

    with st.sidebar:
        st.header("📋 종목 · 기본설정")
        input_method = st.radio("입력 방법", ["직접 입력", "파일 업로드"], horizontal=True)

        if input_method == "직접 입력":
            code_input = st.text_area(
                "종목코드 또는 종목명 (쉼표/줄바꿈)",
                placeholder="005930,삼성전자\n000660",
                height=100,
            )
            if st.button("종목 확인", key="case_btn_check"):
                entries = parse_stock_list(code_input)
                if entries:
                    api = KiwoomAPI()
                    with st.spinner("종목 확인 중..."):
                        success, failed = resolve_names_to_codes(api, entries)
                    st.session_state.case_ticker_map = success
                    st.session_state.case_result_top = None
                    if success:
                        st.success(f"{len(success)}개 확인")
                    if failed:
                        st.warning(
                            f"실패: {', '.join(failed)}\n"
                            "종목코드(예: 005930)로 입력하거나 잠시 후 다시 시도하세요."
                        )
                else:
                    st.warning("종목을 입력하세요.")
        else:
            uploaded = st.file_uploader("종목명 파일 (.txt / .md)", type=["txt", "md"])
            if uploaded is not None:
                raw = uploaded.read().decode("utf-8", errors="ignore")
                names = parse_stock_list(raw)
                st.caption(f"📄 {uploaded.name} · {len(names)}개 (실행 시 변환)")
                st.session_state.case_upload_raw = raw
            elif st.session_state.get("case_upload_raw"):
                n = len(parse_stock_list(st.session_state.case_upload_raw))
                st.caption(f"업로드됨 · {n}개 종목")

        if st.session_state.case_ticker_map:
            st.caption("확인된 종목:")
            for c, n in st.session_state.case_ticker_map.items():
                st.caption(f"  {c} | {n}")

        st.divider()
        st.subheader("기본 파라미터 (고정)")
        p = dict(DEFAULT_PARAMS)
        p["total_budget"] = st.number_input(
            "총 투자금액 (원)", 100_000, 100_000_000,
            int(p["total_budget"]), step=500_000, format="%d",
        )
        b = calc_budgets(p["total_budget"])
        st.caption(f"1차 {b['buy1']:,} / 2차 {b['buy2']:,} / 3차 {b['buy3']:,}")

        p["bb_period"] = st.number_input("BB 기간", 5, 60, int(p["bb_period"]))
        p["bb_multiplier"] = st.number_input(
            "BB 승수", 1.0, 5.0, float(p["bb_multiplier"]), 0.5
        )
        p["ma_period"] = st.number_input("이동평균 기간", 20, 480, int(p["ma_period"]), 10)

        ma_modes = ma_filter_mode_options()
        p["ma_filter_mode"] = st.radio("240일선 조건", ma_modes, horizontal=True)
        if p["ma_filter_mode"] == "240선추세":
            opts = ma_trend_days_options()
            p["ma_trend_days"] = st.selectbox("연속 상승 일수", opts, index=0)

        p["trailing_pct"] = 5
        p["abandon_pct"] = 30
        p["volume_ratio"] = 3.0
        st.caption(
            f"고정: 트레일링 {p['trailing_pct']}% · "
            f"매수포기 {p['abandon_pct']}% · {format_ma_filter(p)}"
        )

    cases = build_case_grid()
    st.info(
        f"조합 수: **{len(cases):,}개** "
        f"(익절쌍 × 손절 {len(STOP_OPTS)} × 시세 {len(PRICE_OPTS)} × "
        f"탐색 {len(SCAN_MONTH_OPTS)}) · "
        f"일봉은 시세별·최대 탐색기간으로 1회 수집 후, 조합별 일수로 잘라 재사용"
    )

    with st.expander("조합 상세"):
        st.markdown(
            f"""
- **1차 익절:** {PROFIT1_OPTS}
- **2차 익절:** {PROFIT2_OPTS} (1차보다 큰 값만)
- **손절:** 기준저가, 1차매수가대비 -5% / -10%
- **시세:** 정규장(KRX), 통합시세
- **탐색:** {[format_scan_months(m) for m in SCAN_MONTH_OPTS]}
- **전체 베스트:** 상위 {TOP_N} (자금대비 순손익률 → 순손익합계 → 동일 시 짧은 탐색만)
- **종목별 베스트:** 종목당 상위 {TOP_N_STOCK}
"""
        )

    ready = bool(st.session_state.case_ticker_map) or bool(
        st.session_state.get("case_upload_raw")
    )
    if not ready:
        st.warning("사이드바에서 종목을 확인하거나 파일을 업로드한 뒤 실행하세요.")
        st.stop()

    if st.button("▶ 전수 조합 시뮬레이션 실행", type="primary"):
        st.session_state.case_result_top = None
        st.session_state.case_all_metrics = None
        st.session_state.case_factor_report = None
        st.session_state.case_per_stock_best = None

        ticker_map = dict(st.session_state.case_ticker_map)
        if not ticker_map and st.session_state.get("case_upload_raw"):
            with st.spinner("종목코드 변환 중..."):
                api = KiwoomAPI()
                names = parse_stock_list(st.session_state.case_upload_raw)
                ticker_map, failed = resolve_names_to_codes(api, names)
                st.session_state.case_ticker_map = ticker_map
                if failed:
                    st.warning(f"매핑 실패 {len(failed)}개: {', '.join(failed)}")
        if not ticker_map:
            st.error("변환된 종목이 없습니다.")
            st.stop()

        api = KiwoomAPI()
        max_months = max(SCAN_MONTH_OPTS)
        data_days = calc_data_days(max_months, int(p["ma_period"]))
        st.write(
            f"대상 **{len(ticker_map)}종목** · 일봉 수집 {data_days:,}일 · "
            f"조합 {len(cases):,}개"
        )

        fetch_bar = st.progress(0, text="일봉 수집 준비")
        fetch_msg = st.empty()

        def fetch_progress(done, total, msg):
            pct = done / total if total else 0
            fetch_bar.progress(pct, text=f"{done}/{total}")
            fetch_msg.caption(msg)

        candle_cache = fetch_candle_cache(
            api, ticker_map, PRICE_OPTS, data_days, progress_cb=fetch_progress
        )
        fetch_bar.empty()
        fetch_msg.empty()

        empty_cnt = sum(1 for v in candle_cache.values() if not v)
        if empty_cnt:
            st.warning(f"일봉 없음 {empty_cnt}건 (해당 종목·시세는 빈행 처리)")

        st.subheader("⏳ 조합별 진행 결과 (실시간)")
        case_bar = st.progress(0, text="조합 시뮬 0%")
        case_status = st.empty()
        latest_box = st.empty()
        live_top_title = st.empty()
        live_top_panel = st.empty()
        live_recent_title = st.empty()
        live_recent_panel = st.empty()

        all_metrics: list[dict] = []
        total_cases = len(cases)

        for i, case in enumerate(cases):
            metrics, _df = run_one_case(case, p, ticker_map, candle_cache)
            metrics["_seq"] = i + 1
            all_metrics.append(metrics)

            done = i + 1
            pct = done / total_cases
            case_bar.progress(
                pct,
                text=f"조합 시뮬 {done}/{total_cases} ({pct * 100:.1f}%)",
            )
            case_status.info(
                f"**[{done}/{total_cases}]** 방금 완료: {_case_label(case)}"
            )

            # 방금 끝난 case 요약
            with latest_box.container():
                st.markdown("**방금 완료된 조합**")
                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("순손익", f"{metrics['순손익합계']:+,}원")
                m2.metric(
                    "자금대비 순손익률",
                    f"{metrics['자금투입대비순손익률(%)']:+.2f}%",
                )
                m3.metric("매수", f"{metrics['매수발생']}건")
                m4.metric("승률", f"{metrics['승률(%)']}%")
                m5.metric("평균손익률", f"{metrics['평균손익률(%)']:+.2f}%")
                st.caption(metrics["조합설명"])

            # 지금까지의 잠정 베스트 5
            live_top_title.markdown(
                f"**잠정 베스트 {TOP_N}** (완료 {done}/{total_cases})"
            )
            live_top_panel.dataframe(
                _live_df(all_metrics, limit=TOP_N),
                use_container_width=True,
                hide_index=True,
                column_config=_metric_col_config(),
            )

            # 최근 완료 목록 (진행 확인용)
            live_recent_title.markdown("**최근 완료 조합** (최신순 최대 15건)")
            live_recent_panel.dataframe(
                _recent_df(all_metrics, n=15),
                use_container_width=True,
                hide_index=True,
                column_config=_metric_col_config(),
            )

        case_bar.progress(1.0, text=f"조합 시뮬 {total_cases}/{total_cases} 완료")
        case_status.success(f"전수 조합 {total_cases}개 시뮬레이션 완료")

        top = rank_results(all_metrics, TOP_N)
        report = build_factor_report(all_metrics)
        per_stock = build_per_stock_best(all_metrics, ticker_map, TOP_N_STOCK)
        st.session_state.case_result_top = top
        st.session_state.case_all_metrics = all_metrics
        st.session_state.case_factor_report = report
        st.session_state.case_per_stock_best = per_stock
        st.success(
            f"완료 · 전체 베스트 {len(top)}개 · "
            f"종목별 베스트 {TOP_N_STOCK}개씩 선정"
        )

    top = st.session_state.case_result_top
    if not top:
        return

    # ── 요인 분석 리포트 (베스트5보다 먼저 요약) ──────────────
    report = st.session_state.case_factor_report
    if report is None and st.session_state.case_all_metrics:
        report = build_factor_report(st.session_state.case_all_metrics)
        st.session_state.case_factor_report = report
    if report:
        render_factor_report(report)

    # ── 종목별 베스트 3 ───────────────────────────────────────
    per_stock = st.session_state.case_per_stock_best
    if per_stock is None and st.session_state.case_all_metrics:
        per_stock = build_per_stock_best(
            st.session_state.case_all_metrics,
            st.session_state.case_ticker_map,
            TOP_N_STOCK,
        )
        st.session_state.case_per_stock_best = per_stock

    if per_stock and len(per_stock) >= 1:
        st.subheader(f"📌 종목별 베스트 {TOP_N_STOCK}")
        st.caption(
            "종목마다 독립 평가 · 순손익합계가 같으면 탐색기간이 짧은 조합만 표시"
        )
        for code, info in per_stock.items():
            name = info.get("name", code)
            stock_top = info.get("top") or []
            with st.expander(
                f"{name} ({code}) — 베스트 {len(stock_top)}건",
                expanded=(len(per_stock) == 1),
            ):
                if not stock_top:
                    st.info("해당 종목에서 유효한 조합 결과가 없습니다.")
                else:
                    st.dataframe(
                        metrics_table(stock_top),
                        use_container_width=True,
                        hide_index=True,
                        column_config=_metric_col_config(),
                    )
                    for row in stock_top:
                        st.markdown(
                            f"**#{row['순위']}** {_case_label(row['_case'])} · "
                            f"순손익 {row['순손익합계']:+,}원 · "
                            f"자금대비 {row['자금투입대비순손익률(%)']:+.2f}%"
                        )

    st.subheader(f"🏆 최종 베스트 {TOP_N} 조합 (전체 포트폴리오)")
    st.caption(
        "정렬: 자금투입대비 순손익률 → 순손익합계 "
        "→ 동일 순손익 시 짧은 탐색기간만 → 승률 → 평균손익률"
    )
    st.dataframe(
        metrics_table(top),
        use_container_width=True,
        hide_index=True,
        column_config=_metric_col_config(),
    )

    for row in top:
        with st.expander(
            f"#{row['순위']} {_case_label(row['_case'])}",
            expanded=(row["순위"] == 1),
        ):
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("순손익 합계", f"{row['순손익합계']:+,}원")
            c2.metric(
                "자금투입대비 순손익률",
                f"{row['자금투입대비순손익률(%)']:+.2f}%",
            )
            c3.metric("최대 동시 투입자금", f"{row['최대동시투입자금']:,}원")
            c4.metric(
                "매수 발생 / 승률",
                f"{row['매수발생']}건 / {row['승률(%)']}%",
            )

            df_case: pd.DataFrame = row["_df"]
            if df_case is not None and not df_case.empty:
                st.markdown("**종목별 요약**")
                st.dataframe(
                    summarize_by_stock(df_case),
                    use_container_width=True,
                    hide_index=True,
                )
                st.markdown("**거래 상세**")
                st.dataframe(
                    detail_trades_df(df_case),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("거래 데이터 없음")

    st.subheader("📥 결과 다운로드")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_sorted = sorted(
        st.session_state.case_all_metrics or [],
        key=lambda r: (
            r["자금투입대비순손익률(%)"],
            r["순손익합계"],
            r["승률(%)"],
            r["평균손익률(%)"],
        ),
        reverse=True,
    )
    df_all = metrics_table(all_sorted)
    if not df_all.empty:
        df_all = df_all.copy()
        df_all.insert(0, "전체순위", range(1, len(df_all) + 1))

    csv_buf = io.StringIO()
    csv_buf.write("[요인분석리포트]\n")
    if report and report.get("markdown"):
        csv_buf.write(report["markdown"].replace("**", "").replace("`", ""))
        csv_buf.write("\n\n")
    csv_buf.write("[베스트5]\n")
    metrics_table(top).to_csv(csv_buf, index=False)
    csv_buf.write("\n[전체조합]\n")
    df_all.to_csv(csv_buf, index=False)
    if report and report.get("tables"):
        for label, tdf in report["tables"].items():
            csv_buf.write(f"\n[요인_{label}]\n")
            tdf.to_csv(csv_buf, index=False)

    xlsx_buf = io.BytesIO()
    with pd.ExcelWriter(xlsx_buf, engine="openpyxl") as writer:
        # 리포트 요약 시트
        if report and report.get("winners"):
            summary_rows = [
                {
                    "구분": "전체1위조합",
                    "설정": report["best_case"].get("조합설명", ""),
                    "자금대비순손익률(%)": report["best_case"]["자금투입대비순손익률(%)"],
                    "순손익합계": report["best_case"]["순손익합계"],
                }
            ]
            for w in report["winners"].values():
                summary_rows.append({
                    "구분": f"요인최적_{w['label']}",
                    "설정": w["value_display"],
                    "자금대비순손익률(%)": w["avg_roi"],
                    "순손익합계": w["avg_net"],
                })
            pd.DataFrame(summary_rows).to_excel(
                writer, sheet_name="요인분석요약", index=False
            )
            for label, tdf in report["tables"].items():
                sheet = f"요인_{label}"[:31]
                tdf.to_excel(writer, sheet_name=sheet, index=False)
        metrics_table(top).to_excel(writer, sheet_name="베스트5", index=False)
        df_all.to_excel(writer, sheet_name="전체조합", index=False)
        if per_stock:
            for code, info in per_stock.items():
                stock_top = info.get("top") or []
                if not stock_top:
                    continue
                sheet = f"종목_{code}"[:31]
                metrics_table(stock_top).to_excel(writer, sheet_name=sheet, index=False)
        for row in top:
            sheet = f"상세_순위{row['순위']}"[:31]
            detail_trades_df(row["_df"]).to_excel(writer, sheet_name=sheet, index=False)
    xlsx_buf.seek(0)

    d1, d2 = st.columns(2)
    with d1:
        st.download_button(
            "📥 CSV (베스트5+전체조합)",
            data=csv_buf.getvalue().encode("utf-8-sig"),
            file_name=f"case_best5_{ts}.csv",
            mime="text/csv",
            key="case_csv",
        )
    with d2:
        st.download_button(
            "📥 Excel (베스트5+전체+상세)",
            data=xlsx_buf.getvalue(),
            file_name=f"case_best5_{ts}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="case_xlsx",
        )


if __name__ == "__main__":
    main()
