# 函数说明速查表

> 用途：快速掌握各函数职责。★ = 本次改造新增或修改过，需重点理解。
> 阅读顺序建议：先看「零、核心概念」，再按「一 → 四」的调用顺序看。

---

## 零、先记住这 6 个核心概念

| 概念 | 含义 | 为什么重要 |
|---|---|---|
| **主图 state** | `CustomerServiceState`，唯一事实源，跨轮存在 Postgres | 子图之间不共享状态，都从它取输入、往里回写 |
| **标量通道** | `current_order_id` 等无 reducer 的字段 | 本轮输入没传时**自动保留上一轮的值** → 追问能成立 |
| **事实（facts）** | 数据库查出来的客观数据（签收天数、金额） | 不能靠用户口述，必须查库 |
| **槽位（slots）** | 用户口述的信息（退货原因、是否拆封） | 数据库没有的，才需要问用户 |
| **签收门** | `is_signed`，退货时效的第一道门 | 未签收 → 随时可退；已签收 → 才计算 7/15 天 |
| **硬规则 vs 信息型规则** | `informational=False/True` | 信息型规则（如"无运费险"）的 fail **不构成拒绝理由** |

---

## 一、工具层（app/tool/）

### mysql_tool.py —— 唯一的数据访问出口（只读）

| 函数 | 一句话职责 | 关键点 |
|---|---|---|
| `db_kwargs(db_name)` | 按 pymysql 版本返回 `database=` 或 `db=` | 避免新版弃用告警 |
| `run_readonly_query(sql, user_id)` | 执行只读查询，返回 JSON 字符串 | **三道安全校验**：必须 SELECT/WITH 开头、禁写关键字、替换 `{user_id}`；返回非 JSON 即代表失败 |
| `mysql_tool` | LangChain Tool 封装 | 给 Agent 自主调用（Text2SQL 场景），内部就是调 `run_readonly_query` |
| `to_datetime(value)` | 把 DB 时间字段统一成 datetime | 兼容 datetime / date / 字符串三种形态 |

### order_facts_tool.py —— 售后事实聚合（0 次 LLM）★

**先看这几个「探测」函数，它们是容错的核心：**

| 函数 | 一句话职责 |
|---|---|
| `_parse_rows(raw)` | JSON 字符串 → list[dict]，失败返回 None（`None` = 查询失败，`[]` = 查到了但没数据） |
| `_table_columns(table)` | 查 information_schema 拿真实列名，进程内缓存 |
| `_list_tables()` | 列出当前库所有表名，缓存 |
| `_resolve_table(logical)` | 按候选表名（order_items→order_detail…）找真实存在的表 |
| `_pick(candidates, available)` | 精确匹配挑列名 |
| `_pick_fuzzy(candidates, available)` | 三层匹配：精确 → 列名⊂候选 → 候选⊂列名（取最短）。★顺序不能反，否则 `status` 会撞上 `order_status` |
| `_filter_clause(order_id)` | 按 order_id 实际类型（BIGINT/VARCHAR）生成等值条件 |
| `_columns_with_type(table)` | 拿列名+类型（判断 order_id 是数字还是字符串） |

**主函数：**

| 函数 | 一句话职责 | 返回的关键字段 |
|---|---|---|
| ★`get_order_facts(order_id, user_id)` | **售后事实总入口**：一次聚合订单主表 + 签收状态 + 品类 + 运费险 + 已有售后单 | `found` / `order_status` / `is_signed` / `days_since_receipt` / `days_since_purchase` / `categories` / `has_freight_insurance` / `existing_after_sales` |

**内部子查询：**

| 函数 | 一句话职责 |
|---|---|
| ★`_query_existing_after_sales(oid, uid)` | **读已有售后单的 type + description**，翻译成 `reason` → 让系统不用问用户原因 |
| ★`_query_receipt_time(oid, uid)` | 只认 `logistics.status=2` 的签收时间；不拿运输记录冒充（否则天数会算错） |
| ★`_query_logistics_status(oid, uid)` | 读物流状态（0未入库/1运输中/2已签收），是"是否签收"的第一依据 |
| `_query_categories(oid, uid)` | 探测表名列名后查商品品类；拿不到就返回空列表，不影响主流程 |
| `is_special_category(categories)` | 品类是否属于不支持无理由退货的特殊品类 |

