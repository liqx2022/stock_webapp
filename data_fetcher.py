"""tushare 全部交互 + SQLite 建表与读写 + 增量更新。

移植自旧脚本 股票统计1_0628智能评分收益回测系统.py(重试/名称匹配/后缀推断)
与 拉取全量股票\拉取全量股票2.py(指数退避 + 限流休眠)。
"""
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import tushare as ts

from config import TS_TOKEN, DB_PATH, REQUEST_SLEEP, MAX_RETRY

ts.set_token(TS_TOKEN)
pro = ts.pro_api(timeout=30)


# ====================== tushare 底层封装 ======================
def _call_with_retry(func, *args, **kwargs):
    """指数退避重试 + 每次调用后限流休眠。彻底失败抛异常,由调用方决定跳过还是终止。"""
    last_err = None
    for i in range(MAX_RETRY):
        try:
            result = func(*args, **kwargs)
            time.sleep(REQUEST_SLEEP)
            return result
        except Exception as e:
            last_err = e
            if i < MAX_RETRY - 1:
                wait = 2 ** (i + 1)  # 2/4/8 秒
                print(f"[WARN] tushare 调用失败,第{i + 1}次重试({wait}秒后):{str(e)[:120]}")
                time.sleep(wait)
    raise last_err


def get_ts_code(symbol: str) -> str:
    """代码后缀推断:5 位→.HK(先判断,港股 00700 这类 0 开头代码不会被误判);
    5/1 开头→场内基金 ETF(5 开头 .SH,1 开头 .SZ);6→.SH, 0/3→.SZ。
    旧脚本顺序有 bug(0 开头港股被误判为 .SZ,靠兜底接口才碰巧工作),这里已修正。
    Excel 数字单元格读出的 "510300.0" 之类会先去掉 ".0"。"""
    symbol = str(symbol).strip()
    if symbol.endswith(".0") and symbol[:-2].isdigit():
        symbol = symbol[:-2]
    if len(symbol) == 5:
        return f"{symbol}.HK"
    if symbol.startswith("5"):
        return f"{symbol}.SH"
    if symbol.startswith("1"):
        return f"{symbol}.SZ"
    if symbol.startswith("6"):
        return f"{symbol}.SH"
    if symbol.startswith(("0", "3")):
        return f"{symbol}.SZ"
    return symbol


def is_etf(ts_code: str) -> bool:
    """场内基金判定(用于费率选择):6 位代码 5/1 开头的 .SH/.SZ。"""
    sym = ts_code.split(".")[0]
    return len(sym) == 6 and sym.startswith(("5", "1")) and ts_code.endswith((".SH", ".SZ"))


def fetch_daily(ts_code: str, start: str, end: str) -> pd.DataFrame:
    """拉日线。兜底顺序:pro.daily → ETF 用 pro.fund_daily → .HK 用 pro.hk_daily。
    start/end 为 'YYYY-MM-DD',API 用 'YYYYMMDD'。
    """
    start8, end8 = start.replace("-", ""), end.replace("-", "")
    df = _call_with_retry(pro.daily, ts_code=ts_code, start_date=start8, end_date=end8)
    if df is None or df.empty:
        if ts_code.endswith(".HK"):
            df = _call_with_retry(pro.hk_daily, ts_code=ts_code, start_date=start8, end_date=end8)
        elif is_etf(ts_code):
            df = _call_with_retry(pro.fund_daily, ts_code=ts_code, start_date=start8, end_date=end8)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.strftime("%Y-%m-%d")
    return df.sort_values("trade_date").reset_index(drop=True)


def fetch_index_daily(ts_code: str, start: str, end: str) -> pd.DataFrame:
    """拉指数日线(pro.index_daily)。start/end 为 'YYYY-MM-DD',API 用 'YYYYMMDD'。"""
    start8, end8 = start.replace("-", ""), end.replace("-", "")
    df = _call_with_retry(pro.index_daily, ts_code=ts_code, start_date=start8, end_date=end8)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.strftime("%Y-%m-%d")
    return df.sort_values("trade_date").reset_index(drop=True)


def fetch_trade_calendar(start: str, end: str) -> pd.DataFrame:
    """交易日历(SSE 口径)。"""
    df = _call_with_retry(pro.trade_cal, exchange="SSE",
                          start_date=start.replace("-", ""), end_date=end.replace("-", ""),
                          fields="cal_date,is_open")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["cal_date"] = pd.to_datetime(df["cal_date"], format="%Y%m%d").dt.strftime("%Y-%m-%d")
    return df


