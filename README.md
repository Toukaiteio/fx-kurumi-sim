# FX 战士久留美 模拟器 — 部署与运行手册

> **读者是 AI Agent，不是人。** 本文档以「可执行、可验证」为准：
> 每一步都给出命令和**预期输出**。不要凭记忆推断行为，一律以命令输出为准。
> 凡标注 `⛔ 禁止` 的条目是硬约束，违反即视为部署失败。

---

## 0. 这是什么 / 边界

用**虚拟本金**按**真实行情**涨跌模拟 USD/JPY 交易的沙盒，供 AI 做交易决策、风控与报告。

| 项 | 值 |
|---|---|
| 交易品种 | USD/JPY |
| 初始本金 | 50000 日元（虚拟） |
| 时区 | Asia/Tokyo（固定 +09:00，无夏令时） |
| 金额类型 | `decimal.Decimal`，数据库/JSON 里一律十进制**字符串** |
| 状态存储 | SQLite，单文件 |

**硬边界（不可协商）：**

- `⛔ 禁止` 连接真实券商下单、执行真实资金转账、请求任何真实支付接口。
- `⛔ 禁止` 用模型记忆或随机数生成行情；行情必须来自实际数据源。
- `⛔ 禁止` 让 AI 指定成交后的余额或盈亏；余额只能由账本变更推导。
- `⛔ 禁止` 用调试/测试/初始化命令绕过资金授权或风控校验。
- `⛔ 禁止` 把「运行中的 Python 循环」说成「已具备后台持久能力」，除非实际验证过。
- `⛔ 禁止` 伪报测试结果。影响账户正确性/强制平仓的关键测试未过时，必须保持账户**禁止开仓**。

---

## 1. 环境前提

| 项 | 要求 | 验证命令 | 预期 |
|---|---|---|---|
| Python | ≥ 3.9（开发机实测 3.13） | `python -V` | 版本号 |
| 第三方运行时依赖 | **无**（纯标准库） | 跑第 3 节冒烟 | 不报 `ModuleNotFoundError` |
| 可选 | `tzdata`（仅当系统缺 tz 库） | `python -c "from zoneinfo import ZoneInfo;ZoneInfo('Asia/Tokyo');print('ok')"` | `ok` |
| 网络 | 出网访问行情源与 LLM 端点 | 见 3.4 | — |

运行依赖清单在 `requirements.txt`，**只有可选项，正常部署不需要 `pip install`**。

---

## 2. 目录与文件（部署后应完全一致）

```
<repo>/
├── config.json            配置（唯一规则来源）— 必须存在
├── config.py              配置加载与严格校验
├── market_data.py         行情：4 源链 + 按小时聚合
├── storage.py             SQLite 持久化（账本 + 审计哈希链）
├── engine.py              交易执行与盈亏核心（唯一）
├── risk_monitor.py        风控监控与保护性平仓
├── reporting.py           报告数据包/快照/模板（严格只读）
├── scheduler.py           调度：监控 + 报告任务 + 每分钟 AI 决策
├── llm_client.py          OpenAI 兼容客户端（function calling，只做传输）
├── agent.py               AI 决策循环 + 记忆
├── memory.py              记忆的文件投影（总索引 + 按天索引 + 每条记忆一个文件）
├── main.py                CLI 入口（python -m main）
├── version.py             版本号
├── data/                  运行时生成（已 gitignore）
│   ├── fxsim.sqlite3      数据库
│   └── backups/           备份
├── reports/               报告输出（已 gitignore）
├── memory/                记忆文件投影（已 gitignore，可重建）
└── tests/                 自动化测试（918 个）
```

运行时目录 `data/`、`reports/`、`memory/` **首次运行自动创建**，不需要手工 mkdir。

---

## 3. 部署步骤（按顺序执行，每步验证）

### 3.1 校验配置

```bash
python -m config
```

**预期**：打印时区、初始本金、交易品种、杠杆/保证金/风险限额，退出码 `0`。
配置有未知字段、缺失字段、超范围值时**会直接报错并指出字段名**（严格 schema）。

### 3.2 运行测试（不碰数据库）

```bash
python -m main test
# 等价：python -m unittest discover -s tests -t .
```

**预期**：`Ran 918 tests ... OK (skipped=5)`。5 个跳过项是真实行情集成测试，需显式开启：
`KURUMI_LIVE_TESTS=1 python -m unittest tests.test_market_data_live -v`

