"""参数试验台(离线,不走 app):股票池参数组合对比。

用法:
    .venv/Scripts/python.exe -X utf8 param_lab.py

口径与 app 完全同源:app.parse_excel / app.parse_regime_file / rules.make_regime_rule /
backtest.run_backtest / config 参数;本脚本额外提供"同日同股重复行去重"开关
(app 引擎暂不去重,同股同日多条会开两笔仓,见 CLAUDE.md 已知简化)。

改 VARIANTS 即可试别的组合;结果为历史回测参考,不构成投资建议。
"""
import logging
import math
import sys

logging.disable(logging.CRITICAL)   # app 裸导入的 streamlit 告警静音

import pandas as pd

from app import parse_excel, parse_regime_file
from backtest import run_backtest
from config import (FEE, X_THRESHOLD, Z_DEFENSE, LIMIT_UP_SKIP,
                    INPUT_DIR, REGIME_FILE_NAME)
import data_fetcher as fetcher
from rules import make_regime_rule

# ============ 试什么:股票池文件 + 参数组合 ============
POOLS = {
    "梅森周": "input/stock_list_梅森周.xlsx",
    "先知": "input/stock_list_先知.xlsx",
    "先知PPT": "input/stock_list_先知PPT.xlsx",
}
VARIANTS = [
    # (名称, 池, y, max_per_day, dedup, open_gap, prev_gain)
    ("先知-现状参数(对照)", "先知", 10, 1, False, None, None),
    ("梅森-最优参数(对照)",  "梅森周", 20, 3, True,  None, 0.05),
    ("PPT-现状参数照搬",     "先知PPT", 10, 1, False, None, None),
    ("PPT-y10×2",           "先知PPT", 10, 2, True,  None, None),
    ("PPT-y15×2",           "先知PPT", 15, 2, True,  None, None),
    ("PPT-y15×3",           "先知PPT", 15, 3, True,  None, None),
    ("PPT-y20×3",           "先知PPT", 20, 3, True,  None, None),
    ("PPT-y15×3+前日涨5%弃",  "先知PPT", 15, 3, True,  None, 0.05),
    ("PPT-y20×3+前日涨5%弃",  "先知PPT", 20, 3, True,  None, 0.05),
]


def dedup_recs(recs: pd.DataFrame) -> pd.DataFrame:
    """同日同股只保留一条(keep=输入文件先出现的行)。不同日的重复推荐是
    独立交易,不在此口径内。"""
    return recs.drop_duplicates(subset=["ts_code", "start_date"],
                                keep="first").reset_index(drop=True)


def metrics(pf: pd.DataFrame) -> dict:
    daily = pf["account_return"] - pf["fee"]
    n = len(daily)
    total = pf["nav"].iloc[-1] - 1
    sd = daily.std(ddof=0)
    return {
        "总收益": total,
        "年化": (1 + total) ** (250 / n) - 1 if n else float("nan"),
        "最大回撤": pf["drawdown"].min(),
        "年化波动": sd * math.sqrt(250) if n else float("nan"),
        "最差单日": daily.min(),
        "夏普(0利率)": daily.mean() / sd * math.sqrt(250) if sd else float("nan"),
        "平均持仓数": pf["n_stocks"].mean(),
    }


def run_one(pool_name, y, mpd, dedup, gap, prev):
    recs = parse_excel(POOLS[pool_name])["recs"]
    if dedup:
        recs = dedup_recs(recs)
    regime_data = parse_regime_file(INPUT_DIR / REGIME_FILE_NAME)
    regime, defense = regime_data["regime"], regime_data["defense"]
    # 防守行并入 recs 交给引擎(与 app._build_rule_and_recs 同构):
    # 引擎按 pos 从 recs 里查防守代码,缺了防守 ETF 会买不进、整段防守收益丢失
    base = len(recs)
    defense_rows = [(base + i, ts_code, name, start_date)
                    for i, (_pid, ts_code, name, start_date) in enumerate(defense)]
    recs_all = pd.concat(
        [recs, pd.DataFrame([d[1:] for d in defense_rows],
                            columns=["ts_code", "name", "start_date"])],
        ignore_index=True)
    rule = make_regime_rule(list(regime), defense_rows, X_THRESHOLD, y, Z_DEFENSE,
                            max_open_gap=gap, prev_gain_skip=prev, max_per_day=mpd,
                            limit_fill_skip=LIMIT_UP_SKIP)
    market = fetcher.load_market_data()
    trade_dates = fetcher.load_trade_dates()
    _dh, pf, _rs = run_backtest(recs_all, market, trade_dates, rule=rule, fee=FEE)
    return metrics(pf)


def main():
    rows = []
    for name, pool, y, mpd, dedup, gap, prev in VARIANTS:
        m = run_one(pool, y, mpd, dedup, gap, prev)
        rows.append({"组合": name, **m})
        print(f"  done: {name}", flush=True)
    df = pd.DataFrame(rows).set_index("组合")
    fmt = {c: (lambda v: f"{v:.1%}")
           for c in ("总收益", "年化", "最大回撤", "年化波动", "最差单日")}
    fmt["夏普(0利率)"] = lambda v: f"{v:.2f}"
    fmt["平均持仓数"] = lambda v: f"{v:.1f}"
    print()
    print(df.to_string(formatters=fmt))
    print()
    print("口径:攻防阈值/防守ETF/费率同 config.py;区间为各自推荐起始日~行情最新日;")
    print("去重=同日同股保留一条(app 引擎不去重,现状参数行含重复仓影响)。")


if __name__ == "__main__":
    main()