### policy_retrieval_tool.py —— 政策条款级 RAG ★

| 函数 | 一句话职责 |
|---|---|
| `_strip_markdown(text)` | 去掉 md 标记，便于向量化与分词 |
| `_load_clauses()` | ★ **把 md 按 `##` 标题切成条款**，每条带 `clause_id`（POLICY-01…），缓存 |
| `_ensure_index()` | 懒加载：BM25 索引 + BGE 模型 + 文档向量（模型加载失败自动降级纯 BM25） |
| `vector_search(query, top_k)` | 向量召回，返回 `[(clause_id, 相似度)]`，阈值 0.7 |
| `bm25_search(query, top_k)` | 关键词召回，阈值 0.3（BM25 分数量纲无上界，阈值必须低） |
| `rrf_fusion(vec, bm25, k)` | RRF 融合两路排序结果 |
| ★`search_clauses(query, top_k)` | **对外主入口**，返回 `[{clause_id,title,text,score}]`；两路都空时降级为标题字面命中 |
| `search(query, top_k)` | 兼容旧接口，返回 `[(文本, 分数)]` |
| `format_clauses(clauses)` | 拼成带编号的 Prompt 文本，让模型能引用依据 |

---

## 二、售后决策子图（app/agent/node/after_sales/）

### schema/after_sales_schema.py —— 业务规则总配置 ★

**数据定义（看常量即可）：** `INTENT_OPTIONS`、`INTENT_LABELS`、`REQUIRED_SLOTS`、`SLOT_QUESTIONS`、`SLOT_LABELS`、`POLICY_ONLY_INTENTS`、`ORDER_STATUS_MAP`、`CONFLICT_TYPES`、`RULES`

**规则表 RULES 的 9 条规则（面试重点）：**

| ID | 名称 | 触发条件 | 判定 |
|---|---|---|---|
| R000 | 未签收期间可退 | 退货类意图 且 未签收 | ✅ 通过（时效还没开始算） |
| R001 | 七天无理由时效 | 退货类 且 已签收 | ≤7天通过；否则转换货/维修 |
| R002 | 十五天换货时效 | 换货 且 已签收 | ≤15天通过 |
| R003 | 特殊品类限制 | 退货类 | 生鲜/定制/虚拟等 → 拒绝 |
| R004 | 质量问题不受时效限制 | 用户说质量问题 | ✅ 通过（短路 R001） |
| R005 | 保修期 | 保修/维修 | 自**下单**起 ≤365 天 |
| R006 | 运费险补偿 | 只问运费险/运费争议时 | **information型**：只影响运费谁出 |
| R007 | 退款金额 | 退货类 | 金额用订单实付 |
| R008 | 重复售后单 | 已有售后单 | → notify_existing |
| R009 | 订单状态可售后 | 有订单事实时 | 状态 1/2/4/5 才可售后 |

**函数：**

| 函数 | 一句话职责 |
|---|---|
| `_intent/_facts/_slots(state)` | 从 state 里便捷取 intent / order_facts / facts |
| ★`_signed(state)` / `_not_signed(state)` | **判断是否已签收 —— 退货时效的第一道门** |
| `_receipt_days(state)` | 签收距今天数（未签收返回 -1） |
| `_within_receipt(limit)` | 生成"自签收起 N 天内"判定；天数未知时不拦截 |
| `_within_purchase(limit)` | 生成"自下单起 N 天内"判定（保修期用，与签收无关） |
| `_is_return_like(state)` | 是否退货类意图（退货/无理由/换货） |
| ★`combine_decisions(results, order_facts)` | **多条规则结果 → 一个决策**；核心是区分硬规则（参与 action）和信息型规则（只记 notes） |
| ★`freight_note(state, decision)` | 生成"运费由谁承担"说明，**不影响能否退货** |
| `missing_required_slots(intent, facts)` | 计算缺哪些必需槽位 |
| ★`apply_existing_after_sales(intent, facts, order_facts)` | **用库里已有售后单补全 reason**，避免无意义追问 |
| `clarify_question_for(missing)` | 按缺槽位生成反问话术 |

