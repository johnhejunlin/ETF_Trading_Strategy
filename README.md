# AI Stock 自动化交易骨架

这是一个面向同花顺桌面 App 的自动化交易骨架。当前默认进入 `sim_run` 阶段，使用同花顺至尊版模拟账户完成模拟交易；真实账户仍被禁用。

## 当前配置

- 股票代码：`588330`
- 交易时间：
  - 上午 `09:15-11:30`
  - 下午 `13:00-15:15`
- 默认执行：
  - `execution.stage`: `sim_run`
  - `execution.mode`: `ths_computer_use`
  - 进入同花顺至尊版模拟账户，先同步资金/持仓，字段和截图校验通过后提交模拟买卖单
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
  - 所有抓取到的行情数据统一保存到项目根目录的 `market_data.sqlite3`
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
6. 执行器根据 `execution.mode` 和 `execution.stage` 处理信号；当前默认 `sim_run` 会在同花顺至尊版模拟账户提交订单。
7. 结果写入日志、`portfolio.json`、`signals.csv` 和 `runtime_state.json`。

文件职责：

- `trading_engine.py`：长期运行入口，负责调度、风控、执行、通知和日志。
- `market_data.py`：行情数据层，负责实时行情和日 K。
- `market_data_store.py`：行情 SQLite 落库层，统一维护 `market_data.sqlite3`。
- `trading_strategy.py`：策略层，只负责生成交易信号。
- `backtest.py`：回测层，只负责历史回测、回测撮合、指标和报告。
- `config.json`：配置中心。
- `portfolio.json`：本地模拟资金和持仓状态，运行时自动创建，不上传 GitHub。
- `signals.csv`：信号和执行审计，运行时自动追加，不上传 GitHub。
- `market_data.sqlite3`：行情数据库，保存回测日 K、回测 1 分钟 K 和实时轮询行情，不上传 GitHub。
- `trading_engine.log`：交易引擎运行日志，不上传 GitHub。
- `trading_engine.monitor.log`：后台托管运行时建议使用的监控日志，不上传 GitHub。
- `tests/`：安全测试和基础解析测试。

## GitHub 上传范围

为了保持仓库通用、轻量且不包含本地运行状态，GitHub 只上传源码、配置模板、脚本、测试和文档。

会上传：

- 源码：`trading_engine.py`、`trading_strategy.py`、`market_data.py`、`market_data_store.py`、`backtest.py`
- 配置与依赖：`config.json`、`requirements.txt`
- 启停脚本：`Trading_Engine.command`、`automation_start_trading_engine.sh`、`automation_stop_trading_engine.sh`
- 测试与文档：`tests/`、`README.md`、`agent.md`、`.gitignore`

不会上传：

- 回测结果：`backtest_*.html`、`backtest_*.png`、`backtest_trades_*.csv`
- 行情数据库：`market_data.sqlite3`
- 本地运行状态：`portfolio.json`、`runtime_state.json`、`signals.csv`、`STOP_TRADING`
- 日志：`*.log`
- GUI 自动化临时产物：`screenshots/*.png`、`screenshots/latest_order_intent.json`、`screenshots/latest_verified_order.json`
- Python 缓存、虚拟环境和 IDE 配置：`__pycache__/`、`.venv/`、`venv/`、`.idea/`、`.vscode/`

后台运行建议：

