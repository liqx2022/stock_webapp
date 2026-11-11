"""买卖规则(可插拔)。以后用户复杂买卖规则只改本文件,不要改 backtest.py。

规则契约:
    decide(date, holdings, bars, recs) -> list[Action]
    - date:     当前交易日 'YYYY-MM-DD'
    - holdings: dict {ts_code: Holding},Holding 含 code/entry_date/weight/cum_return
    - bars:     dict {ts_code: bar},bar 为当日行情 dict(open/high/low/close/pre_close/pct_chg/vol)
                当日无行情的股票(停牌等)不出现在 bars 里
    - recs:     list[(pos, ts_code, name, start_date)],起始日期 <= date 的全部推荐,
                引擎过滤好传入;pos 为推荐唯一键,每条推荐是独立交易(同股多条推荐各自成仓)
    - 返回当日要执行的买卖动作;引擎先执行卖出、再执行买入。
      op 三种:"buy"(开盘买)、"sell"(收盘卖,旧规则用)、"sell_open"(开盘卖,攻防规则用)

带内部状态的规则(持有天数、止损记忆)应写成工厂函数,每次回测新建实例,
避免跨次回测状态残留。注意 recs 是"起始日期 <= date 的全部推荐",不会因为
卖过就消失——卖出类规则要自己维护"已卖"集合(按 pos 记),否则下个交易日
会把同一推荐再买回来。
"""
from collections import namedtuple

Action = namedtuple("Action", ["op", "pos", "weight"])
# op: "buy" | "sell";pos 为推荐唯一键(recs 里的 pos,引擎按它定位持仓);
# weight 仅 buy 用(None 表示等权,权重由引擎归一化)

Holding = namedtuple("Holding", ["code", "entry_date", "weight", "cum_return"])


def make_hold_n_days(hold_days: int = 10):
    """持有第 hold_days 个交易日收盘卖出(买入日算第 1 个交易日)。

    买入:推荐起始日起第一个有行情的交易日,按开盘价(引擎口径);
    卖出:第 hold_days 个交易日收盘价;停牌日不计持有天数,卖出顺延;
    每条推荐只做一轮,卖出后不再买入;同股再次推荐是新 pos,会再次买入。

    注意:本规则带内部状态(持有天数计数 + 已卖集合),一次回测必须传新实例,
    app.py 的 _run_backtest 每次重跑都会新建,避免跨次回测状态残留。
    """
    held_days = {}   # pos -> 已持有交易日数(含买入日)
    sold = set()     # 已卖出的 pos,防止同一推荐被再次买入

    def rule(date, holdings, bars, recs):
        actions = []
        for pos in list(held_days):
            if pos not in holdings:       # 引擎已移除(卖出/停牌未买成)的,清掉计数
                del held_days[pos]
        for pos, *_rest in recs:
            if pos not in holdings and pos not in sold:
                actions.append(Action("buy", pos, None))
                held_days[pos] = 1        # 买入日 = 第 1 个交易日
        for pos, h in holdings.items():
            if h.code in bars:            # 停牌日不计持有天数(按股票代码查当日行情)
                held_days[pos] = held_days.get(pos, 0) + 1
                if held_days[pos] >= hold_days:
                    actions.append(Action("sell", pos, None))
                    sold.add(pos)
        return actions
    return rule


def make_regime_rule(regime, defense_recs, x, y, z):
    """攻防转换规则(完整口径见 RULES.md;参数在 config.py)。

    regime:       list[(start, end, position)],仓位区间,position 为"成"数(0~10)
    defense_recs: list[(pos, ts_code, name, start_date)],防守列表(与引擎 recs 同构)
    x:            仓位阈值,当日仓位 >= x → 进攻;仓位表未覆盖的日期按防守(保守)
    y / z:        进攻持有最近 y 条推荐 / 防守持有最近 z 条防守列表;同起始日期并列全部保留

    每日目标持仓 = 当前攻防状态对应的"最近 k 条"(起始日期 <= 当日),与当前持仓按 ts_code 差分:
    - 差集卖出、新增买入,一律当日开盘价成交(卖出用 sell_open,引擎按开盘涨跌幅计当日收益)
    - 进攻窗口只统计选中 Excel 的推荐(引擎传入的 recs 已并入防守行,按 pos 排除)
    - 同股"卖旧 + 买新"同日 → 合并成一笔:保留最早进场的旧仓(entry 不变,省手续费),
      多余旧仓照常卖出、新仓不开
    - 停牌日无法成交,引擎跳过当日动作,规则次日自然重发(持仓未变即重发)
    组合时刻满仓等权,权重由引擎归一化。
    """
    defense = list(defense_recs)
    defense_pos = {p for p, *_ in defense}

    def regime_position(date):
        for start, end, position in regime:
            if start <= date <= end:
                return position
        return None  # 仓位表未覆盖 → 防守

    def target_keys(recs, k):
        """最近 k 条:起始日期降序,截到第 k 条;与第 k 条同日期的全部保留。"""
        if not recs or k <= 0:
            return set()
        ordered = sorted(recs, key=lambda r: r[3], reverse=True)
        cutoff = ordered[min(k, len(ordered)) - 1][3]
        return {r[0] for r in ordered if r[3] >= cutoff}

    def rule(date, holdings, bars, recs):
        position = regime_position(date)
        if position is not None and position >= x:
            # 进攻只看选中 Excel 的推荐:引擎传入的 recs 已并入防守行,按 pos 排除
            offense = [r for r in recs if r[0] not in defense_pos]
            target = target_keys(offense, y)
        else:
            target = target_keys(defense, z)    # 防守:防守 sheet 的最近 z 条

        sell = [p for p in holdings if p not in target]
        buy = [p for p in target if p not in holdings]
        code_of = {pos: ts_code for pos, ts_code, *_ in recs}
        code_of.update({pos: ts_code for pos, ts_code, *_ in defense})
        buy_codes = {p: code_of.get(p) for p in buy}

        # 同股"卖旧 + 买新"同日 → 合并成一笔:每代码只保留最早进场的一笔旧仓
        # (entry 不变,省手续费),对应新仓不开;多余旧仓照常卖出,否则同股多笔
        # 旧仓会被整体跳过、权重翻倍并永久卡死。
        merged_codes = {holdings[p].code for p in sell} & {c for c in buy_codes.values() if c}
        keep_pos = set()
        for c in merged_codes:
            olds = sorted((p for p in sell if holdings[p].code == c),
                          key=lambda p: (holdings[p].entry_date, p))
            keep_pos.add(olds[0])
        keep_codes = {holdings[p].code for p in keep_pos}

        actions = [Action("sell_open", p, None) for p in sell if p not in keep_pos]
        actions += [Action("buy", p, None) for p in buy if buy_codes[p] not in keep_codes]
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

    对每条推荐:若该推荐(pos)未持仓 → 买入。
    停牌(无 bar)时引擎会跳过本次买入,规则下个交易日自然重发,无需额外处理。
    """
    actions = []
    for pos, *_rest in recs:
        if pos not in holdings:
            actions.append(Action("buy", pos, None))
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