def fetch_name_map() -> pd.DataFrame:
    """全市场名称→代码映射(A 股 + 港股 + 场内基金)。"""
    df_a = _call_with_retry(pro.stock_basic, exchange="", list_status="L",
                            fields="ts_code,symbol,name")
    df_hk = _call_with_retry(pro.hk_basic, fields="ts_code,symbol,name")
    if df_a is None or df_hk is None:
        raise RuntimeError("股票列表拉取失败")
    # 场内基金(ETF/LOF):market='E' 只取场内,避免场外基金代码与股票冲突
    df_fund = _call_with_retry(pro.fund_basic, market="E", fields="ts_code,name")
    parts = [df_a, df_hk]
    if df_fund is not None and not df_fund.empty:
        df_fund = df_fund.copy()
        df_fund["symbol"] = df_fund["ts_code"].str.split(".").str[0]
        df_fund["market"] = "ETF"
        parts.append(df_fund[["symbol", "name", "ts_code", "market"]])
    df = pd.concat(parts, ignore_index=True)
    df["market"] = df["ts_code"].str.split(".").str[1].map({"SH": "A", "SZ": "A", "BJ": "A"}).fillna("HK")
    if df_fund is not None and not df_fund.empty:
        df.loc[df["symbol"].isin(df_fund["symbol"]), "market"] = "ETF"
    df = df.dropna(subset=["symbol", "name"]).drop_duplicates(subset=["symbol"])
    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["name"] = df["name"].astype(str).str.strip()
    return df[["symbol", "name", "ts_code", "market"]]


# ====================== SQLite ======================
def _connect(db_path=DB_PATH):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    return conn