### node/intent_slot_node.py —— ① 意图 + 槽位 ★

| 函数 | 一句话职责 |
|---|---|
| `FactsSchema` / `AfterSalesIntentSchema` | Pydantic 结构化输出定义 |
| `_has_negation(text)` | 是否含否定词（防止"没有质量问题"被误判） |
| ★`_fallback_facts(user_input, facts)` | **关键词兜底**：模型没抽出来的字段用规则补（用户只回"有质量问题"时保命） |
| `_format_history(chat_history)` | 最近几轮消息 → 纯文本 |
| `_extract_facts(data)` | Pydantic → 紧凑 dict（空值不写入） |
| ★`_analyze(prompt)` | 调模型拿结构化结果，**两级兜底**：json_mode 结构化输出 → 纯文本+JSON解析；云端失败降级本地 |
| ★`_coerce_result(result)` | **剥壳**：接口可能返回 `[{...}]`，做 list→dict、str→JSON、`{"result":{...}}` 处理 |
| `_extract_json(text)` | 从文本里抠第一个 JSON 对象 |
| ★`intent_slot_node(state)` | 主节点：指代消解 → 调模型 → order_id 优先级（本轮明说 > 重写后 > 上一轮）→ 关键词兜底 → 算 missing |

### node/order_facts_node.py —— ② 事实聚合 ★

| 函数 | 一句话职责 |
|---|---|
| ★`order_facts_node(state)` | 按固定字段查事实，**并用已有售后单补全槽位**，补全后撤回多余的 clarify 意图 |
| `has_order_facts(state)` | 供条件边使用：事实是否取到 |

### node/policy_rules_node.py —— ③ 政策检索 + 规则判定 ★

| 函数 | 一句话职责 |
|---|---|
| `_build_query(state)` | 拼一个贴近政策语料的检索词（意图名 + 用户原话 + 原因） |
| ★`_existing_after_sales_state(state)` | **已有售后单 → 直接短路**，不进 LLM、不检索政策（确定性结论不该让 LLM 裁决） |
| ★`policy_rules_node(state)` | 主节点：短路检查 → 政策检索 → 遍历规则表 → 合并决策 → 算运费说明 → 判冲突 |

### node/llm_reasoning_node.py —— ④ 灰区推理（仅冲突时进入）★

| 函数 | 一句话职责 |
|---|---|
| `ReasoningSchema` | 推理结果的结构化定义（action / 金额 / 依据条款 / 是否需补充信息） |
| `_format_facts(order_facts)` | 订单事实 → 提示词文本（**含签收情况**） |
| `_format_user_facts(facts)` | 用户口述事实 → 提示词文本 |
| `_invoke(prompt)` | 调模型，两级兜底 + 云端降级本地 |
| `_coerce_result(result)` | 同上的剥壳处理 |
| `_extract_json(text)` | 抠 JSON |
| ★`llm_reasoning_node(state)` | 主节点：拼提示词 → 推理 → **只接受政策里真实存在的条款编号**（防编造依据）→ 按新 action 重算运费说明 |

### node/solution_generate_node.py —— ⑤ 生成答复 + 锁定执行 ★

| 函数 | 一句话职责 |
|---|---|
| `_format_facts(order_facts)` | 事实 → 提示词文本 |
| ★`_compute_execute(decision, state)` | **按最终 action 算出要不要执行、调用哪个工具**；notify_existing 走 notify 不建单 |
| ★`_fallback_solution(decision, state, need_human)` | 模型不可用时的确定性兜底话术（已有售后单 / 转人工 / 各审批动作） |
| ★`solution_generate_node(state)` | 主节点：**先判断订单事实是否缺失**（缺失则转人工，不胡答）→ 生成答复 → 输出执行动作 |

