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
└── tests/                 自动化测试（1004 个）
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

**预期**：`Ran 1004 tests ... OK (skipped=5)`。5 个跳过项是真实行情集成测试，需显式开启：
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
python -m main goal                       # 结算/查看目标与奖励（--dry-run 只预演）
python -m main pressure                   # 结算/查看督促机制（默认关闭）
python -m main notes                      # 看 AI 自己写的笔记
python -m main memory                     # 看记忆文件投影（--rebuild 重建）
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
    "max_leverage": "25",              // 日本金融厅对个人 FX 的法定上限，不要动
    "max_margin_usage_ratio": "0.90",  // 大幅放开后，25x 才真的可达
    "max_risk_per_trade_ratio": "0.15",// 单笔风险；决定止损能放多宽
    "max_total_risk_ratio": "0.60",
    "daily_drawdown_stop_ratio": "0.40",
    "min_reward_risk_ratio": "1.0"     // 盈亏比下限（原先硬编码在引擎里）
  },
  "margin": {
    "margin_call_level": "1.50", "stop_out_level": "1.00",
    "min_equity_to_open_jpy": "1000", "bankruptcy_equity_jpy": "0"
  },
  "market": { "monitor_interval_seconds": 60, "spread_jpy": "0.005", "slippage_jpy": "0.003" },
  "goal": {                  // 目标 + 奖励：贪婪的正向出口
    "enabled": true,
    "target_growth_ratio": "0.20",   // 下一档目标 = 达成时净值 × (1+本比例)
    "reward_jpy": "5000",            // 达成发放的奖励（外部注入）
    "reward_growth_ratio": "0.50"    // 每档奖励递增比例
  },
  "pressure": {              // 督促机制：按时间扣本金（默认关闭，见 5c 节）
    "enabled": false,
    "interval_hours": 6,     // 1–24
    "deduction_jpy": "100",
    "catch_up_missed_slots": true
  },
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

## 5b. 督促机制（`pressure`）—— 默认关闭

> **默认 `enabled: false`。** 这不是成本模型，是一个**游戏旋钮**。
> 真实的资金时间成本量级是每年 2–3%（折到 6 小时约 0.04 日元），作为机制没有意义；
> 而 3000/6h（24%/天）会盖过一切，让人分不清「是交易亏的」还是「被扣的」。
> 现在「驱动 AI 行动」这件事由 5c 节的**目标 + 奖励**（正向激励）负责，
> 不需要这个按时间扣钱的机制。要当倒计时玩请先重新标定数值。

**机制**：每隔固定小时数**无条件**从本金扣一笔钱，与行情、持仓、盈亏完全无关。
空仓观望不会让它停下来。

```jsonc
"pressure": {
  "enabled": true,               // 总开关
  "interval_hours": 6,           // 1–24；槽位按自然小时对齐（6 → 00/06/12/18 JST）
  "deduction_jpy": "3000",
  "catch_up_missed_slots": true  // 停机期间漏掉的槽位是否补扣
}
```

```bash
python -m main pressure              # 结算并查看（幂等，可随时手动跑）
python -m main pressure --dry-run    # 只预演，不扣款、不写任何状态
```

### 必须知道的四条行为

1. **同槽位只扣一次。** 幂等键 `CAPITAL_DECAY:<槽位>`，靠 `cash_flows.event_key`
   的 UNIQUE 约束保证。重复执行、进程重启、调度器每分钟 tick 都不会重复扣。
2. **不追溯生效。** 起算点记在 `meta.capital_pressure_anchor`，**第一次调用时才写入**。
   对一个已经跑了很久的账户启用它，不会立刻补扣过去几天的账；首次扣款落在
   起算点之后的第一个槽位边界。
3. **`catch_up_missed_slots`**：`true` = 停机期间漏掉的槽位全部补扣（停机不能免罚）；
   `false` = 只扣最近一个到期槽位，漏掉的免掉。
