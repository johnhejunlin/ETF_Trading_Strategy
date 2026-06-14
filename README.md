# AI Stock 自动化交易骨架

这是一个面向同花顺桌面 App 的自动化交易骨架。当前默认是 `dry_run` 阶段和 `dry_run` 执行器，只记录计划动作，不会真实下单。

## 更新记录

- 更新日期：2026-06-14
- 更新内容：同步现有项目到 GitHub，补充 README 更新日期和更新内容说明。

## 当前配置

- 股票代码：`588330`
- 交易时间：
  - 上午 `09:15-11:30`
  - 下午 `13:00-15:15`
- 默认执行：
  - `execution.stage`: `dry_run`
  - `execution.mode`: `dry_run`
  - 只写日志和本地模拟状态，不触碰同花顺
- 初始资金：`50000`
- 买入策略：
  - 第一次买入：前两天上涨且当天上涨，`MA5 > MA10 > MA20`，买入可用资金的 50%
  - 第二次买入：当天价格大于第一次买入价格，`MA5 > MA10 > MA20 > MA60`，买入可用资金的 70%
  - 第三次买入：当天价格大于第二次买入价格，`MA5 > MA10 > MA20 > MA60`，买入全部可用资金
  - 买入数量按 100 股一手取整
  - 不要求空仓，已有持仓时也可以继续买入
- 卖出策略：
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
- 回测成本与诊断：
  - 佣金、最低手续费、印花税、滑点会计入资金曲线
  - 报告包含最大回撤、胜率、盈亏比、换手率、停牌/涨跌停检查、未来函数提示、幸存者偏差提示

## 快速开始

```bash
python3 trade_bot.py --once
```

忽略交易时间，仅测试策略：

```bash
python3 trade_bot.py --once --ignore-hours
```

周末或节假日仅做 dry-run 测试：

```bash
python3 trade_bot.py --once --ignore-hours --ignore-trade-day
```

`--ignore-trade-day` 只能搭配 `execution.mode=dry_run` 使用。

持续运行：

```bash
python3 trade_bot.py
```

检查配置：

```bash
python3 trade_bot.py --check-config
```

运行单元测试：

```bash
python3 -m unittest discover -s tests
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