```bash
launchctl submit -l com.aistock.tradingengine -- /usr/bin/env zsh -lc 'cd "$1" && exec "$(command -v python3)" trading_engine.py >> trading_engine.monitor.log 2>&1' zsh "$(pwd)"
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
- `gui_simulation`：生成订单意图后等待受控 GUI 自动化完成同花顺界面填单和字段校验；`final_confirm_enabled=false` 时不会最终提交。
- `sim_run`：使用同花顺至尊版模拟账户执行买入/卖出；必须保持 `execution.ths_account_mode=simulation`，并通过截图/OCR/字段校验。
- `small_live`：预留小资金实盘阶段，默认买入金额上限 `5000`。
- `full_live`：预留完整额度阶段，默认买入金额上限 `50000`。

`config.json` 中的 `execution.mode` 控制执行器：

- `dry_run`：只记录计划动作，不触碰同花顺。
- `manual_confirm`：人工确认占位，当前不会提交真实订单。
- `ths_computer_use`：同花顺 GUI 协作执行器。后台进程生成订单意图并读取校验文件；真实 App UI 操作可由 Codex Computer Use、AppleScript bridge 或其他受控本机自动化完成。当前默认只允许模拟账户交易，实盘账户仍被阻断。

同花顺 Mac 版交易界面必须先使用 App 内的“模拟”交易选项完成调试。`execution.ths_account_mode` 默认是 `simulation`，`execution.live_account_enabled=false`；在这个状态下，如果 GUI 校验识别到实盘/普通交易界面，系统会阻断执行。

`execution.gui_bridge_command` 和 `execution.account_bridge_command` 可用于后续接入受控 GUI bridge，但默认仍建议留空，由人工、Codex Computer Use 或 AppleScript bridge 按需处理。交易引擎会写出 `screenshots/latest_order_intent.json`，然后等待并读取 `screenshots/latest_verified_order.json`；桥接实现必须写入账户模式、订单字段、提交状态、截图路径和可复盘日志。

`gui_simulation` 阶段只要求完成填单并写回 `submitted=false` 的校验文件。`sim_run` 阶段在字段校验通过且 `execution.final_confirm_enabled=true` 后，可由受控 GUI 自动化点击最终的“买入(模拟账户)”或“卖出(模拟账户)”按钮，并写回 `submitted=true`；后台引擎只认校验文件和截图凭证，不假设具体由哪种工具点击。`execution.codex_computer_use_timeout_seconds` 控制引擎等待回写结果的最长时间。

不要把交易密码写入 `config.json`、脚本或任何会提交到 GitHub 的文件。需要输入交易密码时，只能通过受控交互按需输入，不写入仓库。

账户同步由受控 GUI 自动化读取同花顺模拟账户界面后写入 `screenshots/latest_account_snapshot.json`，格式类似：

```json
{
  "account_mode": "simulation",
  "total_assets": 50123.45,
  "available_cash": 12345.67,
  "cash_balance": 20000.0,
  "market_value": 30123.45,
  "profit_loss": -88.5,
  "source": "codex_computer_use",
  "submitted": false
}
```

也可以使用 Apple 原生只读快照桥进行模拟账户诊断：

```bash
python3 App_Bridge_AppleScript.py
python3 apple_account_snapshot.py
python3 apple_account_snapshot.py --write-latest
```

`App_Bridge_AppleScript.py` 会打开同花顺并导航到 App 内“模拟交易”的持仓页，写出 `screenshots/latest_applescript_bridge_holdings.json` 作为导航校验凭证；它不会填单或提交订单。`apple_account_snapshot.py` 会通过 CoreGraphics 查找同花顺窗口，使用 `screencapture` 截指定窗口，再用 Apple Vision OCR 读取账户页。默认只写 `screenshots/apple_account_snapshot_*.json` 和 `screenshots/latest_account_ocr.json` 诊断文件；只有 `--write-latest` 才会写入 `screenshots/latest_account_snapshot.json`。交易引擎在 `execution.account_snapshot_allowed_sources` 显式包含 `apple_vision_ocr` 时，会先运行 `App_Bridge_AppleScript.py` 校验模拟持仓页，再运行 `apple_account_snapshot.py` 生成标准账户快照；快照没有 `warnings` / `validation_errors` 时才会接受。

`trading_engine.py` 打开同花顺 App 后会优先复用未过期且来源属于 `execution.account_snapshot_allowed_sources` 的账户快照，并把 `portfolio.json` 的可用金额、总资产和目标标的持仓同步到该快照；持续轮询行情和策略时不会重复做账户验证。`execution.force_account_sync_on_app_open=true` 时，启动后会强制要求重新读取账户。无论启动时是否复用快照，真正下单前、下单后仍会强制重新读取账户。

如果快照不存在、过期、无效或账户模式不是 `simulation`，引擎会写出账户同步请求到 `screenshots/latest_codex_computer_use_request.json` 并等待受控 GUI 自动化回写结果，超时后把请求状态标记为 `timed_out` 并停止本次运行，避免用过期的本地资金/持仓继续判断交易。

这里的资金口径必须区分：

- `total_assets` / 总资产：只用于账户跟踪和仓位占比计算，会随行情波动，不作为下单资金依据。
- `cash` / `available_cash` / 可用金额：交易引擎里的“金额”口径，买入下单和风控只使用这个字段。

当策略生成买入/卖出信号后，交易引擎会在执行前强制重新读取同花顺模拟账户，刷新 `portfolio.json`，并用最新可用金额和持仓重新生成/校验信号。买入金额必须小于等于最新账户可用金额，卖出数量必须小于等于最新账户持仓。订单提交成功后，引擎会再次强制读取账户快照，确认交易后的资金和持仓状态。

执行限价口径：买入指令使用实时行情中的涨停价作为限价，卖出指令使用实时行情中的跌停价作为限价；如果行情源没有提供对应涨跌停价，本轮执行会被阻断。

同花顺 GUI 校验字段可以通过 `execution.verification_fields_path` 指向一个 JSON 文件，格式示例：

```json
{
  "account_mode": "simulation",
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

## 行情数据保存

项目根目录的 `market_data.sqlite3` 是统一行情数据库：

- `daily_bars`：日 K 数据。回测抓取和实时策略检查抓取的日 K 都会写入这里。
- `minute_bars`：1 分钟 K 数据。回测使用 `mootdx` 抓取的分时数据会写入这里。
- `realtime_quotes`：实时轮询行情。`trading_engine.py` 每次获取目标标的实时行情后会追加一条记录。

查看数据库表：

```bash
sqlite3 market_data.sqlite3 ".tables"
```

## 数据源约定

已安装 `a-stock-data` skill。后续 A 股行情、历史 K 线、交易日等数据获取，优先按照该 skill 的要求和数据源流程执行。

## 下一步需要补充

1. 真实可用资金和真实持仓：当前 `portfolio.json` 使用本地模拟状态。
2. 同花顺 App 自动化方式：按钮坐标、快捷键流程，或其他可用接口。
3. 卖出规则确认：当前按“分段最高浮盈回撤 20%”理解。

## 重要说明

真实交易前请先长时间 dry-run、GUI 模拟和 sim-run。自动化交易可能因为行情延迟、网络、App 弹窗、坐标偏移等原因产生错误操作。当前代码只允许在同花顺模拟账户下提交订单，不会绕过截图/字段校验直接点击真实最终确认。

## 更新记录

- 更新日期：2026-06-30
- 更新内容：准备上传 GitHub：补充 GitHub 上传范围说明，排除回测报告、回测交易明细、行情数据库、运行状态、日志和 GUI 临时产物。

- 更新日期：2026-06-24
- 更新内容：补充同花顺 Mac 模拟交易桥接、安全账户模式校验、GUI 填单验证、行情数据节流与股票代码规范化，并更新自动化启动脚本。

- 更新日期：2026-06-18
- 更新内容：review README 并同步项目更新；交易入口调整为 `trading_engine.py`，补充实时行情层、持续运行/停止命令、项目结构、仓位目标买入、3% 止损、回测输出和测试说明。

- 更新日期：2026-06-14
- 更新内容：同步现有项目到 GitHub，补充 README 更新日期和更新内容说明。
