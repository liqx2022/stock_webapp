# -*- coding: utf-8 -*-
"""月度对照: 实际账户(个人交割单 FIFO+逐日盯市) vs 项目回测(攻防转换, 同口径).

独立命令行模块, 不依赖 app 页面; 回测侧复用本项目 data_fetcher/backtest/rules/config,
与「组合净值」tab 完全同口径(同 recs/仓位文件/参数/费率)。

用法(每月末):
  1) 当月新交割单补进个人交割单 Excel(TRAN_RECORDS, 各券商 sheet 追加行)
  2) 最好先在 app 里点一次「更新行情」(回测侧读 cache/market.db)
  3) 跑:  .venv\\Scripts\\python.exe -X utf8 monthly_compare.py [起始日, 默认 2026-11-13]
输出: 控制台对照表 + output/monthly_compare_report.md

口径说明:
- 实际收益率 = 当月总盈亏增量 / (CAPITAL + 月初总盈亏), CAPITAL 是投入本金(下方常量, 改成实际数)
- 回测收益率 = 组合净值当月增幅(nav 口径与 app 一致)
- 两者差额 = 执行偏差(滑点/未按计划/池外交易/做T), 严格按计划执行时应收敛到滑点级别
"""
import sys, time, sqlite3, collections, warnings
warnings.filterwarnings('ignore')
import pandas as pd
import numpy as np

from config import DB_PATH, CACHE_DIR, OUTPUT_DIR

# ====================== 配置 ======================
TRAN_RECORDS = r'C:\workspace\python_mybasic\人工量化\tran_records.xlsx'  # 个人交割单(只读)
PX_DB = CACHE_DIR / 'personal_px.sqlite'   # 个人复盘行情缓存(与 market.db 分开, 不污染 app 数据)
OUT = OUTPUT_DIR / 'monthly_compare_report.md'
CAPITAL = 1_000_000        # ← 你的投入本金(元), 实际收益率分母用, 改成实际数
START = sys.argv[1] if len(sys.argv) > 1 else '2026-11-13'   # 开始严格执行计划的日期
CURVE_FLOOR = '2025-05-01'  # 曲线起点(与个人复盘 v2 一致)

CANON = {'732459': '603459', '780112': '601112', '787825': '688825'}
BUY, SELL = {'证券买入', '买入'}, {'证券卖出', '卖出'}
DIV = {'股息入账': +1, '股息红利税补': -1, '股息红利税补缴': -1, '利息归本': +1}
RED = {'红股派息', '红股'}

# ====================== 实际侧: 交割单 -> 逐日盯市曲线 ======================
def load_ledger():
    rows = []
    for sheet, dcol, pcol, use_amt in (('大同证券', '成交日期', '成交价格', True),
                                       ('中金财富', '成交日期', '成交均价', False),
                                       ('东北证券', '成交日期', '成交均价', True),
                                       ('国泰海通', '成交日期', '成交均价', False)):
        df = pd.ExcelFile(TRAN_RECORDS, engine='openpyxl').parse(sheet)
        df.columns = [str(c).strip() for c in df.columns]
        df = df[df['操作'].astype(str).isin(BUY | SELL | set(DIV) | RED)]
        for _, r in df.iterrows():
            d = str(r[dcol])[:8]
            if not d.isdigit() or len(d) != 8:
                continue
            op = str(r['操作'])
            code = str(r['证券代码']).strip().split('.')[0].zfill(6)
            code = CANON.get(code, code)
            name = str(r.get('证券名称', '')).strip()
            qty = abs(float(r['成交数量'])) if op in BUY | SELL | RED else 0.0
            price = float(r[pcol])
            mkt = str(r.get('交易市场', '') or '')
            memo = str(r.get('备注', '') or '')
            if qty > 0 and op in BUY | SELL and ('HK' in mkt.upper() or '港股' in memo) \
                    and pd.notna(r.get('成交金额')) and float(r.get('成交金额')):
                price = float(r['成交金额']) / qty   # 港股通: 价格港币 -> 按人民币成交金额折
            amt = float(r['成交金额']) if pd.notna(r.get('成交金额')) else abs(qty * price)
            if use_amt and pd.notna(r.get('发生金额')) and op not in RED:
                cash = float(r['发生金额'])
            elif op in BUY:
                cash = -amt - sum(float(r.get(k, 0) or 0) for k in ('手续费', '佣金', '印花税', '过户费', '其他杂费'))
            elif op in SELL:
                cash = amt - sum(float(r.get(k, 0) or 0) for k in ('手续费', '佣金', '印花税', '过户费', '其他杂费'))
            else:
                cash = float(r.get('发生金额', 0) or 0)
            if op in BUY and qty > 0 and abs(qty * price - amt) > max(amt * 0.02, 1):
                continue
            rows.append({'date': f'{d[:4]}-{d[4:6]}-{d[6:]}', 'code': code, 'name': name,
                         'op': 'B' if op in BUY else ('S' if op in SELL else 'D'),
                         'raw': op, 'qty': qty, 'price': price, 'cash': cash, 'src': sheet})
    return pd.DataFrame(rows).sort_values('date', kind='stable').reset_index(drop=True)

