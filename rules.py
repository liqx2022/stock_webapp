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

from indicators import daily_return, gap_return

Action = namedtuple("Action", ["op", "pos", "weight"])
# op: "buy" | "sell";pos 为推荐唯一键(recs 里的 pos,引擎按它定位持仓);
# weight 仅 buy 用(None 表示等权,权重由引擎归一化)

Holding = namedtuple("Holding", ["code", "entry_date", "weight", "cum_return"])


def target_keys(recs, k, max_per_day=None):
    """最近 k 条:起始日期降序,截到第 k 条;与第 k 条同日期的全部保留。

    recs 条目 (pos, ts_code, name, start_date)。攻防规则与交易计划页共用,
    改这里两边同时生效。

    max_per_day(单日上限,config.py MAX_PER_DAY)非 None 时改为"分散取窗":
    从新到旧扫,每个起始日期至多取 c 条(同日按输入文件行序取前 c 条),
    取满 k 条为止——同时消除"同日并列全保留"导致的窗口膨胀(日均 3 条推荐
    下 y=5 实际持 10~18 只)与单日题材押注聚集;None 关闭,维持原口径
    (与第 k 条同日并列的全部保留,持仓数可超 k)。
    """
    if not recs or k <= 0:
        return set()
    ordered = sorted(recs, key=lambda r: r[3], reverse=True)
    if max_per_day is None:
        cutoff = ordered[min(k, len(ordered)) - 1][3]
        return {r[0] for r in ordered if r[3] >= cutoff}
    picked, per_date = [], {}
    for r in ordered:
        if len(picked) >= k:
            break
        if per_date.get(r[3], 0) >= max_per_day:
            continue
        per_date[r[3]] = per_date.get(r[3], 0) + 1
        picked.append(r[0])
    return set(picked)


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


def prev_gain_discard(recs, bars_by_date, threshold):
    """前收涨幅过滤:推荐「开始日期的前一交易日」收盘涨幅(pct_chg/100)>
    threshold 的 pos 集合,按永久舍弃口径处理(视同无此推荐,永不买入)。

    recs 条目 (pos, ts_code, name, start_date);bars_by_date 为
    {date: {ts_code: bar}}(backtest._build_bars_by_date 的产物)。
    涨幅在推荐日之前就已确定,判定与推荐何时进入窗口无关;
    前一交易日无行情(停牌/上市首日/行情区间未覆盖)的推荐无法判定,不舍弃;
    threshold 为 None 返回空集。交易计划页与本函数共用,保证两边口径一致。
    """
    out = set()
    if threshold is None or not recs or not bars_by_date:
        return out
    dates = sorted(bars_by_date)
    idx = {d: i for i, d in enumerate(dates)}
    for pos, ts_code, _name, sd in recs:
        i = idx.get(sd)
        if not i:               # 0 = 推荐日是行情首日(无前一日);None = 行情无该日
            continue
        bar = bars_by_date[dates[i - 1]].get(ts_code)
        prev = daily_return(bar.get("pct_chg")) if bar else None
        if prev is not None and prev > threshold:
            out.add(pos)
    return out


def limit_ratio(ts_code: str) -> float | None:
    """按板块返回涨跌停幅度,None = 无涨跌幅限制(港股)。

    近似口径:主板 10%、创业板(30)/科创板(68)/科创ETF(588) 20%、北交所 30%。
    不区分新股上市首日等特殊安排(主板首日 44%、创业/科创首五日无限制)——
    极端高开本就难以成交,近似偏向保守(详见 RULES.md)。
    """
    if ts_code.endswith(".HK"):
        return None
    sym = ts_code.split(".")[0]
    if ts_code.endswith(".BJ"):
        return 0.30
    if sym.startswith(("30", "68", "588")):
        return 0.20
    return 0.10