### node/clarify_node.py —— 反问澄清 ★

| 函数 | 一句话职责 |
|---|---|
| ★`_build_question(state)` | 生成反问话术；**订单号已有时绝不问"是哪一笔订单"** |
| `clarify_node(state)` | 反问计数 +1，超上限（2 次）则转人工兜底 |

### node/action_execute_node.py —— ⑥ MCP 执行（本地桩）★

| 函数 | 一句话职责 |
|---|---|
| `_idempotency_key(payload)` | 幂等键 `user_id:order_id:action`，防重复退款 |
| ★`_call_tool(tool, payload)` | **本地桩**：造模拟工单号，不连任何外部系统；真实接入时换成 MCP Client |
| `action_execute_node(state)` | 主节点：白名单校验 → 调用 → 成功附加说明 / 失败降级转人工 |

### graph/after_sales_subgraph.py —— ⑦ 子图编排 ★

| 函数 | 一句话职责 |
|---|---|
| `_need_clarify(state)` | 是否还有反问额度（< 2 次） |
| `_route_after_intent_slot(state)` | 缺槽位 → clarify；否则 → 查事实 |
| ★`_route_after_facts(state)` | 事实取不到时：**订单号已提供 → 转人工**（问用户没用）；订单号缺失 → 反问 |
| `_route_after_rules(state)` | 有冲突 → LLM 复核；否则 → 生成方案 |
| ★`_route_after_reasoning(state)` | 需补充信息时，**只对真正还缺、用户能给的信息反问** |
| `_route_after_solution(state)` | 需执行 → MCP 节点；否则 → END |
| `AfterSalesSubGraph` | 子图类：`get_agent()` 编译图、`invoke/ainvoke` 调用、`draw()` 出拓扑图 |

### 子图 wrapper：app/agent/node/after_sales_node.py ★

| 函数 | 一句话职责 |
|---|---|
| `_get_subgraph()` | 模块级单例（图 compile 有开销，不能每次请求重建） |
| `_recent_history(messages, limit)` | 取最近几轮真实消息（过滤非 Message 对象） |
| ★`after_sales_node(state)` | 主图节点：注入上下文 → 调子图 → **回写 current_order_id** → 包装成主图 state 字段 |

---

## 三、订单查询子图（app/agent/node/order_query/）

### node/query_rewrite_node.py —— 省略指代消解 ★

| 函数 | 一句话职责 |
|---|---|
| `_format_history(chat_history)` | 最近几轮 → 纯文本 |
| `_sanitize(rewritten)` | 清洗模型输出（去"改写为："前缀、引号） |
| ★`rewrite_question(user_input, chat_history, last_order_id, order_context)` | **对外共用函数**（订单子图 + 售后子图都用）：有指代词才调模型，否则原样透传（省一次调用） |
| ★`query_rewrite_node(state)` | 子图节点，写入 `rewritten_input` |

**关键点**：`PRONOUN_PATTERN` 命中「它/那笔/上面说的」且存在上下文时才调用 LLM。

### 其余节点

