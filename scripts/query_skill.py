#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ontology-RAG Query Skill（可复用封装）

目标：
- 输入：OSI/本体 YAML（路径或字符串）+ 自然语言问题
- 输出：结构化 Query 结果（SQL / 意图 / 命中实体 / 校验错误与修复提示 / YAML 定位 / 动作候选）

说明：
- 本脚本是对 `codespace/ontology-rag` 中 Query Pipeline 的“薄封装”，便于任何 Agent 直接调用。
- 运行前需确保已安装 ontology-rag 依赖（建议：pip install -e /path/to/ontology-rag[anthropic,openai,sqlglot]）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


@dataclass(frozen=True)
class ProviderConfig:
    """
    LLM Provider 配置（用于 SQL 生成/修复）。

    provider:
      - mock: 不调用外部模型，直接返回固定 SQL（适合单元测试/无 Key 环境）
      - openai: OpenAI 兼容接口
      - anthropic/minimax: Anthropic 兼容接口（可用于 MiniMax Anthropic 兼容）
    """

    provider: str = "mock"
    model: str = ""
    temperature: float = 0.2
    mock_sql: str = "SELECT 1;"


@dataclass(frozen=True)
class QuerySkillConfig:
    dialect: str = "ANSI_SQL"
    generation_mode: str = "llm_sql"  # llm_sql | ir_sqlglot
    max_fix_retries: int = 2
    max_plan_retries: int = 2
    # 作为“模型指导工具”，默认不直接暴露 SQL（避免把 SQL 当作对外契约/引导用户写 SQL）。
    # 如需调试，可通过 CLI 打开 emit_sql。
    emit_sql: bool = False
    # 默认不产出跨域“固定模板 plan”（容易与当前本体不一致）；仅在显式开启时输出 plan。
    enable_plan: bool = False
    provider: ProviderConfig = ProviderConfig()


