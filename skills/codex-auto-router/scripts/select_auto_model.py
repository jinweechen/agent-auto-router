#!/usr/bin/env python3
"""Select a GPT-5.6 model without changing Codex configuration."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass

MODELS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
STRATEGIES = ("intelligence", "balance", "cost")
EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")

HIGH_RISK = re.compile(
    r"\b(auth(?:entication|orization)?|security|credential|secret|token|production|prod|"
    r"data loss|delete|drop|irreversible|payment|billing|permission|migration|compliance|"
    r"vulnerability|exploit|incident)\b|认证|鉴权|安全|凭据|密钥|令牌|生产|删除|迁移|支付|权限|漏洞|事故",
    re.I,
)
COMPLEX = re.compile(
    r"\b(architect(?:ure)?|redesign|refactor|distributed|concurren|race condition|deadlock|"
    r"multi[- ]?(?:module|service|agent)|dependency|ambiguous|tradeoff|root cause|end[- ]to[- ]end|"
    r"orchestrat|review|audit)\b|架构|重构|分布式|并发|竞态|死锁|多模块|多服务|依赖|歧义|权衡|根因|编排|审查|验收",
    re.I,
)
SIMPLE = re.compile(
    r"\b(extract|format|rename|translate|summari[sz]e|classify|boilerplate|regex|"
    r"single file|exactly|reply with|convert|sort|deduplicate|typo)\b|提取|格式化|重命名|翻译|摘要|分类|单文件|精确回复|转换|排序|去重|错别字",
    re.I,
)

@dataclass(frozen=True)
class Decision:
    model: str
    reason: str
    strategy: str
    effort: str
    prompt_chars: int
    high_risk_hits: int
    complex_hits: int
    simple_hits: int

def select_model(prompt: str, strategy: str = "balance", effort: str = "medium") -> Decision:
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy: {strategy}")
    if effort not in EFFORTS:
        raise ValueError(f"unknown effort: {effort}")

    high = len(HIGH_RISK.findall(prompt))
    complex_count = len(COMPLEX.findall(prompt))
    simple = len(SIMPLE.findall(prompt))
    size_complex = len(prompt) >= 6000 or prompt.count("\n") >= 100

    if high:
        model, reason = "gpt-5.6-sol", "high_risk"
    elif strategy == "intelligence":
        if complex_count or size_complex or effort in {"xhigh", "max"}:
            model, reason = "gpt-5.6-sol", "complexity"
        else:
            model, reason = "gpt-5.6-terra", "intelligence_routine"
    elif strategy == "cost":
        if complex_count or size_complex or effort in {"xhigh", "max"}:
            model, reason = "gpt-5.6-terra", "cost_complexity"
        else:
            model, reason = "gpt-5.6-luna", "cost_default"
    elif simple and len(prompt) <= 3000 and not complex_count:
        model, reason = "gpt-5.6-luna", "constrained"
    elif complex_count or size_complex or effort in {"xhigh", "max"}:
        model, reason = "gpt-5.6-sol", "complexity"
    else:
        model, reason = "gpt-5.6-terra", "balance_default"

    return Decision(model, reason, strategy, effort, len(prompt), high, complex_count, simple)

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", choices=STRATEGIES, default="balance")
    parser.add_argument("--effort", choices=EFFORTS, default="medium")
    route_input = parser.add_mutually_exclusive_group(required=True)
    route_input.add_argument("--text")
    route_input.add_argument("--stdin", action="store_true")
    args = parser.parse_args()
    prompt = sys.stdin.read() if args.stdin else args.text
    decision = select_model(prompt or "", args.strategy, args.effort)
    print(json.dumps({"decision": asdict(decision), "modelCalls": 0}, ensure_ascii=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
