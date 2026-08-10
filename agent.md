# AI Stock Agent Guide

本文件是本项目给 Codex / AI agent 的协作说明。目标是把当前项目逐步建设成一个可长期运行、可回测、可审计、可风控的个人量化交易平台。

## 项目定位

- 当前项目是面向 A 股 / ETF 的个人量化交易系统。
- 现阶段优先级是：数据可靠性、回测可信度、dry-run 稳定性、日志审计、人工确认流程、APP交互可靠性。
- 不要默认直接做真实下单。任何真实交易能力都必须经过显式配置、明确确认和风控保护。
- Codex 适合作为研究、开发、运维和分析助手；长期运行任务应交给脚本、服务、调度器或服务器进程。

## 当前核心文件

- `README.md`：项目说明和常用命令。
- `config.json`：项目配置，策略、组合、交易时段和回测参数等配置。
- `market_data.py`：实时行情和日 K 数据获取；交易入口应通过它监控目标标的。
- `trading_strategy.py`：交易信号和策略实现，不放回测报告、指标或撮合逻辑。
- `trading_engine.py`：交易机器人主流程，当前默认 dry-run。
- `backtest.py`：回测、回测数据处理、回测撮合、指标和报告生成。
- `portfolio.json`：本地模拟资金和持仓状态，运行时自动创建，不上传 GitHub。
- `signals.csv`：策略信号输出，运行时自动追加，不上传 GitHub。
- `trading_engine.log`：运行日志，不上传 GitHub。
- `trading_engine.monitor.log`：后台托管运行时的实时监控日志，不上传 GitHub。
- `runtime_state.json`：执行阶段和 GUI 模拟结果等运行状态记录，不上传 GitHub。
- `screenshots/`：同花顺截图、订单意图和 GUI 校验凭证，不上传 GitHub。需定期清理。
- `tests/`：不依赖网络和真实交易的本地安全单元测试，不上传 GitHub。
- `requirements.txt`：最小 Python 依赖清单。
- `backtest_trades*.csv`：回测交易明细，不上传 GitHub。需定期清理。
- `backtest_*.html`：回测网页报告，不上传 GitHub。需定期清理。

## GitHub 提交范围

本仓库在 GitHub 上应保持通用、轻量、可复现。提交前必须检查 `git status --short --ignored`，确认本地运行产物被 `.gitignore` 忽略。

应该提交：

- 源码：`trading_engine.py`、`trading_strategy.py`、`market_data.py`、`market_data_store.py`、`backtest.py`
- 配置与依赖：`config.json`、`requirements.txt`
- 启停脚本：`Trading_Engine.command`、`automation_start_trading_engine.sh`、`automation_stop_trading_engine.sh`
- 文档与忽略规则：`README.md`、`agent.md`、`.gitignore`

不应提交：

- 回测结果：`backtest_*.html`、`backtest_*.png`、`backtest_trades_*.csv`
- 本地测试：`tests/`
- 行情数据库：`market_data.sqlite3`
- 本地运行状态：`portfolio.json`、`runtime_state.json`、`signals.csv`、`STOP_TRADING`
- 日志：`*.log`
- GUI 自动化临时产物：`screenshots/*.png`、`screenshots/latest_order_intent.json`、`screenshots/latest_verified_order.json`
- Python 缓存、虚拟环境和 IDE 配置：`__pycache__/`、`.venv/`、`venv/`、`.idea/`、`.vscode/`

如果这些文件已经被误提交，应使用 `git rm --cached` 从 Git 索引移除，保留本地文件本身。

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
4. Computer Use / AppleScript / GUI 自动点击只能作为受控辅助方案，不能作为高信任核心执行层。
5. 如果未来接入券商 API，应优先使用官方、稳定、可审计接口。
6. 下单逻辑必须能解释、能回放、能从日志复盘。
7. `execution.mode=ths_applescript` 仍必须通过截图/字段校验；不得为了“跑通”而绕过 `require_screenshot_verification`。
8. 同花顺 Mac 版在完全调试完成前必须使用 App 内“模拟”交易选项；保持 `execution.ths_account_mode=simulation` 和 `execution.live_account_enabled=false`。
9. `sim_run` 是同花顺模拟账户提交阶段，必须保持 `execution.ths_account_mode=simulation`；`small_live` 之前必须先完成 dry-run、GUI 模拟和 sim-run 阶段，不能直接切到实盘。
10. AppleScript 允许用于同花顺 App 激活、窗口定位、模拟账户填单和模拟账户提交；使用前必须有用户明确要求，且必须保留账户模式校验、订单字段校验、截图/日志凭证和提交结果记录。AppleScript 不得用于实盘提交，除非后续另行完成小资金实盘阶段的显式授权和风控配置。

