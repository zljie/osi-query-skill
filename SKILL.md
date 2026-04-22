---
name: 本体咨询器
description: 基于 OSI 本体 YAML 的 Query Skill：输入本体 YAML + 问题，输出结构化查询结果（SQL/意图/命中实体/校验错误/行动候选）。
version: 1.0.0
---

# 本体咨询器（面向任意 Agent 的可复用 Query 能力）

## 1. 这个 Skill 解决什么问题

你们希望“任何 Agent 在拿到本体 YAML 后，都能完成 query 的结构化回复”。这个 Skill 把 `codespace/ontology-rag` 里的 **query pipeline** 抽象成一个可复用组件：

- 输入：**OSI/本体 YAML（文件路径或字符串）** + **自然语言问题**
- 输出：**结构化 JSON**（可直接作为下游 Agent 的中间产物/工具输出），包括：
  - 不直接输出 SQL（默认），而是输出命中数据集与需要用到的属性（`referenced_tables` / `data_requirements`）。如需调试可打开 `--emit-sql`。
  - intent（aggregate/ranking/trend…）
  - 命中实体（datasets/metrics）
  - 校验错误与修复建议（validation_errors / warnings）
  - YAML 定位（yaml_trace：定位到 dataset/field 所在行，便于审计与解释）
  - 动作识别（如果问题更像 command/action，则输出 action_candidates 与 rule_hints）

> 注意：本 Skill 只负责“**生成结构化 query 输出**”，不负责真实执行 SQL（执行属于 Data Agent / 数据执行层）。

---

## 2. 适用场景（When to use）

### 2.1 任意 Agent 的“Query 工具”
- Application Agent：需要把用户问题快速转成 SQL + 证据，再决定如何展示/是否执行
- Ontology Agent：需要 grounding + 规划时的 query 草案（并拿到 YAML trace）
- Data Agent：在执行前需要做 SQL 合规校验与结构化证据输出

### 2.2 本体驱动的统一 Query API
当你们希望把 query 能力变成“平台公共服务”时，这个 Skill 可以作为：
- Python SDK 的最小封装（脚本调用）
- 进一步封装成 HTTP API（统一对外能力）

---

## 3. 不适用场景（When not to use）

- 用户要执行**写操作**（创建/审批/过账/付款等 command）：应走 action 执行链路，并接入审批/审计闸门
- 没有本体 YAML，且无法确定语义边界：应先补模型或走普通对话

---

## 4. 输入与输出契约（建议作为跨 Agent 的标准）

### 4.1 输入
必须提供其一：
- `yaml_path`：本体 YAML 文件路径
- `yaml_text`：本体 YAML 字符串

并提供：
- `question`：自然语言问题

可选：
- `dialect`：SQL 方言（默认 ANSI_SQL）
- `generation_mode`：
  - `llm_sql`：LLM 直接生成 SQL + 校验修复
  - `ir_sqlglot`：LLM 生成 IR，再用 sqlglot 渲染 SQL（更强约束）
- `provider`：
  - `mock`：无需 Key，固定返回 SQL（便于测试）
  - `openai`：需要 OPENAI_API_KEY
  - `anthropic/minimax`：需要 ANTHROPIC_API_KEY（可走 MiniMax Anthropic 兼容）

### 4.2 输出（稳定 JSON）
核心字段：
- `kind`: `query` 或 `action`
- `output.sql`（默认空；仅调试时通过 `--emit-sql` 打开）
- `output.intent`
- `output.referenced_tables`
- `output.referenced_metrics`
- `output.data_requirements`（数据集与字段需求，用于“模型指导”而非 SQL 指导）
- `output.validation_errors / validation_warnings`
- `output.yaml_trace`
- `action.action_candidates / rule_hints`

---

## 5. 内部实现（抽象自 ontology-rag 的哪些模块）

本 Skill 复用并“只暴露 query 的稳定产物”：

- `OntologyRAG.from_yaml/from_string`：加载模型并构建 pipeline
- `ActionRouter`：对输入进行 action 候选识别（基于 action_types / examples / tags）
- `IntentRouter`：实体召回与意图分类（ai_context.synonyms/examples + 中英桥接）
- `ContextBuilder`：子图注入（避免 schema 爆炸）
- `SQLGenerator`：生成 SQL（或 IR）
- `SQLValidator`：校验 SQL（表/字段必须来自模型）
- `YamlLocator`：把 SQL 引用的 table/field 定位回 YAML 行号（用于审计/解释）

此外，在 service 层还存在 `gate_query_sql`（calibrate + verify + evidence），你们若要把 Skill 输出进一步“强闸门化”，可在下一版本把 GateResult 也封装出来。

---

## 6. 快速使用（命令行）

### 6.1 使用 mock（不需要任何 Key）
```bash
python3 scripts/query_skill.py \
  --yaml /path/to/pp_semantic_model_semantic_v3.yaml \
  --question "本月采购订单数量是多少？" \
  --provider mock \
  --mock-sql "SELECT 1;"
```

### 6.2 使用 OpenAI
```bash
export OPENAI_API_KEY=...
python3 scripts/query_skill.py \
  --yaml /path/to/pp_semantic_model_semantic_v3.yaml \
  --question "从请购到采购订单平均要多少天？" \
  --provider openai \
  --model gpt-4o-mini
```

### 6.3 使用 MiniMax Anthropic 兼容
```bash
export ANTHROPIC_BASE_URL=https://api.minimaxi.com/anthropic
export ANTHROPIC_API_KEY=...
python3 scripts/query_skill.py \
  --yaml /path/to/pp_semantic_model_semantic_v3.yaml \
  --question "三单匹配异常主要集中在哪些供应商？" \
  --provider minimax \
  --model MiniMax-M2.1-highspeed
```

---

## 7. 结构化输出如何被“任何 Agent”消费（建议模式）

### 7.1 Application Agent（交互层）
把输出直接渲染：
- 若 `kind=query` 且 `is_valid=true`：展示 SQL + 命中实体 + YAML trace（解释依据）
- 若 `is_valid=false`：展示校验错误与建议（让用户补充限定条件或修模型）
- 若 `kind=action`：展示候选动作（让用户选择或补参数）

### 7.2 Ontology Agent（语义与规划层）
把输出作为规划的中间证据：
- `referenced_tables/metrics` → grounding 证据
- `yaml_trace` → “引用来自哪里”的可审计链
- `validation_errors` → 规划阶段的“不可执行原因”

### 7.3 Data Agent（执行层）
把输出作为执行前校验/执行后回执：
- 执行前：若 SQL 已通过校验，可进入执行器
- 执行后：把真实数据结果 + provenance 与本输出合并，形成完整 DataResult

---

## 8. 后续增强建议（v1.1+）

1) **输出 GateResult**：集成 `service/gate/gate_query_sql`，让输出包含 patches/violations/evidence  
2) **Plan 输出**：当识别为 action 时，直接产出“参数化 action plan”草案（对接你们三层架构的 OntologyPlan）  
3) **连接器接口**：把“执行 SQL”做成可插拔接口（Data Agent 侧），本 Skill 仅保证 query 可审计  
4) **模型健康检查**：输出 behavior layer 解析错误（`kg.get_behavior_errors()`）便于在模型发布阶段阻断
