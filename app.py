r"""股票推荐回测 Web 应用(Streamlit 唯一入口)。

运行: .venv\Scripts\python.exe -m streamlit run app.py
"""
from datetime import datetime
import importlib
from pathlib import Path

import pandas as pd
import streamlit as st
import plotly.graph_objects as go

import config
import data_fetcher as fetcher
import backtest
from config import (TS_TOKEN, DB_PATH, INPUT_DIR, OUTPUT_DIR, EXCEL_COLUMNS,
                    REGIME_FILE_NAME, DEFAULT_INPUT_FILE, BENCHMARKS, BENCHMARK_COMBO)
from indicators import benchmark_navs
from rules import make_hold_n_days, make_regime_rule

st.set_page_config(page_title="股票推荐回测", layout="wide",
                   initial_sidebar_state="expanded")

# ====================== 配色(红涨绿跌,中国股市惯例) ======================
# 注:红绿对色觉障碍者区分度低,但涨跌方向已由柱形上下位置 + 数值文本双重编码,
# 不依赖颜色单独传达含义(领域惯例优先)。
RED = "#C62828"        # 涨
GREEN = "#2E7D32"      # 跌
NEUTRAL = "#1F4E79"    # 净值曲线
MARKER = "#888888"     # 日期推进虚线
GRID = "rgba(0,0,0,0.08)"
GREEN_FILL = "rgba(46,125,50,0.25)"
BENCH_COLORS = ["#E65100", "#6A1B9A", "#2E7D32", "#B71C1C", "#00838F"]  # 基准曲线配色


def _ensure_dirs():
    for d in (INPUT_DIR, OUTPUT_DIR, DB_PATH.parent):
        d.mkdir(parents=True, exist_ok=True)


# ====================== 输入解析 ======================
def parse_excel(source) -> dict:
    """读取输入 Excel(开头版格式)→ 推荐记录。source 为路径或上传文件对象。"""
    df = pd.read_excel(source, engine="openpyxl")
    name_col = EXCEL_COLUMNS["name"]
    date_col = EXCEL_COLUMNS["date"]
    missing = [c for c in EXCEL_COLUMNS.values() if c not in df.columns]
    if missing:
        raise ValueError(f"输入文件缺少必需列:{missing}")
    df = df.dropna(subset=[name_col, date_col]).copy()
    names = df[name_col].astype(str).str.strip()
    dates = pd.to_datetime(df[date_col], errors="coerce")
    dropped_dates = int(dates.isna().sum())
    df["_date"] = dates.dt.strftime("%Y-%m-%d")
    df = df[dates.notna()].reset_index(drop=True)
    names = names[dates.notna()].reset_index(drop=True)

    resolved = fetcher.resolve_names(names.tolist())
    rows, unmatched = [], []
    for i, name in enumerate(names):
        r = resolved.get(name)
        if r is None:
            unmatched.append(name)
            continue
        symbol, ts_code, _market, db_name = r
        rows.append({"ts_code": ts_code, "name": db_name,
                     "start_date": df["_date"].iloc[i]})
    return {"recs": pd.DataFrame(rows), "unmatched": unmatched,
            "dropped_dates": dropped_dates, "total": len(names)}


