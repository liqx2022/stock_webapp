"""指标定义。口径与旧脚本 股票统计1_0628智能评分收益回测系统.py 完全一致。

收益一律存小数(0.05 = 5%),None 表示无数据。
"""
import pandas as pd


def daily_return(pct_chg):
    """全日涨跌 = pct_chg / 100(tushare pct_chg 是百分数,如 3.5 表示 +3.5%)"""
    if pct_chg is None or pd.isna(pct_chg):
        return None
    return round(float(pct_chg) / 100, 6)


def body_return(open_, close):
    """实体涨幅 = 收盘/开盘 - 1(买入当日的收益口径)"""
    if open_ is None or close is None or pd.isna(open_) or pd.isna(close) or float(open_) == 0:
        return None
    return round(float(close) / float(open_) - 1, 6)


def gap_return(open_, pre_close):
    """开盘跳空 = 开盘/昨收 - 1"""
    if open_ is None or pre_close is None or pd.isna(open_) or pd.isna(pre_close) or float(pre_close) == 0:
        return None
    return round(float(open_) / float(pre_close) - 1, 6)


def compound(returns):
    """复合收益:(1+r) 连乘 - 1。跳过 None。"""
    prod = 1.0
    has_value = False
    for r in returns:
        if r is None or pd.isna(r):
            continue
        has_value = True
        prod *= (1 + float(r))
    return round(prod - 1, 6) if has_value else None


def period_return(end_cum, start_cum):
    """区间复利 = (1+end)/(1+start) - 1"""
    if end_cum is None or start_cum is None or pd.isna(end_cum) or pd.isna(start_cum):
        return None
    if (1 + float(start_cum)) == 0:
        return None
    return round((1 + float(end_cum)) / (1 + float(start_cum)) - 1, 6)


def _rebased_nav(rets: list) -> list:
    """基准净值曲线:从区间首日收盘起复利,首日净值 = 1(首日当日涨跌不进曲线,
    与组合净值的区间重定基口径一致)。"""
    nav, navs = 1.0, []
    for r in rets[1:]:
        nav *= (1 + float(r))
        navs.append(round(nav, 6))
    return [1.0] + navs if rets else []


def benchmark_navs(bench: pd.DataFrame, dates: list[str],
                   display: dict, combos: dict) -> dict:
    """基准对比曲线(组合净值页)。返回 {名称: [与 dates 等长的净值序列]}。

    bench:   长表,列 ts_code/trade_date/pct_chg(fetcher.load_benchmark_data)
    display: {ts_code: 显示名} —— 单独成线的基准
    combos:  {合成名: [成员 ts_code]} —— 成员日涨跌幅等权平均后连乘(如双创)
    对齐口径:重索引到 dates,基准当日无数据(停牌/缺数据)收益记 0、曲线持平;
    无任何数据的基准不出现在结果里。
    """
    if bench is None or bench.empty or not dates:
        return {}
    b = bench.copy()
    b["ret"] = b["pct_chg"].apply(daily_return)
    rets = (b.pivot_table(index="trade_date", columns="ts_code", values="ret",
                          aggfunc="mean")
             .reindex(dates).fillna(0.0))
    result = {}
    for code, name in display.items():
        if code in rets.columns:
            result[name] = _rebased_nav(rets[code].tolist())
    for name, members in combos.items():
        cols = [c for c in members if c in rets.columns]
        if cols:
            result[name] = _rebased_nav(rets[cols].mean(axis=1).tolist())
    return result