4. **扣款不超过可用现金**，余额不会被扣成负数。因此实际扣款额可能小于配置值，
   `pressure` 命令输出的 `charged_jpy` 是**实际**扣掉的金额。

### ⚠️ 关键设计：扣款**不计入**当日回撤

这是这个机制最容易写错的地方，写错会让它起**反作用**。

3000 / 50000 = 6%，而 `risk.daily_drawdown_stop_ratio` 是 3%。如果把扣款计入回撤，
**第一次扣款就会越过 3% 阈值**，引擎随即按「当日回撤停止」把账户按成禁止开仓——
AI 从「过度保守」变成「根本不能交易」，与机制的目的完全相反。

所以 `_evaluate_halts` 与 `mark_to_market` 的回撤口径都使用
`engine.drawdown_basis_equity()`：把当日累计的督促扣款**加回**净值后再算回撤。
口径是「回撤只反映交易表现，不反映固定游戏机制」。

**但爆仓与最低净值两项仍用真实净值**——钱是真的少了。实测（`tests/test_pressure.py`）：

| 场景 | 回撤口径 | 是否停交易 |
|---|---|---|
| 只扣款 3000（本金 6%） | 0% | **不停** ✓ |
| 扣款 + 真实亏损 1200（2.4%） | 2.4% | 不停 ✓ |
| 扣款 + 真实亏损 1500（3%） | 3.0% | **停** ✓（真实亏损照常执行） |
| 真实亏损把净值打到 0 | — | **爆仓** ✓ |

### 账本口径

扣款记为一笔 `FlowType.CAPITAL_DECAY` 资金流水（不是直接改余额——账本是余额的
唯一权威来源，直接改会被 `save_account_state` 拒绝）。因此：

- **净值与累计净利润都会下降**——这是督促机制对 AI 的**唯一说服力来源**，
  它必须在 `main report-data` 的 `cumulative_trading_cost.capital_pressure_jpy`
  里单独可见（不与手续费/掉期混在一起）。
- **不参与已实现盈亏口径**（`reconcile_account` 的 P&L 比对只看 `REALIZED_PNL`）。
- **`engine.reconcile_account` 的账本推导公式必须包含它**——新增流水类型时容易漏，
  漏了会被核对器当场抓出来（差额不为 0）。

### AI 必须能看见它

机制只有**被 AI 看见**才会改变行为，所以它同时出现在三处：

| 位置 | 内容 | 段 |
|---|---|---|
| system 提示词「规则 9」 | 「每 N 小时无条件扣 X 日元，空仓也照扣，不计入当日回撤」 | 稳定前缀 |
| `context.capital_pressure`（静态 JSON） | `enabled/interval_hours/deduction_jpy` + 说明 | 稳定前缀 |
| `context.capital_pressure`（实时） | `charged_total_jpy`、`next_charge_at`、`next_charge_jpy`、`charged_count` | 易变尾部 |

⚠️ 改这些文字会让**前缀缓存失效**（见第 7 节）。机制关闭时静态 JSON 只留
`{"enabled": false}`，**不写说明文字**——否则模型会读到一段描述「没在运行的东西」
的文字，属于误导。

---

## 5c. 目标 + 奖励（`goal`）

**这是本项目的核心驱动力，也是「贪婪」的出口。**

```jsonc
"goal": {
  "enabled": true,
  "target_growth_ratio": "0.20",  // 下一档目标 = 达成时净值 × (1+本比例)
  "reward_jpy": "5000",           // 达成发放的奖励（外部注入）
  "reward_growth_ratio": "0.50"   // 每档奖励递增
}
```

```bash
python -m main goal                # 结算并查看进度
python -m main goal --dry-run      # 只预演，不发奖励、不推进档位
```

**机制**：净值 ≥ 当前目标 → 立刻发放奖励金 → 档位 +1 → 目标抬高一档。

- **首次结算时按「当时的净值」建立第一档目标**（`×(1+target_growth_ratio)`），
  所以目标永远是从「现在」出发的一个可达增量，而不是陈旧的绝对值。