def parse_regime_file(path):
    """读仓位/防守文件(结构见 RULES.md):「仓位」sheet → 攻防区间;
    「防守」sheet → 防守列表,股票代码列有值直接推断代码,缺代码用名称匹配。"""
    xls = pd.ExcelFile(path, engine="openpyxl")
    pos_df = xls.parse("仓位")
    regime = []
    for _, r in pos_df.iterrows():
        s = pd.to_datetime(r.get("起始日期"), errors="coerce")
        e = pd.to_datetime(r.get("结束日期"), errors="coerce")
        p = pd.to_numeric(r.get("仓位"), errors="coerce")
        if pd.isna(s) or pd.isna(e) or pd.isna(p):
            continue
        regime.append((s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d"), float(p)))

    def_df = xls.parse("防守")
    def_df = def_df.dropna(subset=["起始日期"]).copy()
    def_df["_date"] = pd.to_datetime(def_df["起始日期"], errors="coerce").dt.strftime("%Y-%m-%d")
    def_df = def_df[def_df["_date"].notna()]
    need_resolve = []
    for _, r in def_df.iterrows():
        code = r.get("股票代码")
        if code is None or pd.isna(code) or not str(code).strip():
            raw_name = r.get("股票名称")
            name = "" if raw_name is None or pd.isna(raw_name) else str(raw_name).strip()
            if name:
                need_resolve.append(name)
    resolved = fetcher.resolve_names(need_resolve) if need_resolve else {}
    rows = []
    for _, r in def_df.iterrows():
        code = r.get("股票代码")
        raw_name = r.get("股票名称")
        name = "" if raw_name is None or pd.isna(raw_name) else str(raw_name).strip()
        ts_code = None
        if code is not None and not pd.isna(code) and str(code).strip():
            ts_code = fetcher.get_ts_code(str(code).strip())
        else:
            hit = resolved.get(name)
            if hit is None:
                continue
            ts_code, name = hit[1], hit[3]
        rows.append((ts_code, name, r["_date"]))
    # 同代码去重:同一标的写多行会开成两笔仓、权重翻倍,只保留起始日期最新的一行
    by_code = {}
    for ts_code, name, d in rows:
        if ts_code not in by_code or d > by_code[ts_code][2]:
            by_code[ts_code] = (ts_code, name, d)
    defense = [(f"D{k}", *row) for k, row in enumerate(by_code.values())]
    return {"regime": regime, "defense": defense}


def _parse_regime_into_state(path):
    """按文件修改时间缓存仓位/防守解析结果。"""
    mkey = f"regime:{path.name}:{path.stat().st_mtime_ns}"
    if st.session_state.get("_regime_mkey") == mkey:
        return
    try:
        st.session_state.regime_data = parse_regime_file(path)
        st.session_state._regime_mkey = mkey
    except Exception as e:
        st.error(f"仓位/防守文件解析失败:{e}")


# ====================== 策略参数(每次页面重跑重读;保存 config.py 后 ~2 秒自动生效) ======================
def _strategy_params():
    """主动 reload config 读策略参数。

    不用模块顶部 from-import:Python 缓存模块,页面重跑只会拿到旧值。
    config.py 是纯数据文件,reload 无副作用。token/路径变更仍需重启。
    """
    importlib.reload(config)
    return config.X_THRESHOLD, config.Y_OFFENSE, config.Z_DEFENSE, config.FEE


_CONFIG_PATH = Path(config.__file__)


@st.fragment(run_every=2)
def _watch_config():
    """每 2 秒检查 config.py 修改时间,变化则整页重跑(重读参数→缓存失效→重算)。

    必须用 fragment 轮询:st.tabs 切 tab、展开收起等是纯前端操作,不触发脚本重跑,
    仅靠交互永远等不到参数生效。fragment 定时器独立于主脚本,页面开着就一直监视。
    """
    mtime = _CONFIG_PATH.stat().st_mtime_ns
    prev = st.session_state.get("_cfg_mtime")
    st.session_state._cfg_mtime = mtime
    if prev is not None and mtime != prev:
        st.rerun(scope="app")


_watch_config()
@st.cache_data(show_spinner=False)
def _load_market(db_key: str, version: int) -> pd.DataFrame:
    return fetcher.load_market_data(db_key)


@st.cache_data(show_spinner=False)
def _load_trade_dates(db_key: str, version: int) -> list:
    return fetcher.load_trade_dates(db_key)


@st.cache_data(show_spinner=False)
def _ensure_benchmarks(db_key: str, bench_ver: int, start: str, codes: tuple) -> dict:
    """基准数据缺口自动补拉(按 bench_version/区间起点缓存,缺什么补什么)。"""
    return fetcher.ensure_benchmarks(list(codes), start)


@st.cache_data(show_spinner=False)
def _load_benchmarks(db_key: str, bench_ver: int, codes: tuple) -> pd.DataFrame:
    return fetcher.load_benchmark_data(list(codes))


def _engine_stamp() -> str:
    """引擎源码 mtime 指纹:backtest/rules/indicators 代码一改,缓存 key 随之变化,
    避免输入文件没变时页面继续用旧口径的回测结果(如改 bug 后买入日期仍旧值)。"""
    root = Path(__file__).parent
    return ";".join(f"{name}:{int((root / name).stat().st_mtime)}"
                    for name in ("backtest.py", "rules.py", "indicators.py"))


# 策略参数作为 _run_backtest 签名的一部分:参数值变化 → 缓存自动失效重算
def _build_rule_and_recs(recs_tuple: tuple, regime_key, x: float, y: int, z: int):
    """推荐元组 + 仓位/防守状态 → (并入防守行的 recs_all, 规则实例)。每次调用新建规则。"""
    recs = pd.DataFrame(list(recs_tuple),
                        columns=["ts_code", "name", "start_date"])
    if regime_key is None:
        return recs, make_hold_n_days(10)  # 仓位/防守文件缺失时的退回规则
    regime, defense = regime_key
    # 防守行并入 recs 交给引擎(pos 编号接在进攻之后),引擎才知道防守代码、能执行买入
    base = len(recs)
    defense_rows = [(base + i, ts_code, name, start_date)
                    for i, (_pid, ts_code, name, start_date) in enumerate(defense)]
    recs_all = pd.concat(
        [recs, pd.DataFrame([d[1:] for d in defense_rows],
                            columns=["ts_code", "name", "start_date"])],
        ignore_index=True)
    return recs_all, make_regime_rule(list(regime), defense_rows, x, y, z)


def _run_engine(recs_all, rule, db_key: str, version: int, fee: dict):
    market = _load_market(db_key, version)
    trade_dates = _load_trade_dates(db_key, version)
    return backtest.run_backtest(recs_all, market, trade_dates, rule=rule, fee=fee)


@st.cache_data(show_spinner=False)
def _run_backtest(recs_tuple: tuple, regime_key, db_key: str, version: int,
                  x: float, y: int, z: int, fee: dict, engine_ver: str = ""):
    recs_all, rule = _build_rule_and_recs(recs_tuple, regime_key, x, y, z)
    return _run_engine(recs_all, rule, db_key, version, fee)


@st.cache_data(show_spinner=False)
def _run_backtest_x0(recs_tuple: tuple, regime_key, db_key: str, version: int,
                     y: int, z: int, fee: dict, engine_ver: str = ""):
    """单押对比组合:X_THRESHOLD=0 的同规则回测(仓位表覆盖到的交易日永远进攻,
    持最近 y 条推荐;未覆盖日仍按防守兜底)。其余口径(费率/停牌顺延/等权满仓)
    与主回测一致,仅用于净值对比展示。"""
    recs_all, rule = _build_rule_and_recs(recs_tuple, regime_key, 0.0, y, z)
    _, portfolio, _ = _run_engine(recs_all, rule, db_key, version, fee)
    return portfolio


# ====================== 图表 ======================
def _base_layout(title: str, height: int, pct_y: bool) -> dict:
    return dict(
        title=dict(text=title, font=dict(size=14)),
        height=height, margin=dict(t=40, b=30, l=55, r=15),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(gridcolor=GRID, tickfont=dict(size=11)),
        yaxis=dict(gridcolor=GRID, tickfont=dict(size=11),
                   tickformat=".0%" if pct_y else None),
        hovermode="x unified", showlegend=False,
    )


def fig_daily_returns(portfolio: pd.DataFrame, sel_dt) -> go.Figure:
    colors = [RED if v >= 0 else GREEN for v in portfolio["account_return"]]
    fig = go.Figure(go.Bar(
        x=portfolio["_dt"], y=portfolio["account_return"], marker_color=colors,
        customdata=[[n] for n in portfolio["n_stocks"]],
        hovertemplate="%{x|%Y-%m-%d}<br>账户收益 %{y:.2%}<br>持仓 %{customdata[0]} 只<extra></extra>",
    ))
    fig.update_layout(**_base_layout("每日账户收益(红涨绿跌,柱形方向亦表涨跌)", 280, pct_y=True))
    fig.add_vline(x=sel_dt, line=dict(color=MARKER, width=1, dash="dash"))
    return fig


def fig_nav(portfolio: pd.DataFrame, sel_dt, bench: dict | None = None,
            dan: list | None = None) -> go.Figure:
    """组合净值曲线(粗线)+ 单押对照(虚线)+ 基准同期对比曲线(细线)。

    bench: {名称: 净值序列},由 indicators.benchmark_navs 按区间重定基(首日=1)。
    dan:   单押(X_THRESHOLD=0)净值序列,与组合同区间重定基(首日=1)。
    单击图例项可隐藏/恢复该曲线(Plotly 原生交互),双击只看单条。
    """
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=portfolio["_dt"], y=portfolio["nav"], mode="lines", name="组合",
        line=dict(color=NEUTRAL, width=3),
        hovertemplate="%{x|%Y-%m-%d}<br>组合 %{y:.4f}<extra></extra>",
    ))
    if dan is not None:
        fig.add_trace(go.Scatter(
            x=portfolio["_dt"], y=dan, mode="lines", name="单押",
            line=dict(color="#1565C0", width=1.8, dash="dash"),
            hovertemplate="%{x|%Y-%m-%d}<br>单押 %{y:.4f}<extra></extra>",
        ))
    for i, (name, ys) in enumerate((bench or {}).items()):
        fig.add_trace(go.Scatter(
            x=portfolio["_dt"], y=ys, mode="lines", name=name,
            line=dict(color=BENCH_COLORS[i % len(BENCH_COLORS)], width=1.4),
            hovertemplate=f"%{{x|%Y-%m-%d}}<br>{name} %{{y:.4f}}<extra></extra>",
        ))
    fig.update_layout(**_base_layout("组合累计净值(区间起点 1,点击图例显隐)", 320, pct_y=False))
    fig.update_layout(showlegend=bool(bench) or dan is not None,
                      legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                  xanchor="right", x=1, font=dict(size=11)))
    fig.add_hline(y=1, line=dict(color="#999999", width=1, dash="dot"))
    fig.add_vline(x=sel_dt, line=dict(color=MARKER, width=1, dash="dash"))
    return fig


