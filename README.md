# 股票推荐回测 Web 应用

个人使用的股票推荐回测工具:输入每天手动更新的股票推荐 Excel,自动从 tushare 拉取 A 股/港股/ETF 日线行情(本地 SQLite 缓存),按"攻防转换"规则回测,网页端逐日查看持仓、净值、回撤与个股明细。

> 仅个人使用,不构成任何投资建议。

## 功能特性

### 输入 Excel

- 侧边栏上传或选择 `input/` 目录已有文件(默认选中项可配置)
- 只需「股票名称 + 起始日期」两列,名称自动匹配代码(精确/子串)
- 仓位/防守独立文件:「仓位」sheet 按区间给定每日仓位(0~10 成),「防守」sheet 为防守期持仓列表

### 行情数据

- tushare 拉日线,SQLite 本地缓存,增量更新只拉缺口
- 股票走 `pro.daily`、ETF 自动回退 `fund_daily`、港股走 `hk_daily`
- 指数/ETF 基准行情自动补齐(见下文基准对比)
- 限流重试:单只失败跳过并提示,下次更新自动补拉

### 回测(攻防转换策略)

- 当日仓位 ≥ 阈值 → 进攻:持有最近 y 条推荐
- 否则 → 防守:持有最近 z 条防守列表
- 调仓一律开盘价成交,同股新旧仓同日合并,重新买入另起一轮
- 手续费按股票/ETF 分别计费,从净值中扣减
- 当日账户收益 = (1 + 昨日持仓平均开盘涨跌幅) × (1 + 今日持仓平均实体涨幅) − 1

### 网页(Streamlit,四个 tab)

- **每日持仓**:任选日期查看当日交易与持仓、个股收益
- **组合净值**:净值/回撤曲线,起止日期可调、区间重定基;叠加基准对比(上证指数/深证成指/沪深300/中证红利/双创),单击图例可显示/隐藏曲线
- **回测明细**:全部推荐逐条汇总(买入日期/交易天数/累计收益/状态)
- **导出**:持仓与汇总导出 Excel

## 环境要求

- Python 3.10+(开发环境为 3.13)
- tushare pro token:需要日线权限;基准对比用到指数接口 `index_daily`,对积分有要求(2000 分以上)

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 创建 config.py(含 token,已被 .gitignore 排除,克隆后需自建)

在项目根目录新建 `config.py`,模板如下(token 换成自己的):

```python
import os
from pathlib import Path

# Tushare 配置
TS_TOKEN = os.environ.get("STOCK_APP_TOKEN", "你的tushare_token")

# 路径配置
BASE_DIR = Path(__file__).parent
CACHE_DIR = BASE_DIR / "cache"
DB_PATH = CACHE_DIR / "market.db"
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"

# 输入 Excel 列名映射
EXCEL_COLUMNS = {
    "name": "股票名称",
    "date": "起始日期",
}
KEEP_COLUMNS = None

# Tushare 限流与重试
REQUEST_SLEEP = 0.35
MAX_RETRY = 4

# 策略参数(攻防转换,详见 RULES.md)
REGIME_FILE_NAME = "stock_list_仓位及防守.xlsx"
DEFAULT_INPUT_FILE = "stock_list_先知.xlsx"
X_THRESHOLD = 4.5       # 仓位 >= x 成 → 进攻
Y_OFFENSE = 6           # 进攻持最近 y 条推荐
Z_DEFENSE = 2           # 防守持最近 z 条防守列表
FEE = {                 # 手续费率(占成交金额比例)
    "stock_buy": 0.0000741,
    "stock_sell": 0.0005741,
    "etf_buy": 0.00005,
    "etf_sell": 0.00005,
}

# 基准对比(组合净值页)
BENCHMARKS = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "000300.SH": "沪深300",
    "515080.SH": "中证红利",
}
BENCHMARK_COMBO = {
    "双创": ["399006.SZ", "000680.SH"],  # 创业板指 + 科创综指
}
```

### 3. 准备输入文件(放到 `input/` 目录)

- **先知推荐列表**(默认 `stock_list_先知.xlsx`):第一个 sheet,包含「股票名称」「起始日期」两列,每行一条推荐(每天手动追加)
- **仓位/防守文件**(`stock_list_仓位及防守.xlsx`):
  - 「仓位」sheet:起始日期 | 结束日期 | 仓位(数值,0~10 成)
  - 「防守」sheet:股票代码 | 股票名称 | 起始日期(代码缺失时按名称匹配;港股代码请存文本格式)

### 4. 启动

```bash
python -m streamlit run app.py
```

首次使用:侧边栏点「初始化/更新行情数据」拉取历史行情,之后每天点一次即可增量补齐(收盘数据 tushare 当日 15:00 后陆续发布)。

## 目录结构

| 文件/目录 | 说明 |
|---|---|
| `app.py` | Streamlit 页面(唯一入口) |
| `backtest.py` | 回测引擎:推荐 + 行情 + 规则 → 每日持仓/净值/个股汇总 |
| `rules.py` | 买卖规则(可插拔) |
| `indicators.py` | 指标与净值口径定义 |
| `data_fetcher.py` | tushare 交互、SQLite 读写、增量更新 |
| `config.py` | token/路径/列名/策略参数(不入库,克隆后自建) |
| `input/` | 输入 Excel(不入库,自行准备) |
| `cache/` | 行情 SQLite 缓存(运行后自动生成) |
| `output/` | 导出文件(运行后自动生成) |
| `RULES.md` | 策略与收益口径的完整定义(唯一事实来源) |
| `CHANGELOG.md` | 行为/口径变更记录 |
| `requirements.txt` | 依赖清单 |

## 免责声明

本项目为个人记录与研究工具,行情数据来自 tushare,准确性以其为准;回测结果不代表未来收益,不构成任何投资建议。