class OntologyQuerySkill:
    """
    可复用 Skill：缓存模型解析 + 统一结构化输出。
    """

    def __init__(self, cfg: QuerySkillConfig):
        self.cfg = cfg
        self._cache: Dict[Tuple[str, str, str], Any] = {}

    # -------------------------
    # 模型加载（路径/字符串）
    # -------------------------
    def load_model_from_yaml_path(self, yaml_path: str):
        yaml_text = _safe_read_text(yaml_path)
        key = (_sha256(yaml_text), self.cfg.dialect, self.cfg.generation_mode)
        if key in self._cache:
            return self._cache[key]

        rag = self._build_rag_from_yaml_path(yaml_path)
        self._cache[key] = rag
        return rag

    def load_model_from_yaml_string(self, yaml_text: str):
        key = (_sha256(yaml_text), self.cfg.dialect, self.cfg.generation_mode)
        if key in self._cache:
            return self._cache[key]

        rag = self._build_rag_from_yaml_string(yaml_text)
        self._cache[key] = rag
        return rag

    # -------------------------
    # Query 主入口
    # -------------------------
    def query(self, *, yaml_path: str | None = None, yaml_text: str | None = None, question: str) -> dict:
        """
        返回结构化结果（可序列化 JSON）。
        """

        if not question or not question.strip():
            return {
                "status": "error",
                "error": {"code": "EMPTY_QUESTION", "message": "question 不能为空"},
            }

        if (yaml_path is None) == (yaml_text is None):
            return {
                "status": "error",
                "error": {"code": "INVALID_INPUT", "message": "必须且只能提供 yaml_path 或 yaml_text 其中一个"},
            }

        rag = self.load_model_from_yaml_path(yaml_path) if yaml_path else self.load_model_from_yaml_string(yaml_text or "")

        # 1) 先做“动作意图”判定（即使没有候选动作，也应结构化返回，避免误走 SQL）
        has_action_verbs = _has_action_verbs(question)
        if has_action_verbs:
            action_payload = self._action_candidates_payload(rag=rag, question=question)
            # 没有候选动作：直接 blocked，让调用方知道“需要补 action_types”
            if not action_payload["action"]["action_candidates"]:
                action_payload["status"] = "blocked"
                action_payload["violations"] = [
                    {
                        "code": "ACTION_NOT_SUPPORTED",
                        "message": "检测到动作意图，但当前本体模型未覆盖对应 action_types（或 examples/synonyms 不足）。",
                        "suggestion": "请在目标对象 dataset.custom_extensions.data 的 behavior_layer.action_types 中补充动作定义与 examples/synonyms，并确保 io_schema 可校验。",
                    }
                ]
                action_payload["summary"] = "动作请求未命中任何 action_types；已返回 blocked（建议补齐动作目录）。"
                return action_payload

            # 1.1) 可选：结构化计划（默认关闭，避免与不同领域本体不一致）
            if self.cfg.enable_plan:
                action_payload["plan"] = self._build_plan_from_action_candidates(rag=rag, question=question, action_payload=action_payload)

            action_payload["status"] = "ok"
            action_payload["summary"] = self._summarize(action_payload)
            return action_payload

        # 2) Query：走 Ontology-RAG 原始 QueryResult（dataclass）
        result = rag.query(question)
        internal_sql = getattr(result, "sql", "") or ""
        referenced_tables = list(getattr(result, "referenced_tables", []) or [])

        # 统一结构化输出（稳定字段，便于下游 Agent 消费）
        payload: dict[str, Any] = {
            "status": "ok",
            "kind": "action" if getattr(result, "is_action", False) else "query",
            "input": {
                "question": question,
                "dialect": self.cfg.dialect,
                "generation_mode": self.cfg.generation_mode,
                "model_name": getattr(getattr(rag, "model", None), "name", None),
            },
            "output": {
                # 作为“模型指导工具”：默认不输出 SQL；但字段保持存在以保证结构稳定
                "sql": internal_sql if self.cfg.emit_sql else "",
                "is_valid": bool(getattr(result, "is_valid", False)),
                "intent": getattr(result, "intent", "unknown"),
                "confidence": float(getattr(result, "confidence", 0.0)),
                "referenced_tables": referenced_tables,
                "referenced_metrics": list(getattr(result, "referenced_metrics", []) or []),
                "validation_errors": list(getattr(result, "validation_errors", []) or []),
                "validation_warnings": list(getattr(result, "validation_warnings", []) or []),
                "yaml_trace": list(getattr(result, "yaml_trace", []) or []),
                "metrics": asdict(getattr(result, "metrics", None)) if getattr(result, "metrics", None) else {},
            },
            "action": {
                "action_intent": getattr(result, "action_intent", "unknown"),
                "action_confidence": float(getattr(result, "action_confidence", 0.0)),
                "action_candidates": list(getattr(result, "action_candidates", []) or []),
                "rule_hints": list(getattr(result, "rule_hints", []) or []),
            },
        }

        # 2.1) 输出“数据集 + 属性”指导信息（不依赖 SQL 外显）
        try:
            fields_map = _extract_referenced_fields_from_sql(internal_sql, rag=rag)
        except Exception:
            fields_map = {}
        # 优先用 SQL 中可解析到的 dataset/field（更贴近“需要哪些属性”）；解析不到再退回 referenced_tables
        all_ds_for_requirements = sorted(list(fields_map.keys())) if fields_map else referenced_tables
        max_ds = 8
        payload["output"]["data_requirements_total_datasets"] = len(all_ds_for_requirements)
        payload["output"]["data_requirements_truncated"] = len(all_ds_for_requirements) > max_ds
        ds_for_requirements = all_ds_for_requirements[:max_ds]
        payload["output"]["data_requirements"] = _build_data_requirements(rag=rag, referenced_tables=ds_for_requirements, fields_map=fields_map)

        # 3) 可选：Gate 产出（更强的结构化证据与违规信息）
        try:
            gate = _build_gate_result(
                trace_id="skill-local",
                question=question,
                sql=internal_sql,
                rag=rag,
                dialect=self.cfg.dialect,
            )
            payload["gate"] = gate
        except Exception:
            payload["gate"] = {}

        # 4) Grounding 兜底：如果没有命中任何 metric 且 SQL 为空/或无效，则标记为 blocked
        # 注意：sql 对外可能被隐藏，所以这里使用 internal_sql 判断
        if (not payload["output"].get("referenced_metrics")) and (not internal_sql.strip()):
            payload["status"] = "blocked"
            payload["violations"] = [
                {
                    "code": "NO_GROUNDING",
                    "message": "未能在当前本体模型中找到足够的指标/对象来回答该问题。",
                    "suggestion": "请检查：1) 是否加载了正确领域的本体模型；2) 是否为相关对象/指标补充 ai_context.synonyms/examples；3) 是否需要新增 metrics 或 datasets。",
                }
            ]

        # 简短摘要（方便 UI 直接展示；下游也可忽略）
        payload["summary"] = self._summarize(payload)
        return payload

    # -------------------------
    # 内部：构造 RAG
    # -------------------------
    def _build_rag_from_yaml_path(self, yaml_path: str):
        try:
            from ontology_rag import OntologyRAG
        except Exception as e:  # pragma: no cover
            raise RuntimeError(
                "无法 import ontology_rag。请先安装：pip install -e /path/to/ontology-rag[anthropic,openai,sqlglot]"
            ) from e

        provider = _build_llm_provider(self.cfg.provider)
        return OntologyRAG.from_yaml(
            yaml_path,
            llm_provider=provider,
            dialect=self.cfg.dialect,
            max_fix_retries=self.cfg.max_fix_retries,
            generation_mode=self.cfg.generation_mode,
            max_plan_retries=self.cfg.max_plan_retries,
        )

    def _build_rag_from_yaml_string(self, yaml_text: str):
        try:
            from ontology_rag import OntologyRAG
        except Exception as e:  # pragma: no cover
            raise RuntimeError(
                "无法 import ontology_rag。请先安装：pip install -e /path/to/ontology-rag[anthropic,openai,sqlglot]"
            ) from e

        provider = _build_llm_provider(self.cfg.provider)
        return OntologyRAG.from_string(
            yaml_text,
            llm_provider=provider,
            dialect=self.cfg.dialect,
            max_fix_retries=self.cfg.max_fix_retries,
            generation_mode=self.cfg.generation_mode,
            max_plan_retries=self.cfg.max_plan_retries,
        )

    def _action_candidates_payload(self, *, rag: Any, question: str) -> dict:
        """
        不依赖 pipeline.query 的 action 短路逻辑：直接从 kg/action_router 构造稳定输出。
        """
        ar = rag.action_router.route(question)
        candidates = [c.__dict__ for c in ar.candidates] if getattr(ar, "candidates", None) else []
        rule_hints = [h.__dict__ for h in ar.rule_hints] if getattr(ar, "rule_hints", None) else []

        # enrich：补齐 action 的 io_schema / effects / tags / examples / synonyms（便于“结构化调用”）
        try:
            cand_map: dict[tuple[str, str], dict[str, Any]] = {(c.get("dataset", ""), c.get("id", "")): c for c in candidates}
            for aref in rag.kg.list_actions():
                key = (aref.dataset_name, aref.action.id)
                c = cand_map.get(key)
                if not c:
                    continue
                a = aref.action
                c["io_schema"] = getattr(a, "io_schema", None) or {}
                c["effects"] = getattr(a, "effects", None) or []
                c["tags"] = getattr(a, "tags", None) or []
                c["examples"] = getattr(a, "examples", None) or []
                c["synonyms"] = getattr(a, "synonyms", None) or []
                c["idempotency"] = getattr(a, "idempotency", None)
        except Exception:
            pass

        # 额外建议：基于 token 召回可能相关的动作（用于“动作目录补齐”）
        suggestions = _suggest_actions(rag.kg, question)

        # 从 top candidate 的 io_schema 中抽取 required 字段，方便调用方“补齐参数”
        top_required: list[str] = []
        try:
            if candidates:
                schema = ((candidates[0].get("io_schema") or {}).get("input_schema") or {})
                top_required = list(schema.get("required") or [])
        except Exception:
            top_required = []

        return {
            "status": "ok",
            "kind": "action",
            "input": {
                "question": question,
                "dialect": self.cfg.dialect,
                "generation_mode": self.cfg.generation_mode,
                "model_name": getattr(getattr(rag, "model", None), "name", None),
            },
            "output": {
                "sql": "",
                "is_valid": True,
                "intent": "action",
                "confidence": float(getattr(ar, "confidence", 0.0)),
                "referenced_tables": [],
                "referenced_metrics": [],
                "validation_errors": [],
                "validation_warnings": [],
                "yaml_trace": [],
                # 统一字段：action 场景下通常不需要数据集字段集合，因此为空
                "data_requirements": [],
                "data_requirements_total_datasets": 0,
                "data_requirements_truncated": False,
                "metrics": {},
            },
            "action": {
                "action_intent": getattr(ar, "action_intent", "unknown"),
                "action_confidence": float(getattr(ar, "confidence", 0.0)),
                "action_candidates": candidates,
                "rule_hints": rule_hints,
                "action_suggestions": suggestions,
                "top_candidate_required_args": top_required,
            },
        }

    def _build_plan_from_action_candidates(self, *, rag: Any, question: str, action_payload: dict) -> dict[str, Any]:
        """
        把“复合指令”组织成用户期望的风格：

        Query-1：去年积分 Top10 会员 + 会员等级（customers/customer_memberships/loyalty_point_ledger）
        Query-2：临期最严重原材料（inventory_lots + inventory_lot_balances + expiry_date）
        Query-3：高消耗菜品（order_items + dish_recipe_items + inventory usage）
        Action-1：为 Top10 发放“免费菜品券”（customer_coupons / coupons）
        Action-2：新增会员套餐并挂载“券权益 + 最低消费门槛”（membership_packages / membership_package_benefits）

        说明：
        - 本 plan 是“可执行计划的骨架”，参数使用依赖引用（$step_xxx）保证结构化。
        - 不做真实执行；真实执行由 Data Agent/业务服务承担。
        """

        now = datetime.now(timezone.utc)
        year = now.year - 1
        start = datetime(year, 1, 1, tzinfo=timezone.utc).isoformat()
        end = datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc).isoformat()

        # action meta：直接从 KG 全量 actions 中按 id 建索引（不依赖当前命中候选，保证计划完整）
        action_meta: dict[str, dict[str, Any]] = {}
        try:
            for aref in rag.kg.list_actions():
                a = aref.action
                action_meta[a.id] = {
                    "dataset": aref.dataset_name,
                    "operation": getattr(a, "operation", None),
                    "aggregate": getattr(a, "aggregate", None),
                    "io_schema": getattr(a, "io_schema", None) or {},
                    "idempotency": getattr(a, "idempotency", None),
                }
        except Exception:
            action_meta = {}

        def _step(step_id: str, title: str, kind: str, action_id: str, args: dict[str, Any], depends_on: list[str] | None = None):
            c = action_meta.get(action_id) or {}
            return {
                "step_id": step_id,
                "title": title,
                "kind": kind,  # query | command
                "action_id": action_id,
                "dataset": c.get("dataset"),
                "operation": c.get("operation"),
                "aggregate": c.get("aggregate"),
                "io_schema": c.get("io_schema") or {},
                "idempotency": c.get("idempotency"),
                "args": args,
                "depends_on": depends_on or [],
            }

        # Query-1: Top members by points (last year)
        q1 = _step(
            "Query-1",
            "去年积分 Top10 会员（含会员等级提示所需字段）",
            "query",
            "customers/top_members_by_points",
            args={"date_range": {"start": start, "end": end}, "top_n": 10, "include_negative": False},
        )

        # Query-2: Most expiring ingredient lots
        q2 = _step(
            "Query-2",
            "当前临期最严重的原材料批次（默认 7 天内）",
            "query",
            "inventory_lots/query_most_expiring",
            args={"as_of_time": now.isoformat(), "expiry_within_days": 7, "top_n": 20},
        )

        # Query-3: High consumption dishes
        # 用已存在的 “销量 TopN” 作为高消耗近似（严格“按配方×销量估算原料消耗量”需后续扩展一个专用 action_type）
        q3 = _step(
            "Query-3",
            "高消耗菜品（用销量 TopN 近似；可按门店/品类过滤）",
            "query",
            "order_items/top_dishes",
            args={"start_date": f"{year}-01-01", "end_date": f"{year}-12-31", "top_n": 10},
        )

        # Query-3b: Recommend dishes to consume expiring ingredients (uses Query-2 output)
        q3b = _step(
            "Query-3b",
            "基于临期原材料推荐可消耗菜品 TopN（用于选择赠券菜品）",
            "query",
            "dishes/recommend_for_expiring_ingredients",
            args={
                "expiry_within_days": 7,
                "top_n": 5,
                "strategy": "EXPECTED_USAGE_7D",
                "seed_ingredient_ids": "$Query-2.result.rows[*].ingredient_id"
            },
            depends_on=["Query-2"],
        )

        # Action-1a: Create coupon template for the chosen free dish
        a1a = _step(
            "Action-1a",
            "创建免费菜品券模板（赠送菜品=临期消耗推荐Top1）",
            "command",
            "coupons/create_free_dish_template",
            args={
                "coupon_code": f"FREE_DISH_{now.strftime('%Y%m%d')}",
                "free_dish_id": "$Query-3b.result.rows[0].dish_id",
                "valid_from": now.isoformat(),
                "valid_to": (now + timedelta(days=7)).isoformat(),
                "reason": "临期原材料消耗运营（自动推荐）",
            },
            depends_on=["Query-3b"],
        )

        # Action-1b: Issue coupon to Top10 members
        a1b = _step(
            "Action-1b",
            "向 Top10 会员发放免费菜品券",
            "command",
            "customer_coupons/issue_free_dish_coupon",
            args={
                "customer_id": "$Query-1.result.rows[*].customer_id",
                "coupon_id": "$Action-1a.result.coupon_id",
                "issued_at": now.isoformat(),
                "reason": "积分Top会员回馈 + 临期消耗活动",
            },
            depends_on=["Query-1", "Action-1a"],
        )

        # Action-2a: Create membership package
        a2a = _step(
            "Action-2a",
            "新增会员套餐（用于承载送菜券权益）",
            "command",
            "membership_packages/create",
            args={
                "package_code": f"PKG_FREE_DISH_{now.strftime('%Y%m%d')}",
                "package_name": "临期消耗送菜券套餐",
                "price": "$Query-3b.result.rows[0].dish_theoretical_unit_cost",
                "currency": "CNY",
                "start_date": now.isoformat(),
                "end_date": (now + timedelta(days=30)).isoformat(),
            },
            depends_on=["Query-3b"],
        )

        # Action-2b: Attach coupon benefit with min_spend_amount = dish basic cost
        a2b = _step(
            "Action-2b",
            "为会员套餐挂载券权益（最低消费=赠送菜品基本成本）",
            "command",
            "membership_package_benefits/add_coupon_benefit",
            args={
                "package_id": "$Action-2a.result.package_id",
                "coupon_id": "$Action-1a.result.coupon_id",
                "min_spend_amount": "$Query-3b.result.rows[0].dish_theoretical_unit_cost",
                "benefit_type": "COUPON",
                "reason": "门槛=基本成本，鼓励复购并消耗临期原料",
            },
            depends_on=["Action-2a", "Action-1a"],
        )

        # Plan skeleton
        return {
            "plan_id": f"plan_{now.strftime('%Y%m%d_%H%M%S')}",
            "style": "Query-1..Action-2",
            "assumptions": [
                "Query-3 的“高消耗”目前用销量 TopN 近似；如需按“配方×销量”精确估算原料消耗量，建议新增专用 action_type（例如 dishes/top_consumers_of_ingredient 或 ingredients/estimated_usage_by_dish）。",
                "Action 的 args 使用 $StepRef 引用上一步结果；真实执行时需由执行器解析依赖并填充参数。",
                "套餐价格/门槛默认取赠送菜品的理论单位成本 dish_theoretical_unit_cost（若该字段不在推荐结果中，则执行器需补一次成本查询）。",
            ],
            "steps": [q1, q2, q3, q3b, a1a, a1b, a2a, a2b],
        }

    # -------------------------
    # 内部：摘要
    # -------------------------
    @staticmethod
    def _summarize(payload: dict) -> str:
        out = payload.get("output") or {}
        kind = payload.get("kind")
        if kind == "action":
            cands = (payload.get("action") or {}).get("action_candidates") or []
            if cands:
                top = cands[0]
                return f"识别为动作请求；候选动作：{top.get('id')}（{top.get('title')}），置信度 {top.get('score')}"
            return "识别为动作请求，但未找到候选动作（请补充 action_types/examples/synonyms）。"

        sql = (out.get("sql") or "").strip()
        valid = bool(out.get("is_valid"))
        intent = out.get("intent")
        tables = out.get("referenced_tables") or []
        if sql and valid:
            return f"生成 SQL 成功（intent={intent}，tables={len(tables)}）。"
        if sql and not valid:
            return f"生成 SQL 但未通过校验（intent={intent}）。"
        # 作为“模型指导工具”，默认隐藏 SQL：此时应基于 data_requirements 给出摘要，避免误判为“没生成”
        total_ds = out.get("data_requirements_total_datasets")
        if total_ds is None:
            total_ds = len(out.get("data_requirements") or [])
        if total_ds:
            truncated = bool(out.get("data_requirements_truncated"))
            suffix = "（已截断）" if truncated else ""
            return f"识别到数据需求（intent={intent}，datasets={int(total_ds)}）{suffix}。"
        return "未生成数据需求（可能是模型/路由/LLM 配置问题）。"


