# AI Stock 自动化交易骨架

这是一个面向同花顺桌面 App 的自动化交易骨架。当前默认是 `dry_run` 阶段和 `dry_run` 执行器，只记录计划动作，不会真实下单。

## 当前配置

- 股票代码：`588330`
- 交易时间：
  - 上午 `09:15-11:30`
  - 下午 `13:00-15:15`
- 默认执行：
  - `execution.stage`: `dry_run`
  - `execution.mode`: `dry_run`
  - 只写日志和本地模拟状态，不触碰同花顺
- 监控频率：`poll_seconds=60`，每 1 分钟获取一次行情并检查策略
- 初始资金：`50000`
- 买入策略：
  - 不再按第几次买入判断，改为按当前仓位状态判断
  - 仓位为 0%：连续 3 个交易日上涨，且 `MA5 > MA10 > MA20`，买入至 50% 仓位
  - 仓位大于等于 50%：当天价格大于最新买入价，`MA5 > MA10 > MA20 > MA60`，买入至 85% 仓位
  - 仓位大于等于 85%：当天价格大于最新买入价，`MA5 > MA10 > MA20 > MA60`，买入至 100% 仓位
  - 每次买入后，将成交价记录为最新买入价
  - 买入数量按 100 股一手取整
  - 买入目标仓位如果不够整手，取大于目标仓位的最小可买整手数量；受可用现金约束
- 卖出策略：
  - 当前亏损达到 3% 时，直接清仓
  - 持仓后记录第 1 段最高浮盈
  - 当前浮盈从当前段最高浮盈回撤达到 20% 时，卖出 50% 持仓
  - 第 1 次卖出后，将卖出时的当前浮盈设置为第 2 段最高浮盈
  - 第 2 次卖出后，将卖出时的当前浮盈设置为第 3 段最高浮盈
  - 第 3 次卖出基于第 3 段最高浮盈触发，触发时直接清仓
- 风控：
  - 每天最多执行 1 次交易
  - 交易白名单默认只有 `588330`
  - 支持 `STOP_TRADING` 一键停止文件
  - 非交易日和非交易时段默认不下单
  - 小资金实盘阶段默认买入金额上限 `5000`
  - 状态保存在 `portfolio.json`
- 执行层：
  - `DryRunExecutor`：只记录模拟订单
  - `ManualConfirmExecutor`：人工确认占位，尚不提交真实订单
  - `ThsComputerUseExecutor`：同花顺安全执行框架，要求截图/字段校验；真实最终确认默认关闭
- 行情层：
  - `market_data.py`：负责实时行情和日 K 数据
  - `trading_engine.py` 持续运行时会先记录目标标的实时行情，再调用策略判断
- 回测成本与诊断：
  - 佣金、最低手续费、印花税、滑点会计入资金曲线
  - 报告包含最大回撤、胜率、盈亏比、换手率、停牌/涨跌停检查、未来函数提示、幸存者偏差提示

## 快速开始

```bash
python3 trading_engine.py --once
```

忽略交易时间，仅测试策略：

```bash
python3 trading_engine.py --once --ignore-hours
```

周末或节假日仅做 dry-run 测试：

```bash
python3 trading_engine.py --once --ignore-hours --ignore-trade-day
```

`--ignore-trade-day` 只能搭配 `execution.mode=dry_run` 使用。

持续运行：

```bash
python3 trading_engine.py
```

持续运行入口是 `trading_engine.py`。它会按照 `config.json` 中的 `poll_seconds` 循环监控目标股票，当前为每 1 分钟一次；行情获取逻辑由 `market_data.py` 提供。持续运行时日志会显示在当前终端，按 `Ctrl+C` 可以立即停止。

如果想额外打开实时日志窗口：

```bash
python3 trading_engine.py --open-log
```

查看状态、停止和恢复：

```bash
python3 trading_engine.py --status
python3 trading_engine.py --stop
python3 trading_engine.py --clear-stop
```

`--stop` 会写入 `STOP_TRADING`，持续运行中的交易引擎会在睡眠期间每秒检查一次并尽快退出；下次重新运行前先执行 `--clear-stop`。

检查配置：

```bash
python3 trading_engine.py --check-config
```

运行单元测试：

```bash
python3 -m unittest discover -s tests
```

## 项目结构与工作顺序

核心工作流：

1. `trading_engine.py` 启动并进入循环。
2. `config.json` 提供股票代码、交易时段、风控和执行阶段配置。
3. `market_data.py` 获取目标股票实时行情和日 K 数据。
4. `trading_strategy.py` 根据行情、均线、持仓和最新买入价生成买卖信号。
5. `trading_engine.py` 的风控模块检查白名单、交易时段、交易日、仓位、金额和停止文件。
6. 执行器根据 `execution.mode` 处理信号；默认 `dry_run` 只记录，不真实下单。
7. 结果写入日志、`portfolio.json`、`signals.csv` 和 `runtime_state.json`。

