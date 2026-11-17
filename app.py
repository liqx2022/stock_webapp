r"""股票推荐回测 Web 应用(Streamlit 唯一入口)。

运行: .venv\Scripts\python.exe -m streamlit run app.py
"""
from datetime import datetime, timedelta
import importlib
import json
from pathlib import Path

import pandas as pd
import streamlit as st
import plotly.graph_objects as go

import config
import data_fetcher as fetcher
import backtest
from config import (TS_TOKEN, DB_PATH, INPUT_DIR, OUTPUT_DIR, EXCEL_COLUMNS,
                    REGIME_FILE_NAME, DEFAULT_INPUT_FILE, BENCHMARKS, BENCHMARK_COMBO)
from indicators import benchmark_navs, body_return, daily_return
from rules import (make_fixed_hold_rule, make_hold_n_days, make_regime_rule,
                   make_slot_rule, prev_gain_discard, slot_capacity, target_keys)

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

# ====================== 回测算法(侧边栏可切换) ======================
ALGO_REGIME = "攻防转换"          # 仓位表攻防切换,持最近 y/z 条推荐(参数在 config.py)
ALGO_FIXED = "固定持有N日等权"     # 推荐起始日开盘买入,持有 N 个交易日,第 N+1 日开盘卖出,每日等权
ALGO_SLOT = "限仓轮换"            # 每只固定 W 成仓位(其余现金),FIFO 新老替换,持有 D 个交易日到期全卖


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
    防御:长时间运行的 server 进程多次热重载后,命名空间里的 config 可能是
    __spec__ 丢失的旧模块对象(实测 reload 报 "module config not in
    sys.modules"),先按名重取;sys.modules 条目同样损坏时强制重导。
    """
    import config
    if getattr(config, "__spec__", None) is None:
        import sys
        sys.modules.pop("config", None)
        import config
    importlib.reload(config)
    return (config.X_THRESHOLD, config.Y_OFFENSE, config.Z_DEFENSE,
            config.FEE, config.OPEN_GAP_SKIP, config.PREV_DAY_GAIN_SKIP,
            config.MAX_PER_DAY, config.LIMIT_UP_SKIP)


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
def _bars_by_date(db_key: str, version: int) -> dict:
    """{date: {ts_code: bar}} 形式的行情(交易计划页前收涨幅过滤用,带缓存)。"""
    return backtest._build_bars_by_date(_load_market(db_key, version))


@st.cache_data(show_spinner=False)
def _ensure_benchmarks(db_key: str, bench_ver: int, start: str, codes: tuple) -> dict:
    """基准数据缺口自动补拉(按 bench_version/区间起点缓存,缺什么补什么)。"""
    return fetcher.ensure_benchmarks(list(codes), start)


@st.cache_data(show_spinner=False)
def _load_benchmarks(db_key: str, bench_ver: int, codes: tuple) -> pd.DataFrame:
    return fetcher.load_benchmark_data(list(codes))


def _engine_stamp() -> str:
    """引擎源码 mtime 指纹:backtest/rules/indicators/app 代码一改,缓存 key 随之变化,
    避免输入文件没变时页面继续用旧口径的回测结果(如改 bug 后买入日期仍旧值)。
    app.py 也计入:_build_rule_and_recs 在 app.py 里,规则构造口径变了同样要重算。"""
    root = Path(__file__).parent
    return ";".join(f"{name}:{int((root / name).stat().st_mtime)}"
                    for name in ("backtest.py", "rules.py", "indicators.py",
                                 "app.py"))


# 策略参数作为 _run_backtest 签名的一部分:参数值变化 → 缓存自动失效重算
def _build_rule_and_recs(recs_tuple: tuple, regime_key, x: float, y: int, z: int,
                         gap: float | None = None, prev_gain: float | None = None,
                         mpd: int | None = None, limit_fill: bool = False,
                         algo: str = ALGO_REGIME, hold_days: int = 5,
                         slot_w: float = 0.1, slot_d: int = 10):
    """推荐元组 + 仓位/防守状态 → (并入防守行的 recs_all, 规则实例)。每次调用新建规则。

    algo=ALGO_FIXED 时只用 Excel 推荐池(防守列表与攻防过滤参数不适用),
    规则为固定持有 N 日等权(见 rules.make_fixed_hold_rule)。
    algo=ALGO_SLOT 时同日同股合并(名称+推荐日期相同只占一个仓位)后,
    规则为限仓轮换(见 rules.make_slot_rule):每只 slot_w 成、FIFO 新老替换、
    持有 slot_d 个交易日后到期全卖;引擎按真实成数加权计净值,其余资金为现金。
    """
    recs = pd.DataFrame(list(recs_tuple),
                        columns=["ts_code", "name", "start_date"])
    if algo == ALGO_FIXED:
        return recs, make_fixed_hold_rule(int(hold_days))
    if algo == ALGO_SLOT:
        # 同日同股合并:名称+推荐日期相同的多行只占一个仓位(保留首行)
        recs = recs.drop_duplicates(subset=["ts_code", "start_date"],
                                    keep="first").reset_index(drop=True)
        # slot_w 已是小数成数(0.1 = 1 成,调用方从页面"成"换算),直接进规则/引擎
        return recs, make_slot_rule(float(slot_w), int(slot_d))
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
    return recs_all, make_regime_rule(list(regime), defense_rows, x, y, z, gap,
                                      prev_gain, mpd, limit_fill)


def _run_engine(recs_all, rule, db_key: str, version: int, fee: dict):
    market = _load_market(db_key, version)
    trade_dates = _load_trade_dates(db_key, version)
    return backtest.run_backtest(recs_all, market, trade_dates, rule=rule, fee=fee)


@st.cache_data(show_spinner=False)
def _run_backtest(recs_tuple: tuple, regime_key, db_key: str, version: int,
                  x: float, y: int, z: int, gap: float | None,
                  prev_gain: float | None, mpd: int | None, limit_fill: bool,
                  fee: dict, algo: str = ALGO_REGIME, hold_days: int = 5,
                  slot_w: float = 0.1, slot_d: int = 10, engine_ver: str = ""):
    recs_all, rule = _build_rule_and_recs(recs_tuple, regime_key, x, y, z, gap,
                                          prev_gain, mpd, limit_fill,
                                          algo, hold_days, slot_w, slot_d)
    return _run_engine(recs_all, rule, db_key, version, fee)


@st.cache_data(show_spinner=False)
def _run_backtest_x0(recs_tuple: tuple, regime_key, db_key: str, version: int,
                     y: int, z: int, gap: float | None, prev_gain: float | None,
                     mpd: int | None, limit_fill: bool, fee: dict,
                     engine_ver: str = ""):
    """单押对比组合:X_THRESHOLD=0 的同规则回测(仓位表覆盖到的交易日永远进攻,
    持最近 y 条推荐;未覆盖日仍按防守兜底)。其余口径(开盘涨幅/前收涨幅过滤/
    单日上限/涨跌停可成交性/费率/停牌顺延/等权满仓)与主回测一致,仅用于净值
    对比展示。"""
    recs_all, rule = _build_rule_and_recs(recs_tuple, regime_key, 0.0, y, z, gap,
                                          prev_gain, mpd, limit_fill)
    _, portfolio, _ = _run_engine(recs_all, rule, db_key, version, fee)
    return portfolio


# ====================== 图表 ======================
def _base_layout(title: str, height: int, pct_y: bool) -> dict:
    return dict(
        title=dict(text=title, font=dict(size=14)),
        height=height, margin=dict(t=40, b=30, l=55, r=15),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(gridcolor=GRID, tickfont=dict(size=11),
                   tickformat="%m-%d"),   # 坐标只显示 月-日(如 11-02),悬停仍看完整日期
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


def _base_nav_before(pf: pd.DataFrame, start_str: str) -> float:
    """区间重定基基准:开始日**前一交易日**的收盘净值,区间首日自身的当日收益计入曲线;
    开始日已是全历史首日(无前一日)时为 1.0(引擎初始净值,首日收益从绝对起点起算)。"""
    prev = pf[pf["date"] < start_str]
    base = float(prev["nav"].iloc[-1]) if len(prev) else 1.0
    return base or 1.0


def fig_nav(portfolio: pd.DataFrame, sel_dt, bench: dict | None = None,
            dan: list | None = None) -> go.Figure:
    """组合净值曲线(粗线)+ 单押对照(虚线)+ 基准同期对比曲线(细线)。

    bench: {名称: 净值序列},与组合同区间、按前一交易日收盘重定基(=1)。
    dan:   单押(X_THRESHOLD=0)净值序列,与组合同区间、同口径重定基。
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
    fig.update_layout(**_base_layout("组合累计净值(区间前一交易日=1,点击图例显隐)", 320, pct_y=False))
    fig.update_layout(showlegend=bool(bench) or dan is not None,
                      legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                  xanchor="right", x=1, font=dict(size=11)))
    fig.add_hline(y=1, line=dict(color="#999999", width=1, dash="dot"))
    fig.add_vline(x=sel_dt, line=dict(color=MARKER, width=1, dash="dash"))
    return fig