def init_db(db_path=DB_PATH):
    """建表(幂等)。"""
    with _connect(db_path) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS trade_calendar (
            cal_date TEXT PRIMARY KEY,
            is_open  INTEGER
        );
        CREATE TABLE IF NOT EXISTS stock_names (
            symbol  TEXT PRIMARY KEY,
            name    TEXT,
            ts_code TEXT,
            market  TEXT
        );
        CREATE TABLE IF NOT EXISTS daily_bars (
            ts_code    TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            pre_close REAL, pct_chg REAL, vol REAL,
            PRIMARY KEY (ts_code, trade_date)
        );
        CREATE INDEX IF NOT EXISTS idx_bars_date ON daily_bars(trade_date);
        """)


def _get_meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(conn, key: str, value: str):
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))


def get_data_version(db_path=DB_PATH) -> int:
    init_db(db_path)
    with _connect(db_path) as conn:
        v = _get_meta(conn, "data_version")
        return int(v) if v else 0


# ====================== 名称→代码 ======================
def normalize_name(name: str) -> str:
    """去空格、去 *ST/ST 前缀(与旧脚本一致)。"""
    return str(name).strip().replace("*ST", "").replace("ST", "").strip()


def ensure_name_map(db_path=DB_PATH, force: bool = False) -> int:
    """stock_names 表为空或 force=True 时全量拉取。返回表内行数。"""
    init_db(db_path)
    with _connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM stock_names").fetchone()[0]
        if count > 0 and not force:
            return count
        print("[INFO] 正在拉取全部股票列表,初始化名称→代码映射...")
        df = fetch_name_map()
        if df.empty:
            raise RuntimeError("股票列表拉取失败")
        df.to_sql("stock_names", conn, if_exists="replace", index=False)
        _set_meta(conn, "name_map_update", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        print(f"[OK] 名称→代码映射完成,共 {len(df)} 只股票")
        return len(df)


def _match_one(name: str, conn) -> tuple | None:
    """单名称匹配:精确 → 子串互含。返回 (symbol, ts_code, market, 库内名称)。"""
    norm = normalize_name(name)
    if not norm:
        return None
    row = conn.execute("SELECT symbol, ts_code, market, name FROM stock_names WHERE name=?",
                       (norm,)).fetchone()
    if row:
        return row
    rows = conn.execute("SELECT symbol, ts_code, market, name FROM stock_names").fetchall()
    for symbol, ts_code, market, db_name in rows:
        db_norm = normalize_name(db_name)
        if not db_norm:
            continue
        if norm in db_norm or db_norm in norm:
            return (symbol, ts_code, market, db_name)
    return None


def resolve_names(names: list[str], db_path=DB_PATH) -> dict:
    """{输入名: (symbol, ts_code, market, 库内名称)};未匹配的值为 None。
    整体失败一次自动刷新名称表再试。"""
    init_db(db_path)
    result = {}
    with _connect(db_path) as conn:
        for name in names:
            result[name] = _match_one(name, conn)
    missing = [n for n, v in result.items() if v is None]
    if missing:
        print(f"[WARN] {len(missing)} 个名称未匹配,刷新名称表后重试一次...")
        ensure_name_map(db_path, force=True)
        with _connect(db_path) as conn:
            for name in missing:
                result[name] = _match_one(name, conn)
    return result


# ====================== 交易日历 ======================
def ensure_trade_calendar(min_date: str, max_date: str, db_path=DB_PATH) -> int:
    """只拉缺失的前段/后段,INSERT OR REPLACE。返回表内总行数。"""
    init_db(db_path)
    with _connect(db_path) as conn:
        row_min = conn.execute("SELECT MIN(cal_date) FROM trade_calendar").fetchone()[0]
        row_max = conn.execute("SELECT MAX(cal_date) FROM trade_calendar").fetchone()[0]

        def _pull_and_write(s, e):
            if s > e:
                return
            df = fetch_trade_calendar(s, e)
            if not df.empty:
                df.to_sql("trade_calendar", conn, if_exists="append", index=False)

        if row_min is None:
            _pull_and_write(min_date, max_date)
        else:
            if min_date < row_min:
                _pull_and_write(min_date, str((datetime.strptime(row_min, "%Y-%m-%d") -
                                               timedelta(days=1)).date()))
            if max_date > row_max:
                d = datetime.strptime(row_max, "%Y-%m-%d") + timedelta(days=1)
                _pull_and_write(str(d.date()), max_date)
        return conn.execute("SELECT COUNT(*) FROM trade_calendar").fetchone()[0]


def load_trade_dates(db_path=DB_PATH) -> list[str]:
    """升序交易日列表(仅 is_open=1)。"""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT cal_date FROM trade_calendar WHERE is_open=1 ORDER BY cal_date"
        ).fetchall()
    return [r[0] for r in rows]


# ====================== 行情增量更新 ======================
def update_market_data(recs: pd.DataFrame, progress_cb=None, db_path=DB_PATH) -> dict:
    """增量更新所有推荐股的日线。
    recs: DataFrame,需含 ts_code、start_date('YYYY-MM-DD') 两列。
    每只股票只拉缺口区间;失败记 skipped,下次自动补。
    """
    init_db(db_path)
    stats = {"updated": 0, "new_rows": 0, "skipped": [], "elapsed": 0.0}
    t0 = time.time()
    today = datetime.now().strftime("%Y-%m-%d")  # 每次调用现取:页面长开跨天也能拉到新数据
    # 同股多条推荐只拉一次行情,起始日取最早,保证首次拉取覆盖全部推荐区间
    unique_recs = (recs.sort_values("start_date")
                   .drop_duplicates(subset=["ts_code"], keep="first"))
    with _connect(db_path) as conn:
        for i, (_, row) in enumerate(unique_recs.iterrows()):
            ts_code, start_date = row["ts_code"], row["start_date"]
            name = row.get("name", ts_code)
            if progress_cb:
                progress_cb(i, len(unique_recs), name)
            try:
                row_min, row_max = conn.execute(
                    "SELECT MIN(trade_date), MAX(trade_date) FROM daily_bars WHERE ts_code=?",
                    (ts_code,)).fetchone()
                if row_min is None:
                    pull_start, pull_end = start_date, today
                else:
                    pull_start = str((datetime.strptime(row_max, "%Y-%m-%d") +
                                      timedelta(days=1)).date())
                    pull_end = today
                if pull_start > pull_end:
                    continue  # 已是最新
                df = fetch_daily(ts_code, pull_start, pull_end)
                if df.empty:
                    continue
                df["ts_code"] = ts_code
                df[["ts_code", "trade_date", "open", "high", "low", "close",
                    "pre_close", "pct_chg", "vol"]].to_sql(
                    "daily_bars", conn, if_exists="append", index=False)
                stats["updated"] += 1
                stats["new_rows"] += len(df)
            except Exception as e:
                stats["skipped"].append({"ts_code": ts_code, "name": name, "reason": str(e)[:200]})
        _set_meta(conn, "data_version", str(int(_get_meta(conn, "data_version") or 0) + 1))
        _set_meta(conn, "last_update", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    stats["elapsed"] = round(time.time() - t0, 1)
    return stats


def get_bench_version(db_path=DB_PATH) -> int:
    """基准数据版本号:ensure_benchmarks 实际写入新行时 +1(用于页面缓存失效)。"""
    init_db(db_path)
    with _connect(db_path) as conn:
        v = _get_meta(conn, "bench_version")
        return int(v) if v else 0


def ensure_benchmarks(codes: list[str], start: str, progress_cb=None,
                      db_path=DB_PATH) -> dict:
    """增量拉取基准指数/ETF 日线,与股票同存 daily_bars 表。
    指数走 index_daily,ETF(515080 等)走 fetch_daily 的 fund_daily 兜底。
    start 为回测最早日('YYYY-MM-DD');只拉缺口,失败记 skipped 下次自动补。
    返回 stats 结构同 update_market_data。仅在写入新行时递增 bench_version,
    避免页面每次重跑都空转递增。"""
    init_db(db_path)
    stats = {"updated": 0, "new_rows": 0, "skipped": [], "elapsed": 0.0}
    t0 = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    with _connect(db_path) as conn:
        for i, ts_code in enumerate(codes):
            if progress_cb:
                progress_cb(i, len(codes), ts_code)
            try:
                row_min, row_max = conn.execute(
                    "SELECT MIN(trade_date), MAX(trade_date) FROM daily_bars WHERE ts_code=?",
                    (ts_code,)).fetchone()
                if row_min is None:
                    pull_start = start
                else:
                    pull_start = str((datetime.strptime(row_max, "%Y-%m-%d") +
                                      timedelta(days=1)).date())
                pull_end = today
                if pull_start > pull_end:
                    continue  # 已是最新
                fetch = fetch_daily if is_etf(ts_code) else fetch_index_daily
                df = fetch(ts_code, pull_start, pull_end)
                if df.empty:
                    stats["skipped"].append({"ts_code": ts_code, "name": ts_code,
                                             "reason": "无数据"})
                    continue
                df["ts_code"] = ts_code
                df[["ts_code", "trade_date", "open", "high", "low", "close",
                    "pre_close", "pct_chg", "vol"]].to_sql(
                    "daily_bars", conn, if_exists="append", index=False)
                stats["updated"] += 1
                stats["new_rows"] += len(df)
            except Exception as e:
                stats["skipped"].append({"ts_code": ts_code, "name": ts_code,
                                         "reason": str(e)[:200]})
        if stats["new_rows"]:
            _set_meta(conn, "bench_version", str(int(_get_meta(conn, "bench_version") or 0) + 1))
            _set_meta(conn, "benchmarks_update", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    stats["elapsed"] = round(time.time() - t0, 1)
    return stats


def load_benchmark_data(codes: list[str], db_path=DB_PATH) -> pd.DataFrame:
    """读基准代码的日线长表(ts_code/trade_date/close/pct_chg)。"""
    init_db(db_path)
    if not codes:
        return pd.DataFrame(columns=["ts_code", "trade_date", "close", "pct_chg"])
    placeholders = ",".join("?" for _ in codes)
    with _connect(db_path) as conn:
        df = pd.read_sql(
            f"SELECT ts_code, trade_date, close, pct_chg FROM daily_bars "
            f"WHERE ts_code IN ({placeholders}) ORDER BY ts_code, trade_date",
            conn, params=list(codes))
    return df


def load_market_data(db_path=DB_PATH) -> pd.DataFrame:
    """全量读 daily_bars → 长表 DataFrame。"""
    init_db(db_path)
    with _connect(db_path) as conn:
        df = pd.read_sql("SELECT * FROM daily_bars ORDER BY ts_code, trade_date", conn)
    return df


def get_db_stats(db_path=DB_PATH) -> dict:
    """侧边栏缓存状态展示。"""
    init_db(db_path)
    with _connect(db_path) as conn:
        n_bars = conn.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
        n_codes = conn.execute("SELECT COUNT(DISTINCT ts_code) FROM daily_bars").fetchone()[0]
        latest = conn.execute("SELECT MAX(trade_date) FROM daily_bars").fetchone()[0]
        n_names = conn.execute("SELECT COUNT(*) FROM stock_names").fetchone()[0]
        name_update = _get_meta(conn, "name_map_update") or "未初始化"
        last_update = _get_meta(conn, "last_update") or "从未更新"
    return {"bars": n_bars, "codes": n_codes, "latest": latest,
            "names": n_names, "name_map_update": name_update,
            "last_update": last_update, "version": get_data_version(db_path)}
