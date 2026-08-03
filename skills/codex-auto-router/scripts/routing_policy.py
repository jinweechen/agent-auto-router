from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

SOL_MODEL = "gpt-5.6-sol"
TERRA_MODEL = "gpt-5.6-terra"
LUNA_MODEL = "gpt-5.6-luna"
MODELS = (SOL_MODEL, TERRA_MODEL, LUNA_MODEL)
STRATEGIES = ("intelligence", "balance", "cost")
EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")

COMPLEXITY_TERMS = (
    "architecture", "architect", "redesign", "refactor", "distributed",
    "concurrency", "concurrent", "race condition", "deadlock", "performance",
    "multi-module", "multi-service", "cross-system", "dependency", "ambiguous",
    "tradeoff", "root cause", "end-to-end", "workflow", "integration",
    "orchestration", "review", "audit", "架构", "重新设计", "重构", "分布式",
    "并发", "竞态", "死锁", "性能", "多模块", "多服务", "跨系统", "依赖",
    "歧义", "权衡", "根因", "端到端", "工作流", "集成", "编排", "审查", "验收",
)
RISK_TERMS = (
    "security", "authentication", "authorization", "credential", "secret",
    "token", "production", "data loss", "delete", "drop", "irreversible",
    "payment", "billing", "permission", "migration", "compliance", "privacy",
    "vulnerability", "exploit", "incident", "安全", "认证", "鉴权", "授权",
    "凭据", "密钥", "令牌", "生产", "数据丢失", "删除", "支付", "权限",
    "迁移", "合规", "隐私", "漏洞", "事故",
)
RISK_ACTION_TERMS = (
    "implement authentication", "change authentication", "configure security",
    "deploy", "migrate", "rotate secret", "rotate token", "revoke", "remediate",
    "fix vulnerability", "delete production", "drop database", "实施认证",
    "修改认证", "配置安全", "部署", "迁移", "轮换密钥", "轮换令牌", "撤销",
    "修复漏洞", "删除生产", "删除数据库",
)
SENSITIVE_DOMAIN_TERMS = (
    "authentication", "authorization", "credential", "secret", "token",
    "production", "database", "schema", "payment", "billing", "permission",
    "compliance", "privacy", "security", "认证", "鉴权", "授权", "凭据",
    "密钥", "令牌", "生产", "数据库", "表结构", "支付", "权限", "合规",
    "隐私", "安全",
)
INHERENT_HIGH_RISK_TERMS = (
    "data loss", "vulnerability", "vulnerabilities", "exploit", "incident",
    "数据丢失", "漏洞", "事故",
)
PARALLEL_TERMS = (
    "frontend and backend", "api and tests", "multiple modules", "independent",
    "parallel", "workstreams", "前后端", "接口和测试", "多个模块", "独立任务",
    "并行", "子任务",
)
AMBIGUITY_TERMS = (
    "best approach", "explore", "investigate", "redesign", "open-ended",
    "最佳方案", "探索", "调研", "重新设计", "开放性",
)
SIMPLE_TERMS = (
    "extract", "classify", "transform", "summarize", "format", "rename",
    "translate", "convert", "sort", "deduplicate", "typo", "single file",
    "exactly", "reply with", "提取", "分类", "转换", "摘要", "格式化", "重命名",
    "翻译", "排序", "去重", "错别字", "单文件", "精确回复",
)


@dataclass(frozen=True)
class RoutingFeatures:
    prompt_chars: int
    criteria_count: int
    complexity_hits: int
    risk_hits: int
    risk_action_hits: int
    simple_hits: int
    parallel_hits: int
    ambiguity_hits: int
    complexity_score: int
    risk_score: int
    clarity_score: int
    high_risk: bool
    constrained: bool
    parallelizable: bool
    dependency_ambiguity: bool


@dataclass(frozen=True)
class ModelDecision:
    model: str
    reason: str
    strategy: str
    effort: str
    prompt_chars: int
    criteria_count: int
    high_risk_hits: int
    risk_action_hits: int
    complex_hits: int
    simple_hits: int
    complexity_score: int
    risk_score: int
    clarity_score: int
    high_risk: bool
    constrained: bool
    parallelizable: bool
    dependency_ambiguity: bool


def _count_hits(text: str, terms: Sequence[str]) -> int:
    return sum(1 for term in terms if term in text)