- **速度激励是内生的**：目标只在达成后才抬高，所以慢了只是**推迟**拿到奖励、
  推迟开始追下一档。**没有任何惩罚**——没达成只是拿不到钱。
- **奖励是外部注入**：`GOAL_REWARD` 流水，**不计入交易盈亏**（见下），
  但会立刻变成可动用的本金。档位越高奖励越大。

### ⚠️ 关键设计：奖励不能连锁刷取

新目标以 **(达成时净值 + 奖励) × (1+增长比例)** 为基准，而不是拿旧目标递推。

如果按旧目标递推（例如每次 ×1.2），奖励金本身会把净值推高，可能**一笔奖励就够跨过
下一档**，于是奖励可以自我兑现——那就成了印钱。

以「达成时净值（含奖励）」为基准，则新目标永远严格高于当前位置，
**奖励无法自我兑现**。实测：发完奖励后立刻用新净值再结算，`achieved` 必为 `False`。
（`tests/test_goal.py::TestGoalAchievement::test_cannot_chain_rewards_immediately`）

### 会计口径：奖励不是交易盈利

```
累计净利润 = 净值 + 出金 − 初始本金 − 入金 − 目标奖励
```

奖励是外部注入的钱，不是交易赚的。如果不剔除，**「达成目标」会被记成投资收益，
交易表现被美化**。所以：

- `account.goal_reward_jpy` 单独列出，报告模板里也单独一行；
- `net_profit` 已剔除奖励；
- `return_pct` 的分母仍是「初始本金 + 入金」，即**自有资金的收益率**（奖品不算本金）。

### AI 必须能看见它

| 位置 | 内容 | 段 |
|---|---|---|
| system 提示词「目标与奖励」段 | 目标、奖励额、「越快越好」、「不要孤注一掷」 | 稳定前缀 |
| `context.goal`（静态） | `target_growth_ratio` / `reward_jpy` / `reward_growth_ratio` + 说明 | 稳定前缀 |
| `context.goal`（实时） | `level`、`target_jpy`、`gap_jpy`、`progress_ratio`、`elapsed_hours`、`reward_on_success_jpy`、`achieved_count`、`total_reward_jpy` | 易变尾部 |

---

## 5d. 「贪婪但理性」的人设

AI 的性格由 system 提示词定义，**所有数字都由 `config.json` 生成**：

- **贪婪**：目标是让净值尽可能快地复利增长。空仓不是「安全」，而是**放弃收益的
  机会成本**。看到机会就上。
- **理性**：贪婪不等于莽撞。只在**有理由**时下注；下注规模要让你在正常的连续亏损后
  仍然活着；不报复性加仓；不因为「手痒」而下单。

「理性」这一半必须给足**算术依据**，否则模型只能靠感觉下注。所以 `static_context.economics`
里直接摆出（全部由配置算出，属于稳定前缀、命中缓存）：

| 字段 | 含义 |
|---|---|
| `reward_risk_ratio` | 当前盈亏比下限 |
| `break_even_win_rate` | 该盈亏比下光靠胜率打平需要多少胜率（`1/(1+R)`） |
| `risk_budget_jpy` | 单笔风险预算（日元） |
| `equity_after_3/5/10_losses_pct` | 连亏 3/5/10 笔后还剩多少净值 |
| `spread_jpy` / `slippage_jpy` | 每笔固定摩擦成本 |

提示词里也直接写出：「下注前先问自己：这个仓位连亏 5 次我还活着吗？」
以及「如果你对方向**没有理由**，那这笔的期望是负的（要白付点差）——
这时 hold 才是理性的选择，不是怯懦」。

### 为什么风险参数放得这么开

原始规格是「单笔风险 2% / 保证金 60% / 日回撤 3%」。在这套参数下：

