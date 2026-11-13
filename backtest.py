"""回测引擎:推荐记录 + 行情 + 规则 → 每日持仓/组合净值/个股汇总。

口径与旧脚本 股票统计1_0628智能评分收益回测系统.py 对齐:
- 每条推荐是独立交易(持仓键 pos=推荐行号),同股多条推荐各自开仓、互不影响
- 个股累计收益口径:买入当日 = 实体涨幅(开盘→收盘),之后每日 = pct_chg/100;
  停牌日无行情 → 收益按 None,连乘跳过
- 当日账户收益率 = (1 + 上一交易日持仓的平均开盘涨跌幅) × (1 + 当日收盘持仓的平均实体涨幅) - 1
  (等权;停牌不进平均;空集合因子记 1;完整口径见 RULES.md「当日账户收益率」)
- 卖出分两种:"sell" 按当日收盘(旧规则);"sell_open" 按当日开盘(攻防规则),
  卖出腿当日收益率 = 开盘涨跌幅(开盘/昨收-1);买入一律当日开盘
- 持仓明细「当日收益率」展示口径:买入=实体涨幅、持有=全天涨跌幅(收盘/昨收-1,
  即 pct_chg/100,与累计收益连乘口径一致)、卖出=开盘涨跌幅;
  账户收益第二因子不受展示口径影响,仍为实体涨幅
- 手续费(fee 参数,费率见 config.py)按成交金额计,从当日净值扣减
- 停牌日(当日无行情)买卖动作一律跳过:持仓保留,规则下个交易日自然重发
- 同一推荐卖出后再次买入(如防守 ETF 被攻防切换卖出后又买回)= 新一轮:
  进场日期/交易天数/累计收益按新一轮起算,entry_info 记录最新一轮
"""
import pandas as pd