def _build_llm_provider(cfg: ProviderConfig):
    provider = (cfg.provider or "mock").strip().lower()

    # 1) Mock：不依赖 Key
    if provider == "mock":
        from ontology_rag import MockProvider

        return MockProvider(cfg.mock_sql or "SELECT 1;")

    # 2) OpenAI
    if provider == "openai":
        from ontology_rag import OpenAIProvider

        model = cfg.model or os.getenv("ONTOLOGY_SQL_MODEL") or "gpt-4o-mini"
        api_key = os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("OPENAI_BASE_URL")
        return OpenAIProvider(model=model, api_key=api_key, base_url=base_url, temperature=cfg.temperature)

    # 3) Anthropic / MiniMax Anthropic 兼容
    if provider in {"anthropic", "minimax", "minimax_anthropic"}:
        from ontology_rag import MiniMaxAnthropicProvider

        model = cfg.model or os.getenv("ONTOLOGY_SQL_MODEL") or "MiniMax-M2.1-highspeed"
        return MiniMaxAnthropicProvider(model=model, temperature=cfg.temperature)

    raise ValueError(f"未知 provider: {cfg.provider}. 支持: mock | openai | anthropic/minimax")


def _has_action_verbs(text: str) -> bool:
    """
    与 ontology_rag.router.action_router.ActionRouter._has_action_verbs 对齐（轻量复用）。
    """
    t = (text or "").strip().lower()
    triggers = [
        "创建",
        "新增",
        "添加",
        "更新",
        "修改",
        "删除",
        "关闭",
        "开单",
        "提交",
        "审批",
        "赠送",
        "发放",
        "发券",
        "送券",
        "设为",
    ]
    return any(x in t for x in triggers)