| 文件 / 函数 | 一句话职责 |
|---|---|
| `intent_classify_node` ★ | 判定查询类型（订单/物流/售后/商品…），**优先用 rewritten_input** |
| `schema_select_node` | 按意图裁剪 schema（字典映射，未来可换图谱召回） |
| `sql_generate_node` | 生成 SQL；重试时把上次错误和原 SQL 拼进提示词 |
| `strip_markdown(text)` | 剥掉 ```json 代码块 |
| `parse_json_output(raw)` | 解析模型输出的 JSON |
| `check_dangerous_sql(sql)` | 挡 `SELECT *` 和写操作 |
| `has_user_id_placeholder(sql)` | 是否含 `user_id = {user_id}` |
| ★`_insert_condition(sql, condition)` | **把条件插到 LIMIT/ORDER BY 之前**（原来追加到末尾会产生非法 SQL） |
| ★`supplement_user_id(sql, intent, tables)` | 自动补 user_id 过滤，插入位置由上一个函数保证 |
| `sql_check_node(state)` | 总检查：JSON 解析 → 空值 → 危险 SQL → user_id → 写 `sql_check_result` 供路由 |
| ★`_extract_order_ids(rows)` | 从查询结果回收 order_id（追问链路的前提） |
| `_extract_order_ids_from_input(user_input)` | 结果为空时从用户原话兜底提取 |
| ★`sql_execute_node(state)` | 执行 SQL，回收 `resolved_order_ids` |
| `_is_error(raw)` | 非 JSON 即视为失败 |
| `answer_translate_node` | 把 DB 结果翻译成友好中文，含状态码映射与兜底话术 |

### graph/order_subgraph.py

| 函数 | 一句话职责 |
|---|---|
| `_route_after_check(state)` | 检查不通过 → 重试；超限 → 翻译兜底；通过 → 执行 |
| `_route_after_execute(state)` | 执行报错 → 重试；成功 → 翻译 |
| `OrderQuerySubGraph` | 子图类（入口是 `query_rewrite_node`） |

---

## 四、主图（app/agent/）

### graph/customer_graph.py ★

| 函数 | 一句话职责 |
|---|---|
| `order_node_with_fallback(state)` | 订单节点异常 → 兜底话术 + 跳 chat |
| ★`after_sales_node_with_fallback(state)` | 售后节点异常 → 兜底话术 + 跳 chat |
| `CustomerServiceGraph.build_graph()` | 组装主图：START→intent→manager→(order/after_sales/chat)→END |
| `create_session(user_id, session_id)` | 建会话（写 Redis，24h TTL） |
| `check_session(user_id, session_id)` | 校验会话存在性与归属 |
| ★`chat(question, user_id, session_id)` | **流式主入口**：存记忆 → 建提示词 → 会话锁内 astream → 更新记忆 |
| `_session_lock(session_id)` | Redis SET NX + Lua 释放的会话级分布式锁 |
| `draw()` | 导出主图拓扑图 |

### 各节点

| 函数 | 一句话职责 |
|---|---|
| `intent_node(state)` | 一级意图识别（4 类），云端优先、本地兜底 |
| `manager_node(state)` | 中枢路由：order_query→order / after_sales→after_sales / 其他→chat |
| `order_node(state)` ★ | 订单子图 wrapper：注入上下文、回写 `current_order_id` |
| `chat_node_stream(state)` | 闲聊：本地模型流式优先，失败降级云端 |
| `policy_node(state)` | 旧的售后单节点，**已不接入图**，保留备查 |

---

## 五、排错速查：出现异常先看这里

| 现象 | 大概率原因 | 看哪个函数 |
|---|---|---|
| `KeyError: '__end__'` | 条件边映射表漏了 `END` | `add_conditional_edges` 的映射 dict |
| 明明有订单号还问"是哪一笔" | missing_slots 没过滤已满足项 | `_route_after_reasoning` / `_build_question` |
| 反问反复问同一个问题 | 槽位没抽出来 | `_fallback_facts`、`_extract_facts` |
| `'messages' must contain the word 'json'` | json_mode 要求提示词含 "json" | `_SYSTEM_PROMPT`（意图/推理两个节点） |
| `Input should be an object` | 接口把结果包成 `[{...}]` | `_coerce_result` |
| SQL 1064 语法错误 | `AND user_id` 被拼到 LIMIT 后 | `_insert_condition` |
| `Unknown column 1054` | 硬编码的列名/表名与库不符 | `_table_columns` / `_resolve_table` / `_pick_fuzzy` |
| 天数算得明显偏大 | 拿运输记录当签收时间 | `_query_receipt_time`（只认 status=2） |
| 该退的被拒（无运费险等） | 信息型规则参与了对错判定 | `informational` 标志 + `combine_decisions` |
| 已有售后单还在追问原因 | 没读库补全槽位 | `_query_existing_after_sales` + `apply_existing_after_sales` |