from data_fetcher import is_etf
from indicators import body_return, daily_return, gap_return
from rules import decide as _default_rule
from rules import Holding


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
                 rule=_default_rule, fee: dict | None = None
                 ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """返回 (daily_holdings, portfolio_daily, rec_summary)。

    recs:  DataFrame,列 ts_code/name/start_date。不去重:每行一条独立推荐,
           行号即持仓键 pos,同股多条推荐各自独立交易
    market: load_market_data() 的长表
    trade_dates: 升序交易日列表
    fee:   费率表 {stock_buy, stock_sell, etf_buy, etf_sell}(占成交金额比例),
           None 表示不计手续费。成交金额按等权满仓近似:每笔 ±1/当日持仓数
    """
    recs = recs.reset_index(drop=True)
    bars_by_date = _build_bars_by_date(market)

    # 日期范围:从最早推荐日起,到行情最晚日
    if not trade_dates:
        trade_dates = sorted(bars_by_date.keys())
    min_rec = recs["start_date"].min() if not recs.empty else None
    max_data = max(bars_by_date.keys()) if bars_by_date else None
    if min_rec is None or max_data is None:
        return (pd.DataFrame(columns=["date", "code", "name", "action", "rec_date",
                                      "entry_date", "weight", "day_return", "cum_return",
                                      "trade_days", "pos"]),
                pd.DataFrame(columns=["date", "n_stocks", "account_return", "fee", "nav",
                                      "drawdown"]),
                pd.DataFrame(columns=["code", "name", "rec_date", "entry_date",
                                      "last_date", "n_trade_days", "cum_return", "status"]))
    dates = [d for d in trade_dates if min_rec <= d <= max_data]

    rec_rows = {pos: recs.iloc[pos].to_dict() for pos in range(len(recs))}
    last_bar = {}    # ts_code -> 最晚有行情的日期(用于区分"未进场"与"真缺数据")
    for d, day in bars_by_date.items():
        for code in day:
            if code not in last_bar or d > last_bar[code]:
                last_bar[code] = d

    holdings = {}          # pos -> Holding(code=ts_code)
    holding_rows = []      # daily_holdings 行
    portfolio_rows = []    # portfolio_daily 行
    entry_info = {}        # pos -> (entry_date, rec_date, name)
    trade_day_count = {}   # pos -> 已交易天数(entry 起,含 entry 日)
    last_bar_date = {}     # pos -> 最后有 bar 的日期
    last_cum = {}          # pos -> 最后 cum_return

    prev_codes = {}        # 上一交易日收盘持仓 pos -> ts_code(账户收益第一因子输入)
    nav = 1.0
    peak_nav = 1.0

    for date in dates:
        day_bars = bars_by_date.get(date, {})
        fee_day = 0.0
        recs_upto = [(pos, info["ts_code"], info["name"], info["start_date"])
                     for pos, info in rec_rows.items() if info["start_date"] <= date]

        actions = rule(date, holdings, day_bars, recs_upto)

        # 1. 卖出:sell=收盘价(旧规则,当日仍整天持有);sell_open=开盘价(攻防规则),
        #    卖出腿当日收益率=开盘涨跌幅(开盘/昨收-1),计入当日账户收益第一因子
        n_before = max(len(holdings), 1)   # 卖出成交金额 ≈ 组合的 1/n(等权满仓近似)
        for act in actions:
            if act.op in ("sell", "sell_open") and act.pos in holdings:
                h = holdings[act.pos]
                bar = day_bars.get(h.code)
                if bar is None:
                    # 停牌无法成交:保留持仓,规则下个交易日自然重发(与买入跳过同口径,
                    # RULES.md:顺延到下一个有行情的交易日按开盘执行)
                    continue
                g = gap_return(bar["open"], bar["pre_close"])
                b = body_return(bar["open"], bar["close"])
                c = daily_return(bar["pct_chg"])
                r = g if act.op == "sell_open" else c
                trade_day_count[act.pos] = trade_day_count.get(act.pos, 0) + 1
                last_bar_date[act.pos] = date
                if fee is not None:
                    rate = fee["etf_sell"] if is_etf(h.code) else fee["stock_sell"]
                    fee_day += (1.0 / n_before) * rate
                if r is not None:
                    h = h._replace(cum_return=round((1 + (h.cum_return or 0)) * (1 + r) - 1, 6))
                holding_rows.append(_hold_row(date, act.pos, h, entry_info, weight=0.0,
                                              day_return=r, action="sell",
                                              gap_ret=g, body_ret=b, close_ret=c,
                                              trade_days=trade_day_count.get(act.pos, 0)))
                last_cum[act.pos] = h.cum_return
                del holdings[act.pos]

        # 2. 买入:按当日开盘;当日无 bar(停牌)则跳过,规则下个交易日自然重发
        bought_codes = []
        for act in actions:
            if act.op != "buy" or act.pos in holdings:
                continue
            info = rec_rows.get(act.pos)
            if info is None:
                continue
            bar = day_bars.get(info["ts_code"])
            if bar is None:
                continue
            # 同一推荐卖出后再买入 = 新一轮:进场信息覆盖为最新一轮
            # (买入日期/交易天数/累计收益都从本轮起算,页面"买入日期"展示重新买入日)
            entry_info[act.pos] = (date, info["start_date"], info["name"])
            holdings[act.pos] = Holding(code=info["ts_code"], entry_date=date,
                                        weight=act.weight, cum_return=None)
            trade_day_count[act.pos] = 1
            last_bar_date[act.pos] = date
            bought_codes.append(info["ts_code"])
        if fee is not None and bought_codes:
            n_after = max(len(holdings), 1)
            for code in bought_codes:
                rate = fee["etf_buy"] if is_etf(code) else fee["stock_buy"]
                fee_day += (1.0 / n_after) * rate

        # 3. 当日各持仓收益:day_returns 用于累计收益连乘(entry 当日实体、之后全日涨跌);
        #    body_returns 为展示/账户因子口径(实体涨幅);无 bar(停牌)→ None;
        #    gap/close/body 三字典为持仓明细展示列(开盘/收盘/实体涨幅)
        day_returns = {}
        body_returns = {}
        gap_returns = {}
        close_returns = {}
        weights = {}
        for pos, h in holdings.items():
            bar = day_bars.get(h.code)
            if bar is None:
                day_returns[pos] = None
                body_returns[pos] = None
                gap_returns[pos] = None
                close_returns[pos] = None
            else:
                body = body_return(bar["open"], bar["close"])
                body_returns[pos] = body
                gap_returns[pos] = gap_return(bar["open"], bar["pre_close"])
                close_returns[pos] = daily_return(bar["pct_chg"])
                if h.entry_date == date:
                    day_returns[pos] = body
                    trade_day_count[pos] = 1
                else:
                    day_returns[pos] = daily_return(bar["pct_chg"])
                    trade_day_count[pos] = trade_day_count.get(pos, 0) + 1
                last_bar_date[pos] = date
            weights[pos] = 1.0 if h.weight is None else float(h.weight)

        # 4. 更新累计收益(连乘,None 跳过)
        for pos, h in holdings.items():
            r = day_returns[pos]
            if r is not None:
                h = h._replace(cum_return=round((1 + (h.cum_return or 0)) * (1 + r) - 1, 6))
                holdings[pos] = h

        # 5. 当日账户收益率 = (1+上一交易日持仓今日平均开盘涨跌幅)×(1+今日收盘持仓平均实体涨幅)-1
        #    (等权;停牌不进平均;空集合因子记 1;口径见 RULES.md「当日账户收益率」)
        gaps = []
        for code in prev_codes.values():
            bar = day_bars.get(code)
            if bar is not None:
                g = gap_return(bar["open"], bar["pre_close"])
                if g is not None:
                    gaps.append(g)
        bodies = [b for b in body_returns.values() if b is not None]
        avg_gap = round(sum(gaps) / len(gaps), 6) if gaps else 0.0
        avg_body = round(sum(bodies) / len(bodies), 6) if bodies else 0.0
        account_return = round((1 + avg_gap) * (1 + avg_body) - 1, 6)

        # 组合净值:账户收益率 - 当日手续费拖累
        net = account_return - fee_day
        if net:
            nav = round(nav * (1 + net), 6)
        peak_nav = max(peak_nav, nav)
        drawdown = round(nav / peak_nav - 1, 6) if peak_nav > 0 else 0.0

        portfolio_rows.append({
            "date": date, "n_stocks": len(holdings),
            "account_return": account_return, "fee": fee_day, "nav": nav,
            "drawdown": drawdown,
        })

        # 6. 当日持仓明细行(操作=买入/持有;weight 为归一化仓位占比)
        #    展示口径:买入=实体涨幅;持有=全天涨跌幅(收盘/昨收-1,即 day_returns
        #    的 pct_chg/100,与累计收益连乘口径一致);卖出=开盘涨跌幅(见步骤 1)
        w_sum = sum(weights.values())
        for pos, h in holdings.items():
            w = weights.get(pos, 1.0) / w_sum if w_sum else None
            is_entry = h.entry_date == date
            holding_rows.append(_hold_row(date, pos, h, entry_info, weight=w,
                                          day_return=(body_returns.get(pos) if is_entry
                                                      else day_returns.get(pos)),
                                          gap_ret=gap_returns.get(pos),
                                          body_ret=body_returns.get(pos),
                                          close_ret=close_returns.get(pos),
                                          action="buy" if is_entry else "hold",
                                          trade_days=trade_day_count.get(pos, 0)))

        # 记录收盘持仓,供次日账户收益第一因子使用
        prev_codes = {pos: h.code for pos, h in holdings.items()}

    # 循环结束:仍持有的股票记录最终累计收益
    for pos, h in holdings.items():
        last_cum[pos] = h.cum_return

    # 推荐汇总(每条推荐一行,同股多轮各一行)
    rec_summary_rows = []
    for pos, info in rec_rows.items():
        entry_date, rec_date, name = entry_info.get(
            pos, (None, info["start_date"], info["name"]))
        lb = last_bar.get(info["ts_code"])
        if pos in entry_info:
            status = "正常"
        elif info["start_date"] > max_data:
            status = "未到买入日"  # 推荐日晚于最新行情日,尚未到达买入时间
        elif lb is not None and lb >= info["start_date"]:
            status = "未进场"  # 推荐日后有行情但从未进入目标窗口(防守期发布/被新推荐挤出)
        else:
            status = "无行情数据"  # 推荐日之后整个区间无行情(长期停牌/退市/代码错)
        rec_summary_rows.append({
            "code": info["ts_code"], "name": info["name"], "rec_date": info["start_date"],
            "entry_date": entry_date,
            "last_date": last_bar_date.get(pos),
            "n_trade_days": trade_day_count.get(pos, 0),
            "cum_return": last_cum.get(pos),
            "status": status,
        })

    daily_holdings = pd.DataFrame(holding_rows,
                                  columns=["date", "code", "name", "action", "rec_date",
                                           "entry_date", "weight", "open_ret", "body_ret",
                                           "close_ret", "day_return", "cum_return",
                                           "trade_days", "pos"])
    portfolio_daily = pd.DataFrame(portfolio_rows)
    rec_summary = pd.DataFrame(rec_summary_rows)
    return daily_holdings, portfolio_daily, rec_summary


def _hold_row(date, pos, h, entry_info, weight, day_return, trade_days, action,
              gap_ret=None, body_ret=None, close_ret=None):
    """单日持仓明细行。gap/body/close 为该股当日 开盘/实体/收盘 涨幅(展示列,
    与操作无关;停牌无 bar → None)。"""
    entry_date, rec_date, name = entry_info.get(pos, (h.entry_date, None, h.code))
    return {
        "date": date, "code": h.code, "name": name, "action": action,
        "rec_date": rec_date, "entry_date": entry_date,
        "weight": weight,
        "open_ret": gap_ret, "body_ret": body_ret, "close_ret": close_ret,
        "day_return": day_return, "cum_return": h.cum_return,
        "trade_days": trade_days, "pos": pos,
    }
