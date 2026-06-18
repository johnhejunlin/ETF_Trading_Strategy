# AI Stock Agent Guide

本文件是本项目给 Codex / AI agent 的协作说明。目标是把当前项目逐步建设成一个可长期运行、可回测、可审计、可风控的个人量化交易平台。

## 项目定位

- 当前项目是面向 A 股 / ETF 的个人量化交易实验平台。
- 现阶段优先级是：数据可靠性、回测可信度、dry-run 稳定性、日志审计、人工确认流程。
- 不要默认直接做真实下单。任何真实交易能力都必须经过显式配置、明确确认和风控保护。
- Codex 适合作为研究、开发、运维和分析助手；长期运行任务应交给脚本、服务、调度器或服务器进程。

## 当前核心文件

- `README.md`：项目说明和常用命令。
- `config.json`：策略、组合、交易时段和回测参数配置。
- `market_data.py`：实时行情和日 K 数据获取；交易入口应通过它监控目标标的。
- `trading_strategy.py`：交易信号和策略实现，不放回测报告、指标或撮合逻辑。
- `trading_engine.py`：交易机器人主流程，当前默认 dry-run。
- `backtest.py`：回测、回测数据处理、回测撮合、指标和报告生成脚本。
- `portfolio.json`：本地模拟资金和持仓状态。
- `signals.csv`：策略信号输出。
- `trading_engine.log`：运行日志。
- `trading_engine.monitor.log`：后台托管运行时的实时监控日志。
- `runtime_state.json`：执行阶段和 GUI 模拟结果等运行状态记录。
- `screenshots/`：同花顺截图、订单意图和 GUI 校验凭证。
- `tests/`：不依赖网络和真实交易的安全单元测试。
- `requirements.txt`：最小 Python 依赖清单。
- `backtest_trades*.csv`：回测交易明细。
- `backtest_*.html`：回测网页报告。

## 安全边界

1. 默认保持 dry-run，不应在未获得明确授权前启用真实下单。
2. 不要把账户密码、交易密码、券商 token、cookie 或个人敏感信息写入仓库明文文件。
3. 任何交易执行相关修改必须同时考虑：
   - 最大单笔金额
   - 最大单票仓位
   - 单日最大交易次数
   - 单日最大亏损
   - 一键停止机制
   - 下单前后的日志和截图/凭证
4. Computer Use / GUI 自动点击只能作为受控辅助方案，不能作为高信任核心执行层。
5. 如果未来接入券商 API，应优先使用官方、稳定、可审计接口。
6. 下单逻辑必须能解释、能回放、能从日志复盘。
7. `execution.mode=ths_computer_use` 仍必须通过截图/字段校验；不得为了“跑通”而绕过 `require_screenshot_verification`。
8. `small_live` 之前必须先完成 dry-run 和 GUI 模拟阶段，不能直接把 `final_confirm_enabled` 打开。

## 开发原则

- 先读现有代码和配置，再修改。
- 保持改动小而清晰，避免顺手重构无关模块。
- 优先复用项目已有结构，不随意引入复杂框架。
- 策略逻辑、风控逻辑、执行逻辑应逐步拆清楚，不要混在 UI 自动化里。
- 对涉及资金、持仓、下单、风控的改动，必须加日志或可验证输出。
- 回测不能只看收益率，还要关注最大回撤、手续费、滑点、换手率、未来函数和幸存者偏差。

## 推荐架构方向

长期目标可以拆成四层：

1. 数据层：行情、历史 K 线、公告、财报、指数成分、交易日历。
2. 策略层：信号生成、参数管理、回测、模拟交易。
3. 风控层：仓位限制、亏损限制、交易频率限制、异常熔断、人工确认。
4. 执行层：dry-run、模拟盘、券商 API、受控 GUI 辅助。

Codex 可以帮助开发和维护这些层，但不应作为长期生产交易进程本身。

## 常用命令

```bash
python3 trading_engine.py --once
python3 trading_engine.py --once --ignore-hours
python3 trading_engine.py --once --ignore-hours --ignore-trade-day
python3 trading_engine.py --check-config
python3 trading_engine.py --status
python3 trading_engine.py --stop
python3 trading_engine.py --clear-stop
python3 trading_engine.py --open-log
python3 trading_engine.py
python3 backtest.py --no-open
python3 -m unittest discover -s tests
```

后台托管运行使用 label `com.aistock.tradingengine`，不要再使用旧的 `com.aistock.tradebot`。
持续运行时默认只在当前终端显示日志；如果需要额外实时日志窗口，用 `--open-log`。前台持续运行时可直接看终端日志并用 `Ctrl+C` 停止；后台/另一终端可用 `--stop` 写入 `STOP_TRADING` 让引擎尽快退出。

修改策略后，至少运行一次回测或 dry-run 检查：

```bash
python3 backtest.py
python3 trading_engine.py --once --ignore-hours
```

策略修改后的回测报告应自动在 Microsoft Edge 中打开；只有用户明确要求只生成文件时才使用 `--no-open`。
回测默认日期固定为 `2025-01-01` 至今天；不要把回测日期默认值分散到其他文件。

## 真实交易前检查清单

- 已连续多日 dry-run，无异常信号、异常日志或数据中断。
- 已确认真实持仓和本地 `portfolio.json` 不会互相误导。
- 已确认交易标的白名单。
- 已设置单笔、单日、单票、总仓位限制。
- 已设置异常行情、数据失败、网络失败、App 弹窗或接口失败的停止逻辑。
- 已保留下单前信号、价格、数量、理由和账户状态。
- 已保留下单后结果、成交状态、失败原因和账户状态。
- 已有人工确认或一键停止方案。

## 与用户沟通方式

- 涉及真实交易、券商接入、交易密码、下单执行时，必须先向用户说明风险和确认边界。
- 如果只是回测、日志、报告、dry-run、代码整理，可以直接实施并验证。
- 对不确定的数据源、交易规则或券商能力，要明确说出假设，不要装作已经确认。
- 如果发现策略可能存在未来函数、数据偏差或风控缺口，应优先指出。

## 下一步建设建议

1. 把交易执行抽象成 `ExecutionAdapter`：`DryRunExecutor`、`ManualConfirmExecutor`、未来的 `BrokerApiExecutor`。
2. 把风控独立成模块，在任何执行器之前统一拦截。
3. 增加交易日历，避免非交易日和节假日误判。
4. 增加通知渠道，例如本地通知、邮件、Slack、企业微信或 Telegram。
5. 增加持仓同步能力，避免本地模拟状态和真实账户状态不一致。
6. 建立长期运行方式，例如 `systemd`、Docker、cron 或云服务器部署。