def _tokenize(text: str) -> list[str]:
    import re

    tokens: list[str] = []
    t = (text or "").lower()
    tokens.extend(re.findall(r"[a-z0-9_]+", t))
    cn = "".join(re.findall(r"[\u4e00-\u9fff]+", t))
    if cn:
        for n in (4, 3, 2):
            for i in range(0, len(cn) - n + 1):
                tokens.append(cn[i : i + n])
    return [x for x in tokens if len(x) >= 2]


def _suggest_actions(kg: Any, question: str, top_k: int = 5) -> list[dict[str, Any]]:
    """
    当没有 action_candidates 时，用 token 去 action_term_index 做弱召回，给“应该补哪些动作”提示。
    """
    scored: dict[tuple[str, str], int] = {}
    for tok in _tokenize(question):
        for aref in kg.lookup_actions_by_term(tok) or []:
            key = (aref.dataset_name, aref.action.id)
            scored[key] = scored.get(key, 0) + 1
    ranked = sorted(scored.items(), key=lambda x: x[1], reverse=True)[:top_k]
    out: list[dict[str, Any]] = []
    for (ds, aid), sc in ranked:
        out.append({"dataset": ds, "action_id": aid, "score": sc})
    return out


def _build_gate_result(*, trace_id: str, question: str, sql: str, rag: Any, dialect: str) -> dict[str, Any]:
    """
    复用 ontology-rag 的 gate：输出 patches/violations/evidence（实体证据）。
    """
    try:
        from ontology_rag.service.gate.gate import gate_query_sql
    except Exception as e:  # pragma: no cover
        raise RuntimeError("无法导入 ontology_rag.service.gate.gate") from e

    gate = gate_query_sql(trace_id=trace_id, question=question, sql=sql or "", kg=rag.kg, dialect=dialect)
    return gate.model_dump()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ontology-RAG Query Skill: YAML + question -> structured result JSON")
    p.add_argument("--yaml", dest="yaml_path", help="OSI/本体 YAML 文件路径")
    p.add_argument("--question", required=True, help="自然语言问题")
    p.add_argument("--dialect", default="ANSI_SQL", help="SQL 方言（默认 ANSI_SQL）")
    p.add_argument("--mode", default="llm_sql", choices=["llm_sql", "ir_sqlglot"], help="生成模式")
    p.add_argument("--provider", default="mock", choices=["mock", "openai", "anthropic", "minimax"], help="LLM provider")
    p.add_argument("--model", default="", help="LLM 模型名（可选）")
    p.add_argument("--temperature", type=float, default=0.2, help="temperature")
    p.add_argument("--mock-sql", default="SELECT 1;", help="provider=mock 时返回的 SQL")
    p.add_argument("--max-fix-retries", type=int, default=2, help="SQL 修复重试次数")
    p.add_argument("--max-plan-retries", type=int, default=2, help="IR 规划重试次数")
    p.add_argument("--emit-sql", action="store_true", help="对外输出 SQL（仅用于调试；默认关闭）")
    p.add_argument("--enable-plan", action="store_true", help="输出 plan（默认关闭；不同领域本体容易不一致）")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    cfg = QuerySkillConfig(
        dialect=args.dialect,
        generation_mode=args.mode,
        max_fix_retries=args.max_fix_retries,
        max_plan_retries=args.max_plan_retries,
        emit_sql=bool(getattr(args, "emit_sql", False)),
        enable_plan=bool(getattr(args, "enable_plan", False)),
        provider=ProviderConfig(
            provider=args.provider,
            model=args.model,
            temperature=args.temperature,
            mock_sql=args.mock_sql,
        ),
    )
    skill = OntologyQuerySkill(cfg)
    result = skill.query(yaml_path=args.yaml_path, question=args.question)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _extract_referenced_fields_from_sql(sql: str, *, rag: Any) -> dict[str, list[str]]:
    """
    从内部生成的 SQL 中尽力抽取 “dataset -> fields”：
    - 不作为强契约，只用于给调用方展示“需要哪些属性”；
    - 如果 SQL 为空或无法解析，则返回空映射。
    """
    import re

    s = (sql or "").strip()
    if not s:
        return {}

    # dataset 名单：以本体的 dataset 为准（避免把 alias 当作 dataset）
    dataset_names: set[str] = set()
    try:
        dataset_names = set(getattr(rag.kg, "_dataset_map", {}).keys())
    except Exception:
        dataset_names = set()

    pairs = re.findall(r"([a-zA-Z_][a-zA-Z0-9_]*)\\.([a-zA-Z_][a-zA-Z0-9_]*)", s)
    out: dict[str, set[str]] = {}
    for t, c in pairs:
        if dataset_names and (t not in dataset_names):
            continue
        out.setdefault(t, set()).add(c)

    return {k: sorted(v) for k, v in out.items()}


