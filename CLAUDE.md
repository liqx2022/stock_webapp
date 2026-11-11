# 股票推荐回测 Web 应用

个人使用的股票推荐回测工具:输入 Excel(每天手动更新的股票推荐列表),从 tushare 拉日线行情,计算收益/回撤,网页按日期查看每日持仓。

## 运行

```bash
.venv\Scripts\python.exe -m streamlit run app.py
```

## 文件职责

| 文件 | 职责 |
|---|---|
| app.py | Streamlit 页面(唯一入口):上传 Excel、更新行情、四个 tab(每日持仓/组合净值/回测明细/导出) |
| data_fetcher.py | tushare 全部交互、SQLite 建表与读写、增量更新(只拉缺口) |
| backtest.py | 回测引擎:推荐记录 + 行情 + 规则 → 每日持仓/组合净值/个股汇总 |
| rules.py | 买卖规则(可插拔)。**改规则只动本文件,不要改 backtest.py** |
| indicators.py | 指标定义(与旧脚本口径一致) |
| config.py | token 与列名映射(含密钥,已 gitignore,不入库) |

## 数据流

输入 Excel(列名映射在 config.py)→ 名称→代码(stock_names 缓存)→ 行情 SQLite(cache/market.db,增量)→ 引擎 → 页面/导出(output/)。

## 规则契约

规则函数签名:`decide(date, holdings, bars, recs) -> list[Action]`,`Action(op, code, weight)`,`op ∈ {buy, sell}`。当前规则:推荐起始日开盘买入,持有第 10 个交易日收盘卖出(见 rules.py `make_hold_n_days(10)`,天数可调)。规则带内部状态(持有天数/已卖集合),app.py 每次回测新建实例;改规则只动 rules.py。

## 约定

- 日期一律 `YYYY-MM-DD` 字符串;收益存小数(0.05 = 5%)
- 口径(与旧脚本 `股票统计1_0628智能评分收益回测系统.py` 对齐):买入当日收益=实体涨幅(收盘/开盘-1),之后每日=pct_chg/100;停牌日收益 0;组合收益=持仓等权平均
- 图表配色:红涨绿跌(中国股市惯例)
- 行情表主键 ts_code+trade_date,写入用 INSERT OR REPLACE
- 代码后缀:6→.SH,0/3→.SZ,5位→.HK;名称匹配先精确、后子串互含

## 禁止项

- 不引入 FastAPI/Flask/Docker/异步/ORM,保持单机简单
- config.py、cache/、input/、output/ 不提交 git

## 已知简化(v1)

智能评分汇总行、万德全A基准对比未做;买入按开盘、卖出按收盘;同股多条推荐按代码去重(最早日期、最高评分)。

## 常见问题

- tushare 限流:重试 4 次仍失败→该股票记跳过,下次点更新自动补
- 名称匹配不上:侧边栏警告列出,可点"刷新股票名称表"
- 输出旧版对照:旧脚本/旧数据在 `C:\workspace\python_mybasic\`(只读参考,勿改动)
