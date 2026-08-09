from __future__ import annotations

import re
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from benchmark_priors import (
    BenchmarkPriors,
    benchmark_priors_digest,
    load_benchmark_priors,
)
from model_registry import (
    EFFORTS,
    TIER_RANK,
    ModelRegistry,
    load_model_registry,
    registry_digest,
)

DEFAULT_REGISTRY = load_model_registry()
STRATEGIES = ("intelligence", "balance", "cost")
POLICY_SCHEMA_VERSION = 2
LEGACY_POLICY_SCHEMA_VERSION = 1
DEFAULT_POLICY_VERSION = "builtin-v3"
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
DEBUGGING_TERMS = (
    "debug", "debugging", "diagnose", "root cause", "flaky", "failing test",
    "test failure", "regression failure", "崩溃原因", "调试", "诊断", "根因",
    "失败测试", "回归失败", "偶发失败",
)
LONG_CONTEXT_TERMS = (
    "large codebase", "large repository", "repository-wide", "codebase-wide",
    "long context", "many files", "multi-module", "cross-module", "monorepo",
    "大型代码库", "大型仓库", "全仓库", "长上下文", "大量文件", "多模块",
    "跨模块",
)
MULTI_FILE_TERMS = (
    "multi-file", "cross-file", "multiple files", "multiple modules",
    "several files", "several modules", "across files", "across modules",
    "多文件", "跨文件", "多个文件", "多个模块", "协调修改",
)
COMPUTER_USE_TERMS = (
    "computer use", "browser automation", "desktop automation", "gui automation",
    "click through", "control the browser", "control the desktop", "操作浏览器",
    "控制浏览器", "桌面自动化", "界面自动化", "点击界面", "操作桌面",
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
    debugging_hits: int
    long_context_hits: int
    multi_file_hits: int
    computer_use_hits: int
    complexity_score: int
    risk_score: int
    clarity_score: int
    high_risk: bool
    constrained: bool
    parallelizable: bool
    dependency_ambiguity: bool
    orchestration_eligible: bool
    complex_debugging: bool
    long_context: bool
    multi_file: bool
    computer_use: bool
    validation_configured: bool
    validated_bounded: bool


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
    debugging_hits: int
    long_context_hits: int
    multi_file_hits: int
    computer_use_hits: int
    complexity_score: int
    risk_score: int
    clarity_score: int
    high_risk: bool
    constrained: bool
    parallelizable: bool
    dependency_ambiguity: bool
    orchestration_eligible: bool
    complex_debugging: bool
    long_context: bool
    multi_file: bool
    computer_use: bool
    validation_configured: bool
    validated_bounded: bool
    policy_version: str
    policy_digest: str
    registry_digest: str
    benchmark_prior_version: str
    benchmark_prior_as_of: str
    benchmark_prior_digest: str
    benchmark_signals: tuple[str, ...]


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


def load_policy_for_route(
    state_dir: Path | None,
    route_id: str,
    *,
    registry_digest_value: str | None = None,
    benchmark_priors_digest_value: str | None = None,
) -> tuple[RoutingPolicy, str]:
    """Select the active or guarded canary policy for one opaque route ID."""
    active, source = load_active_policy(state_dir)
    if not route_id or len(route_id) > 200:
        raise ValueError("route_id must be a non-empty opaque identifier")
    root = active_policy_path(state_dir).parent
    config_path = root / "guarded-auto-config.json"
    state_path = root / "guarded-auto-state.json"
    if not config_path.is_file() or not state_path.is_file():
        return active, source
    config = json.loads(config_path.read_text(encoding="utf-8"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not isinstance(state, dict):
        raise ValueError("guarded-auto configuration and state must be objects")
    if config.get("mode") != "guarded-auto" or state.get("status") != "canary":
        return active, source
    if state.get("basePolicyDigest") != policy_digest(active):
        raise ValueError("active policy changed during guarded-auto canary")
    canary_percent = state.get("canaryPercent")
    if isinstance(canary_percent, bool) or not isinstance(canary_percent, int):
        raise ValueError("guarded-auto canaryPercent must be an integer")
    if not 1 <= canary_percent <= 50:
        raise ValueError("guarded-auto canaryPercent must be between 1 and 50")

    candidate_root = (root / "candidates").resolve()
    candidate_path = Path(str(state.get("candidatePath", ""))).resolve()
    if candidate_path.parent != candidate_root or not candidate_path.is_file():
        raise ValueError("guarded-auto candidate path is outside the candidate directory")
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    if not isinstance(candidate, dict):
        raise ValueError("guarded-auto candidate must be an object")
    candidate_id = candidate.get("candidateId")
    unsigned = dict(candidate)
    unsigned.pop("candidateId", None)
    canonical = json.dumps(
        unsigned, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if not isinstance(candidate_id, str) or candidate_id != hashlib.sha256(canonical).hexdigest():
        raise ValueError("guarded-auto candidate integrity check failed")
    if candidate_id != state.get("candidateId"):
        raise ValueError("guarded-auto candidate identity changed")
    if registry_digest_value is not None and candidate.get("modelRegistryDigest") != registry_digest_value:
        raise ValueError("guarded-auto candidate is stale because the registry changed")
    if (
        benchmark_priors_digest_value is not None
        and candidate.get("benchmarkPriorsDigest") != benchmark_priors_digest_value
    ):
        raise ValueError("guarded-auto candidate is stale because benchmark priors changed")
    candidate_policy = policy_from_dict(candidate.get("policy", {}))
    if policy_digest(candidate_policy) != state.get("candidatePolicyDigest"):
        raise ValueError("guarded-auto candidate policy digest changed")
    bucket = int(hashlib.sha256(route_id.encode("utf-8")).hexdigest()[:8], 16) % 100
    if bucket < canary_percent:
        return candidate_policy, f"guarded-auto-canary:{candidate_id}"
    return active, source


def _count_hits(text: str, terms: Sequence[str]) -> int:
    return sum(1 for term in terms if term in text)


def analyze_task(
    prompt: str,
    acceptance_criteria: Sequence[str] | None = None,
    repository_features: dict[str, object] | None = None,
    validation_configured: bool = False,
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
    debugging_hits = _count_hits(text, DEBUGGING_TERMS)
    long_context_hits = _count_hits(text, LONG_CONTEXT_TERMS)
    multi_file_hits = _count_hits(text, MULTI_FILE_TERMS)
    computer_use_hits = _count_hits(text, COMPUTER_USE_TERMS)
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
        complexity_score >= 2
        or criteria_count >= 3
        or len(prompt) >= 900
        or debugging_hits > 0
        or long_context_hits > 0
        or multi_file_hits > 0
    )
    long_context = (
        long_context_hits > 0
        or len(prompt) >= 6000
        or (bool(repo.get("large_repo")) and not constrained)
        or (bool(repo.get("monorepo")) and scope_hits > 0)
    )
    complex_debugging = debugging_hits > 0
    multi_file = multi_file_hits > 0
    computer_use = computer_use_hits > 0
    validated_bounded = (
        validation_configured
        and not high_risk
        and not long_context
        and not complex_debugging
        and not multi_file
        and not computer_use
        and complexity_score <= 1
        and scope_hits == 0
        and algorithm_hits == 0
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
        debugging_hits=debugging_hits,
        long_context_hits=long_context_hits,
        multi_file_hits=multi_file_hits,
        computer_use_hits=computer_use_hits,
        complexity_score=complexity_score,
        risk_score=risk_score,
        clarity_score=clarity_score,
        high_risk=high_risk,
        constrained=constrained,
        parallelizable=parallelizable,
        dependency_ambiguity=dependency_ambiguity,
        orchestration_eligible=orchestration_eligible,
        complex_debugging=complex_debugging,
        long_context=long_context,
        multi_file=multi_file,
        computer_use=computer_use,
        validation_configured=validation_configured,
        validated_bounded=validated_bounded,
    )


def select_model(
    prompt: str,
    strategy: str = "balance",
    effort: str = "medium",
    acceptance_criteria: Sequence[str] | None = None,
    policy: RoutingPolicy | None = None,
    registry: ModelRegistry | None = None,
    repository_features: dict[str, object] | None = None,
    backends: Sequence[str] | None = None,
    allow_explicit_only: bool = False,
    validation_configured: bool = False,
    benchmark_priors: BenchmarkPriors | None = None,
) -> ModelDecision:
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy: {strategy}")
    if effort not in EFFORTS:
        raise ValueError(f"unknown effort: {effort}")

    active_policy = policy or RoutingPolicy()
    active_registry = registry or DEFAULT_REGISTRY
    active_priors = benchmark_priors or load_benchmark_priors(registry=active_registry)
    features = analyze_task(
        prompt,
        acceptance_criteria,
        repository_features,
        validation_configured=validation_configured,
    )
    benchmark_signals: list[str] = []
    if features.high_risk:
        target_tier, reason = "frontier", "high_risk"
        required_capabilities = ("high-risk-primary",)
    elif features.computer_use:
        rule = active_priors.guidance("computerUse")
        target_tier, reason = rule["minimumTier"], rule["reason"]
        required_capabilities = ()
        benchmark_signals.append("computerUse")
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
    elif features.validated_bounded:
        rule = active_priors.guidance("validatedBoundedCoding")
        target_tier, reason = rule["recommendedTier"], rule["reason"]
        required_capabilities = ()
        benchmark_signals.append("validatedBoundedCoding")
    elif features.constrained:
        target_tier, reason = "fast", "constrained"
        required_capabilities = ()
    elif features.complexity_score >= active_policy.balance_frontier_threshold or effort in {"xhigh", "max"}:
        target_tier, reason = "frontier", "complexity"
        required_capabilities = ()
    else:
        target_tier, reason = "balanced", "balance_default"
        required_capabilities = ()

    for active, signal in (
        (features.complex_debugging, "complexDebugging"),
        (features.long_context, "longContext"),
        (features.multi_file, "multiFile"),
    ):
        if not active:
            continue
        rule = active_priors.guidance(signal)
        benchmark_signals.append(signal)
        minimum_tier = rule["minimumTier"]
        if TIER_RANK[target_tier] < TIER_RANK[minimum_tier]:
            target_tier, reason = minimum_tier, rule["reason"]

    resolved_model = active_registry.resolve_tier(
        target_tier,
        role="direct",
        required_capabilities=required_capabilities,
        backends=backends,
        allow_explicit_only=allow_explicit_only,
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
        debugging_hits=features.debugging_hits,
        long_context_hits=features.long_context_hits,
        multi_file_hits=features.multi_file_hits,
        computer_use_hits=features.computer_use_hits,
        complexity_score=features.complexity_score,
        risk_score=features.risk_score,
        clarity_score=features.clarity_score,
        high_risk=features.high_risk,
        constrained=features.constrained,
        parallelizable=features.parallelizable,
        dependency_ambiguity=features.dependency_ambiguity,
        orchestration_eligible=features.orchestration_eligible,
        complex_debugging=features.complex_debugging,
        long_context=features.long_context,
        multi_file=features.multi_file,
        computer_use=features.computer_use,
        validation_configured=features.validation_configured,
        validated_bounded=features.validated_bounded,
        policy_version=active_policy.policy_version,
        policy_digest=policy_digest(active_policy),
        registry_digest=registry_digest(active_registry),
        benchmark_prior_version=active_priors.version,
        benchmark_prior_as_of=active_priors.as_of,
        benchmark_prior_digest=benchmark_priors_digest(active_priors),
        benchmark_signals=tuple(dict.fromkeys(benchmark_signals)),
    )