## App 交互方式

- 同花顺普通版常规导航、填单和按钮操作应优先使用 macOS Accessibility：按控件语义名称执行 `AXPress`，按“代码/价格/数量”等标签与文本框的几何关系设置并回读 `AXValue`；不得依赖 `child[n]` 控件序号。
- 同花顺证券代码栏禁止粘贴，也禁止直接设置 `AXValue`；必须聚焦代码文本框后逐字符键入证券代码，让 App 触发证券和市场代码匹配逻辑。价格和数量字段可继续设置并回读 `AXValue`。
- Accessibility 成功不能替代截图/OCR 安全校验。模拟账户、买卖方向、代码、价格、数量、确认页和提交回执仍必须通过独立视觉校验并保留凭证。
- `AppBridge_UIMap.py` 和 OCR 坐标点击只用于 Accessibility 未暴露的自绘控件、WebView 或特殊弹窗，不参与常规交易导航和填单。Accessibility 操作成功但 OCR 安全校验失败时必须直接停止，不得转入坐标兜底。
- 当任务需要与本机 App 互动、操作 App UI、通过截图/OCR 定位控件或使用 AppleScript/System Events 点击时，必须先加载并遵循 `AppBridge_OCRPositionCalculation.py` 。坐标转换要按该 skill 的流程处理：优先激活目标 App 后截图，记录窗口 bounds、截图尺寸、OCR 框和点击坐标，不得把截图像素坐标直接当成 System Events 点击坐标。
- 同花顺常规 App 交互默认使用项目内 AppleScript + macOS Accessibility + Apple Vision OCR bridge。没有用户明确要求或特殊诊断需求时，不使用 Codex Computer Use。
- 只有用户明确指定 Computer Use，或现有 AppleScript/Accessibility bridge 无法完成且任务确有特殊 UI 诊断需求时，才允许使用 Codex Computer Use；使用前必须说明原因，并继续遵守模拟账户和截图校验边界。
- 本项目的 OCR 坐标换算应优先复用 `AppBridge_OCRPositionCalculation.py`；它封装了 Vision OCR 归一化坐标、Retina scale、`screencapture -l` 活跃/非活跃窗口边框差异和 System Events 点击坐标之间的转换。
- 当前项目不要使用 Hermes/CUA 的 `cua-driver` 作为 App 交互层实现路径；只有在用户明确要求临时诊断，且必须说明这是降级/备选路径时，才允许调用。
- 同花顺相关 UI 调试、账户同步验证、填单验证和截图校验，默认通过 AppleScript、Accessibility 和 Apple Vision OCR 完成；无论使用哪种方式，都必须确认 App 内为“模拟”交易入口，并写出可复盘的字段、截图和日志凭证。
- 用户已授权将同花顺模拟账户登录密码保存到 macOS Keychain，service 名称为 `ETF_Trading_System_THS_Simulation_Password`。需要登录模拟账户时，可用 `security find-generic-password -a "$USER" -s "ETF_Trading_System_THS_Simulation_Password" -w` 读取后通过受控交互输入；不得把密码写入仓库、配置、脚本、日志或回复正文。

## 开发原则

- 先读现有代码和配置，再修改。
- 保持改动小而清晰，避免顺手重构无关模块。
- 优先复用项目已有结构，不随意引入复杂框架。
- 策略逻辑、风控逻辑、执行逻辑应逐步拆清楚，不要混在 UI 自动化里。
- 对涉及资金、持仓、下单、风控的改动，必须加日志或可验证输出。
- 回测不能只看收益率，还要关注最大回撤、手续费、滑点、换手率、未来函数和幸存者偏差。

## 测试规范

- 所有自动化测试必须放在 `tests/` 目录，测试文件名必须以 `test_` 开头。
- 新增测试前先检查 `tests/` 中是否已有对应模块或功能的测试文件；如果已有，优先在原测试文件中补充或修改测试，不得遗忘既有覆盖，也不得为同一功能重复建立测试文件。
- 只有在现有测试文件均不适合承载新测试时，才新建对应的 `tests/test_*.py` 文件。
- 功能修改应同步更新相关测试，并运行对应测试；涉及公共执行、风控或 App bridge 行为时，还应运行完整 `python3 -m unittest discover -s tests`。
- 没有特殊要求时，测试应使用单元测试、mock、fixture 和项目脚本完成，不使用 Codex Computer Use 操作 App UI，也不以临时手工 UI 操作代替 `tests/` 中的可重复测试。

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

后台托管运行使用 label `com.aistock.tradingengine`。
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