文件职责：

- `trading_engine.py`：长期运行入口，负责调度、风控、执行、通知和日志。
- `market_data.py`：行情数据层，负责实时行情和日 K。
- `trading_strategy.py`：策略层，只负责生成交易信号。
- `backtest.py`：回测层，只负责历史回测、回测撮合、指标和报告。
- `config.json`：配置中心。
- `portfolio.json`：本地模拟资金和持仓状态。
- `signals.csv`：信号和执行审计。
- `trading_engine.log`：交易引擎运行日志。
- `trading_engine.monitor.log`：后台托管运行时建议使用的监控日志。
- `tests/`：安全测试和基础解析测试。

后台运行建议：

```bash
launchctl submit -l com.aistock.tradingengine -- /bin/zsh -lc 'cd /Users/yangdiandian/AI\ Stock && exec /usr/bin/python3 trading_engine.py >> trading_engine.monitor.log 2>&1'
```

查看后台日志：

```bash
tail -f trading_engine.monitor.log
```

停止后台运行：

```bash
launchctl remove com.aistock.tradingengine
```

## 执行阶段

`config.json` 中的 `execution.stage` 控制准入闸门：

- `dry_run`：只能使用 `execution.mode=dry_run`。
- `gui_simulation`：允许激活同花顺、截图、读取校验字段，但 `final_confirm_enabled=false` 时不会最终提交。
- `small_live`：预留小资金实盘阶段，默认买入金额上限 `5000`。
- `full_live`：预留完整额度阶段，默认买入金额上限 `50000`。

`config.json` 中的 `execution.mode` 控制执行器：

- `dry_run`：默认模式，最安全。
- `manual_confirm`：人工确认占位，当前不会提交真实订单。
- `ths_computer_use`：同花顺 GUI 自动化安全框架，当前必须通过截图字段校验，真实提交仍被阻断，直到外部 Computer Use 适配器接入。

同花顺 GUI 校验字段可以通过 `execution.verification_fields_path` 指向一个 JSON 文件，格式示例：

```json
{
  "symbol": "588330",
  "side": "BUY",
  "quantity": 100,
  "limit_price": 1.234
}
```

如果未读取到这些字段，且 `require_screenshot_verification=true`，系统会阻断下单。

## 回测报告

默认回测区间为 `2025-01-01` 至今天，生成可交互网页报告，不生成 PNG，并自动用 Microsoft Edge 打开报告。成交价默认使用 `mootdx` 1 分钟 K 的分时价格，分时数据保存在本地 SQLite 数据库 `market_data.sqlite3`，后续运行只按数据库最新时间增量抓取新分钟线：

```bash
python3 backtest.py
```

输出文件：

- `backtest_588330_YYYYMMDD_YYYYMMDD.html`

如果只想生成文件、不打开浏览器：

```bash
python3 backtest.py --no-open
```

如果要回退到旧的日收盘价成交假设：

```bash
python3 backtest.py --no-open --execution-price close
```

如果要指定分时成交时间：

```bash
python3 backtest.py --no-open --execution-time 09:31
```

如果要忽略数据库最新时间并重新回补可获取的分钟线：

```bash
python3 backtest.py --no-open --refresh-minute-cache
```

如需额外导出 PNG：

```bash
python3 backtest.py --png
```

## 数据源约定

已安装 `a-stock-data` skill。后续 A 股行情、历史 K 线、交易日等数据获取，优先按照该 skill 的要求和数据源流程执行。

## 下一步需要补充

1. 真实可用资金和真实持仓：当前 `portfolio.json` 使用本地模拟状态。
2. 同花顺 App 自动化方式：按钮坐标、快捷键流程，或其他可用接口。
3. 卖出规则确认：当前按“分段最高浮盈回撤 20%”理解。

## 重要说明

真实交易前请先长时间 dry-run 和 GUI 模拟。自动化交易可能因为行情延迟、网络、App 弹窗、坐标偏移等原因产生错误操作。当前代码不会绕过截图/字段校验直接点击真实最终确认。

## 更新记录

- 更新日期：2026-06-18
- 更新内容：review README 并同步项目更新；交易入口调整为 `trading_engine.py`，补充实时行情层、持续运行/停止命令、项目结构、仓位目标买入、3% 止损、回测输出和测试说明。

- 更新日期：2026-06-14
- 更新内容：同步现有项目到 GitHub，补充 README 更新日期和更新内容说明。
