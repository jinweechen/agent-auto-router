from __future__ import annotations

from typing import Any


VALID_MODES = {"intelligence", "balance", "cost"}

COMPLEXITY_TERMS = (
    "architecture", "architect", "migration", "concurrency", "distributed",
    "performance", "debug", "refactor", "multi-module", "cross-system",
    "dependency", "workflow", "integration", "架构", "迁移", "并发",
    "分布式", "性能", "排查", "重构", "跨系统", "依赖", "工作流", "集成",
)
RISK_TERMS = (
    "security", "authentication", "authorization", "payment", "production",
    "database migration", "data loss", "destructive", "compliance", "privacy",
    "安全", "认证", "授权", "支付", "生产", "数据库迁移", "数据丢失",
    "破坏性", "合规", "隐私",
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
REPEATABLE_TERMS = (
    "extract", "classify", "transform", "summarize", "format", "rename",
    "convert", "生成测试", "提取", "分类", "转换", "摘要", "格式化", "重命名",
)


def _count_hits(text: str, terms: tuple[str, ...]) -> int:
    return sum(1 for term in terms if term in text)


def route_case(case: dict[str, Any], mode: str = "balance") -> dict[str, Any]:
    if mode not in VALID_MODES:
        raise ValueError(f"Unknown routing mode: {mode}")

    prompt = str(case.get("prompt", ""))
    text = prompt.lower()
    criteria = case.get("acceptance_criteria", [])
    criteria_count = len(criteria) if isinstance(criteria, list) else 0

    complexity_hits = _count_hits(text, COMPLEXITY_TERMS)
    risk_hits = _count_hits(text, RISK_TERMS)
    parallel_hits = _count_hits(text, PARALLEL_TERMS)
    ambiguity_hits = _count_hits(text, AMBIGUITY_TERMS)
    repeatable_hits = _count_hits(text, REPEATABLE_TERMS)

    complexity_score = min(
        10,
        complexity_hits
        + (2 if len(prompt) >= 900 else 1 if len(prompt) >= 450 else 0)
        + (2 if criteria_count >= 5 else 1 if criteria_count >= 3 else 0),
    )
    risk_score = min(10, risk_hits * 3)
    clarity_score = min(
        10,
        criteria_count * 2
        + (2 if "must" in text or "必须" in text else 0)
        + (1 if "provide" in text or "输出" in text or "提供" in text else 0),
    )
    parallelizable = parallel_hits > 0 or criteria_count >= 4
    dependency_ambiguity = (
        ambiguity_hits > 0
        or ("dependency" in text or "依赖" in text) and criteria_count < 3
    )
    high_risk = risk_score >= 3
    clear_repeatable = repeatable_hits > 0 and clarity_score >= 4 and complexity_score <= 3

    if high_risk or complexity_score >= 7:
        if parallelizable:
            variant = "C" if dependency_ambiguity else "B"
        else:
            variant = "A"
    elif mode == "intelligence":
        if complexity_score >= 4 and parallelizable:
            variant = "B"
        elif complexity_score >= 3 or clarity_score < 4:
            variant = "A"
        else:
            variant = "E"
    elif mode == "balance":
        if complexity_score >= 4 and parallelizable:
            variant = "B"
        elif complexity_score >= 3 or clarity_score < 3:
            variant = "E"
        else:
            variant = "F" if clear_repeatable or complexity_score <= 1 else "E"
    else:
        if complexity_score >= 6 and parallelizable:
            variant = "B"
        elif complexity_score >= 5 or high_risk:
            variant = "A"
        elif complexity_score >= 3 or clarity_score < 3:
            variant = "E"
        else:
            variant = "F"

    labels = {
        "A": "direct-sol",
        "B": "sol-plan-luna-workers-sol-review",
        "C": "sol-plan-terra-dispatch-luna-workers-sol-review",
        "D": "terra-plan-luna-workers-terra-review",
        "E": "direct-terra",
        "F": "direct-luna",
    }
    reasons = [
        f"complexity={complexity_score}/10",
        f"risk={risk_score}/10",
        f"clarity={clarity_score}/10",
        f"parallelizable={str(parallelizable).lower()}",
        f"dependency_ambiguity={str(dependency_ambiguity).lower()}",
    ]
    if high_risk:
        reasons.append("high-risk terms require Sol")
    if clear_repeatable:
        reasons.append("clear repeatable task is eligible for Luna")
    if variant == "C":
        reasons.append("Terra dispatch is enabled only for ambiguous dependency decomposition")

    return {
        "router_version": "auto-lite-v1",
        "mode": mode,
        "variant": variant,
        "route": labels[variant],
        "features": {
            "prompt_characters": len(prompt),
            "acceptance_criteria": criteria_count,
            "complexity_score": complexity_score,
            "risk_score": risk_score,
            "clarity_score": clarity_score,
            "parallelizable": parallelizable,
            "dependency_ambiguity": dependency_ambiguity,
        },
        "reasons": reasons,
    }
