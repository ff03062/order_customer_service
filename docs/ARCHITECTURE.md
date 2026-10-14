# 智能订单客服 Agentic AI 系统 — 架构设计文档

> 版本：v2（新增售后决策子图 + 查询层状态共享）
> 适用代码：`app/agent/**`

---

## 一、整体架构

系统采用 **「主图编排 + 多子图分工 + 共享查询层」** 的三层结构：

- **主图（CustomerServiceState）**：唯一的事实源与编排层，负责意图路由、状态持久化、流式输出。
- **子图（OrderQueryState / AfterSalesState）**：各自独立的临时状态，负责一类完整业务决策。
- **共享查询层（Tool Layer）**：只读数据访问能力，被主图与子图共同复用，不与任何 State 耦合。

```
┌──────────────────────────────────────────────────────────────────────┐
│                         前端 (Vue2 + SSE)                             │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ HTTP / SSE
┌───────────────────────────────▼──────────────────────────────────────┐
│                    FastAPI  (app/main.py / app/router)                │
│  /login  /chat  /create_session  /chat/stream  /sendEmail  /checkCode │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────────┐
│                      CustomerServiceGraph（主图）                      │
│                                                                       │
│   START ──► intent ──► manager ──┬──► order      ──► END               │
│                                  ├──► after_sales ──► END              │
│                                  └──► chat       ──► END               │
│                                                                       │
│   State: CustomerServiceState   Checkpointer: AsyncPostgresSaver       │
│   Thread: thread_id = session_id                                       │
└───────┬─────────────────────┬──────────────────────┬─────────────────┘
        │                     │                      │
        ▼                     ▼                      ▼
┌───────────────┐   ┌──────────────────┐   ┌──────────────────┐
│ 订单查询子图   │   │  售后决策子图     │   │  闲聊节点 chat    │
│ OrderQueryState│  │ AfterSalesState  │   │  (纯 LLM 流式)    │
│ (Text2SQL)    │   │ (规则+LLM 决策)   │   │                  │
└───────┬───────┘   └────────┬─────────┘   └──────────────────┘
        │                    │
        │                    │
        └────────┬───────────┘
                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│                        共享查询层（Tool Layer）                        │
│                                                                       │
│  mysql_tool            只读 SQL 执行（禁写操作 / 禁 SELECT * /        │
│                        强制 user_id 占位符替换）                       │
│  order_facts_tool      订单事实聚合（参数化查询，0 次 LLM 调用）        │
│  policy_retrieval_tool 政策条款检索（向量 + BM25 + RRF 融合）          │
└───────────────────────────────┬──────────────────────────────────────┘
                                ▼
      ┌──────────────┬──────────────────┬─────────────┬─────────────┐
      ▼              ▼                  ▼             ▼             ▼
   MySQL         PostgreSQL           Redis       ChromaDB     政策文档
 (order_db)   (checkpoint/摘要)   (窗口/画像/锁) (长期记忆)  (policies/*.md)
```

---

## 二、主图：CustomerServiceState

**文件**：`app/agent/state/customer_state.py`

```python
messages        : Annotated[list[AnyMessage], operator.add]  # 追加型，跨轮累积
intent          : str      # order_query | after_sales | chat | unknown
detail          : str
current_order_id: str      # ★ 跨轮保留：上一轮解析出的订单号（订单/售后共用）
current_order_ids: list    # ★ 跨轮保留：累积出现的订单号
order_context   : str      # 上一轮在聊什么（用于指代消解）
sql / db_result / db_error / sql_attempts / retry_count
retrieved_policies : list
user_id / user_email
current_step    : str
```

### 状态通道语义（务必理解，这是"查询层状态共享"的基础）

LangGraph 中每个状态字段是一条**通道（channel）**，语义分两类：

| 类型 | 例子 | 更新语义 |
|---|---|---|
| **带 reducer** | `messages`（`operator.add`） | 新值 **追加** 到旧值之后 |
| **无 reducer（LastValue）** | `current_order_id`、`order_context` | 本轮输入**有值就覆盖**，**没值则保留 checkpoint 里的旧值** |

这带来一个关键结论：

> 主图每轮调用 `astream(user_msg, config)` 时，`user_msg` **只包含 messages/user_id/user_email**。
> 因此 `current_order_id` 不会被打回默认值，而是**从上一轮的 checkpoint 中自动恢复**。