def fig_drawdown(portfolio: pd.DataFrame, sel_dt) -> go.Figure:
    fig = go.Figure(go.Scatter(
        x=portfolio["_dt"], y=portfolio["drawdown"], mode="lines",
        line=dict(color=GREEN, width=2), fill="tozeroy", fillcolor=GREEN_FILL,
        hovertemplate="%{x|%Y-%m-%d}<br>回撤 %{y:.2%}<extra></extra>",
    ))
    fig.update_layout(**_base_layout("组合最大回撤", 280, pct_y=True))
    fig.add_vline(x=sel_dt, line=dict(color=MARKER, width=1, dash="dash"))
    return fig


# ====================== 表格着色 ======================
def _pct_cell(v):
    if v is None or pd.isna(v):
        return ""
    if v > 0.01:
        return "background-color: #FFE6E6; color: #B71C1C"
    if v < -0.01:
        return "background-color: #E6FFE6; color: #1B5E20"
    return "background-color: #F5F5F5"


def style_holdings(df: pd.DataFrame):
    """对已重命名为中文列名的表着色:红涨绿跌,百分比格式。"""
    pct_cols = [c for c in df.columns if c in ("当日收益率", "累计收益")]
    fmt = {c: "{:.2%}" for c in pct_cols}
    if "仓位占比" in df.columns:
        fmt["仓位占比"] = "{:.1%}"
    return df.style.map(_pct_cell, subset=pct_cols).format(fmt)