测试使用临时数据库，**不会**写入 `data/fxsim.sqlite3`，**默认不联网**（唯一例外见第 8 节）。

### 3.3 初始化账户

```bash
python -m main init
python -m main status --no-quote
```

**预期**：`status` 显示 `现金余额 50000`、`账户状态 ACTIVE`、`当前持仓 无`。
`init` 是幂等的——重复执行不会重复发放本金（账本用 `event_key` 唯一约束保证）。

### 3.4 验证行情可用性

```bash
python -m market_data
```

**预期**：打印当前 USD/JPY 报价与来源、以及 1 分钟 K 线可用性。
**注意**：免密钥数据源只提供指示性中间价，买卖价按配置点差合成，标记为 `synthetic_spread`。
若整个链不可用会报 `NoProviderAvailableError`——这是**如实报告**，不是 bug。

### 3.5 打开验收闸门

```bash
python -m main gate
```

**预期**：`结果 : 16/16 通过` + `闸门通过：正式模拟账户允许开仓`。

**闸门机制**：它跑 16 项「账户正确性 / 强制平仓」关键测试，结果写进 `meta.acceptance_gate`
（`PASSED` 或 `BLOCKED:<原因>`）。引擎在**开仓校验**和**盯市**两处**直接读这个 meta 键**。
未通过时账户保持禁止开仓，`status` 会显示阻断原因。

```bash
python -m main gate && echo "GATE PASSED" || echo "GATE BLOCKED — 不得开仓"
```

---

## 4. 三种运行模式

### 4.1 人工单步（调试用）

```bash
python -m main quote                      # 看行情
python -m main open-long --volume 1000 --stop 157.950 --take-profit 158.100 --reason "突破回踩"
python -m main monitor-once               # 跑一次风控（可能触发保护性平仓）
python -m main close --reason manual      # 平仓
python -m main report-data --kind evening # 报告数据包
python -m main verify                     # 核对账户与审计链
```

开仓/平仓**必须**经 `engine` 完整校验，命令行无法绕过。

### 4.2 调度器（推荐）

```bash
python -m scheduler capabilities   # 先看本机实际支持哪些后台能力（实测结论）
python -m scheduler tick           # 执行一次（幂等，可被外部定时器每分钟调用）
python -m scheduler start          # 分离进程常驻
python -m scheduler status         # 查看运行状态
python -m scheduler stop           # 优雅停止
python -m scheduler install-task --apply   # 交给 Windows 任务计划程序
```

`tick` 是**幂等**的：到点才监控、到点才建报告任务、到点才决策。重复调用不产生重复任务或重复交易。

### 4.3 AI 决策

```bash
python -m llm_client                    # 自检：打印配置 + 发一次最小 function calling 请求
python -m main decide                   # 默认 dry-run：只校验不落单
python -m main decide --live --decision-id "2026-10-11-001"   # 真开仓
python -m main decide --show-context    # 打印真正发出的两段上下文
```

调度器默认**每分钟问一次模型**，策略是「**失败就等下一个间隔**」——不重试、不退避。
`--no-decide` 关闭决策；`--decision-interval 60` 调间隔；`--live-decisions` 真下单（默认 dry-run）。

---

## 5. 配置（`config.json`）

**配置是唯一规则来源。** 其他模块（含提示词）都从它读取，源码里不另写一份。

```jsonc
{
  "version": 1,
  "timezone": "Asia/Tokyo",
  "account":    { "initial_capital_jpy": "50000", "currency": "JPY" },
  "instrument": { "symbol": "USD/JPY", "min_trade_unit": "1000" },
  "risk": {
    "max_leverage": "25",
    "max_margin_usage_ratio": "0.60",
    "max_risk_per_trade_ratio": "0.02",
    "max_total_risk_ratio": "0.05",
    "daily_drawdown_stop_ratio": "0.03"
  },
  "margin": {
    "margin_call_level": "1.50", "stop_out_level": "1.00",
    "min_equity_to_open_jpy": "5000", "bankruptcy_equity_jpy": "0"
  },
  "market": { "monitor_interval_seconds": 60, "spread_jpy": "0.01", "slippage_jpy": "0.005" },
  "llm": {
    "base_url": "https://api.openai.com/v1",
    "api_key": "",          // 留空，用环境变量
    "model_id": "",         // 留空则跳过 AI 决策（不报错）
    "temperature": "0.2", "timeout_seconds": 120, "max_tokens": 4096, "max_retries": 2
  },
  "paths": { "database_filename": "fxsim.sqlite3", "backup_dirname": "backups" }
}
```

