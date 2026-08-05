from __future__ import annotations

import re
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from model_registry import EFFORTS, ModelRegistry, load_model_registry, registry_digest

DEFAULT_REGISTRY = load_model_registry()
STRATEGIES = ("intelligence", "balance", "cost")
POLICY_SCHEMA_VERSION = 2
LEGACY_POLICY_SCHEMA_VERSION = 1
DEFAULT_POLICY_VERSION = "builtin-v2"
DEFAULT_STATE_DIR = Path.home() / ".codex" / "auto-router"

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
    "authorization bypass", "privacy leakage", "数据丢失", "漏洞", "事故",
    "越权", "隐私泄露",
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
SCOPE_TERMS = (
    "across", "backward compatibility", "breaking change", "public api", "monorepo",
    "packages", "repositories", "many files", "codebase-wide", "cross-repository",
    "跨", "向后兼容", "兼容性", "破坏性变更", "公共接口", "多个包", "多个仓库",
    "大量文件", "全仓库",
)
ALGORITHM_TERMS = (
    "algorithm", "proof", "prove", "invariant", "property-based", "compiler",
    "parser", "state machine", "red-black", "b-tree", "graph algorithm",
    "算法", "证明", "不变量", "性质测试", "编译器", "解析器", "状态机", "红黑树",
    "图算法",
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
    scope_hits: int
    algorithm_hits: int
    complexity_score: int
    risk_score: int
    clarity_score: int
    high_risk: bool
    constrained: bool
    parallelizable: bool
    dependency_ambiguity: bool
    orchestration_eligible: bool


@dataclass(frozen=True)
class ModelDecision:
    model: str
    target_tier: str
    required_capabilities: tuple[str, ...]
    reason: str
    strategy: str
    effort: str
    prompt_chars: int
    criteria_count: int
    high_risk_hits: int
    risk_action_hits: int
    complex_hits: int
    simple_hits: int
    scope_hits: int
    algorithm_hits: int
    complexity_score: int
    risk_score: int
    clarity_score: int
    high_risk: bool
    constrained: bool
    parallelizable: bool
    dependency_ambiguity: bool
    orchestration_eligible: bool
    policy_version: str
    policy_digest: str
    registry_digest: str


@dataclass(frozen=True)
class RoutingPolicy:
    """Small, auditable policy surface that the calibration loop may tune."""

    policy_version: str = DEFAULT_POLICY_VERSION
    intelligence_frontier_threshold: int = 3
    balance_frontier_threshold: int = 3
    cost_balanced_threshold: int = 3


def policy_to_dict(policy: RoutingPolicy) -> dict[str, object]:
    return {
        "schemaVersion": POLICY_SCHEMA_VERSION,
        "policyVersion": policy.policy_version,
        "thresholds": {
            "intelligenceFrontier": policy.intelligence_frontier_threshold,
            "balanceFrontier": policy.balance_frontier_threshold,
            "costBalanced": policy.cost_balanced_threshold,
        },
        "targetTiers": {
            "fast": "fast",
            "routine": "balanced",
            "complex": "frontier",
            "highRisk": "frontier"
        },
    }


def policy_digest(policy: RoutingPolicy) -> str:
    payload = json.dumps(
        policy_to_dict(policy), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def policy_from_dict(payload: dict[str, object]) -> RoutingPolicy:
    schema_version = payload.get("schemaVersion")
    if schema_version not in {LEGACY_POLICY_SCHEMA_VERSION, POLICY_SCHEMA_VERSION}:
        raise ValueError("unsupported routing policy schemaVersion")
    if schema_version == POLICY_SCHEMA_VERSION:
        target_tiers = payload.get("targetTiers")
        expected_tiers = {
            "fast": "fast",
            "routine": "balanced",
            "complex": "frontier",
            "highRisk": "frontier",
        }
        if target_tiers != expected_tiers:
            raise ValueError("routing policy targetTiers may not change capability boundaries")
    thresholds = payload.get("thresholds")
    if not isinstance(thresholds, dict):
        raise ValueError("routing policy thresholds must be an object")
    version = str(payload.get("policyVersion") or "candidate")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", version):
        raise ValueError("routing policy version contains unsupported characters")
    raw_thresholds = (
        thresholds.get(
            "intelligenceFrontier" if schema_version == POLICY_SCHEMA_VERSION else "intelligenceSol",
            3,
        ),
        thresholds.get(
            "balanceFrontier" if schema_version == POLICY_SCHEMA_VERSION else "balanceSol",
            3,
        ),
        thresholds.get(
            "costBalanced" if schema_version == POLICY_SCHEMA_VERSION else "costTerra",
            3,
        ),
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in raw_thresholds):
        raise ValueError("routing policy thresholds must be integers")
    policy = RoutingPolicy(
        policy_version=version,
        intelligence_frontier_threshold=raw_thresholds[0],
        balance_frontier_threshold=raw_thresholds[1],
        cost_balanced_threshold=raw_thresholds[2],
    )
    for name, value in (
        ("intelligenceFrontier", policy.intelligence_frontier_threshold),
        ("balanceFrontier", policy.balance_frontier_threshold),
        ("costBalanced", policy.cost_balanced_threshold),
    ):
        if not 1 <= value <= 8:
            raise ValueError(f"routing threshold {name} must be between 1 and 8")
    return policy


def load_policy_file(path: Path) -> RoutingPolicy:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("routing policy file must contain an object")
    return policy_from_dict(payload)


def active_policy_path(state_dir: Path | None = None) -> Path:
    configured = os.environ.get("CODEX_AUTO_ROUTER_STATE_DIR")
    root = state_dir or (Path(configured) if configured else DEFAULT_STATE_DIR)
    return root / "active-policy.json"


def load_active_policy(state_dir: Path | None = None) -> tuple[RoutingPolicy, str]:
    path = active_policy_path(state_dir)
    if not path.is_file():
        return RoutingPolicy(), "builtin"
    return load_policy_file(path), str(path)


def _count_hits(text: str, terms: Sequence[str]) -> int:
    return sum(1 for term in terms if term in text)


def analyze_task(
    prompt: str,
    acceptance_criteria: Sequence[str] | None = None,
    repository_features: dict[str, object] | None = None,
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
    scope_hits = _count_hits(text, SCOPE_TERMS)
    algorithm_hits = _count_hits(text, ALGORITHM_TERMS)
    if re.search(
        r"\b(?:[2-9]\d|[1-9]\d{2,})\b.{0,24}\b(?:files?|packages?|modules?|services?|repos(?:itories)?)\b",
        text,
    ):
        scope_hits += 1
    repo = repository_features or {}
    repository_complexity = 0
    if not simple_hits and bool(repo.get("large_repo")):
        repository_complexity += 1
    if scope_hits > 0 and bool(repo.get("monorepo")):
        repository_complexity += 1

    complexity_score = min(
        10,
        complexity_hits
        + scope_hits
        + algorithm_hits
        + repository_complexity
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
    constrained = (
        simple_hits > 0
        and scope_hits == 0
        and algorithm_hits == 0
        and complexity_score <= 2
        and len(prompt) <= 3000
    )
    # Criteria count increases complexity, but does not prove independence.
    parallelizable = parallel_hits > 0
    dependency_ambiguity = ambiguity_hits > 0 or (
        ("dependency" in text or "依赖" in text) and criteria_count < 3
    )
    orchestration_eligible = parallelizable and (
        complexity_score >= 2 or criteria_count >= 3 or len(prompt) >= 900
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
        scope_hits=scope_hits,
        algorithm_hits=algorithm_hits,
        complexity_score=complexity_score,
        risk_score=risk_score,
        clarity_score=clarity_score,
        high_risk=high_risk,
        constrained=constrained,
        parallelizable=parallelizable,
        dependency_ambiguity=dependency_ambiguity,
        orchestration_eligible=orchestration_eligible,
    )


def select_model(
    prompt: str,
    strategy: str = "balance",
    effort: str = "medium",
    acceptance_criteria: Sequence[str] | None = None,
    policy: RoutingPolicy | None = None,
    registry: ModelRegistry | None = None,
    repository_features: dict[str, object] | None = None,
) -> ModelDecision:
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy: {strategy}")
    if effort not in EFFORTS:
        raise ValueError(f"unknown effort: {effort}")

    active_policy = policy or RoutingPolicy()
    active_registry = registry or DEFAULT_REGISTRY
    features = analyze_task(prompt, acceptance_criteria, repository_features)
    if features.high_risk:
        target_tier, reason = "frontier", "high_risk"
        required_capabilities = ("high-risk-primary",)
    elif strategy == "intelligence":
        if features.complexity_score >= active_policy.intelligence_frontier_threshold or effort in {"xhigh", "max"}:
            target_tier, reason = "frontier", "complexity"
        else:
            target_tier, reason = "balanced", "intelligence_routine"
        required_capabilities = ()
    elif strategy == "cost":
        if features.complexity_score >= active_policy.cost_balanced_threshold or effort in {"xhigh", "max"}:
            target_tier, reason = "balanced", "cost_proxy_complexity"
        else:
            target_tier, reason = "fast", "cost_proxy_default"
        required_capabilities = ()
    elif features.constrained:
        target_tier, reason = "fast", "constrained"
        required_capabilities = ()
    elif features.complexity_score >= active_policy.balance_frontier_threshold or effort in {"xhigh", "max"}:
        target_tier, reason = "frontier", "complexity"
        required_capabilities = ()
    else:
        target_tier, reason = "balanced", "balance_default"
        required_capabilities = ()

    resolved_model = active_registry.resolve_tier(
        target_tier,
        role="direct",
        required_capabilities=required_capabilities,
    )

    return ModelDecision(
        model=resolved_model.model_id,
        target_tier=target_tier,
        required_capabilities=required_capabilities,
        reason=reason,
        strategy=strategy,
        effort=effort,
        prompt_chars=features.prompt_chars,
        criteria_count=features.criteria_count,
        high_risk_hits=features.risk_hits,
        risk_action_hits=features.risk_action_hits,
        complex_hits=features.complexity_hits,
        simple_hits=features.simple_hits,
        scope_hits=features.scope_hits,
        algorithm_hits=features.algorithm_hits,
        complexity_score=features.complexity_score,
        risk_score=features.risk_score,
        clarity_score=features.clarity_score,
        high_risk=features.high_risk,
        constrained=features.constrained,
        parallelizable=features.parallelizable,
        dependency_ambiguity=features.dependency_ambiguity,
        orchestration_eligible=features.orchestration_eligible,
        policy_version=active_policy.policy_version,
        policy_digest=policy_digest(active_policy),
        registry_digest=registry_digest(active_registry),
    )