- 单笔风险 2% 且最小单位 1000 美元 → **止损距离被卡在 ≤1.00 日元（价格的 0.63%）**；
- 随机游走下，任何止损/止盈结构的期望**恰好为 0**，唯一能改变它的是每笔固定的点差；
- 于是点差吃掉风险预算的 **1.01%**，**每一笔都是负期望**。

一个推理模型能算出这个，于是它的最优选择就是**不交易**——那不是「过度保守」，
是**正确地拒绝了一个负期望的游戏**。结果就是「要么什么都不发生，要么一路归零」，
两种都无聊。

放开之后（实测）：1000 美元仓位的止损可以放到 **2.50 日元（1.58%）**——
真实波段级别，而不是噪音级；点差占风险预算从 **1.01% 降到 0.20%**；
最大仓位 4000 → 5000 美元，可达实际杠杆 **15.8x**。

另外把价格历史从 12 小时延长到 **48 小时**（`agent.DEFAULT_HISTORY_HOURS`）：
看不到趋势就没法「理性地贪婪」。

**但要说清楚：放开风险不会让它变「会赚」，只会让它更能表达观点。**
如果它没有优势，结果就是输得更快更戏剧化——但那至少是一个有结果的游戏。

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
| （`cash_flows` 内） | `CAPITAL_DECAY` = 督促机制扣款；`GOAL_REWARD` = 目标奖励 |
| （`meta` 内） | `goal_state` = 当前目标档位/目标值/起算时刻 |
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
| `pressure` 显示「未到下一个扣款槽位」 | 正常（未到点） | 无需处理；用 `next_charge_at` 看下次时间 |
| 扣款额小于配置值 | 现金不足，已按可用余额封顶 | 正常；**不要**去改余额补齐 |
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
13. **督促机制默认关闭，且不建议按原数值打开**：3000/6h 对 50000 本金是约 24%/天，
    连续空仓约 4 天净值归零 → 触发爆仓。它的问题是**改错了变量**——
    「不交易就罚款」不创造优势，只强迫 AI 去接期望仍为负的交易；
    而且 24%/天 与「单笔风险 2%」互相矛盾（要跑赢它需要每天几十笔盈利交易，
    而规则只允许同时持一仓），数学上不可能。要惩罚性机制请先读 5b 节的说明。
14. **目标奖励是外部注入，不是交易盈利**：`net_profit` 已剔除它，但
    **净值会因奖励上升**——所以「净值增长」和「交易表现」是两个不同的数，
    报告里分开列，不要混着看。
15. **目标档位与奖励会随达成次数增长**：`reward_growth_ratio=0.5` 意味着第 10 档奖励
    是首档的约 38 倍。长期跑下去奖励会远超本金，届时「交易表现」在净值里的占比会被稀释。
    这是刻意的（越来越贪），但要清楚它意味着什么。
14. **督促扣款不计入当日回撤**（刻意设计，理由见第 5b 节）。这意味着
    「净值下降」与「回撤报警」在报告里可能不一致——报告会分别列出，不会混为一谈。
15. **推理型模型很慢**（单次决策约 1900 completion token，其中约 1800 是推理），实测数十秒。

---

## 10. 验收标准（部署成功的判定）

全部满足才算部署完成：

```bash
python -m config                       # 退出码 0
python -m main test                    # Ran 1004 tests ... OK (skipped=5)
python -m main init                    # 幂等，余额仍为 50000
python -m main status --no-quote       # ACTIVE / 余额 50000 / 持仓 无
python -m main verify                  # 整体 : OK（审计链、账本、账户核对、引擎核对）
python -m main gate                    # 16/16 通过
python -m main memory                  # 记忆目录与计数可读
python -m main goal --dry-run          # 目标机制可读（预演不发奖励）
python -m main pressure --dry-run      # 督促机制可读（默认关闭）
```

任一项失败 → **不得开仓**，按第 8 节处置。

---

## 11. 免责声明

趣味性**模拟**项目，仅用于让 AI 在虚拟环境中练习交易决策。
不构成投资建议，不能用于真实交易。本项目不连接任何真实券商，不执行任何真实资金转账。
