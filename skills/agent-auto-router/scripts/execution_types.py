from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CallRecord:
    role: str
    model: str
    effort: str
    latency_seconds: float
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float | None
    response_id: str
    cached_input_tokens: int = 0
    reasoning_output_tokens: int = 0


@dataclass
class RunContext:
    records: list[CallRecord] = field(default_factory=list)

    @property
    def total_cost(self) -> float | None:
        costs = [record.estimated_cost_usd for record in self.records]
        if not costs or any(cost is None for cost in costs):
            return None
        return sum(cost for cost in costs if cost is not None)

    @property
    def total_latency(self) -> float:
        return sum(record.latency_seconds for record in self.records)

    @property
    def total_input_tokens(self) -> int:
        return sum(record.input_tokens for record in self.records)

    @property
    def total_output_tokens(self) -> int:
        return sum(record.output_tokens for record in self.records)

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def total_cached_input_tokens(self) -> int:
        return sum(record.cached_input_tokens for record in self.records)

    @property
    def total_uncached_input_tokens(self) -> int:
        return max(0, self.total_input_tokens - self.total_cached_input_tokens)

    @property
    def total_reasoning_output_tokens(self) -> int:
        return sum(record.reasoning_output_tokens for record in self.records)
