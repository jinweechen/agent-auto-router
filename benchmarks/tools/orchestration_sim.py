from __future__ import annotations

import random
from dataclasses import dataclass


SEED = 20260803
TASKS_PER_RUN = 20_000


@dataclass(frozen=True)
class TaskClass:
    name: str
    weight: float
    sol_success: float
    terra_success: float
    luna_success: float


TASK_CLASSES = (
    TaskClass("simple", 0.35, 0.995, 0.985, 0.970),
    TaskClass("medium", 0.30, 0.970, 0.940, 0.890),
    TaskClass("complex", 0.18, 0.920, 0.850, 0.740),
    TaskClass("ambiguous", 0.10, 0.850, 0.740, 0.580),
    TaskClass("high_risk", 0.07, 0.900, 0.800, 0.660),
)

# Illustrative normalized units, not API prices or measured latency.
MODEL_COST = {"sol": 6.0, "terra": 2.5, "luna": 1.0}
MODEL_LATENCY = {"sol": 9.0, "terra": 5.0, "luna": 3.0}
REVIEW_DETECTION = {"sol": 0.96, "terra": 0.88}
REPAIR_BONUS = 0.08
TERRA_ROUTING_FAILURE = 0.01


@dataclass
class Metrics:
    total: int = 0
    success: int = 0
    escaped_defects: int = 0
    human_interventions: int = 0
    retries: int = 0
    cost: float = 0.0
    latency: float = 0.0

    def row(self, name: str) -> str:
        return (
            f"{name:<28} "
            f"{self.success / self.total:>8.2%} "
            f"{self.escaped_defects / self.total:>8.2%} "
            f"{self.human_interventions / self.total:>8.2%} "
            f"{self.retries / self.total:>8.2f} "
            f"{self.cost / self.total:>8.2f} "
            f"{self.latency / self.total:>8.2f}"
        )


def choose_task(rng: random.Random) -> TaskClass:
    point = rng.random()
    cumulative = 0.0
    for task in TASK_CLASSES:
        cumulative += task.weight
        if point <= cumulative:
            return task
    return TASK_CLASSES[-1]


def attempt(rng: random.Random, probability: float) -> bool:
    return rng.random() < max(0.0, min(1.0, probability))


def single_model(rng: random.Random, task: TaskClass) -> tuple[bool, bool, bool, int, float, float]:
    ok = attempt(rng, task.sol_success)
    return ok, not ok, False, 0, MODEL_COST["sol"], MODEL_LATENCY["sol"]


def delegated(
    rng: random.Random,
    task: TaskClass,
    *,
    planner: str,
    reviewer: str,
    terra_routing_benefit: float = 0.0,
) -> tuple[bool, bool, bool, int, float, float]:
    plan_probability = task.sol_success if planner == "sol" else task.terra_success
    plan_ok = attempt(rng, plan_probability)

    cost = MODEL_COST[planner] + MODEL_COST["luna"] + MODEL_COST[reviewer]
    latency = MODEL_LATENCY[planner] + MODEL_LATENCY["luna"] + MODEL_LATENCY[reviewer]

    routing_ok = True
    if terra_routing_benefit:
        cost += MODEL_COST["terra"]
        latency += MODEL_LATENCY["terra"]
        routing_ok = attempt(rng, 1.0 - TERRA_ROUTING_FAILURE)

    execution_probability = task.luna_success + terra_routing_benefit
    execution_ok = plan_ok and routing_ok and attempt(rng, execution_probability)
    if execution_ok:
        return True, False, False, 0, cost, latency

    defect_detected = attempt(rng, REVIEW_DETECTION[reviewer])
    if not defect_detected:
        return False, True, False, 0, cost, latency

    retries = 1
    cost += MODEL_COST["luna"] + MODEL_COST[reviewer]
    latency += MODEL_LATENCY["luna"] + MODEL_LATENCY[reviewer]
    repaired = plan_ok and routing_ok and attempt(
        rng, execution_probability + REPAIR_BONUS
    )
    if repaired:
        return True, False, False, retries, cost, latency

    return False, False, True, retries, cost, latency


def run_strategy(name: str, terra_routing_benefit: float = 0.0) -> Metrics:
    rng = random.Random(f"{SEED}:{name}:{terra_routing_benefit}")
    metrics = Metrics()

    for _ in range(TASKS_PER_RUN):
        task = choose_task(rng)
        if name == "A Sol only":
            result = single_model(rng, task)
        elif name == "B Sol-Luna-Sol":
            result = delegated(rng, task, planner="sol", reviewer="sol")
        elif name == "C Sol-Terra-Luna-Sol":
            result = delegated(
                rng,
                task,
                planner="sol",
                reviewer="sol",
                terra_routing_benefit=terra_routing_benefit,
            )
        elif name == "D Terra-Luna-Terra":
            result = delegated(rng, task, planner="terra", reviewer="terra")
        else:
            raise ValueError(name)

        ok, escaped, human, retries, cost, latency = result
        metrics.total += 1
        metrics.success += int(ok)
        metrics.escaped_defects += int(escaped)
        metrics.human_interventions += int(human)
        metrics.retries += retries
        metrics.cost += cost
        metrics.latency += latency

    return metrics


def main() -> None:
    print("Synthetic orchestration simulation")
    print(f"seed={SEED}, tasks_per_variant={TASKS_PER_RUN}")
    print("All costs and latency values are normalized assumptions, not API measurements.\n")
    print(
        f"{'strategy':<28} {'success':>8} {'escaped':>8} {'human':>8} "
        f"{'retries':>8} {'cost':>8} {'latency':>8}"
    )
    print("-" * 92)
    print(run_strategy("A Sol only").row("A Sol only"))
    print(run_strategy("B Sol-Luna-Sol").row("B Sol-Luna-Sol"))
    print(run_strategy("D Terra-Luna-Terra").row("D Terra-Luna-Terra"))

    for benefit in (0.01, 0.03, 0.05, 0.08, 0.12):
        metrics = run_strategy("C Sol-Terra-Luna-Sol", benefit)
        label = f"C Terra benefit +{benefit:.0%}"
        print(metrics.row(label))


if __name__ == "__main__":
    main()