# ====================== 日期导航(每日持仓/组合净值共用) ======================
def _nearest_trade_date(dates: list[str], d: str) -> str:
    """把任意日历日期就近对齐到交易日(两侧距离相等取较早的一天),超范围夹到边界。"""
    if d <= dates[0]:
        return dates[0]
    if d >= dates[-1]:
        return dates[-1]
    idx = next((i for i, x in enumerate(dates) if x >= d), len(dates) - 1)
    if dates[idx] == d:
        return d
    if idx == 0:
        return dates[0]
    before, after = dates[idx - 1], dates[idx]
    t = pd.Timestamp(d)
    return before if (t - pd.Timestamp(before)) <= (pd.Timestamp(after) - t) else after


def _date_nav(prefix: str, dates: list[str], label: str,
              with_end: bool = True) -> tuple[str, str, str]:
    """日期选择控件 + 滑块。

    with_end=False(每日持仓):单个「日期」卡片即所选日期,与滑块双向同步——
      改卡片 → 滑块/持仓跟随(非交易日就近对齐到交易日);拖滑块 → 卡片跟随。
    with_end=True(组合净值):开始/结束日期定义展示区间,滑块在区间内选日,
      图表随区间截取,净值按区间重定基。
    prefix 区分两个 tab 的控件 key;只影响展示,不影响回测。
    返回 (选中日期, 开始日期, 结束日期),均为 'YYYY-MM-DD' 字符串。
    """
    ss = st.session_state
    dset = set(dates)
    first, last = dates[0], dates[-1]
    sel_key = f"{prefix}_sel"

    if sel_key not in ss or ss[sel_key] not in dset:
        ss[sel_key] = last            # 首次/数据刷新后默认最后交易日

    if not with_end:
        # 每日持仓:卡片与滑块选同一天,on_change 回调在下一次运行前触发,实现双向同步
        date_key = f"{prefix}_date"
        if date_key not in ss:
            ss[date_key] = pd.Timestamp(last).date()

        def _on_card():
            ss[sel_key] = _nearest_trade_date(dates, ss[date_key].isoformat())

        def _on_slider():
            ss[date_key] = pd.Timestamp(ss[sel_key]).date()

        date_val = st.date_input("日期", min_value=pd.Timestamp(first).date(),
                                 max_value=pd.Timestamp(last).date(),
                                 format="YYYY-MM-DD", key=date_key, on_change=_on_card)
        sel = st.select_slider(label, options=dates, key=sel_key, on_change=_on_slider)
        return sel, str(date_val), last

    start_key = f"{prefix}_start"
    end_key = f"{prefix}_end"
    if start_key not in ss:
        ss[start_key] = pd.Timestamp(first).date()
    if end_key not in ss:
        ss[end_key] = pd.Timestamp(last).date()

    c0, c1 = st.columns(2)
    with c0:
        start_val = st.date_input("开始日期", min_value=pd.Timestamp(first).date(),
                                  max_value=pd.Timestamp(last).date(),
                                  format="YYYY-MM-DD", key=start_key)
    with c1:
        end_val = st.date_input("结束日期", min_value=pd.Timestamp(first).date(),
                                max_value=pd.Timestamp(last).date(),
                                format="YYYY-MM-DD", key=end_key)

    start_str, end_str = str(start_val), str(end_val)
    start_idx = next((i for i, d in enumerate(dates) if d >= start_str), len(dates) - 1)
    end_idx = next((i for i, d in enumerate(dates) if d > end_str),
                   len(dates)) - 1      # <= 结束日期的最后一个交易日
    if end_idx < start_idx:
        end_idx = start_idx             # 开始 > 结束:夹成同一天,避免滑块范围为空

    # 范围变化后,选中日期被挤出则夹回边界
    if ss[sel_key] > dates[end_idx]:
        ss[sel_key] = dates[end_idx]
    elif ss[sel_key] < dates[start_idx]:
        ss[sel_key] = dates[start_idx]

    sel = st.select_slider(label, options=dates[start_idx:end_idx + 1], key=sel_key)
    return sel, start_str, end_str