于是"上一轮的订单号"在新一轮里天然可读 —— 不需要任何额外代码去手动搬运。**这就是"查询层状态共享"的机制。**

---

## 三、子图与状态隔离（重要概念澄清）

### 3.1 子图之间不共享状态

三个 State 是**完全独立**的 TypedDict：

| State | 归属 | 生命周期 |
|---|---|---|
| `CustomerServiceState` | 主图 | 跨轮（Checkpointer 持久化） |
| `OrderQueryState` | 订单子图 | 单次调用，调用结束即销毁 |
| `AfterSalesState` | 售后子图 | 单次调用，调用结束即销毁 |

**横向没有任何共享**：订单子图读不到售后子图的状态，反之亦然。

### 3.2 信息如何跨子图传递

只有三条通道：

```
        ┌─ ① 主图 state 的跨轮字段（current_order_id / order_context）
        │
信息流 ──┼─ ② 对话历史（messages，追加型通道）
        │
        └─ ③ 数据库（每次现查，不缓存状态）
```

**设计原则**：

> 子图的输出只回写主图 state 的**少量标量字段**；
> 子图需要的**业务事实**一律**从数据库现查**，不从别的子图的结果里"搬"。

原因：子图的自然语言输出（如"您的订单已发货"）无法作为决策输入，决策需要精确数值（如"签收后第 8 天"）。

### 3.3 当前实现方式：函数调用式，而非图嵌套

`order_node` / `after_sales_node` 是**主图的普通节点函数**，内部 `new` 出子图并 `invoke`：

```python
# app/agent/node/order_node.py
result = _get_subgraph().invoke({
    "user_input": ...,
    "user_id": ...,
    "chat_history": state["messages"][-6:],
    "last_order_id": state.get("current_order_id", ""),
})
```

**优点**：状态边界清晰、易调试、子图可单独 `python -m` 运行验证。
**注意**：子图通过模块级单例复用，**不可每次请求重新 compile**（会造成不必要开销）。

---

## 四、订单查询子图（OrderQueryState）

**目录**：`app/agent/node/order_query/`

```
START
  │
  ▼
query_rewrite_node     ← ★ 新增：结合 chat_history + last_order_id 消解省略指代
  │                      例："那它到哪了" → "订单 202609280001 的物流到哪了"
  ▼
intent_classify_node   → intent（6 类：query_orders / logistics / after_sales ...）
  ▼
schema_select_node     → 按意图裁剪 schema（当前为字典映射，后续可换图谱召回）
  ▼
sql_generate_node  ◄──────────────┐
  ▼                               │ db_error
sql_check_node                    │
  ├─ invalid_json/empty/         │
  │  dangerous/no_user_id ───────┘（重试，上限 3）
  ├─ exhausted ──► answer_translate_node
  └─ pass ──► sql_execute_node ───┤ db_error → 回到 sql_generate_node
                                  └─ 成功 ──► answer_translate_node ──► END
```

**本次修复的两个问题**：

1. **★ 回写实体**：原先 `current_order_id` 定义了但**从未被写入**。
   现在 `sql_execute_node` 从结果中提取 `order_id` → `resolved_order_ids`，
   `order_node` 回写 `current_order_id` / `current_order_ids` 到主图 state。
2. **★ 传入上下文**：原先子图只收 `user_input` + `user_id`，导致"那笔订单呢"必然查不到。
   现在额外传入 `chat_history`、`last_order_id`、`order_context`。

---

## 五、售后决策子图（AfterSalesState）

**目录**：`app/agent/node/after_sales/`

### 5.1 为什么售后要独立成子图

售后不是"查数据"，而是**多步决策**：
先判断"政策允不允许"，再判断"这一单能不能办"，冲突时还要权衡（如"拆封了但有质量问题"），
信息不足要反问，最后可能触发执行动作。**这是一条有分支的决策链，不是一次 LLM 调用能覆盖的。**

### 5.2 拓扑