def _at_limit_price(ts_code, open_, pre_close, up: bool) -> bool:
    """开盘价是否达到涨停(up=True)/跌停价。涨停价 = round(昨收×(1±幅度), 2)。

    open_/pre_close 缺失或无法计算时返回 False(按可成交处理,交由停牌顺延逻辑)。
    """
    lim = limit_ratio(ts_code)
    if lim is None or open_ is None or pre_close is None or not pre_close:
        return False
    try:
        factor = (1 + lim) if up else (1 - lim)
        limit_px = round(float(pre_close) * factor, 2)
        return float(open_) >= limit_px - 1e-6 if up else float(open_) <= limit_px + 1e-6
    except (TypeError, ValueError):
        return False


def open_at_limit_up(ts_code, open_, pre_close) -> bool:
    """开盘即涨停(实盘挂单大概率买不进)。"""
    return _at_limit_price(ts_code, open_, pre_close, up=True)


def open_at_limit_down(ts_code, open_, pre_close) -> bool:
    """开盘即跌停(实盘挂单大概率卖不出)。"""
    return _at_limit_price(ts_code, open_, pre_close, up=False)


def make_regime_rule(regime, defense_recs, x, y, z, max_open_gap=None,
                     prev_gain_skip=None, max_per_day=None, limit_fill_skip=False):
    """攻防转换规则(完整口径见 RULES.md;参数在 config.py)。

    regime:       list[(start, end, position)],仓位区间,position 为"成"数(0~10)
    defense_recs: list[(pos, ts_code, name, start_date)],防守列表(与引擎 recs 同构)
    x:            仓位阈值,当日仓位 >= x → 进攻;仓位表未覆盖的日期按防守(保守)
    y / z:        进攻持有最近 y 条推荐 / 防守持有最近 z 条防守列表;同起始日期并列全部保留
    max_open_gap: 开盘涨幅过滤阈值(config.py OPEN_GAP_SKIP):推荐首次将被买入的
                  当天(停牌则顺延到实际可成交日),开盘涨幅(开盘/昨收-1)>
                  阈值 → 永久舍弃(视同无此推荐,之后任何日期都不再买入),
                  空出的坑由排序靠后的推荐即时补位(补位者同日同样过检);
                  None 关闭;进攻/防守列表同口径
    prev_gain_skip: 前收涨幅过滤阈值(config.py PREV_DAY_GAIN_SKIP):推荐开始日期
                  的前一交易日收盘涨幅(pct_chg/100)> 阈值 → 永久舍弃;None 关闭。
                  涨幅在推荐日前已确定,推荐日当天一次性判定;与 prev_gain_discard
                  同口径(该函数供交易计划页在计划时即剔除,两者必须一致)
    max_per_day:   单日上限(config.py MAX_PER_DAY):窗口内同一推荐日至多 c 条
                  (同日按输入行序取前 c),取满 y/z 条为止;None 关闭(同日并列
                  全保留)。详见 target_keys。防御非法配置:非 None 且 < 1 直接抛错
    limit_fill_skip: 涨跌停可成交性(config.py LIMIT_UP_SKIP,默认 True):买入侧,
                  推荐首次将被买入的当天开盘已达涨停价 → 实盘买不进,永久舍弃
                  (同开盘涨幅过滤口径,由后续推荐补位);卖出侧对称处理,开盘
                  已达跌停价的持仓当日不卖、顺延到下一交易日开盘(规则自然重发)。
                  涨停价按板块近似判定(limit_ratio);False 关闭(回测按开盘价
                  无条件成交,与旧行为一致)

    每日目标持仓 = 当前攻防状态对应的"最近 k 条"(起始日期 <= 当日),与当前持仓按 ts_code 差分:
    - 差集卖出、新增买入,一律当日开盘价成交(卖出用 sell_open,引擎按开盘涨跌幅计当日收益)
    - 进攻窗口只统计选中 Excel 的推荐(引擎传入的 recs 已并入防守行,按 pos 排除)
    - 同股"卖旧 + 买新"同日 → 合并成一笔:保留最早进场的旧仓(entry 不变,省手续费),
      多余旧仓照常卖出、新仓不开
    - 停牌日无法成交,引擎跳过当日动作,规则次日自然重发(持仓未变即重发)
    组合时刻满仓等权,权重由引擎归一化。
    """
    if max_per_day is not None and int(max_per_day) < 1:
        raise ValueError("max_per_day(配置 MAX_PER_DAY)需为 >= 1 的整数或 None")

    defense = list(defense_recs)
    defense_pos = {p for p, *_ in defense}
    discarded = set()   # 永久舍弃的推荐 pos(开盘涨幅/前收涨幅两种过滤共用)
    prev_bars = {}      # 上一交易日的 {ts_code: bar}(供前收涨幅过滤判定)

    def regime_position(date):
        for start, end, position in regime:
            if start <= date <= end:
                return position
        return None  # 仓位表未覆盖 → 防守

    def rule(date, holdings, bars, recs):
        position = regime_position(date)
        if position is not None and position >= x:
            # 进攻只看选中 Excel 的推荐:引擎传入的 recs 已并入防守行,按 pos 排除
            pool = [r for r in recs if r[0] not in defense_pos]
            k = y
        else:
            pool = defense                       # 防守:防守 sheet 的最近 z 条
            k = z

        # 前收涨幅过滤(PREV_DAY_GAIN_SKIP):推荐开始日期的前一交易日收盘涨幅
        # > 阈值 → 永久舍弃。涨幅在推荐日前已确定,故在其 start_date 当天一次性
        # 判定(prev_bars 即上一交易日的行情);与窗口进出无关。前一交易日停牌/
        # 无行情的无法判定,不舍弃。与 prev_gain_discard() 同口径(计划页用)。
        if prev_gain_skip is not None:
            for pos, ts_code, _name, sd in recs:
                if sd == date and pos not in discarded:
                    pb = prev_bars.get(ts_code)
                    prev = daily_return(pb.get("pct_chg")) if pb else None
                    if prev is not None and prev > prev_gain_skip:
                        discarded.add(pos)

        # 开盘涨幅过滤(OPEN_GAP_SKIP)与涨停可成交性(LIMIT_UP_SKIP):
        # 推荐首次将被买入的当天,开盘涨幅 > 阈值、或开盘已达涨停价(实盘
        # 买不进)→ 永久舍弃,空出的坑由后面的推荐即时补位(补位者同日同样
        # 过检,循环到目标集稳定)。已持仓的不复检(过滤只看买入当天);当日
        # 停牌无行情的无法判断,保持占坑,顺延到实际可成交日(规则次日重发)
        # 再检。discarded 状态只活在本实例内,一次回测新建实例即重置。
        while True:
            active = [r for r in pool if r[0] not in discarded]
            target = target_keys(active, k, max_per_day)
            if max_open_gap is None and not limit_fill_skip:
                break
            code_of = {pos: ts_code for pos, ts_code, *_ in active}
            newly = set()
            for pos in target:
                if pos in holdings:
                    continue
                bar = bars.get(code_of[pos])
                if bar is None:
                    continue
                gap = gap_return(bar.get("open"), bar.get("pre_close"))
                if max_open_gap is not None and gap is not None and gap > max_open_gap:
                    newly.add(pos)
                    continue
                if (limit_fill_skip
                        and open_at_limit_up(code_of[pos], bar.get("open"),
                                             bar.get("pre_close"))):
                    newly.add(pos)
            if not newly:
                break
            discarded.update(newly)

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

        # 开盘跌停卖不出(LIMIT_UP_SKIP):当日不卖、持仓保留,规则次日自然重发;
        # 同股卖旧买新的 keep_pos 旧仓不受影响(它本来就不卖)
        def _sell_blocked(p):
            h = holdings[p]
            bar = bars.get(h.code)
            return (limit_fill_skip and bar is not None
                    and open_at_limit_down(h.code, bar.get("open"),
                                           bar.get("pre_close")))

        actions = [Action("sell_open", p, None) for p in sell
                   if p not in keep_pos and not _sell_blocked(p)]
        actions += [Action("buy", p, None) for p in buy if buy_codes[p] not in keep_codes]
        prev_bars.clear()
        prev_bars.update(bars)   # 记为"上一交易日"行情,供次日的前收涨幅判定
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