def suffix(c):
    if c.startswith('159'): return c + '.SZ'
    if c.startswith(('588', '51', '56')): return c + '.SH'
    if c.startswith('92'): return c + '.BJ'
    if c.startswith(('60', '68')): return c + '.SH'
    if c.startswith(('00', '30')): return c + '.SZ'
    return None

def ensure_px(codes_needed):
    """补齐 personal_px.sqlite 行情缺口(新代码/新日期), 离线时跳过并警告."""
    PX_DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(PX_DB)
    con.execute('create table if not exists px(ts_code text, trade_date text, close real, primary key(ts_code, trade_date))')
    fetched = skipped = 0
    try:
        import tushare as ts
        from config import TS_TOKEN
        pro = ts.pro_api(TS_TOKEN)
    except Exception as e:
        print(f'! 行情补拉不可用({e}), 按现有数据继续')
        return con
    today = time.strftime('%Y%m%d')
    for c6 in sorted(codes_needed):
        s = suffix(c6)
        if s is None:
            skipped += 1
            continue
        row = con.execute('select max(trade_date) from px where ts_code = ?', (s,)).fetchone()
        have_max = row[0] if row and row[0] else None
        start = '20250501' if have_max is None else have_max
        if have_max is not None and have_max >= today:
            continue
        df = None
        for attempt in range(4):
            try:
                if s.split('.')[0].startswith(('159', '51', '56', '58')):
                    df = pro.fund_daily(ts_code=s, start_date=start, end_date=today)
                else:
                    df = pro.daily(ts_code=s, start_date=start, end_date=today)
                break
            except Exception:
                time.sleep(1.5 * (attempt + 1))
        if df is None or len(df) == 0:
            if have_max is None:
                skipped += 1
            continue
        con.executemany('insert or replace into px values(?,?,?)',
                        [(r.ts_code, r.trade_date, float(r.close)) for r in df.itertuples()])
        con.commit()
        fetched += 1
        time.sleep(0.25)
    print(f'行情补拉: 更新 {fetched} 只, 跳过 {skipped} 只(无后缀映射/拉取失败)')
    return con

