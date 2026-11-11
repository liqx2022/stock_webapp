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