```
                       START
                         │
                         ▼
                  intent_slot_node           ① 二级意图 + 槽位抽取（LLM + Pydantic）
                         │
          ┌──────────────┴───────────────┐
    缺必需槽位                       槽位齐全
          │                              │
          ▼                              ▼
    clarify_node                 order_facts_node      ② 事实聚合（参数化查询，0 次 LLM）
          │                       │            │
          ▼                  缺订单号/查库失败   成功
         END                       │            │
                                   ▼            ▼
                             clarify_node  policy_rules_node   ③ 政策检索 + 规则判定
                                                │        │
                                          冲突    │        │ 规则直接判定
                                                ▼        ▼
                                      llm_reasoning_node  │  ④ 灰区推理（仅冲突时进入）
                                                │        │
                                     需补充信息  │        │
                                                ▼        ▼
                                          clarify_node  solution_generate_node  ⑤ 生成方案
                                                              │        │
                                                     需执行    │        │ 仅告知
                                                              ▼        ▼
                                                   action_execute_node  END
                                                              │
                                                              ▼
                                                             END
```

### 5.3 决策分层（"规则引擎 + LLM" 的边界）

| 判定类型 | 由谁做 | 例子 |
|---|---|---|
| **确定性判定** | 规则表（程序） | 七天/十五天时效、特殊品类限制、退款金额、运费险 |
| **模糊判定** | LLM（仅冲突时） | 责任归属、质量问题优先级、拆封与完好的权衡 |
| **信息收集** | clarify_node | 是否拆封、损坏描述、退货原因 |

**核心设计**：规则命中失败时**不直接拒绝用户**，而是标记 `conflict_detected` 交给 LLM 推理。
规则负责"确定的对错"，LLM 负责"规则没覆盖的灰区"。

### 5.4 死循环防护

| 风险 | 防护 |
|---|---|
| 反问循环 | `clarify` 在单次调用中最多进入 **2** 次，超限直接生成兜底方案 |
| 规则 ↔ LLM 循环 | `llm_reasoning` **不允许**回到 `policy_rules`（图中无此边） |
| 子图不收敛 | 条件边全部为有限 DAG，无自环 |

---

## 六、核心数据流：一次"多轮追问"的完整链路

```
第 1 轮
  用户："我的订单 202609280001 到哪了"
  ① intent_node            → order_query
  ② order_node → 订单子图   → query_rewrite 不改写（无指代）
                            → SQL 生成/校验/执行（db_result 含 order_id）
                            → resolved_order_ids = ["202609280001"]
  ③ order_node 回写主图      → current_order_id = "202609280001"
  ④ Checkpointer 持久化      → Postgres（thread_id = session_id）

第 2 轮
  用户："那它什么时候能到"          ← 省略指代
  ① intent_node            → order_query（提示词已要求结合上下文判断）
  ② order_node 读取主图      → last_order_id = "202609280001"（来自 checkpoint）
  ③ query_rewrite_node      → "订单 202609280001 什么时候能到"
  ④ 后续节点拿到的是自包含问题 → 正常生成 SQL 并回答
```

**关键点**：`current_order_id` 的读写是这条链路成立的唯一前提。
之前缺失这一步，导致第 2 轮必然失败。

---

## 七、目录结构总览

```
app/
├── main.py                          FastAPI 入口 + lifespan
├── router/                           chat_router / login_router
├── html/                             login.html / chat.html
├── agent/
│   ├── graph/customer_graph.py       主图 + 会话锁 + 流式输出
│   ├── state/customer_state.py        CustomerServiceState
│   ├── schema/intent_schema.py        一级意图结构化输出
│   ├── model/my_model.py              模型单例（云端 / 本地双链路）
│   ├── memory/                        四层记忆（窗口/摘要/长期/画像）
│   └── node/
│       ├── intent_node.py             一级意图
│       ├── manager_node.py            路由中枢
│       ├── order_node.py              订单子图 wrapper（★ 已修）
│       ├── after_sales_node.py        售后子图 wrapper（★ 新增）
│       ├── policy_node.py             售后政策单节点（旧，保留）
│       ├── chat_node.py               闲聊 + 流式
│       ├── order_query/               订单查询子图
│       │   ├── graph/order_subgraph.py
│       │   ├── state/order_state.py
│       │   ├── schema/order_schema.py
│       │   └── node/
│       │       ├── query_rewrite_node.py      ★ 新增
│       │       ├── intent_classify_node.py
│       │       ├── schema_select_node.py
│       │       ├── sql_generate_node.py
│       │       ├── sql_check_node.py
│       │       ├── sql_execute_node.py        ★ 修改：提取 order_id
│       │       └── answer_translate_node.py
│       └── after_sales/                售后决策子图（★ 全部新增）
│           ├── graph/after_sales_subgraph.py
│           ├── state/after_sales_state.py
│           ├── schema/after_sales_schema.py
│           └── node/
│               ├── intent_slot_node.py
│               ├── order_facts_node.py
│               ├── policy_rules_node.py
│               ├── llm_reasoning_node.py
│               ├── solution_generate_node.py
│               ├── clarify_node.py
│               └── action_execute_node.py
└── tool/
    ├── mysql_tool.py                  只读 SQL 执行
    ├── order_facts_tool.py            ★ 新增：订单事实聚合
    └── policy_retrieval_tool.py       ★ 新增：条款级政策检索

data/policies/after_sales_policy.md    政策知识库（条款切分来源）
```