def actual_curve(tr):
    """FIFO + 全交易票逐日盯市 -> cv(date, cash_cum, mtm, equity_pnl), res(平仓配对)."""
    lots = collections.defaultdict(collections.deque)
    realized, unmatched = [], []
    for _, r in tr.iterrows():
        q = lots[r['code']]
        if r['op'] == 'B':
            q.append([r['qty'], r['price'], r['date']])
        elif r['op'] == 'S':
            need = r['qty']
            while need > 1e-6 and q:
                lot = q[0]
                m = min(need, lot[0])
                realized.append({'date': r['date'], 'code': r['code'], 'name': r['name'],
                                 'pnl': (r['price'] - lot[1]) * m, 'qty': m, 'bdate': lot[2],
                                 'hold': (pd.Timestamp(r['date']) - pd.Timestamp(lot[2])).days})
                lot[0] -= m; need -= m
                if lot[0] <= 1e-6:
                    q.popleft()
            if need > 1e-6:
                unmatched.append({'date': r['date'], 'code': r['code'], 'qty': need, 'px': r['price']})
        elif r['op'] == 'D' and r['raw'] in RED:
            q.append([r['qty'], 0.0, r['date']])
    res = pd.DataFrame(realized)
    open_pos = {c: sum(l[0] for l in q) for c, q in lots.items() if sum(l[0] for l in q) > 1e-6}
    cost_open = {c: sum(l[0] * l[1] for l in lots[c]) for c in open_pos}
    buy_amt = tr[tr.op == 'B'].groupby('code').apply(lambda g: (g['qty'] * g['price']).sum())
    buy_qty = tr[tr.op == 'B'].groupby('code')['qty'].sum()
    cost_any = (buy_amt / buy_qty.replace(0, np.nan)).to_dict()

    con = ensure_px(set(tr['code']) | set(open_pos))
    pxdf = pd.read_sql('select ts_code, trade_date, close from px', con)
    pxdf['d'] = pxdf['trade_date'].str[:4] + '-' + pxdf['trade_date'].str[4:6] + '-' + pxdf['trade_date'].str[6:]
    mc = sqlite3.connect(DB_PATH)
    mdb = pd.read_sql('select ts_code, trade_date, close from daily_bars', mc)
    mdb['d'] = mdb['trade_date']
    for d in (pxdf, mdb):
        d['c6'] = d['ts_code'].str.split('.').str[0].str.zfill(6)
    allpx = pd.concat([pxdf[['c6', 'd', 'close']], mdb[['c6', 'd', 'close']]])
    allpx = allpx.drop_duplicates(subset=['c6', 'd'], keep='first')

    dates = sorted(set(allpx['d']) | set(tr['date']))
    dates = [d for d in dates if d >= CURVE_FLOOR]
    wide = allpx.pivot_table(index='d', columns='c6', values='close', aggfunc='last').reindex(dates).ffill()
    codes_all = set(wide.columns)
    last_px_date = max(dates)

    tr_by_day = {d: g.to_dict('records') for d, g in tr.groupby('date')}
    open_codes = set(tr.loc[tr.op.isin(['B', 'S']), 'code'])
    cur_cash, lots_now, curve = 0.0, collections.defaultdict(float), []
    for d in dates:
        for r in tr_by_day.get(d, []):
            if r['op'] == 'B' or (r['op'] == 'D' and r['raw'] in RED):
                cur_cash += r['cash']; lots_now[r['code']] += r['qty']
            elif r['op'] == 'S':
                cur_cash += r['cash']; lots_now[r['code']] -= r['qty']
                short = -lots_now[r['code']]
                if short > 1e-6:
                    lots_now[r['code']] = 0.0
                    cur_cash -= short * cost_any.get(r['code'], r['price'])
            else:
                cur_cash += r['cash']
        mtm = 0.0
        for c in open_codes:
            q = lots_now.get(c, 0.0)
            if q > 1e-6:
                p = wide.at[d, c] if c in codes_all else np.nan
                if pd.isna(p):
                    p = (cost_open[c] / open_pos[c]) if c in open_pos else cost_any.get(c, 0.0)
                mtm += q * float(p)
        curve.append({'date': d, 'cash_cum': cur_cash, 'mtm': mtm, 'equity_pnl': cur_cash + mtm})
    cv = pd.DataFrame(curve)
    return cv, res, open_pos, cost_open, cost_any, last_px_date

# ====================== 回测侧: 与 app 完全同口径 ======================
def parse_recs():
    import data_fetcher as fetcher
    from config import EXCEL_COLUMNS, DEFAULT_INPUT_FILE, INPUT_DIR
    df = pd.read_excel(INPUT_DIR / DEFAULT_INPUT_FILE, engine='openpyxl')
    name_col, date_col = EXCEL_COLUMNS['name'], EXCEL_COLUMNS['date']
    df = df.dropna(subset=[name_col, date_col]).copy()
    names = df[name_col].astype(str).str.strip()
    dates = pd.to_datetime(df[date_col], errors='coerce')
    df['_date'] = dates.dt.strftime('%Y-%m-%d')
    df = df[dates.notna()].reset_index(drop=True)
    names = names[dates.notna()].reset_index(drop=True)
    resolved = fetcher.resolve_names(names.tolist())
    rows = [{'ts_code': resolved[n][1], 'name': resolved[n][3], 'start_date': df['_date'].iloc[i]}
            for i, n in enumerate(names) if n in resolved]
    return pd.DataFrame(rows), {resolved[n][1].split('.')[0].zfill(6) for n in names if n in resolved}

