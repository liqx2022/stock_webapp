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

规则函数签名:`decide(date, holdings, bars, recs) -> list[Action]`,`Action(op, pos, weight)`,`op ∈ {buy, sell, sell_open}`(买入均开盘价;sell=收盘卖,sell_open=开盘卖,开盘卖当日收益=开盘涨跌幅)。`recs` 条目为 `(pos, ts_code, name, start_date)`,`pos` 是推荐唯一键,每条推荐是独立交易。当前规则:攻防转换(完整口径见 RULES.md,参数在 config.py 策略参数区):仓位 ≥ x 进攻持最近 y 条推荐,否则防守持最近 z 条防守列表;调仓一律开盘价成交,同股新旧仓同日合并(保留最早进场旧仓、不开新仓);引擎 fee 参数计手续费(费率在 config.py)。规则带状态,app.py 每次回测新建实例;改规则只动 rules.py。

## 约定

- 日期一律 `YYYY-MM-DD` 字符串;收益存小数(0.05 = 5%)
- 口径:个股累计收益=买入当日实体涨幅(收盘/开盘-1),之后每日=pct_chg/100,停牌日跳过;当日账户收益=(1+昨日持仓今日平均开盘涨跌幅)×(1+今日持仓平均实体涨幅)-1;持仓页当日收益率展示:卖出=开盘涨跌幅、当日新买=实体涨幅、持有=全天涨跌幅(收盘/昨收-1)(完整口径见 RULES.md)
- 图表配色:红涨绿跌(中国股市惯例)
- 行情表主键 ts_code+trade_date,写入用 INSERT OR REPLACE
- 代码后缀:6→.SH,0/3→.SZ,5位→.HK;名称匹配先精确、后子串互含

## 禁止项

- 不引入 FastAPI/Flask/Docker/异步/ORM,保持单机简单
- config.py、cache/、input/、output/ 不提交 git

## 已知简化

万德全A基准对比未做;同一天同股多条推荐会开成两笔相同持仓(各自独立计算)。行为/口径变更按日期记在 CHANGELOG.md,策略口径细节以 RULES.md 为准。

## 常见问题

- tushare 限流:重试 4 次仍失败→该股票记跳过,下次点更新自动补
- 名称匹配不上:侧边栏警告列出,可点"刷新股票名称表"
- 改 config.py 策略参数(x/y/z/费率):保存后 ~2 秒页面自动重算(内置 fragment 监视文件);改 token/路径仍需重启
- 输出旧版对照:旧脚本/旧数据在 `C:\workspace\python_mybasic\`(只读参考,勿改动)