def analyze_task(
    prompt: str,
    acceptance_criteria: Sequence[str] | None = None,
) -> RoutingFeatures:
    text = prompt.lower()
    criteria_count = len(acceptance_criteria or ())
    complexity_hits = _count_hits(text, COMPLEXITY_TERMS)
    risk_hits = _count_hits(text, RISK_TERMS)
    risk_action_hits = _count_hits(text, RISK_ACTION_TERMS)
    sensitive_domain_hits = _count_hits(text, SENSITIVE_DOMAIN_TERMS)
    inherent_high_risk_hits = _count_hits(text, INHERENT_HIGH_RISK_TERMS)
    simple_hits = _count_hits(text, SIMPLE_TERMS)
    parallel_hits = _count_hits(text, PARALLEL_TERMS)
    ambiguity_hits = _count_hits(text, AMBIGUITY_TERMS)

    complexity_score = min(
        10,
        complexity_hits
        + (2 if len(prompt) >= 6000 or prompt.count("\n") >= 100 else 1 if len(prompt) >= 900 else 0)
        + (2 if criteria_count >= 6 else 1 if criteria_count >= 4 else 0),
    )
    risk_score = min(10, risk_hits * 2 + risk_action_hits * 2)
    clarity_score = min(
        10,
        criteria_count * 2
        + (2 if "must" in text or "必须" in text else 0)
        + (1 if any(term in text for term in ("provide", "output", "输出", "提供")) else 0),
    )
    high_risk = inherent_high_risk_hits > 0 or (
        sensitive_domain_hits > 0 and risk_action_hits > 0
    )
    constrained = simple_hits > 0 and complexity_score <= 2 and len(prompt) <= 3000
    # Criteria count increases complexity, but does not prove independence.
    parallelizable = parallel_hits > 0
    dependency_ambiguity = ambiguity_hits > 0 or (
        ("dependency" in text or "依赖" in text) and criteria_count < 3
    )

    return RoutingFeatures(
        prompt_chars=len(prompt),
        criteria_count=criteria_count,
        complexity_hits=complexity_hits,
        risk_hits=risk_hits,
        risk_action_hits=risk_action_hits,
        simple_hits=simple_hits,
        parallel_hits=parallel_hits,
        ambiguity_hits=ambiguity_hits,
        complexity_score=complexity_score,
        risk_score=risk_score,
        clarity_score=clarity_score,
        high_risk=high_risk,
        constrained=constrained,
        parallelizable=parallelizable,
        dependency_ambiguity=dependency_ambiguity,
    )


def select_model(
    prompt: str,
    strategy: str = "balance",
    effort: str = "medium",
    acceptance_criteria: Sequence[str] | None = None,
) -> ModelDecision:
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy: {strategy}")
    if effort not in EFFORTS:
        raise ValueError(f"unknown effort: {effort}")

    features = analyze_task(prompt, acceptance_criteria)
    if features.high_risk:
        model, reason = SOL_MODEL, "high_risk"
    elif strategy == "intelligence":
        if features.complexity_score >= 3 or effort in {"xhigh", "max"}:
            model, reason = SOL_MODEL, "complexity"
        else:
            model, reason = TERRA_MODEL, "intelligence_routine"
    elif strategy == "cost":
        if features.complexity_score >= 3 or effort in {"xhigh", "max"}:
            model, reason = TERRA_MODEL, "cost_proxy_complexity"
        else:
            model, reason = LUNA_MODEL, "cost_proxy_default"
    elif features.constrained:
        model, reason = LUNA_MODEL, "constrained"
    elif features.complexity_score >= 3 or effort in {"xhigh", "max"}:
        model, reason = SOL_MODEL, "complexity"
    else:
        model, reason = TERRA_MODEL, "balance_default"

    return ModelDecision(
        model=model,
        reason=reason,
        strategy=strategy,
        effort=effort,
        prompt_chars=features.prompt_chars,
        criteria_count=features.criteria_count,
        high_risk_hits=features.risk_hits,
        risk_action_hits=features.risk_action_hits,
        complex_hits=features.complexity_hits,
        simple_hits=features.simple_hits,
        complexity_score=features.complexity_score,
        risk_score=features.risk_score,
        clarity_score=features.clarity_score,
        high_risk=features.high_risk,
        constrained=features.constrained,
        parallelizable=features.parallelizable,
        dependency_ambiguity=features.dependency_ambiguity,
    )