# ====================== 主流程 ======================
_ensure_dirs()
fetcher.init_db()

st.title("📊 股票推荐回测")

# ---------- 侧边栏 ----------
def parse_into_state(path):
    """按文件修改时间缓存解析结果,避免每次交互都重读 Excel。"""
    mkey = f"{path.name}:{path.stat().st_mtime_ns}"
    if st.session_state.get("_parsed_mkey") == mkey:
        return
    try:
        result = parse_excel(path)
    except PermissionError:
        st.error("文件被 Excel 占用,请关闭后重试")
        return
    except Exception as e:
        st.error(f"解析失败:{e}")
        return
    st.session_state.recs = result["recs"]
    st.session_state.unmatched = result["unmatched"]
    st.session_state.dropped = result["dropped_dates"]
    st.session_state._parsed_mkey = mkey
    st.session_state._parse_msg = (f"已解析 {result['total']} 行 → {len(result['recs'])} 只有效股票"
                                   f"({len(result['unmatched'])} 只未匹配)")


with st.sidebar:
    st.markdown("### ① 输入 Excel")
    uploaded = st.file_uploader("上传 Excel(xlsx)", type=["xlsx"])
    existing_files = sorted(f for f in INPUT_DIR.glob("*.xlsx")
                            if not f.name.startswith("~$"))  # 排除 Excel 锁文件
    existing_names = ["— 不选择 —"] + [f.name for f in existing_files]
    # 默认选中 config.py DEFAULT_INPUT_FILE(浏览器刷新后 session 清空,index 重新生效)
    default_idx = (existing_names.index(DEFAULT_INPUT_FILE)
                   if DEFAULT_INPUT_FILE in existing_names else 0)
    existing = st.selectbox("或选择 input/ 目录已有文件", existing_names,
                            index=default_idx, key="sel_existing")

    if uploaded is not None:
        _ensure_dirs()
        saved = INPUT_DIR / uploaded.name
        saved.write_bytes(uploaded.getbuffer())
        parse_into_state(saved)
    elif existing != "— 不选择 —":
        parse_into_state(INPUT_DIR / existing)
    if msg := st.session_state.get("_parse_msg"):
        st.success(msg)

    recs = st.session_state.get("recs")

    # 仓位/防守文件(RULES.md):固定读 input/ 下配置名,改文件后自动重解析
    regime_path = INPUT_DIR / REGIME_FILE_NAME
    if regime_path.exists():
        _parse_regime_into_state(regime_path)
    else:
        st.warning(f"未找到 {REGIME_FILE_NAME},将退回「持有10个交易日」规则")
    regime_data = st.session_state.get("regime_data")

    st.markdown("### ② 行情数据")
    if not TS_TOKEN:
        st.error("config.py 中 TS_TOKEN 为空。请把旧脚本第 55 行的 token 粘贴到 config.py 后重启。")
    else:
        if st.button("初始化/更新行情数据", width="stretch"):
            if recs is None or recs.empty:
                st.warning("请先上传/选择 Excel")
            else:
                with st.status("更新行情数据中...", expanded=True) as status:
                    try:
                        fetch_all = recs
                        if regime_data:
                            defense_recs = pd.DataFrame(
                                regime_data["defense"],
                                columns=["pos", "ts_code", "name", "start_date"])
                            fetch_all = pd.concat([recs, defense_recs], ignore_index=True)
                        min_date = fetch_all["start_date"].min()
                        fetcher.ensure_trade_calendar(min_date,
                                                      datetime.now().strftime("%Y-%m-%d"))
                        bar = st.progress(0.0)
                        txt = st.empty()

                        def cb(i, n, name):
                            bar.progress((i + 1) / n)
                            txt.text(f"拉取 {name} ({i + 1}/{n})")

                        stats = fetcher.update_market_data(fetch_all, progress_cb=cb)
                        bar.progress(1.0)
                        txt.text("完成")
                        st.write(f"更新 {stats['updated']} 只股票,新增 {stats['new_rows']} 行,"
                                 f"耗时 {stats['elapsed']} 秒")
                        if stats["skipped"]:
                            st.warning(f"本次跳过 {len(stats['skipped'])} 只,下次点更新自动补拉:"
                                       f"{[s['name'] for s in stats['skipped']][:20]}")
                        status.update(label="更新完成", state="complete")
                    except Exception as e:
                        status.update(label="更新失败", state="error")
                        st.error(f"tushare 连接失败,请检查 config.py 的 TS_TOKEN 和积分:{str(e)[:200]}")

    st.markdown("### ③ 缓存状态")
    db_stats = fetcher.get_db_stats()
    st.caption(f"日线行数:{db_stats['bars']}  |  股票数:{db_stats['codes']}  |  "
               f"最新行情:{db_stats['latest']}")
    st.caption(f"名称表:{db_stats['names']} 只(更新于 {db_stats['name_map_update']})")
    st.caption(f"上次行情更新:{db_stats['last_update']}")
    if TS_TOKEN and st.button("刷新股票名称表(改过名字/新股票匹配不上时点)", width="stretch"):
        try:
            fetcher.ensure_name_map(force=True)
            st.rerun()
        except Exception as e:
            st.error(f"刷新失败:{str(e)[:200]}")

