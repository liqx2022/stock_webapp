"""回测引擎:推荐记录 + 行情 + 规则 → 每日持仓/组合净值/个股汇总。

口径与旧脚本 股票统计1_0628智能评分收益回测系统.py 对齐:
- 买入当日收益 = 实体涨幅(开盘→收盘);之后每日 = pct_chg/100
- 累计收益 = (1+r) 连乘 - 1;停牌日无行情 → 收益按 None,连乘跳过(旧脚本 dayN真 口径)
- 组合收益 = 当日有行情的持仓等权平均,含当日收盘卖出的持仓(与旧表"平均收盘涨幅"行同口径)
- 卖出按当日收盘(当日仍计收益),买入按当日开盘
"""
import pandas as pd

from indicators import body_return, daily_return
from rules import decide as _default_rule
from rules import Holding


def _dedup_recs(recs: pd.DataFrame) -> pd.DataFrame:
    """同股多条推荐按代码去重:最早起始日期、最高评分(与旧脚本去重哲学一致)。"""
    if recs.empty:
        return recs
    df = recs.copy()
    df["score"] = pd.to_numeric(df.get("score"), errors="coerce").fillna(0)
    df = df.sort_values(["start_date", "score"], ascending=[True, False])
    return df.drop_duplicates(subset=["ts_code"], keep="first").reset_index(drop=True)


def _build_bars_by_date(market: pd.DataFrame) -> dict:
    """长表 → {date: {ts_code: bar_dict}}"""
    result = {}
    for _, row in market.iterrows():
        bar = {
            "open": row.get("open"), "high": row.get("high"),
            "low": row.get("low"), "close": row.get("close"),
            "pre_close": row.get("pre_close"), "pct_chg": row.get("pct_chg"),
            "vol": row.get("vol"),
        }
        result.setdefault(row["trade_date"], {})[row["ts_code"]] = bar
    return result