# ====================== 图例显隐记忆(隐形桥接组件) ======================
# 1.67 的 st.plotly_chart 直接把 Plotly 画在主文档(.js-plotly-plot,无 iframe)。
# 本组件不渲染任何可见内容、不碰图表本身,只在浏览器里定位净值图 div:
# 加载后按 ui_prefs 应用上次显隐,并监听 plotly_restyle(图例单击/双击都触发)
# 回传可见曲线名,由 Python 写 ui_prefs.json。图例点击从此有记忆。
# 失败模式:若未来 Streamlit 改了内部 DOM 结构,选择器找不到图 → 仅记忆功能
# 失效,页面渲染完全不受影响(图本体仍是原生 st.plotly_chart)。
_LEGEND_SYNC_JS = """\
export default function (component) {
  const { data, parentElement, setStateValue } = component

  // 重渲染时 export 会重跑,而 gd 上的事件处理器随每次 plotly newPlot 被清空——
  // 用挂在容器上的可变 store 让处理器始终读到最新 data,并用定时巡检重挂
  const store = (parentElement._legendStore = parentElement._legendStore || { lastSig: null })
  store.saved = data.saved

  function visibleNames(gd) {
    const out = []
    for (const t of gd.data || []) {
      if (t.name) out.push([t.name, t.visible !== false && t.visible !== "legendonly"])
    }
    return out.filter((p) => p[1]).map((p) => p[0])
  }

  function sig(gd) { return visibleNames(gd).join("|") }

  function isNavChart(gd) {
    try {
      const t = gd._fullLayout && gd._fullLayout.title && gd._fullLayout.title.text
      return (typeof t === "string" && t.indexOf("组合累计净值") !== -1) ||
             (gd.data || []).some((tr) => tr.name === "组合")
    } catch (e) { return false }
  }

  function applySaved(gd) {
    if (!Array.isArray(store.saved)) return
    const want = new Set(store.saved)
    const idx = [], vals = []
    ;(gd.data || []).forEach((tr, i) => {
      const v = tr.name && !want.has(tr.name) ? "legendonly" : true
      if (tr.visible !== v) { idx.push(i); vals.push(v) }
    })
    if (!idx.length) return
    // plotly 新版已移除 gd.restyle 等 div 方法;用全局 Plotly(Streamlit 已暴露)
    const P = window.Plotly
    if (P && typeof P.restyle === "function") P.restyle(gd, { visible: vals }, idx)
    store.lastSig = sig(gd)
  }

  function onRestyle(gd) {
    const s = sig(gd)
    if (gd._legendSig !== s) {           // 去重:applySaved 触发的 restyle 不回传
      gd._legendSig = s
      store.lastSig = s
      setStateValue("visible", visibleNames(gd))   // → Python 落盘
    }
  }

  // newPlot 会清掉 gd 的全部事件监听并重置 visible;监听器在 = 图没被重画。
  // 监听器丢了 → 重挂 + 重放存档;监听器还在但显隐变了 → 用户点了图例 → 回传。
  // 另:图在隐藏 tab 里时 restyle 不生效,所以「首次变为可见」也要重放一次存档
  // (对隐藏图重放无害,可见时对齐才是关键)。
  function tick() {
    document.querySelectorAll(".js-plotly-plot").forEach((gd) => {
      if (!isNavChart(gd)) return
      // 监听器登记在 gd._ev._events(EventEmitter 实例);newPlot 清空它
      const evReg = gd._ev && gd._ev._events
      const hasListener = evReg ? !!evReg.plotly_restyle : gd._legendHooked
      if (!hasListener) {
        if (gd.removeAllListeners) {
          gd.removeAllListeners("plotly_restyle")
          gd.removeAllListeners("plotly_react")
        }
        if (typeof gd.on !== "function") return
        gd.on("plotly_restyle", () => onRestyle(gd))
        gd.on("plotly_react", () => applySaved(gd))
        gd._legendHooked = true
        gd._legendAppliedVis = false
        applySaved(gd)
        store.lastSig = sig(gd)
        return
      }
      const visNow = gd.getBoundingClientRect().width > 50
      if (visNow && !gd._legendAppliedVis) {
        gd._legendAppliedVis = true
        applySaved(gd)
        return
      }
      const s = sig(gd)
      if (s !== store.lastSig) {
        store.lastSig = s
        gd._legendSig = s
        setStateValue("visible", visibleNames(gd))
      }
    })
  }

  if (!parentElement._scanTimer) parentElement._scanTimer = setInterval(tick, 400)
  tick()

  return () => {
    if (parentElement._scanTimer) { clearInterval(parentElement._scanTimer); parentElement._scanTimer = null }
  }
}
"""

_LEGEND_SYNC = st.components.v2.component(
    "legend_sync", html="<div style='display:none'></div>", js=_LEGEND_SYNC_JS)
LEGEND_SYNC_KEY = "legend_sync"


def legend_sync() -> None:
    """挂载图例显隐记忆组件:组件回传状态优先,否则读 ui_prefs.json。"""
    comp_state = st.session_state.get(LEGEND_SYNC_KEY, {})
    saved = (comp_state["visible"] if "visible" in comp_state
             else _load_ui_prefs().get("nav_shown"))

    def _on_visible_change():
        val = st.session_state.get(LEGEND_SYNC_KEY, {}).get("visible")
        if val is not None:
            _save_ui_pref("nav_shown", list(val))

    _LEGEND_SYNC(key=LEGEND_SYNC_KEY, data={"saved": saved},
                 on_visible_change=_on_visible_change)


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
    pct_cols = [c for c in df.columns
                if c in ("开盘涨幅", "实体涨幅", "收盘涨幅", "当日收益率", "累计收益")]
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