# ---------- 主区域 ----------
if not TS_TOKEN:
    st.warning("⚠️ 请先在 config.py 填好 tushare token,然后重启应用。")
elif recs is None:
    st.info("👈 请先在侧边栏上传/选择输入 Excel。")
else:
    version = fetcher.get_data_version()
    db_key = str(DB_PATH)
    recs_tuple = tuple(recs[["ts_code", "name", "start_date"]]
                       .itertuples(index=False, name=None))
    regime_key = None
    if regime_data:
        regime_key = (tuple(regime_data["regime"]), tuple(regime_data["defense"]))
    x_th, y_off, z_def, fee = _strategy_params()
    daily_holdings, portfolio, summary = _run_backtest(
        recs_tuple, regime_key, db_key, version, x_th, y_off, z_def, fee,
        engine_ver=_engine_stamp())

    if portfolio.empty:
        st.info("数据库中还没有行情数据。点击侧边栏「初始化/更新行情数据」按钮。")

    tab1, tab2, tab3, tab4 = st.tabs(["每日持仓", "组合净值", "回测明细", "导出"])

    dates = portfolio["date"].tolist() if not portfolio.empty else []
    portfolio["_dt"] = pd.to_datetime(portfolio["date"])

    # ---------- Tab1 每日持仓 ----------
    with tab1:
        if not dates:
            st.info("暂无数据")
        else:
            sel_date, _, _ = _date_nav("d1", dates, "选择日期(展示当日交易与持仓)",
                                       with_end=False)
            day_df = daily_holdings[daily_holdings["date"] == sel_date]
            st.session_state.sel_date = sel_date
            st.session_state.sel_day_df = day_df
            prow = portfolio[portfolio["date"] == sel_date].iloc[0]
            m1, m2, m3 = st.columns(3)
            m1.metric("当日账户收益率", f"{prow['account_return']:.2%}")
            m2.metric("收盘持仓", f"{int(prow['n_stocks'])} 只")
            m3.metric("当日费用", f"{prow['fee']:.4%}")
            if day_df.empty:
                st.info(f"{sel_date} 当日无交易、无持仓")
            else:
                show = day_df[["name", "code", "action", "weight", "day_return",
                               "cum_return", "rec_date", "entry_date", "trade_days"]].copy()
                show["操作"] = show["action"].map({"buy": "买入", "sell": "卖出", "hold": "持有"})
                show = show[["name", "code", "操作", "weight", "day_return", "cum_return",
                             "rec_date", "entry_date", "trade_days"]]
                show.columns = ["股票名称", "代码", "操作", "仓位占比", "当日收益率",
                                "累计收益", "推荐日期", "买入日期", "交易天数"]
                order = {"卖出": 0, "买入": 1, "持有": 2}
                show = show.sort_values(
                    ["操作", "累计收益"], ascending=[True, False], na_position="last",
                    key=lambda s: s.map(order) if s.name == "操作" else s)
                st.dataframe(style_holdings(show), width="stretch",
                             hide_index=True)
                st.caption(f"仓位占比 = 等权 1/收盘持仓数,卖出的股票计 0%;"
                           f"当日收益率:卖出=开盘涨跌幅,当日新买=实体涨幅,持有=全天涨跌幅(收盘/昨收-1);"
                           f"账户收益率 = (1+昨日持仓今日平均开盘涨跌幅)×(1+今日持仓平均实体涨幅)-1;"
                           f"累计收益自买入日起复利(买入日实体涨幅、之后全日涨跌幅)")

    # ---------- Tab2 组合净值 ----------
    with tab2:
        if not dates:
            st.info("暂无数据")
        else:
            sel_dt, start_str, end_str = _date_nav("d2", dates, "推进日期")
            sel_dt_v = pd.to_datetime(sel_dt)
            # 图表跟随起止日期;净值按区间重定基:开始日 = 1,回撤按区间内峰值重算
            p_show = portfolio[(portfolio["date"] >= start_str)
                               & (portfolio["date"] <= end_str)].copy()
            if not p_show.empty and p_show["nav"].iloc[0]:
                nav_rel = p_show["nav"] / p_show["nav"].iloc[0]
                p_show["nav"] = nav_rel
                p_show["drawdown"] = nav_rel / nav_rel.cummax() - 1
            row = p_show[p_show["date"] == sel_dt].iloc[0]
            # 单押对照(X_THRESHOLD=0):与组合同区间、同口径重定基(首日=1)
            dan_nav = None
            if regime_key is not None:
                try:
                    dan_pf = _run_backtest_x0(recs_tuple, regime_key, db_key, version,
                                              y_off, z_def, fee, engine_ver=_engine_stamp())
                    dan_show = dan_pf[(dan_pf["date"] >= start_str)
                                      & (dan_pf["date"] <= end_str)]
                    if not dan_show.empty and dan_show["nav"].iloc[0]:
                        dan_nav = dan_show["nav"] / dan_show["nav"].iloc[0]
                except Exception as e:
                    st.warning(f"单押对照计算失败(不影响回测):{str(e)[:150]}")
            cols = st.columns(6 if dan_nav is not None else 5)
            cols[0].metric("净值", f"{row['nav']:.4f}")
            cols[1].metric("当日收益", f"{row['account_return']:.2%}")
            cols[2].metric("回撤", f"{row['drawdown']:.2%}")
            cols[3].metric("持仓数", f"{int(row['n_stocks'])} 只")
            cols[4].metric("当日费用", f"{row['fee']:.4%}")
            if dan_nav is not None:
                excess = row["nav"] - float(dan_nav.loc[row.name])
                cols[5].metric("单押超额(净值差)", f"{excess:+.2%}",
                               help="所选日期 组合净值 − 单押净值(均按区间起点重定基为 1),"
                                    "即攻防转换相对“一直满仓押推荐”的超额;负值代表跑输单押")
            st.plotly_chart(fig_daily_returns(p_show, sel_dt_v),
                            width="stretch")
            # 基准对比:数据缺口自动补拉(失败只警告,不影响回测);曲线与组合同区间重定基
            codes_all = tuple(BENCHMARKS) + tuple(c for cs in BENCHMARK_COMBO.values()
                                                  for c in cs)
            bench = {}
            try:
                _ensure_benchmarks(db_key, fetcher.get_bench_version(), dates[0], codes_all)
                bench_df = _load_benchmarks(db_key, fetcher.get_bench_version(), codes_all)
                bench = benchmark_navs(bench_df, p_show["date"].tolist(),
                                       BENCHMARKS, BENCHMARK_COMBO)
            except Exception as e:
                st.warning(f"基准数据加载失败(不影响回测):{str(e)[:150]}")
            st.plotly_chart(fig_nav(p_show, sel_dt_v, bench,
                                    dan=dan_nav.tolist() if dan_nav is not None else None),
                            width="stretch")
            st.plotly_chart(fig_drawdown(p_show, sel_dt_v), width="stretch")

    # ---------- Tab3 回测明细 ----------
    with tab3:
        if summary.empty:
            st.info("暂无数据")
        else:
            show = summary.sort_values("cum_return", ascending=False,
                                       na_position="last").rename(columns={
                "name": "股票名称", "code": "代码", "rec_date": "推荐日期",
                "entry_date": "买入日期", "last_date": "最后交易日",
                "n_trade_days": "交易天数", "cum_return": "累计收益",
                "status": "状态"})
            st.dataframe(style_holdings(show), width="stretch", hide_index=True)
            issues = []
            if st.session_state.get("unmatched"):
                issues.append(f"未匹配代码(已排除):{'、'.join(dict.fromkeys(st.session_state.unmatched))}")
            if st.session_state.get("dropped"):
                issues.append(f"起始日期无效(已丢弃 {st.session_state.dropped} 行)")
            no_data = summary[summary["status"] == "无行情数据"]
            if not no_data.empty:
                issues.append(f"无行情数据:{'、'.join(dict.fromkeys(no_data['name']))}")
            not_entered = summary[summary["status"] == "未进场"]
            if not not_entered.empty:
                issues.append(f"未进场 {len(not_entered)} 条(有行情但未进入最近 y 条目标窗口,"
                              f"多为防守期发布的推荐,策略上不买入):"
                              f"{'、'.join(dict.fromkeys(not_entered['name']))[:120]}…")
            pending = summary[summary["status"] == "未到买入日"]
            if not pending.empty:
                issues.append(f"未到买入日(推荐日晚于最新行情):{'、'.join(dict.fromkeys(pending['name']))}")
            if issues:
                with st.expander(f"问题清单({len(issues)})"):
                    for s in issues:
                        st.write(s)

    # ---------- Tab4 导出 ----------
    with tab4:
        st.markdown("导出文件写入 output/ 目录")

        def _write_export(df: pd.DataFrame, prefix: str):
            _ensure_dirs()
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = OUTPUT_DIR / f"{prefix}_{ts}.xlsx"
            df.to_excel(path, index=False, engine="openpyxl")
            return path

        c1, c2 = st.columns(2)
        with c1:
            if st.button("导出当日持仓表", width="stretch"):
                sel = st.session_state.get("sel_date")
                day_df = st.session_state.get("sel_day_df")
                if sel is not None and day_df is not None and not day_df.empty:
                    path = _write_export(day_df, f"持仓_{sel}")
                    st.success(f"已导出:{path}")
                    st.download_button("下载", path.read_bytes(),
                                       file_name=path.name)
                else:
                    st.info("请先在「每日持仓」页选择有持仓的日期")
        with c2:
            if st.button("导出回测报表(3 个 sheet)", width="stretch"):
                if not summary.empty:
                    path = _write_export(pd.DataFrame(), "回测报表")
                    with pd.ExcelWriter(path, engine="openpyxl") as writer:
                        summary.to_excel(writer, sheet_name="个股汇总", index=False)
                        portfolio.drop(columns=["_dt"]).to_excel(
                            writer, sheet_name="每日组合", index=False)
                        daily_holdings.to_excel(writer, sheet_name="每日持仓明细", index=False)
                    st.success(f"已导出:{path}")
                    st.download_button("下载", path.read_bytes(),
                                       file_name=path.name)
                else:
                    st.info("暂无数据可导出")