def run_backtest(recs: pd.DataFrame, market: pd.DataFrame, trade_dates: list[str],
                 rule=_default_rule) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """返回 (daily_holdings, portfolio_daily, rec_summary)。

    recs:  DataFrame,列 ts_code/name/start_date/score(已去重)
    market: load_market_data() 的长表
    trade_dates: 升序交易日列表
    """
    recs = _dedup_recs(recs)
    bars_by_date = _build_bars_by_date(market)

    # 日期范围:从最早推荐日起,到行情最晚日
    if not trade_dates:
        trade_dates = sorted(bars_by_date.keys())
    min_rec = recs["start_date"].min() if not recs.empty else None
    max_data = max(bars_by_date.keys()) if bars_by_date else None
    if min_rec is None or max_data is None:
        return (pd.DataFrame(columns=["date", "code", "name", "rec_date", "entry_date",
                                      "score", "weight", "day_return", "cum_return",
                                      "trade_days"]),
                pd.DataFrame(columns=["date", "n_stocks", "avg_return", "nav", "drawdown"]),
                pd.DataFrame(columns=["code", "name", "rec_date", "score", "entry_date",
                                      "last_date", "n_trade_days", "cum_return", "status"]))
    dates = [d for d in trade_dates if min_rec <= d <= max_data]

    rec_rows = recs.set_index("ts_code").to_dict("index")

    holdings = {}          # ts_code -> Holding
    holding_rows = []      # daily_holdings 行
    portfolio_rows = []    # portfolio_daily 行
    entry_info = {}        # ts_code -> (entry_date, rec_date, score, name)
    trade_day_count = {}   # ts_code -> 已交易天数(entry 起,含 entry 日)
    last_bar_date = {}     # ts_code -> 最后有 bar 的日期
    last_cum = {}          # ts_code -> 最后 cum_return

    nav = 1.0
    peak_nav = 1.0

    for date in dates:
        day_bars = bars_by_date.get(date, {})
        recs_upto = [(code, rec_rows[code]["name"], rec_rows[code]["start_date"],
                      rec_rows[code]["score"])
                     for code in rec_rows if rec_rows[code]["start_date"] <= date]

        actions = rule(date, holdings, day_bars, recs_upto)

        # 1. 卖出:当日仍计收益(收盘卖出),记录后移除
        # 收盘卖出 = 当日整天仍持有,卖出日收益要计入组合当日平均
        sold_today = {}   # ts_code -> (day_return, weight)
        for act in actions:
            if act.op == "sell" and act.code in holdings:
                h = holdings[act.code]
                r = None
                bar = day_bars.get(act.code)
                if bar is not None:
                    r = daily_return(bar["pct_chg"])
                    trade_day_count[act.code] = trade_day_count.get(act.code, 0) + 1
                    last_bar_date[act.code] = date
                if r is not None:
                    h = h._replace(cum_return=round((1 + (h.cum_return or 0)) * (1 + r) - 1, 6))
                    sold_today[act.code] = (r, 1.0 if h.weight is None else float(h.weight))
                holding_rows.append(_hold_row(date, h, entry_info, weight=None,
                                              day_return=r,
                                              trade_days=trade_day_count.get(act.code, 0)))
                last_cum[act.code] = h.cum_return
                del holdings[act.code]

        # 2. 买入:按当日开盘;当日无 bar(停牌)则跳过,规则下个交易日自然重发
        for act in actions:
            if act.op != "buy" or act.code in holdings:
                continue
            info = rec_rows.get(act.code)
            if info is None:
                continue
            bar = day_bars.get(act.code)
            if bar is None:
                continue
            if act.code not in entry_info:
                entry_info[act.code] = (date, info["start_date"], info["score"], info["name"])
            holdings[act.code] = Holding(code=act.code, entry_date=date,
                                         weight=act.weight, cum_return=None)
            trade_day_count[act.code] = 1
            last_bar_date[act.code] = date

        # 3. 计算当日各持仓收益(entry 当日用实体涨幅,之后用全日涨跌;无 bar → None)
        day_returns = {}
        weights = {}
        for code, h in holdings.items():
            bar = day_bars.get(code)
            if bar is None:
                day_returns[code] = None
            elif h.entry_date == date:
                day_returns[code] = body_return(bar["open"], bar["close"])
                trade_day_count[code] = 1
                last_bar_date[code] = date
            else:
                day_returns[code] = daily_return(bar["pct_chg"])
                trade_day_count[code] = trade_day_count.get(code, 0) + 1
                last_bar_date[code] = date
            weights[code] = 1.0 if h.weight is None else float(h.weight)

        # 4. 更新累计收益(连乘,None 跳过)
        for code, h in holdings.items():
            r = day_returns[code]
            if r is not None:
                h = h._replace(cum_return=round((1 + (h.cum_return or 0)) * (1 + r) - 1, 6))
                holdings[code] = h

        # 5. 组合收益:当日有行情的持仓 + 当日收盘卖出的持仓,按权重归一化平均
        rets = {c: (day_returns[c], weights[c]) for c in holdings
                if day_returns[c] is not None}
        rets.update(sold_today)
        if rets:
            w_sum = sum(w for _r, w in rets.values())
            avg_return = round(sum(r * w / w_sum for r, w in rets.values()), 6)
        else:
            avg_return = None

        if avg_return is not None:
            nav = round(nav * (1 + avg_return), 6)
        peak_nav = max(peak_nav, nav)
        drawdown = round(nav / peak_nav - 1, 6) if peak_nav > 0 else 0.0

        portfolio_rows.append({
            "date": date, "n_stocks": len(holdings) + len(sold_today),
            "avg_return": avg_return, "nav": nav, "drawdown": drawdown,
        })

        # 6. 当日持仓明细行
        for code, h in holdings.items():
            holding_rows.append(_hold_row(date, h, entry_info, weight=weights.get(code),
                                          day_return=day_returns[code],
                                          trade_days=trade_day_count.get(code, 0)))

    # 循环结束:仍持有的股票记录最终累计收益
    for code, h in holdings.items():
        last_cum[code] = h.cum_return

    # 个股汇总
    rec_summary_rows = []
    for code, info in rec_rows.items():
        entry_date, rec_date, score, name = entry_info.get(
            code, (None, info["start_date"], info["score"], info["name"]))
        if code in entry_info:
            status = "正常"
        elif info["start_date"] > max_data:
            status = "未到买入日"  # 推荐日晚于最新行情日,尚未到达买入时间
        else:
            status = "无行情数据"  # 区间内无任何行情(停牌/未上市/代码错)
        rec_summary_rows.append({
            "code": code, "name": info["name"], "rec_date": info["start_date"],
            "score": info["score"], "entry_date": entry_date,
            "last_date": last_bar_date.get(code),
            "n_trade_days": trade_day_count.get(code, 0),
            "cum_return": last_cum.get(code),
            "status": status,
        })

    daily_holdings = pd.DataFrame(holding_rows,
                                  columns=["date", "code", "name", "rec_date", "entry_date",
                                           "score", "weight", "day_return", "cum_return",
                                           "trade_days"])
    portfolio_daily = pd.DataFrame(portfolio_rows)
    rec_summary = pd.DataFrame(rec_summary_rows)
    return daily_holdings, portfolio_daily, rec_summary


def _hold_row(date, h, entry_info, weight, day_return, trade_days):
    entry_date, rec_date, score, name = entry_info.get(h.code, (h.entry_date, None, None, h.code))
    return {
        "date": date, "code": h.code, "name": name,
        "rec_date": rec_date, "entry_date": entry_date,
        "score": score, "weight": weight,
        "day_return": day_return, "cum_return": h.cum_return,
        "trade_days": trade_days,
    }