---

## 八、本次改造清单

| 文件 | 变更 | 说明 |
|---|---|---|
| `app/agent/state/customer_state.py` | 修改 | 新增 `current_order_ids`、`after_sales_result`；注明标量通道跨轮保留语义 |
| `app/agent/node/order_node.py` | 修改 | 传入 `chat_history`/`last_order_id`/`order_context`；回写 `current_order_id`；子图单例化 |
| `app/agent/node/order_query/state/order_state.py` | 修改 | 新增上下文与 `resolved_order_ids` 字段 |
| `app/agent/node/order_query/node/query_rewrite_node.py` | 新增 | 省略指代消解（无指代时不调用模型） |
| `app/agent/node/order_query/node/intent_classify_node.py` | 修改 | 优先使用 `rewritten_input` |
| `app/agent/node/order_query/node/sql_execute_node.py` | 修改 | 从查询结果回收 `order_id` |
| `app/agent/node/order_query/graph/order_subgraph.py` | 修改 | 入口接入 `query_rewrite_node` |
| `app/tool/mysql_tool.py` | 修改 | 抽出 `run_readonly_query`（只读校验 + user_id 替换）供内部复用 |
| `app/tool/order_facts_tool.py` | 新增 | 订单事实聚合（参数化查询，0 次 LLM） |
| `app/tool/policy_retrieval_tool.py` | 重写 | 政策改为从 md 切分成条款；保留向量 + BM25 + RRF |
| `app/agent/node/after_sales/**` | 新增 | 售后决策子图（7 节点 + state + schema） |
| `app/agent/node/after_sales_node.py` | 新增 | 售后子图 wrapper |
| `app/agent/node/manager_node.py` | 修改 | `after_sales` 路由到售后子图（原来是单节点 policy） |
| `app/agent/graph/customer_graph.py` | 修改 | 注册 `after_sales` 节点 + 异常兜底；注明状态通道共享机制 |

**未接入图但保留的文件**：`app/agent/node/policy_node.py`（旧的售后政策单节点）。
若要彻底清理，可在确认售后子图稳定后删除该文件与 `app/prompt/policy_node.yaml`。

---

## 九、当前未实现（后续路线）

| 项 | 状态 | 说明 |
|---|---|---|
| Neo4j Schema 图谱 | 未做 | 现为 `INTENT_TO_SCHEMA` 字典映射 |
| 多跳政策检索 | 未做 | 现为单跳（向量 + BM25 + RRF） |
| MCP 真实对接 | 桩实现 | `action_execute_node` 目前为本地桩，接口已按 MCP 形态预留 |
| 评测集与埋点 | 未做 | 指标需自行跑数据 |
| 记忆与子图打通 | 部分 | 长期记忆/画像尚未注入子图 |
| 二次确认 | 未做 | 写操作（退款/换货）目前由规则直接触发，生产环境需加用户确认环节 |

---

## 十、本地自测入口

每个新增模块都带 `__main__` 自测块，单文件即可运行验证：

```bash
# 政策条款切分与检索
python -m app.tool.policy_retrieval_tool

# 订单事实聚合（需要 MySQL 可用）
python -m app.tool.order_facts_tool

# 指代消解
python -m app.agent.node.order_query.node.query_rewrite_node

# 订单子图（含上下文注入）
python -m app.agent.node.order_node

# 售后子图（完整决策链路）
python -m app.agent.node.after_sales_node

# 生成售后子图拓扑图
python -m app.agent.node.after_sales.graph.after_sales_subgraph
```