def parse_regime_file(path):
    import data_fetcher as fetcher
    xls = pd.ExcelFile(path, engine='openpyxl')
    pos_df = xls.parse('仓位')
    regime = []
    for _, r in pos_df.iterrows():
        s, e = pd.to_datetime(r.get('起始日期'), errors='coerce'), pd.to_datetime(r.get('结束日期'), errors='coerce')
        p = pd.to_numeric(r.get('仓位'), errors='coerce')
        if pd.isna(s) or pd.isna(e) or pd.isna(p):
            continue
        regime.append((s.strftime('%Y-%m-%d'), e.strftime('%Y-%m-%d'), float(p)))
    def_df = xls.parse('防守').dropna(subset=['起始日期']).copy()
    def_df['_date'] = pd.to_datetime(def_df['起始日期'], errors='coerce').dt.strftime('%Y-%m-%d')
    def_df = def_df[def_df['_date'].notna()]
    need = []
    for _, r in def_df.iterrows():
        code = r.get('股票代码')
        raw_name = r.get('股票名称')
        name = '' if raw_name is None or pd.isna(raw_name) else str(raw_name).strip()
        if (code is None or pd.isna(code) or not str(code).strip()) and name:
            need.append(name)
    resolved = fetcher.resolve_names(need) if need else {}
    rows = []
    for _, r in def_df.iterrows():
        code = r.get('股票代码')
        raw_name = r.get('股票名称')
        name = '' if raw_name is None or pd.isna(raw_name) else str(raw_name).strip()
        ts_code = None
        if code is not None and not pd.isna(code) and str(code).strip():
            ts_code = fetcher.get_ts_code(str(code).strip())
        else:
            hit = resolved.get(name)
            if hit is None:
                continue
            ts_code, name = hit[1], hit[3]
        rows.append((ts_code, name, r['_date']))
    by_code = {}
    for ts_code, name, d in rows:
        if ts_code not in by_code or d > by_code[ts_code][2]:
            by_code[ts_code] = (ts_code, name, d)
    defense = [(f'D{k}', *row) for k, row in enumerate(by_code.values())]
    return regime, defense

def run_backtest_side():
    import backtest
    from rules import make_regime_rule
    import data_fetcher as fetcher
    from config import (X_THRESHOLD, Y_OFFENSE, Z_DEFENSE, FEE, OPEN_GAP_SKIP,
                        PREV_DAY_GAIN_SKIP, MAX_PER_DAY, LIMIT_UP_SKIP,
                        REGIME_FILE_NAME, INPUT_DIR)
    recs, pool6 = parse_recs()
    regime, defense = parse_regime_file(INPUT_DIR / REGIME_FILE_NAME)
    base = len(recs)
    defense_rows = [(base + i, ts, n, sd) for i, (_p, ts, n, sd) in enumerate(defense)]
    recs_all = pd.concat([recs, pd.DataFrame([d[1:] for d in defense_rows],
                                             columns=['ts_code', 'name', 'start_date'])],
                         ignore_index=True)
    rule = make_regime_rule(list(regime), defense_rows, X_THRESHOLD, Y_OFFENSE, Z_DEFENSE,
                            OPEN_GAP_SKIP, PREV_DAY_GAIN_SKIP, MAX_PER_DAY, LIMIT_UP_SKIP)
    market = fetcher.load_market_data(DB_PATH)
    trade_dates = fetcher.load_trade_dates(DB_PATH)
    _, portfolio, _ = backtest.run_backtest(recs_all, market, trade_dates, rule=rule, fee=FEE)
    return portfolio, pool6