**校验规则**：未知字段、缺失字段、类型不符、超范围 → `ConfigError`，并指出字段名。
跨字段关系（如 `stop_out_level ≤ margin_call_level`）由专门的一致性检查负责。

### LLM 凭据（只能走环境变量或本地覆盖文件）

```bash
export KURUMI_LLM_API_KEY="..."      # 优先级更高
export OPENAI_API_KEY="..."          # 兼容
export KURUMI_LLM_BASE_URL="https://.../v1"
export KURUMI_LLM_MODEL_ID="..."
```

或写入 `config.local.json`（已被 gitignore）。`⛔ 禁止` 把密钥写进 `config.json` 或源码。

优先级：`KURUMI_LLM_*` > `OPENAI_*` > `config.local.json` > `config.json`。
`api_key` 或 `model_id` 为空 → `llm.ready == False` → AI 决策**自动跳过并如实说明原因**。

---

## 6. 数据布局与权威来源

**权威来源只有 SQLite。** 文件是投影。

| 表 | 内容 |
|---|---|
| `meta` | schema 版本、验收闸门状态 |
| `account` / `account_state` | 账户与状态投影（含 `revision` 乐观锁） |
| `cash_flows` | 现金流水（**余额的权威来源**） |
| `positions` / `trades` | 持仓与成交 |
| `quotes` | 行情快照 |
| `audit_events` | **追加式审计哈希链** |
| `daily_equity_baseline` | 每日基准（08:00 JST 分界） |
| `scheduler_runs` | 调度运行记录 |
| `agent_notes` | AI 笔记 + 系统日记忆 |
| `agent_turns` | 逐小时对话存档（重放用，存**当时真正发出的完整消息列表**） |
| `hour_summaries` | 小时压缩摘要 |

**关键不变量：**

1. `cash_balance == SUM(cash_flows)`。`save_account_state` 会拒绝不一致的写入。
2. **绝不用 SQL `SUM()`** 算金额——SQLite 会转 float。一律在 Python 侧用 `Decimal` 累加。
3. 审计链：`sha256(prev_hash|seq|event_type|occurred_at|canonical_json)`，GENESIS = 64 个 `0`。
4. **纯新增表不递增 `SCHEMA_VERSION`**（旧库自动建空表，数据不动）；破坏性改列才递增。

### 记忆文件投影（`memory/`）

```
memory/
├── INDEX.md                      # 总索引：所有天
├── index/<YYYY-MM-DD>.md         # 当天索引
├── notes/<天>/<编号>-<类别>-<slug>.md
└── hours/<天>/<HH>.md
```

**可随时重建**，删掉整个目录不丢数据：

```bash
python -m main memory --rebuild
python -m main notes --kind LESSON     # 读数据库
```

---

## 7. 上下文与前缀缓存（改提示词前必读）

每次决策的消息分三段，顺序固定：

| 段 | 内容 | 缓存 |
|---|---|---|
| **稳定前缀**：`tools` + `system` | 角色、规则、风控限额、成本模型 | 进程内**逐字节不变** → 命中 |
| **本小时对话历史** | 之前几轮的 `user/assistant/tool` | **只追加不改写** → 命中 |
| **易变尾部**：新 `user` | 时刻、行情、账户、持仓、价格区间、小时记忆、笔记 | 不命中 |

**实测命中率**（`cached_tokens / prompt_tokens`）：

| 场景 | 命中率 |
|---|---|
| 同一请求连发 10 次（前缀 1412 token） | 10/10 都是 100%（首次 0%） |
| 前缀递增（第 N+1 次前缀 = 第 N 次完整消息） | 89% / 96% / 95% |
| 真实 agent 同一小时内连续 5 次决策 | 0% → 65% → 75% → 79% → **84%** |

