"""买卖规则(可插拔)。以后用户复杂买卖规则只改本文件,不要改 backtest.py。

规则契约:
    decide(date, holdings, bars, recs) -> list[Action]
    - date:     当前交易日 'YYYY-MM-DD'
    - holdings: dict {ts_code: Holding},Holding 含 code/entry_date/weight/cum_return
    - bars:     dict {ts_code: bar},bar 为当日行情 dict(open/high/low/close/pre_close/pct_chg/vol)
                当日无行情的股票(停牌等)不出现在 bars 里
    - recs:     list[(ts_code, name, start_date, score)],起始日期 <= date 的全部推荐,引擎过滤好传入
    - 返回当日要执行的买卖动作;引擎先执行卖出、再执行买入

带内部状态的规则(持有天数、止损记忆)应写成工厂函数,每次回测新建实例,
避免跨次回测状态残留。注意 recs 是"起始日期 <= date 的全部推荐",不会因为
卖过就消失——卖出类规则要自己维护"已卖"集合,否则下个交易日会把同一推荐再买回来。
"""
from collections import namedtuple

Action = namedtuple("Action", ["op", "code", "weight"])
# op: "buy" | "sell";weight 仅 buy 用(None 表示等权,权重由引擎归一化)

Holding = namedtuple("Holding", ["code", "entry_date", "weight", "cum_return"])


def make_hold_n_days(hold_days: int = 10):
    """持有第 hold_days 个交易日收盘卖出(买入日算第 1 个交易日)。

    买入:推荐起始日起第一个有行情的交易日,按开盘价(引擎口径);
    卖出:第 hold_days 个交易日收盘价;停牌日不计持有天数,卖出顺延;
    每只股票只做一轮,卖出后不再买入。

    注意:本规则带内部状态(持有天数计数 + 已卖集合),一次回测必须传新实例,
    app.py 的 _run_backtest 每次重跑都会新建,避免跨次回测状态残留。
    """
    held_days = {}   # ts_code -> 已持有交易日数(含买入日)
    sold = set()     # 已卖出的 ts_code,防止卖出次日被再次买入

    def rule(date, holdings, bars, recs):
        actions = []
        for code in list(held_days):
            if code not in holdings:      # 引擎已移除(卖出/停牌未买成)的,清掉计数
                del held_days[code]
        for ts_code, _name, _start_date, _score in recs:
            if ts_code not in holdings and ts_code not in sold:
                actions.append(Action("buy", ts_code, None))
                held_days[ts_code] = 1    # 买入日 = 第 1 个交易日
        for code in holdings:
            if code in bars:              # 停牌日不计持有天数
                held_days[code] = held_days.get(code, 0) + 1
                if held_days[code] >= hold_days:
                    actions.append(Action("sell", code, None))
                    sold.add(code)
        return actions
    return rule


_default = make_hold_n_days(10)


def decide(date, holdings, bars, recs):
    """规则入口(默认):持有 10 个交易日收盘卖出。
    注意 _default 带状态;同一进程内多次 run_backtest 请显式传入新实例
    (app.py 已按要求每次新建)。"""
    return _default(date, holdings, bars, recs)


def hold_from_recommend(date, holdings, bars, recs):
    """v1 占位规则:推荐日起一直持有,不卖,等权。

    对每条推荐:若代码不在持仓中 → 买入。
    停牌(无 bar)时引擎会跳过本次买入,规则下个交易日自然重发,无需额外处理。
    """
    actions = []
    for ts_code, _name, _start_date, _score in recs:
        if ts_code not in holdings:
            actions.append(Action("buy", ts_code, None))
    return actions


# ====================== 未来复杂规则的编写参考 ======================
# holdings 带 entry_date 和 cum_return,可写"持有超过 N 日卖出"、"累计亏损 X% 止损":
#
# def stop_loss_rule(max_loss=-0.10, max_days=None):
#     def rule(date, holdings, bars, recs):
#         actions = []
#         for code, h in holdings.items():
#             if h.cum_return is not None and h.cum_return <= max_loss:
#                 actions.append(Action("sell", code, None))
#         for code, *_ in recs:
#             if code not in holdings:
#                 actions.append(Action("buy", code, None))
#         return actions
#     return rule
#
# bars 是当日 OHLC,可写"当日跌破开盘价卖出"之类的日内条件;
# 需要更长历史数据的规则,用 make_xxx_rule(market_df) 工厂函数闭包携带历史数据。