# ====================== 月度对照 ======================
def main():
    tr = load_ledger()
    print(f'交割单 {len(tr)} 条 ({tr["date"].min()} ~ {tr["date"].max()})')
    cv, res, open_pos, cost_open, cost_any, last_px_date = actual_curve(tr)
    portfolio, pool6 = run_backtest_side()

    cv['ym'] = cv['date'].str[:7]
    pf = portfolio.set_index('date')['nav']
    months = sorted({m for m in cv['ym'] if m >= START[:7]})
    rows_tbl = []
    prev_eq = prev_nav = None
    if months:
        m0_first = f'{months[0]}-01'
        pre = cv[cv['date'] < m0_first]
        prev_eq = float(pre['equity_pnl'].iloc[-1]) if len(pre) else 0.0
        pre_nav = [d for d in pf.index if d < m0_first]
        prev_nav = float(pf.loc[pre_nav[-1]]) if pre_nav else 1.0
    for ym in months:
        sub = cv[cv['ym'] == ym]
        eq_end = float(sub['equity_pnl'].iloc[-1])
        sub_nav = [d for d in pf.index if d.startswith(ym)]
        nav_end = float(pf.loc[sub_nav[-1]]) if sub_nav else prev_nav
        act_pnl = eq_end - prev_eq
        act_ret = act_pnl / max(CAPITAL + prev_eq, 1.0)
        bt_ret = nav_end / prev_nav - 1 if prev_nav else 0.0
        bt_pnl = CAPITAL * bt_ret
        rows_tbl.append({'ym': ym, 'act_pnl': act_pnl, 'act_ret': act_ret,
                         'bt_ret': bt_ret, 'bt_pnl': bt_pnl, 'diff': act_pnl - bt_pnl})
        prev_eq, prev_nav = eq_end, nav_end

    # 执行检查(START 起)
    per_start = tr[tr['date'] >= START]
    res_start = res[res['date'] >= START] if len(res) else res
    tp = res_start[res_start['hold'] <= 3] if len(res_start) else res_start
    off = per_start[(per_start['op'] == 'B') & (~per_start['code'].isin(pool6))] if len(per_start) else per_start
    off_amt = float((off['qty'] * off['price']).sum()) if len(off) else 0.0
    cur = cv.iloc[-1]

    def w(v): return f'{v:+,.0f}'
    lines = ['# 月度对照: 实际账户 vs 项目回测(攻防转换)', '',
             f'- 生成时间: {time.strftime("%Y-%m-%d %H:%M")}, 对比起始: {START}, 本金设定: {CAPITAL:,.0f}',
             f'- 实际侧行情截至 {last_px_date}; 交割单截至 {tr["date"].max()}',
             f'- 回测侧与 app「组合净值」tab 同口径(同 recs/仓位文件/参数/费率)',
             '', '| 月份 | 实际盈亏(元) | 实际收益率 | 回测收益率 | 回测盈亏@本金 | 差额(实际-回测) |', '|---|---|---|---|---|---|']
    for r in rows_tbl:
        lines.append(f'| {r["ym"]} | {w(r["act_pnl"])} | {r["act_ret"]:+.2%} | {r["bt_ret"]:+.2%} | {w(r["bt_pnl"])} | {w(r["diff"])} |')
    tot_act = sum(r['act_pnl'] for r in rows_tbl)
    tot_bt = sum(r['bt_pnl'] for r in rows_tbl)
    lines += ['', f'**期间合计: 实际 {w(tot_act)} 元 / 回测 {w(tot_bt)} 元 / 差额 {w(tot_act - tot_bt)} 元**', '',
              '## 执行检查(起始日起)', '',
              f'- 交易笔数: {len(per_start)} 笔(做T 频率应趋近 0)',
              f'- 池外买入: {len(off)} 笔 / {off_amt:,.0f} 元(目标 0)',
              f'- 短线平仓(<=3天): {len(tp)} 笔 / {tp["pnl"].sum() if len(tp) else 0:+,.0f} 元(目标 0)',
              f'- 当前总盈亏: {cur["equity_pnl"]:+,.0f} 元(已实现+浮动), 持仓 {len(open_pos)} 只',
              '', '口径备注: 中金账户手续费无记录(费用已隐含在其余账户现金流里); 实际收益率分母 = 本金 + 月初总盈亏;',
              '首月为不完整月; 差额长期应为小额负值(滑点+费用), 大幅为负 = 未按计划执行。']
    OUT.parent.mkdir(exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print('\n'.join(lines))
    print('WROTE', OUT)

if __name__ == '__main__':
    main()