def _load_ui_prefs() -> dict:
    """UI 偏好(跨 session 记忆),存 cache/ui_prefs.json(不入 git)。"""
    try:
        return json.loads((DB_PATH.parent / "ui_prefs.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_ui_pref(key: str, value) -> None:
    prefs = _load_ui_prefs()
    prefs[key] = value
    try:
        (DB_PATH.parent / "ui_prefs.json").write_text(
            json.dumps(prefs, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # 偏好写失败不影响功能,退回默认行为


def _clamp_pref_date(raw, first: str, last: str):
    """偏好里存的日期夹回当前数据范围内(输入文件/行情变了也不报错)。"""
    if not raw:
        return None
    try:
        d = pd.Timestamp(raw).date()
    except Exception:
        return None
    lo, hi = pd.Timestamp(first).date(), pd.Timestamp(last).date()
    return min(max(d, lo), hi)


def _date_nav(prefix: str, dates: list[str], label: str,
              with_end: bool = True, quick: bool = False) -> tuple[str, str, str]:
    """日期选择控件 + 滑块。

    with_end=False(每日持仓):单个「日期」卡片即所选日期,与滑块双向同步——
      改卡片 → 滑块/持仓跟随(非交易日就近对齐到交易日);拖滑块 → 卡片跟随。
    with_end=True(组合净值):开始/结束日期定义展示区间,滑块在区间内选日,
      图表随区间截取,净值按区间重定基。
    quick=True(需 with_end=True):区间上方加快捷按钮 本周/本月/近三月/今年/全部,
      点击即写入开始/结束日期并持久化(与手动改日期框同一偏好存储)。
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
        prefs = _load_ui_prefs()
        ss[start_key] = (_clamp_pref_date(prefs.get(start_key), first, last)
                         or pd.Timestamp(first).date())
    if end_key not in ss:
        prefs = _load_ui_prefs()
        ss[end_key] = (_clamp_pref_date(prefs.get(end_key), first, last)
                       or pd.Timestamp(last).date())

    if quick:
        # 快捷区间(同花顺式)。按钮必须在日期框之前渲染:点击后先写 session_state,
        # 日期框随后实例化即取到新值(widget 实例化后再改状态会抛异常)。
        base = pd.Timestamp(dates[-1])
        lo, hi = pd.Timestamp(first).date(), pd.Timestamp(last).date()
        presets = {
            "本周": base - pd.Timedelta(days=base.day_of_week),   # 所选日所在周的周一
            "本月": base.replace(day=1),
            "近三月": (base - pd.DateOffset(months=2)).replace(day=1),   # 上上个月 1 号起(含本月共 3 个自然月)
            "今年": base.replace(month=1, day=1),
            "全部": pd.Timestamp("2025-09-01"),   # 池子/净值的有效起点
        }
        picked = None
        for name, col in zip(presets, st.columns(5)):
            if col.button(name, key=f"{prefix}_qr_{name}", width="stretch"):
                picked = (min(max(presets[name].date(), lo), hi), hi)
        if picked:
            ss[start_key], ss[end_key] = picked
            _save_ui_pref(start_key, str(picked[0]))
            _save_ui_pref(end_key, str(picked[1]))

    def _save_range():
        # 任一日期框改动都把当前区间整体持久化,刷新页面后仍还原
        _save_ui_pref(start_key, str(ss[start_key]))
        _save_ui_pref(end_key, str(ss[end_key]))

    c0, c1 = st.columns(2)
    with c0:
        start_val = st.date_input("开始日期", min_value=pd.Timestamp(first).date(),
                                  max_value=pd.Timestamp(last).date(),
                                  format="YYYY-MM-DD", key=start_key,
                                  on_change=_save_range)
    with c1:
        end_val = st.date_input("结束日期", min_value=pd.Timestamp(first).date(),
                                max_value=pd.Timestamp(last).date(),
                                format="YYYY-MM-DD", key=end_key,
                                on_change=_save_range)

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


# ====================== 攻防贡献视图(只看进攻/只看防守,收益率归因) ======================
REGIME_VIEWS = ["攻防都看", "只看进攻", "只看防守"]


def _regime_labels(regime: list, dates: list[str], x: float) -> dict:
    """逐日攻防标签:仓位表覆盖且当日仓位 >= x → 进攻;否则防守(未覆盖=防守,
    保守口径;区间重叠行序靠后覆盖靠前——与 rules.regime_position 同口径)。"""
    labels = {}
    for d in dates:
        pos = None
        for s, e, p in regime:
            if s <= d <= e:
                pos = p
        labels[d] = pos is not None and pos >= x
    return labels


def _regime_view_portfolio(portfolio: pd.DataFrame, regime: list, x: float,
                           view: str) -> pd.DataFrame:
    """攻防贡献视图:只看进攻 → 防守日 account_return/fee 置 0 并重算净值/回撤;
    只看防守对称。持仓与交易明细不变,仅当日收益归因;净值从 1.0 起按
    (1+account_return−fee) 逐日连乘(与引擎同口径、同舍入)。"""
    labels = _regime_labels(regime, portfolio["date"].tolist(), x)
    pf = portfolio.copy()
    off = pd.Series([labels[d] for d in pf["date"]], index=pf.index)
    zero = ~off if view == "只看进攻" else off
    pf.loc[zero, "account_return"] = 0.0
    pf.loc[zero, "fee"] = 0.0
    nav, peak = 1.0, 1.0
    navs, dds = [], []
    for net in pf["account_return"] - pf["fee"]:
        nav = round(nav * (1 + net), 6)
        peak = max(peak, nav)
        navs.append(nav)
        dds.append(round(nav / peak - 1, 6) if peak > 0 else 0.0)
    pf["nav"] = navs
    pf["drawdown"] = dds
    return pf


# ====================== 买入持有收益阶梯(导出用,与算法无关) ======================
def _hold_ladder_rows(merged: pd.DataFrame, market: pd.DataFrame):
    """逐条推荐产出 (ts_code, name, start_date, seq),seq = [(trade_date, 当日涨幅, 累计涨幅), ...]。

    口径:首日 = 起始日期(停牌顺延到首个有行情交易日,即实际买入日)的**实体涨幅**
    (收盘/开盘-1);其后每个**有行情交易日** = **收盘涨幅**(pct_chg/100,停牌日跳过);
    累计 = 自买入日开盘持有到当日收盘的复利涨幅 = (1+首日)×(1+次日)×...-1。
    一直持有不卖,与任何算法无关。缺数据日跳过、不计入持有天数。
    """
    m = market[market["ts_code"].isin(set(merged["ts_code"]))].sort_values("trade_date")
    by_code = {c: g for c, g in m.groupby("ts_code")}
    for _, r in merged.iterrows():
        bars = by_code.get(r["ts_code"])
        bars = bars[bars["trade_date"] >= r["start_date"]] if bars is not None else None
        seq, cum, started = [], None, False
        if bars is not None and not bars.empty:
            for _, b in bars.iterrows():
                d = (body_return(b["open"], b["close"]) if not started
                     else daily_return(b["pct_chg"]))
                if d is None:
                    continue            # 缺数据日跳过,不计入持有天数
                started = True
                cum = round((1 + (cum or 0)) * (1 + d) - 1, 6)
                seq.append((b["trade_date"], d, cum))
        yield (r["ts_code"], r["name"], r["start_date"], seq)


def _build_hold_ladder(recs: pd.DataFrame, market: pd.DataFrame) -> pd.DataFrame:
    """sheet「收益阶梯」:每条(股票, 起始日期)一行,股票代码/股票名称/起始日期 +
    day1/累计day1/day2/累计day2/... 直至最新行情(列数 = 最长阶梯,不足留空)。
    同日同股合并为一行(与限仓轮换算法同口径,保留首行名称)。"""
    merged = recs.drop_duplicates(subset=["ts_code", "start_date"],
                                  keep="first").reset_index(drop=True)
    rows, max_days = [], 0
    for code, name, sd, seq in _hold_ladder_rows(merged, market):
        row = {"股票代码": code, "股票名称": name, "起始日期": sd}
        for n, (_d, ret, cum) in enumerate(seq, 1):
            row[f"day{n}"] = ret
            row[f"累计day{n}"] = cum
        max_days = max(max_days, len(seq))
        rows.append(row)
    cols = (["股票代码", "股票名称", "起始日期"]
            + [c for n in range(1, max_days + 1)
               for c in (f"day{n}", f"累计day{n}")])
    return pd.DataFrame(rows, columns=cols)


def _build_hold_date_matrix(recs: pd.DataFrame, market: pd.DataFrame) -> pd.DataFrame:
    """sheet「按日期对齐」:每行一只股票(行序与「收益阶梯」sheet 一致),
    前 3 列 = 股票代码/股票名称/起始日期,其后列 = 池内最早起始日期起的每个交易日,
    成对 (YYYYMMDD, YYYYMMDD累计):当日该股在自身阶梯上的涨幅(买入日=实体涨幅,
    其后=收盘涨幅)与截至当日收盘的累计涨幅;未到买入日/停牌/已无行情的格子留空。"""
    merged = recs.drop_duplicates(subset=["ts_code", "start_date"],
                                  keep="first").reset_index(drop=True)
    seqs = [(code, name, sd, dict((d, (ret, cum)) for d, ret, cum in seq))
            for code, name, sd, seq in _hold_ladder_rows(merged, market)]
    min_sd = merged["start_date"].min()
    dates = sorted(market[market["trade_date"] >= min_sd]["trade_date"].unique())
    cols = (["股票代码", "股票名称", "起始日期"]
            + [c for d in dates
               for c in (d.replace("-", ""), d.replace("-", "") + "累计")])
    if len(cols) == 3:
        return pd.DataFrame([{"提示": "池内最早起始日期晚于最新行情,无数据"}])
    rows = []
    for code, name, sd, m in seqs:
        row = {"股票代码": code, "股票名称": name, "起始日期": sd}
        for d, (ret, cum) in m.items():
            key = d.replace("-", "")
            row[key] = ret
            row[key + "累计"] = cum
        rows.append(row)
    return pd.DataFrame(rows, columns=cols)


def _style_ret_sheet(ws, start_col: int, freeze: str, bold_last_row: bool):
    """收益 sheet 通用样式:收益率列百分比格式;>1% 浅红底、<-1% 浅绿底、
    −1%~1% 灰底,一律黑字(浅底配黑字反差,红涨绿跌);可选末行(平均收益率行)加粗;冻结窗格。"""
    from openpyxl.styles import Font, PatternFill
    fill_red = PatternFill("solid", fgColor="FFE6E6")    # 浅红(与页面表格同色)
    fill_green = PatternFill("solid", fgColor="E6FFE6")  # 浅绿(与页面表格同色)
    fill_gray = PatternFill("solid", fgColor="808080")
    last_row = ws.max_row
    for col in ws.iter_cols(min_col=start_col):
        for cell in col:
            if not isinstance(cell.value, (int, float)):
                continue
            cell.number_format = "0.00%"
            if cell.value > 0.01:
                cell.fill = fill_red
            elif cell.value < -0.01:
                cell.fill = fill_green
            else:
                cell.fill = fill_gray
            cell.font = Font(color="000000",
                             bold=bold_last_row and cell.row == last_row)
    if bold_last_row:
        ws.cell(row=last_row, column=1).font = Font(bold=True)
    ws.freeze_panes = freeze


# ====================== 主流程 ======================
_ensure_dirs()
fetcher.init_db()

st.title("📊 股票推荐回测")

# ---------- 侧边栏 ----------
def _next_plan_date(recs: pd.DataFrame) -> tuple[str | None, str | None]:
    """(last_closed, plan_date):最后一个已收盘交易日(15:00 后含今日)的下一交易日。

    日历自动补拉未来两周(失败则用库里已有日历,缺未来日期由调用方提示);
    plan_date 为 None 表示日历未覆盖下一交易日。
    """
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    try:
        fetcher.ensure_trade_calendar(
            recs["start_date"].min(),
            (now + timedelta(days=14)).strftime("%Y-%m-%d"))
    except Exception:
        pass  # 日历补拉失败时先用库里已有的日历,缺未来日期则下方提示
    tds_all = fetcher.load_trade_dates()   # 直查 DB(含未来日期),不走缓存
    closed = [d for d in tds_all if d < today]
    if today in tds_all and now.hour >= 15:
        closed.append(today)               # 今天已过 15:00 收盘
    last_closed = max(closed) if closed else None
    after = [d for d in tds_all if last_closed and d > last_closed]
    return last_closed, (after[0] if after else None)


def _trade_plan(plan_date: str, last_closed: str, recs: pd.DataFrame,
                regime_data: dict, daily_holdings: pd.DataFrame,
                x: float, y: int, z: int, bars_by_date: dict,
                prev_gain: float | None, max_per_day: int | None = None) -> dict:
    """下一交易日计划:攻防规则在计划日的目标持仓 vs 最新收盘持仓的差分。

    与回测完全同源:仓位判定/最近 y/z 条窗口(rules.target_keys)/同股卖旧买新
    合并保留最早进场仓/前收涨幅剔除(rules.prev_gain_discard 共用),口径见
    RULES.md。返回 dict:
    pos/inherited/offense/target/cur_date/sell/buy/hold/notes
    (sell/buy/hold 为 pos 集合;lookup 元组 (pos, ts_code, name, start_date))
    """
    def _pos_of(d):
        # 区间重叠时行序靠后者覆盖靠前者(与 rules.regime_position 同口径)
        hit = None
        for s, e, p in regime_data["regime"]:
            if s <= d <= e:
                hit = p
        return hit
    pos_plan = _pos_of(plan_date)
    inherited = pos_plan is None
    if inherited:
        pos_plan = _pos_of(last_closed)
    offense = pos_plan is not None and pos_plan >= x

    base = len(recs)
    offense_recs = [(int(i), r.ts_code, r.name, r.start_date)
                    for i, r in enumerate(recs.itertuples())
                    if r.start_date <= plan_date]
    defense_recs_p = [(base + i, ts, n, sd)
                      for i, (_pid, ts, n, sd) in enumerate(regime_data["defense"])]
    defense_pos_p = {p for p, *_ in defense_recs_p}
    # 前收涨幅过滤与回测同口径:开始日期前一交易日收盘涨幅超阈值的推荐已永久舍弃
    dropped = prev_gain_discard(offense_recs + defense_recs_p, bars_by_date, prev_gain)
    if offense:
        target_pool = [r for r in offense_recs if r[0] not in defense_pos_p
                       and r[0] not in dropped]
        target = target_keys(target_pool, y, max_per_day)
    else:
        target = target_keys([r for r in defense_recs_p if r[0] not in dropped],
                             z, max_per_day)
    lookup = {r[0]: r for r in offense_recs + defense_recs_p}

    # 当前持仓:最近一个回测日的收盘持仓(卖出行除外)
    cur_date = daily_holdings["date"].max()
    cur_rows = daily_holdings[daily_holdings["date"] == cur_date]
    cur = {int(r["pos"]): {"ts_code": r["code"], "name": r["name"],
                           "entry_date": r["entry_date"],
                           "rec_date": r["rec_date"]}
           for _, r in cur_rows[cur_rows["action"] != "sell"].iterrows()}
    cur = {p: info for p, info in cur.items() if p in lookup}

    sell_pos = {p for p in cur if p not in target}
    buy_pos = {p for p in target if p not in cur}
    hold_pos = {p for p in target if p in cur}
    # 同股"卖旧+买新"合并:保留最早进场旧仓(与规则口径一致)
    buy_codes = {p: lookup[p][1] for p in buy_pos}
    merged_codes = {cur[p]["ts_code"] for p in sell_pos} \
        & set(c for c in buy_codes.values() if c)
    for c in merged_codes:
        olds = sorted((p for p in sell_pos if cur[p]["ts_code"] == c),
                      key=lambda p: (cur[p]["entry_date"] or "", p))
        keep = olds[0]
        sell_pos.discard(keep)
        hold_pos.add(keep)
        buy_pos = {p for p in buy_pos if buy_codes[p] != c}

    notes = []
    if inherited and pos_plan is not None:
        notes.append(f"计划日仓位表未覆盖,继承 {last_closed} 的仓位 {pos_plan:.1f} 成")
    if inherited and pos_plan is None:
        notes.append(f"计划日与上一交易日({last_closed})仓位表均未覆盖,"
                     f"按防守处理(与回测引擎的保守口径一致);请在仓位 sheet 补充区间")
    if cur_date < last_closed:
        notes.append(f"最新行情只到 {cur_date},当前持仓按该日收盘计算"
                     f"(点「初始化/更新行情数据」刷新)")
    if offense and not target_pool:
        notes.append("进攻但尚无起始日期 ≤ 计划日的推荐,空仓等待")
    return {"pos": pos_plan, "inherited": inherited, "offense": offense,
            "target": target, "cur_date": cur_date, "lookup": lookup,
            "cur": cur, "sell": sell_pos, "buy": buy_pos, "hold": hold_pos,
            "notes": notes}


def _trade_plan_fixed(plan_date: str, last_closed: str, recs: pd.DataFrame,
                      daily_holdings: pd.DataFrame, hold_days: int) -> dict:
    """固定持有N日等权规则的计划:计划日开盘买新推荐、卖满 N+1 个交易日的持仓。

    与回测完全同源(rules.make_fixed_hold_rule):持仓与已卖集合直接取回测
    daily_holdings 的最后交易日状态;买入 = 起始日期 <= 计划日且未持有未卖出
    的推荐(停牌未买成的规则自然重发);卖出 = 已持有交易日数(trade_days,
    含买入日、停牌日不计)>= N 的持仓(计划日即第 N+1 个交易日开盘卖;
    计划日停牌则实盘顺延,与引擎口径一致)。等权,权重由引擎归一化。
    """
    if daily_holdings.empty:
        return {"cur_date": None, "cur": {}, "buys": [], "sells": [],
                "holds": [], "notes": ["暂无回测数据,点侧边栏「初始化/更新行情数据」"]}
    cur_date = daily_holdings["date"].max()
    cur_rows = daily_holdings[daily_holdings["date"] == cur_date]
    cur = {int(r["pos"]): {"ts_code": r["code"], "name": r["name"],
                           "entry_date": r["entry_date"], "rec_date": r["rec_date"],
                           "trade_days": int(r["trade_days"] or 0)}
           for _, r in cur_rows[cur_rows["action"] != "sell"].iterrows()}
    sold = {int(p) for p in daily_holdings.loc[
        daily_holdings["action"] == "sell", "pos"].dropna()}
    buys = [(i, r.ts_code, r.name, r.start_date)
            for i, r in enumerate(recs.itertuples())
            if r.start_date <= plan_date and i not in cur and i not in sold]
    sells = [p for p in cur if cur[p]["trade_days"] >= hold_days]
    holds = [p for p in cur if p not in sells]
    notes = []
    if cur_date < last_closed:
        notes.append(f"最新行情只到 {cur_date},当前持仓按该日收盘计算"
                     f"(点「初始化/更新行情数据」刷新)")
    if not buys and not sells and cur:
        notes.append(f"{plan_date} 无新增推荐、无到期持仓,维持当前 {len(cur)} 只持仓不动")
    return {"cur_date": cur_date, "cur": cur, "sold": sold, "buys": buys,
            "sells": sells, "holds": holds, "notes": notes}


def _trade_plan_slot(plan_date: str, last_closed: str, recs: pd.DataFrame,
                     daily_holdings: pd.DataFrame, weight_cheng: float,
                     hold_days: int) -> dict:
    """限仓轮换规则的计划:计划日开盘买目标集内新推荐、卖被挤出的最老仓与到期仓。

    与回测完全同源(rules.make_slot_rule):同日同股先合并(与 _build_rule_and_recs
    一致);目标 = 未卖出推荐中最新 floor(满仓/每只成数) 只;持仓与已卖集合取回测
    daily_holdings 最后交易日状态。卖出两类:不在目标集(被新推荐 FIFO 挤出)或
    已持有交易日数(trade_days,含买入日,停牌日不计)>= D(计划日即第 D+1 个交易日
    到期)。weight_cheng 为"成"(1.0 = 10%),与规则/引擎的小数口径 ÷10 换算。
    等计划日停牌则实盘顺延,与引擎口径一致。
    """
    merged = recs.drop_duplicates(subset=["ts_code", "start_date"],
                                  keep="first").reset_index(drop=True)
    if daily_holdings.empty:
        return {"cur_date": None, "cur": {}, "buys": [], "sells": [],
                "holds": [], "notes": ["暂无回测数据,点侧边栏「初始化/更新行情数据」"]}
    cur_date = daily_holdings["date"].max()
    cur_rows = daily_holdings[daily_holdings["date"] == cur_date]
    cur = {int(r["pos"]): {"ts_code": r["code"], "name": r["name"],
                           "entry_date": r["entry_date"], "rec_date": r["rec_date"],
                           "trade_days": int(r["trade_days"] or 0)}
           for _, r in cur_rows[cur_rows["action"] != "sell"].iterrows()}
    sold = {int(p) for p in daily_holdings.loc[
        daily_holdings["action"] == "sell", "pos"].dropna()}
    alive = [(i, r.ts_code, r.name, r.start_date)
             for i, r in enumerate(merged.itertuples())
             if r.start_date <= plan_date and i not in sold]
    capacity = slot_capacity(weight_cheng / 10.0)
    newest_first = sorted(alive, key=lambda r: (r[3], -r[0]), reverse=True)
    target = {r[0] for r in newest_first[:capacity]}
    buys = [(p, ts, name, sd) for p, ts, name, sd in alive
            if p in target and p not in cur]
    sells = [p for p in cur if p not in target
             or cur[p]["trade_days"] >= hold_days]
    holds = [p for p in cur if p not in sells]
    notes = []
    if cur_date < last_closed:
        notes.append(f"最新行情只到 {cur_date},当前持仓按该日收盘计算"
                     f"(点「初始化/更新行情数据」刷新)")
    if not buys and not sells and cur:
        notes.append(f"{plan_date} 无新增推荐、无到期/被替换持仓,"
                     f"维持当前 {len(cur)} 只持仓不动")
    return {"cur_date": cur_date, "cur": cur, "sold": sold, "buys": buys,
            "sells": sells, "holds": holds, "capacity": capacity,
            "notes": notes}


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

    # 回测算法(选择持久化到 ui_prefs,刷新页面后保留)
    st.markdown("### ② 回测算法")
    if "algo" not in st.session_state:
        st.session_state.algo = _load_ui_prefs().get("algo", ALGO_REGIME)

    def _save_algo():
        _save_ui_pref("algo", st.session_state.algo)

    st.selectbox("收益率算法", [ALGO_REGIME, ALGO_FIXED, ALGO_SLOT], key="algo",
                 on_change=_save_algo,
                 help=f"{ALGO_REGIME}:按仓位表攻防切换,持最近 y/z 条推荐(参数在 config.py);"
                      f"{ALGO_FIXED}:推荐起始日开盘买入、持有 N 个交易日、"
                      f"第 N+1 个交易日开盘卖出,每日等权(1/当日持仓数);"
                      f"{ALGO_SLOT}:每只推荐固定成数(其余现金),新推荐挤掉最早买入的,"
                      f"持有满 D 个交易日后到期全卖")
    if "hold_days" not in st.session_state:
        st.session_state.hold_days = int(_load_ui_prefs().get("hold_days", 5) or 5)

    def _save_hold_days():
        _save_ui_pref("hold_days", st.session_state.hold_days)

    if st.session_state.algo == ALGO_FIXED:
        st.number_input("持有交易日数 N(第 N+1 个交易日开盘卖出)",
                        min_value=1, max_value=60, step=1, key="hold_days",
                        on_change=_save_hold_days)

    if "slot_w" not in st.session_state:
        st.session_state.slot_w = float(_load_ui_prefs().get("slot_w", 1.0) or 1.0)

    def _save_slot_w():
        _save_ui_pref("slot_w", st.session_state.slot_w)

    if "slot_d" not in st.session_state:
        st.session_state.slot_d = int(_load_ui_prefs().get("slot_d", 10) or 10)

    def _save_slot_d():
        _save_ui_pref("slot_d", st.session_state.slot_d)

    if st.session_state.algo == ALGO_SLOT:
        st.number_input("每只仓位(成)", min_value=0.05, max_value=10.0, step=0.05,
                        format="%.2f", key="slot_w", on_change=_save_slot_w,
                        help="每只推荐固定占的仓位(1 成 = 10%);满仓 100% ÷ 每只成数 = "
                             "每天最多持仓只数;其余资金为空仓现金")
        st.number_input("持仓天数 D(第 D+1 个交易日开盘卖出)",
                        min_value=1, max_value=120, step=1, key="slot_d",
                        on_change=_save_slot_d,
                        help="持有满 D 个交易日(买入日=第 1 天,停牌日不计)后到期全卖;"
                             "期间新推荐挤掉最早买入的(FIFO 新老替换)")
        st.caption(f"每天最多 {slot_capacity(st.session_state.slot_w / 10.0)} 只"
                   f"(满仓 10 成 ÷ 每只 {st.session_state.slot_w:.2f} 成)")

    # 仓位/防守文件(RULES.md):攻防转换用,固定读 input/ 下配置名,改文件后自动重解析
    regime_path = INPUT_DIR / REGIME_FILE_NAME
    if regime_path.exists():
        _parse_regime_into_state(regime_path)
    elif st.session_state.algo == ALGO_REGIME:
        st.warning(f"未找到 {REGIME_FILE_NAME},将退回「持有10个交易日」规则")
    regime_data = st.session_state.get("regime_data")

    # 攻防贡献视图:归因进攻/防守各自贡献(仅攻防转换 + 仓位文件存在时)
    if st.session_state.algo == ALGO_REGIME and regime_data:
        if "regime_view" not in st.session_state:
            st.session_state.regime_view = _load_ui_prefs().get("regime_view", "攻防都看")

        def _save_regime_view():
            _save_ui_pref("regime_view", st.session_state.regime_view)

        st.selectbox("攻防贡献视图", REGIME_VIEWS, key="regime_view",
                     on_change=_save_regime_view,
                     help="只看进攻:防守日收益率按 0 计(持仓与交易不变,净值只剩进攻日贡献);"
                          "只看防守对称;用于对比收益是进攻还是防守贡献的多")

    st.markdown("### ③ 行情数据")
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

    st.markdown("### ④ 缓存状态")
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
    x_th, y_off, z_def, fee, gap_skip, prev_skip, mpd, limit_fill = _strategy_params()
    algo = st.session_state.algo
    hold_days = st.session_state.get("hold_days", 5)
    slot_w = st.session_state.get("slot_w", 1.0)
    slot_d = st.session_state.get("slot_d", 10)
    daily_holdings, portfolio, summary = _run_backtest(
        recs_tuple, regime_key, db_key, version, x_th, y_off, z_def, gap_skip,
        prev_skip, mpd, limit_fill, fee, algo, hold_days,
        slot_w / 10.0,   # 页面"成" → 规则/引擎小数口径;换算放在缓存 key 一侧,
                         # 换算口径变化(如这次的 ÷10)会自然改变 key、使旧缓存失效
        slot_d,
        engine_ver=_engine_stamp())

    # 攻防贡献视图:全量 portfolio 留给导出,展示用的 portfolio 按视图过滤
    # (持仓/交易不变,被过滤日的当日收益与费用置 0,净值/回撤重算)
    portfolio_full = portfolio
    regime_view = st.session_state.get("regime_view", "攻防都看")
    if (algo == ALGO_REGIME and regime_key is not None
            and regime_view != "攻防都看" and not portfolio.empty):
        portfolio = _regime_view_portfolio(portfolio, regime_data["regime"],
                                           x_th, regime_view)

    if portfolio.empty:
        st.info("数据库中还没有行情数据。点击侧边栏「初始化/更新行情数据」按钮。")

    # tab 顺序(从左到右):组合净值(默认选中)、每日持仓、交易计划、回测明细、导出
    tab_nav, tab_hold, tab_plan, tab_detail, tab_export = st.tabs(
        ["组合净值", "每日持仓", "交易计划", "回测明细", "导出"])

    dates = portfolio["date"].tolist() if not portfolio.empty else []
    portfolio["_dt"] = pd.to_datetime(portfolio["date"])

    # ---------- Tab1 每日持仓 ----------
    with tab_hold:
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
                show = day_df[["name", "code", "action", "weight", "open_ret",
                               "body_ret", "close_ret", "day_return",
                               "cum_return", "rec_date", "entry_date", "trade_days"]].copy()
                show["操作"] = show["action"].map({"buy": "买入", "sell": "卖出", "hold": "持有"})
                show = show[["name", "code", "操作", "weight", "open_ret", "body_ret",
                             "close_ret", "day_return", "cum_return",
                             "rec_date", "entry_date", "trade_days"]]
                show.columns = ["股票名称", "代码", "操作", "仓位占比", "开盘涨幅",
                                "实体涨幅", "收盘涨幅", "当日收益率",
                                "累计收益", "推荐日期", "买入日期", "交易天数"]
                order = {"卖出": 0, "买入": 1, "持有": 2}
                show = show.sort_values(
                    ["操作", "累计收益"], ascending=[True, False], na_position="last",
                    key=lambda s: s.map(order) if s.name == "操作" else s)
                st.dataframe(style_holdings(show), width="stretch",
                             hide_index=True)
                if algo == ALGO_SLOT:
                    st.caption(f"仓位占比 = 每只固定 {slot_w} 成(其余为空仓现金,"
                               f"净值按真实仓位计),卖出的股票计 0%;"
                               f"当日收益率:卖出=开盘涨跌幅,当日新买=实体涨幅,持有=全天涨跌幅(收盘/昨收-1);"
                               f"账户收益率 = (1+Σ昨日持仓成数×今日开盘涨跌幅)×(1+Σ今日持仓成数×实体涨幅)-1;"
                               f"累计收益自买入日起复利(买入日实体涨幅、之后全日涨跌幅)")
                else:
                    st.caption(f"仓位占比 = 等权 1/收盘持仓数,卖出的股票计 0%;"
                               f"当日收益率:卖出=开盘涨跌幅,当日新买=实体涨幅,持有=全天涨跌幅(收盘/昨收-1);"
                               f"账户收益率 = (1+昨日持仓今日平均开盘涨跌幅)×(1+今日持仓平均实体涨幅)-1;"
                               f"累计收益自买入日起复利(买入日实体涨幅、之后全日涨跌幅)")
                if algo == ALGO_REGIME and regime_view != "攻防都看":
                    st.caption(f"攻防贡献视图:当日为"
                               f"{'防守' if regime_view == '只看进攻' else '进攻'}日,"
                               f"收益率与费用按 0 计")

    # ---------- Tab2 组合净值 ----------
    with tab_nav:
        if not dates:
            st.info("暂无数据")
        else:
            sel_dt, start_str, end_str = _date_nav("d2", dates, "推进日期", quick=True)
            sel_dt_v = pd.to_datetime(sel_dt)
            # 图表跟随起止日期;净值按区间重定基:区间**前一交易日**收盘 = 1,
            # 区间首日自身的当日收益计入曲线(否则选两天会丢首日收益);回撤按区间内峰值重算
            p_show = portfolio[(portfolio["date"] >= start_str)
                               & (portfolio["date"] <= end_str)].copy()
            base_nav = _base_nav_before(portfolio, start_str)
            if not p_show.empty and base_nav:
                nav_rel = p_show["nav"] / base_nav
                p_show["nav"] = nav_rel
                p_show["drawdown"] = nav_rel / nav_rel.cummax() - 1
            row = p_show[p_show["date"] == sel_dt].iloc[0]
            # 单押对照(X_THRESHOLD=0):与组合同区间、同口径重定基(首日=1)
            # 仅攻防转换有意义(固定持有没有仓位阈值概念),固定持有下不展示;
            # 攻防贡献视图(只看进攻/防守)下净值被过滤,对照失真,同样隐藏
            dan_nav = None
            if regime_key is not None and algo == ALGO_REGIME \
                    and regime_view == "攻防都看":
                try:
                    dan_pf = _run_backtest_x0(recs_tuple, regime_key, db_key, version,
                                              y_off, z_def, gap_skip, prev_skip, mpd,
                                              limit_fill, fee,
                                              engine_ver=_engine_stamp())
                    dan_show = dan_pf[(dan_pf["date"] >= start_str)
                                      & (dan_pf["date"] <= end_str)]
                    dan_base = _base_nav_before(dan_pf, start_str)
                    if not dan_show.empty and dan_base:
                        dan_nav = dan_show["nav"] / dan_base
                except Exception as e:
                    st.warning(f"单押对照计算失败(不影响回测):{str(e)[:150]}")
            # 基准对比:数据缺口自动补拉(失败只警告,不影响回测)
            # 曲线与组合同口径:前一交易日收盘 = 1 —— 把前一日并入序列交给 benchmark_navs
            # (其「首日=1、首日涨跌不进曲线」的口径正好落在前一日上),再截掉首日
            codes_all = tuple(BENCHMARKS) + tuple(c for cs in BENCHMARK_COMBO.values()
                                                  for c in cs)
            bench = {}
            try:
                _ensure_benchmarks(db_key, fetcher.get_bench_version(), dates[0], codes_all)
                bench_df = _load_benchmarks(db_key, fetcher.get_bench_version(), codes_all)
                prior = [d for d in dates if d < start_str]
                bench_dates = ([prior[-1]] if prior else []) + p_show["date"].tolist()
                bench_raw = benchmark_navs(bench_df, bench_dates,
                                           BENCHMARKS, BENCHMARK_COMBO)
                bench = {k: v[1:] for k, v in bench_raw.items()} if prior else bench_raw
            except Exception as e:
                st.warning(f"基准数据加载失败(不影响回测):{str(e)[:150]}")

            # 对比基准选择器:日/月/年三个视图共用(默认双创)
            _bench_opts = (["双创"] + [k for k in bench if k != "双创"]
                           + (["单押"] if dan_nav is not None else []))
            if "bench_choice" in st.session_state and \
                    st.session_state.bench_choice not in _bench_opts:
                del st.session_state.bench_choice
            if _bench_opts:
                _bc = st.columns([1, 3])
                with _bc[0]:
                    bench_sel = st.selectbox("对比基准", _bench_opts,
                                             key="bench_choice")
            else:
                bench_sel = None

            def _bench_daily_series(sel):
                """基准逐日收益(与 p_show 逐行对齐):由重定基净值序列还原;无则 None。"""
                if sel == "单押":
                    base = dan_nav.tolist() if dan_nav is not None else None
                elif sel is not None:
                    base = bench.get(sel)
                else:
                    base = None
                if not base:
                    return None
                out, prev = [], 1.0
                for _v in base:
                    _v = float(_v)
                    out.append(_v / prev - 1 if prev else 0.0)
                    prev = _v
                return out

            def _period_frame(fmt: str, label: str) -> pd.DataFrame:
                """区间内按 fmt 分组:组合(及选中基准)的费后复利收益与超额。"""
                _b = _bench_daily_series(bench_sel)
                _d = p_show.assign(_net=lambda d: d["account_return"] - d["fee"],
                                   _p=lambda d: pd.to_datetime(d["date"]).dt.strftime(fmt))
                if _b is not None:
                    _d = _d.assign(_b=_b)
                agg = {"收益率": ("_net", lambda s: (1 + s).prod() - 1)}
                if _b is not None:
                    agg["基准"] = ("_b", lambda s: (1 + s).prod() - 1)
                out = _d.groupby("_p", sort=True).agg(**agg)
                out = out.reset_index().rename(columns={"_p": label})
                if _b is not None:
                    out["超额"] = out["收益率"] - out["基准"]
                return out

            if regime_view != "攻防都看":
                st.caption(f"攻防贡献视图:{'防守' if regime_view == '只看进攻' else '进攻'}"
                           "日收益率按 0 计(持仓与交易明细不变,净值/回撤按过滤后重算);"
                           "单押对照已隐藏")
            sub_day, sub_month, sub_year = st.tabs(["日收益", "月收益", "年收益"])
            with sub_day:
                _has_bench_card = (bench_sel == "单押" and dan_nav is not None) \
                    or (bench_sel is not None and bench_sel in bench)
                cols = st.columns(5 + (1 if dan_nav is not None else 0)
                                  + (1 if _has_bench_card else 0))
                cols[0].metric("净值", f"{row['nav']:.4f}")
                cols[1].metric("当日收益", f"{row['account_return']:.2%}")
                cols[2].metric("回撤", f"{row['drawdown']:.2%}")
                cols[3].metric("持仓数", f"{int(row['n_stocks'])} 只")
                cols[4].metric("当日费用", f"{row['fee']:.4%}")
                _pos = 5
                if dan_nav is not None:
                    excess = row["nav"] - float(dan_nav.loc[row.name])
                    cols[_pos].metric("单押超额(净值差)", f"{excess:+.2%}",
                                      help="所选日期 组合净值 − 单押净值(均按区间前一交易日收盘重定基为 1),"
                                           "即攻防转换相对“一直满仓押推荐”的超额;负值代表跑输单押")
                    _pos += 1
                if _has_bench_card:
                    _bv = (float(dan_nav.loc[row.name]) if bench_sel == "单押"
                           else float(bench[bench_sel][p_show.index.get_loc(row.name)]))
                    sc_excess = row["nav"] - _bv
                    cols[_pos].metric(f"{bench_sel}超额(净值差)", f"{sc_excess:+.2%}",
                                      help=f"所选日期 组合净值 − {bench_sel}同期净值"
                                           "(与组合同区间、按区间前一交易日收盘重定基为 1);"
                                           "负值代表跑输该基准;对比基准可在上方下拉切换")
                # 每日账户收益柱图开关(记忆到 ui_prefs):关掉后净值曲线上移
                _daily_key = "show_daily_bars"
                if _daily_key not in st.session_state:
                    st.session_state[_daily_key] = _load_ui_prefs().get(_daily_key, True)

                def _save_show_daily():
                    _save_ui_pref(_daily_key, bool(st.session_state[_daily_key]))

                st.toggle("显示每日账户收益图", key=_daily_key, on_change=_save_show_daily)
                if st.session_state[_daily_key]:
                    st.plotly_chart(fig_daily_returns(p_show, sel_dt_v),
                                    width="stretch")
                st.plotly_chart(fig_nav(p_show, sel_dt_v, bench,
                                        dan=dan_nav.tolist() if dan_nav is not None else None),
                                width="stretch")
                legend_sync()   # 图例显隐记忆(隐形组件,不渲染内容)
                st.plotly_chart(fig_drawdown(p_show, sel_dt_v), width="stretch")
            with sub_month:
                st.caption(f"每月一个收益率:区间内该月各交易日费后收益(account_return−fee)复利;"
                           f"右下角为组合相对 {bench_sel or '基准'} 同期收益的超额;随上方日期联动")
                # 月历色块:真 3 行 × 4 列,每格 = 月份 + 当月收益率 + 对比基准超额(右下角)
                _pfm = _period_frame("%Y-%m", "月份")
                _ret_map, _exc_map = {}, {}
                for _, _r in _pfm.iterrows():
                    _yy, _mm = _r["月份"].split("-")
                    _ret_map[(int(_yy), int(_mm))] = _r["收益率"]
                    _exc_map[(int(_yy), int(_mm))] = _r.get("超额", float("nan"))

                def _chip(label: str, v, excess=None) -> str:
                    """单个月份色块(背景/字色规则同 _pct_cell)。"""
                    if pd.isna(v):
                        return ("<div style='background:#F5F5F5;color:#BDBDBD;"
                                "border-radius:6px;padding:6px 8px;text-align:center;'>"
                                f"<div style='font-size:0.8em;'>{label}</div>"
                                "<div style='font-size:1.15em;font-weight:600;'>—</div></div>")
                    if v > 0.01:
                        bg, fg, sign = "#FFE6E6", "#B71C1C", "+"
                    elif v < -0.01:
                        bg, fg, sign = "#E6FFE6", "#1B5E20", ""
                    else:
                        bg, fg, sign = "#F5F5F5", "#616161", ""
                    _extra = ""
                    if excess is not None and not pd.isna(excess):
                        _ec = "#B71C1C" if excess > 0.005 else \
                            ("#1B5E20" if excess < -0.005 else "#757575")
                        _es = "+" if excess > 0 else ""
                        _extra = (f"<div style='text-align:right;font-size:0.72em;"
                                  f"color:{_ec};margin-top:3px;'>vs {bench_sel} "
                                  f"{_es}{excess:.2%}</div>")
                    return (f"<div style='background:{bg};color:{fg};"
                            "border-radius:6px;padding:6px 8px;text-align:center;'>"
                            f"<div style='font-size:0.8em;opacity:0.8;'>{label}</div>"
                            f"<div style='font-size:1.15em;font-weight:600;'>"
                            f"{sign}{v:.2%}</div>{_extra}</div>")

                for _year in sorted(set(y for y, _ in _ret_map)):
                    st.markdown(f"**{_year} 年**")
                    for _row in range(3):
                        cols = st.columns(4, gap="small")
                        for _ci in range(4):
                            _mno = _row * 4 + _ci + 1
                            _v = _ret_map.get((_year, _mno), float("nan"))
                            _e = _exc_map.get((_year, _mno), float("nan"))
                            with cols[_ci]:
                                st.markdown(_chip(f"{_mno}月", _v, _e),
                                            unsafe_allow_html=True)
                    st.write("")
            with sub_year:
                st.caption(f"每年一个收益率:口径同上,按年复利;"
                           f"超额 = 组合年收益 − {bench_sel or '基准'}年收益")
                _pfy = _period_frame("%Y", "年份")
                _sub = ["收益率"] + ([c for c in ("基准", "超额") if c in _pfy.columns])
                st.dataframe(_pfy.style
                             .format(formatter={c: "{:.2%}" for c in _sub}, na_rep="")
                             .map(_pct_cell, subset=_sub),
                             hide_index=True, width="content")

    # ---------- Tab3 回测明细 ----------
    with tab_detail:
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
                _ne_reason = ("有行情但未进入最近 y 条目标窗口,多为防守期发布的推荐,策略上不买入"
                              if algo == ALGO_REGIME
                              else "推荐日之后有行情但从未买入(被更新推荐挤出窗口或数据异常,"
                                   "请检查行情数据)")
                issues.append(f"未进场 {len(not_entered)} 条({_ne_reason}):"
                              f"{'、'.join(dict.fromkeys(not_entered['name']))[:120]}…")
            pending = summary[summary["status"] == "未到买入日"]
            if not pending.empty:
                issues.append(f"未到买入日(推荐日晚于最新行情):{'、'.join(dict.fromkeys(pending['name']))}")
            if issues:
                with st.expander(f"问题清单({len(issues)})"):
                    for s in issues:
                        st.write(s)

    # ---------- Tab4 导出 ----------
    with tab_export:
        st.markdown("导出文件写入 output/ 目录")

        def _write_export(df: pd.DataFrame, prefix: str):
            _ensure_dirs()
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = OUTPUT_DIR / f"{prefix}_{ts}.xlsx"
            df.to_excel(path, index=False, engine="openpyxl")
            return path

        c1, c2, c3 = st.columns(3)
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
                        # 导出始终用全量回测(不受攻防贡献视图过滤影响)
                        portfolio_full.drop(columns=["_dt"], errors="ignore").to_excel(
                            writer, sheet_name="每日组合", index=False)
                        daily_holdings.to_excel(writer, sheet_name="每日持仓明细", index=False)
                    st.success(f"已导出:{path}")
                    st.download_button("下载", path.read_bytes(),
                                       file_name=path.name)
                else:
                    st.info("暂无数据可导出")
        with c3:
            if st.button("导出买入持有收益阶梯", width="stretch"):
                if recs is not None and not recs.empty:
                    market = _load_market(db_key, version)
                    ladder = _build_hold_ladder(recs, market)
                    # 末尾追加平均收益率行:每个收益率列对非空值求平均
                    avg = {"股票代码": "平均收益率"}
                    for col in ladder.columns[3:]:
                        s = ladder[col].dropna()
                        avg[col] = round(float(s.mean()), 6) if len(s) else None
                    ladder = pd.concat([ladder, pd.DataFrame([avg])],
                                       ignore_index=True)
                    date_mat = _build_hold_date_matrix(recs, market)
                    _ensure_dirs()
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    path = OUTPUT_DIR / f"收益阶梯_{ts}.xlsx"
                    with pd.ExcelWriter(path, engine="openpyxl") as writer:
                        ladder.to_excel(writer, sheet_name="收益阶梯", index=False)
                        date_mat.to_excel(writer, sheet_name="按日期对齐", index=False)
                    from openpyxl import load_workbook
                    wb = load_workbook(path)
                    _style_ret_sheet(wb["收益阶梯"], start_col=4, freeze="D2",
                                     bold_last_row=True)
                    _style_ret_sheet(wb["按日期对齐"], start_col=4, freeze="D2",
                                     bold_last_row=False)
                    wb.save(path)
                    st.success(f"已导出:{path}(收益阶梯 {len(ladder)} 行 × "
                               f"{len(ladder.columns)} 列含平均行;"
                               f"按日期对齐 {len(date_mat)} 行 × {len(date_mat.columns)} 列)")
                    st.download_button("下载", path.read_bytes(),
                                       file_name=path.name)
                else:
                    st.info("请先在侧边栏选择输入 Excel")

    # ---------- Tab5 交易计划 ----------
    with tab_plan:
        if recs is None or recs.empty:
            st.info("需要推荐数据,先在侧边栏选择输入 Excel")
        elif algo == ALGO_FIXED:
            last_closed, plan_date = _next_plan_date(recs)
            if plan_date is None:
                st.info("交易日历未覆盖下一交易日,点侧边栏「初始化/更新行情数据」补拉日历后重试")
            else:
                plan = _trade_plan_fixed(plan_date, last_closed, recs,
                                         daily_holdings, hold_days)
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("计划日期", plan_date)
                m2.metric("算法", f"固定持有{hold_days}日等权")
                m3.metric("计划买入", f"{len(plan['buys'])} 只")
                m4.metric("计划卖出", f"{len(plan['sells'])} 只")
                for s in plan["notes"]:
                    st.warning(s)

                rows = []
                for p in plan["sells"]:
                    src = plan["cur"][p]
                    rows.append({"操作": "卖出(到期)", "名称": src["name"],
                                 "代码": src["ts_code"], "推荐日期": src["rec_date"],
                                 "买入日期": src["entry_date"],
                                 "已持有": f"{src['trade_days']} 个交易日"})
                for _pos, ts, name, sd in plan["buys"]:
                    rows.append({"操作": "买入(新增)", "名称": name, "代码": ts,
                                 "推荐日期": sd, "买入日期": plan_date + "(计划)",
                                 "已持有": "—"})
                for p in plan["holds"]:
                    src = plan["cur"][p]
                    rows.append({"操作": "持有", "名称": src["name"],
                                 "代码": src["ts_code"], "推荐日期": src["rec_date"],
                                 "买入日期": src["entry_date"],
                                 "已持有": f"{src['trade_days']} 个交易日"})
                plan_df = pd.DataFrame(rows)
                if plan_df.empty:
                    st.info(f"{plan_date} 无持仓计划(空仓)")
                else:
                    order = {"卖出(到期)": 0, "买入(新增)": 1, "持有": 2}
                    plan_df = plan_df.sort_values(
                        "操作", key=lambda s: s.map(order)).reset_index(drop=True)
                    st.dataframe(plan_df, width="stretch", hide_index=True)
                # 快照存档:留痕"当天计划是什么"(行情/参数变了计划会变),供月度对照核对
                if st.button("存档今日计划", key="btn_plan_snapshot"):
                    snap = {
                        "plan_date": plan_date,
                        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "algo": ALGO_FIXED,
                        "params": {"hold_days": hold_days},
                        "cur_bars_date": plan["cur_date"],
                        "actions": rows,
                        "notes": plan["notes"],
                    }
                    snap_dir = OUTPUT_DIR / "plan_snapshots"
                    snap_dir.mkdir(parents=True, exist_ok=True)
                    snap_path = snap_dir / f"{plan_date}.json"
                    snap_path.write_text(json.dumps(snap, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
                    st.success(f"已存档 {plan_date} 计划 → {snap_path}")
                st.caption(f"计划 = 固定持有{hold_days}日等权规则在计划日的交易:"
                           "起始日期 ≤ 计划日且未持有未卖出的推荐开盘买入;"
                           f"已持有满 {hold_days} 个交易日(含买入日,停牌日不计)的持仓计划日开盘卖出"
                           "(即第 N+1 个交易日);其余持有,全部等权 1/持仓数。"
                           "买入当日收益=实体涨幅,卖出当日收益=开盘涨跌幅;"
                           "计划日停牌则实际顺延到下一个有行情交易日。"
                           "决策依据与回测完全同源(rules.make_fixed_hold_rule)。")
        elif algo == ALGO_SLOT:
            last_closed, plan_date = _next_plan_date(recs)
            if plan_date is None:
                st.info("交易日历未覆盖下一交易日,点侧边栏「初始化/更新行情数据」补拉日历后重试")
            else:
                plan = _trade_plan_slot(plan_date, last_closed, recs,
                                        daily_holdings, slot_w, slot_d)
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("计划日期", plan_date)
                m2.metric("算法", f"限仓轮换 每只{slot_w}成×{slot_d}日")
                m3.metric("计划买入", f"{len(plan['buys'])} 只")
                m4.metric("计划卖出", f"{len(plan['sells'])} 只")
                for s in plan["notes"]:
                    st.warning(s)

                rows = []
                for p in plan["sells"]:
                    src = plan["cur"][p]
                    if src["trade_days"] >= slot_d:
                        op, why = "卖出(到期)", f"持有满 {slot_d} 个交易日"
                    else:
                        op, why = "卖出(被替换)", "被更新推荐挤出(FIFO)"
                    rows.append({"操作": op, "名称": src["name"], "代码": src["ts_code"],
                                 "推荐日期": src["rec_date"], "买入日期": src["entry_date"],
                                 "已持有": f"{src['trade_days']} 个交易日", "说明": why})
                for _p, ts, name, sd in plan["buys"]:
                    rows.append({"操作": "买入(新增)", "名称": name, "代码": ts,
                                 "推荐日期": sd, "买入日期": plan_date + "(计划)",
                                 "已持有": "—", "说明": f"固定 {slot_w} 成,其余现金"})
                for p in plan["holds"]:
                    src = plan["cur"][p]
                    rows.append({"操作": "持有", "名称": src["name"],
                                 "代码": src["ts_code"], "推荐日期": src["rec_date"],
                                 "买入日期": src["entry_date"],
                                 "已持有": f"{src['trade_days']} 个交易日", "说明": ""})
                plan_df = pd.DataFrame(rows)
                if plan_df.empty:
                    st.info(f"{plan_date} 无持仓计划(空仓)")
                else:
                    order = {"卖出(到期)": 0, "卖出(被替换)": 1, "买入(新增)": 2, "持有": 3}
                    plan_df = plan_df.sort_values(
                        "操作", key=lambda s: s.map(order)).reset_index(drop=True)
                    st.dataframe(plan_df, width="stretch", hide_index=True)
                if st.button("存档今日计划", key="btn_plan_snapshot"):
                    snap = {
                        "plan_date": plan_date,
                        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "algo": ALGO_SLOT,
                        "params": {"weight": slot_w, "hold_days": slot_d},
                        "cur_bars_date": plan["cur_date"],
                        "actions": rows,
                        "notes": plan["notes"],
                    }
                    snap_dir = OUTPUT_DIR / "plan_snapshots"
                    snap_dir.mkdir(parents=True, exist_ok=True)
                    snap_path = snap_dir / f"{plan_date}.json"
                    snap_path.write_text(json.dumps(snap, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
                    st.success(f"已存档 {plan_date} 计划 → {snap_path}")
                st.caption(f"计划 = 限仓轮换规则在计划日的交易:目标 = 未卖出推荐中最新 "
                           f"{plan['capacity']} 只(满仓 10 成 ÷ 每只 {slot_w} 成);"
                           "计划日开盘买入目标内未持有的新推荐(每只固定成数,其余现金),"
                           f"卖出不在目标的旧仓(被更新推荐 FIFO 挤出)与持有满 {slot_d} "
                           "个交易日(含买入日,停牌日不计)的到期仓;其余持有。"
                           "同日同股已合并为一个仓位。"
                           "买入当日收益=实体涨幅,卖出当日收益=开盘涨跌幅;"
                           "计划日停牌则实际顺延到下一个有行情交易日。"
                           "决策依据与回测完全同源(rules.make_slot_rule)。")
        elif regime_data is None:
            st.info("攻防转换需要仓位/防守文件,先在侧边栏选择输入 Excel")
        else:
            last_closed, plan_date = _next_plan_date(recs)
            if plan_date is None:
                st.info("交易日历未覆盖下一交易日,点侧边栏「初始化/更新行情数据」补拉日历后重试")
            else:
                plan = _trade_plan(plan_date, last_closed, recs, regime_data,
                                   daily_holdings, x_th, y_off, z_def,
                                   _bars_by_date(db_key, version), prev_skip, mpd)
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("计划日期", plan_date)
                m2.metric("攻防状态", "进攻" if plan["offense"] else "防守")
                m3.metric("仓位", f"{plan['pos']:.1f} 成" if plan["pos"] is not None
                          else "仓位表未覆盖")
                m4.metric("目标持仓", f"{len(plan['target'])} 只")
                for s in plan["notes"]:
                    st.warning(s)

                rows = []
                for p in plan["sell"]:
                    src = plan["cur"][p]
                    rows.append({"操作": "卖出(被替换)", "名称": src["name"],
                                 "代码": src["ts_code"],
                                 "推荐日期": src["rec_date"],
                                 "买入日期": src["entry_date"]})
                for p in plan["buy"]:
                    r = plan["lookup"][p]
                    rows.append({"操作": "买入(新增)", "名称": r[2], "代码": r[1],
                                 "推荐日期": r[3], "买入日期": plan_date + "(计划)"})
                for p in plan["hold"]:
                    src = plan["cur"][p]
                    rows.append({"操作": "持有", "名称": src["name"],
                                 "代码": src["ts_code"], "推荐日期": src["rec_date"],
                                 "买入日期": src["entry_date"]})
                plan_df = pd.DataFrame(rows)
                if plan_df.empty:
                    st.info(f"{plan_date} 无持仓计划(空仓)")
                else:
                    order = {"卖出(被替换)": 0, "买入(新增)": 1, "持有": 2}
                    plan_df = plan_df.sort_values(
                        "操作", key=lambda s: s.map(order)).reset_index(drop=True)
                    st.dataframe(plan_df, width="stretch", hide_index=True)
                # 快照存档:留痕“当天计划是什么”(行情/参数变了计划会变),供月度对照做计划 vs 实际成交核对
                if st.button("存档今日计划", key="btn_plan_snapshot"):
                    snap = {
                        "plan_date": plan_date,
                        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "offense": bool(plan["offense"]),
                        "pos": plan["pos"],
                        "pos_inherited": bool(plan["inherited"]),
                        "cur_bars_date": plan["cur_date"],
                        "params": {"x": x_th, "y": y_off, "z": z_def, "max_per_day": mpd,
                                   "prev_gain_skip": prev_skip, "open_gap_skip": gap_skip,
                                   "limit_up_skip": limit_fill},
                        "target": [[int(p), *plan["lookup"][p][1:]]
                                   for p in sorted(plan["target"])],
                        "actions": rows,
                        "notes": plan["notes"],
                    }
                    snap_dir = OUTPUT_DIR / "plan_snapshots"
                    snap_dir.mkdir(parents=True, exist_ok=True)
                    snap_path = snap_dir / f"{plan_date}.json"
                    snap_path.write_text(json.dumps(snap, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
                    st.success(f"已存档 {plan_date} 计划 → {snap_path}")
                _gap_note = (f"开盘涨幅过滤:新买入股票当日开盘涨幅(开盘/昨收-1)超 "
                             f"{gap_skip:.0%} 时实盘会直接舍弃该推荐(视同无此股票,"
                             "永不买入)、由后续推荐补位,计划表无法预知次日开盘价,"
                             "以实际成交为准"
                             if gap_skip is not None
                             else "开盘涨幅过滤:未启用(OPEN_GAP_SKIP = None)")
                _pgain_note = (f"前收涨幅过滤:推荐前一交易日收盘涨幅超 {prev_skip:.0%} "
                               "的已从目标池剔除(与回测口径一致)"
                               if prev_skip is not None
                               else "前收涨幅过滤:未启用(PREV_DAY_GAIN_SKIP = None)")
                _mpd_note = (f"单日上限:同一推荐日至多 {mpd} 条进窗口(MAX_PER_DAY)"
                             if mpd is not None
                             else "单日上限:未启用(MAX_PER_DAY = None,同日并列全保留)")
                _limit_note = ("涨跌停可成交性:开盘即涨停的新买推荐实盘挂单大概率不成,"
                               "回测按舍弃处理;开盘跌停的持仓当日卖不出、顺延"
                               "(LIMIT_UP_SKIP)" if limit_fill else
                               "涨跌停可成交性:未启用(LIMIT_UP_SKIP = False)")
                st.caption("计划 = 攻防规则在计划日的目标持仓与最新收盘持仓的差分:"
                           "新增推荐买入、跌出最近 y/z 条窗口的旧仓卖出(替换最旧的优先),"
                           "其余持有;同股卖旧买新合并保留最早进场仓。"
                           "实际成交按次日开盘价,停牌顺延;每日 15:00 收盘后计划自动切到下一交易日。"
                           "决策依据与回测完全同源(rules.target_keys 共用)。"
                           + _mpd_note + "。" + _limit_note + "。" + _pgain_note + "。"
                           + _gap_note)