**`⛔ 改 `system` 提示词或工具描述等于让前缀缓存整体失效。** 改动要有意识。
稳定前缀的单一来源是 `agent.build_static_context()`；提示词数字全部由 `config.json` 生成。

看缓存命中：`python -m llm_client` 会打印命中率；`main decide` 的 `usage` 里带 `cached_tokens`。
注意 `usage` 是**多轮累加**值。响应头 `EO-Cache-Status` 是 CDN 对 HTTP 响应的缓存，与 KV 前缀缓存无关。

---

## 8. 失败模式 → 含义 → 处置

| 现象 | 含义 | 处置 |
|---|---|---|
| `ConfigError: <字段>` | 配置非法 | 按提示修 `config.json`；**不要**绕过校验 |
| `[存储错误] disk I/O error` | 磁盘满/不可写 | 腾空间；**不要**删库重建。用 `verify` 确认数据完好 |
| `SchemaVersionError` | 库非本模块创建或版本不符 | **不要**就地覆盖；换路径或人工确认 |
| `NoProviderAvailableError` | 行情源全挂 | 正常降级：`price_history.available=false`，模型照常决策 |
| `HistoryUnavailableError` | 历史窗口超出源能力（Yahoo 1m 只回溯约 9 天） | 缩短窗口；**不得**伪造历史 |
| `LLMConfigError` | 缺 `api_key`/`model_id` | 设环境变量；或接受「跳过 AI 决策」 |
| `LLMEmptyCompletionError` | 推理 token 吃满 `max_tokens` | 调大 `llm.max_tokens`；**不要**当成「模型没意见」 |
| `LLMAuthError` / `LLMRequestError` | 4xx，重试无意义 | 修凭据/请求，别重试 |
| `账户尚未初始化` | 未 `init` | `python -m main init` |
| 闸门 `BLOCKED:<原因>` | 关键测试未过 | **保持禁止开仓**，修好再跑 `gate` |
| `status` 显示「无法核实」 | 缺行情 | 正常；**不要**把 `null` 写成 `0` |

**唯一会走真实网络的测试**：`tests/test_scheduler.py::TestProcessManagement` 会启动真实分离
进程，其监控器取一次真实报价，耗时 2–40 秒（超时给到 120 秒）。其余测试离线。

---

## 9. 已知限制（部署前必须知道，不要当成 bug 去「修」）

1. **Yahoo 1 分钟历史只回溯约 9 天**；更早的缺口如实记为数据缺口。
2. **免密钥源没有真实 Bid/Ask**，买卖价按配置点差合成，属**模拟报价**。
3. **Alpha Vantage 的 `Last Refreshed` 是接口刷新时刻**，不是市场成交时刻。
4. **「保证金水平 ≤150% 预警」在当前配置下无法由行情自然触发**（单笔风险上限 2% 与当日 3% 回撤会更早触发）。分支已实现，需调配置才可能触发。
5. **开仓不是单一数据库事务**（建仓/保证金投影/净值投影分三次）。中断会留下不一致，可由 `verify` 发现。
6. **审计链防不住「整链重写」**——需把链头哈希锚定到库外。
7. **价格历史是降采样的小时区间**，无分钟细节、无成交量、无技术指标。
8. **同一小时内的历史重放会撑大请求**（第 N 次含前 N−1 轮）。这是为命中缓存的**故意**设计；不支持缓存的端点上是纯增成本。兜底是「一小时最多 60 次」。
9. **缓存命中率不稳定**：首次总是 0%（冷启动），且受端点侧负载/驱逐影响，不是可依赖常数。
10. **笔记是自我影响回路**：模型自己写、自己读。上下文有免责声明但无法消除被旧措辞带偏的风险。
11. **小时压缩是确定性的**，只归并「几次决策/什么结果/什么理由」，不做行情判断。
12. **路由器可能忽略强制 `tool_choice`**，工具选择只能靠提示词引导。
13. **推理型模型很慢**（单次决策约 1900 completion token，其中约 1800 是推理），实测数十秒。

---

## 10. 验收标准（部署成功的判定）

全部满足才算部署完成：

```bash
python -m config                       # 退出码 0
python -m main test                    # Ran 918 tests ... OK (skipped=5)
python -m main init                    # 幂等，余额仍为 50000
python -m main status --no-quote       # ACTIVE / 余额 50000 / 持仓 无
python -m main verify                  # 整体 : OK（审计链、账本、账户核对、引擎核对）
python -m main gate                    # 16/16 通过
python -m main memory                  # 记忆目录与计数可读
```

任一项失败 → **不得开仓**，按第 8 节处置。

---

## 11. 免责声明

趣味性**模拟**项目，仅用于让 AI 在虚拟环境中练习交易决策。
不构成投资建议，不能用于真实交易。本项目不连接任何真实券商，不执行任何真实资金转账。