def _build_data_requirements(*, rag: Any, referenced_tables: list[str], fields_map: dict[str, list[str]]) -> list[dict[str, Any]]:
    """
    输出“数据集 + 属性”指导信息（稳定、可解释）：
    - referenced_tables：来自模型 grounding 的命中数据集
    - fields_map：从内部 SQL 尽力抽取的字段集合（可能为空）
    """
    reqs: list[dict[str, Any]] = []
    dataset_map = getattr(getattr(rag, "kg", None), "_dataset_map", {}) or {}

    for ds_name in referenced_tables:
        ds = dataset_map.get(ds_name)
        pk = []
        try:
            pk = list(getattr(ds, "primary_key", []) or [])
        except Exception:
            pk = []

        used_fields = fields_map.get(ds_name, []) or []
        # 如果抽不到 used_fields，至少给出主键作为“可用属性入口”
        stable_fields = used_fields or pk

        # 字段描述（只取 stable_fields，避免刷屏）
        field_desc: dict[str, str] = {}
        try:
            fields = getattr(ds, "fields", []) or []
            meta = {f.name: (f.description or "") for f in fields if getattr(f, "name", None)}
            field_desc = {fn: meta.get(fn, "") for fn in stable_fields}
        except Exception:
            field_desc = {fn: "" for fn in stable_fields}

        reqs.append(
            {
                "dataset": ds_name,
                "primary_key": pk,
                "fields": stable_fields,
                "field_descriptions": field_desc,
            }
        )
    return reqs


if __name__ == "__main__":
    main()
