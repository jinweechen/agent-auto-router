# Benchmark-informed routing

`scripts/benchmark_priors.json` is a reviewed, versioned snapshot. The router never fetches the web at runtime. Every decision emits the snapshot version, date, digest, and applied signals.

The current exact GPT-5.6 model IDs have public coding, debugging, long-context, computer-use, security, price, and terminal-agent evidence. The policy uses multiple signals conservatively:

- deterministic validation plus bounded routine work may use `fast` with low effort;
- complex debugging, coordinated multi-file changes, and long-context work have a `balanced` floor;
- computer-use work has a `frontier` floor;
- high-risk classification remains authoritative and cannot be downgraded by benchmarks.

Scores are priors, not local acceptance results. Harness, tools, effort, prompt, repository, and benchmark defects can change outcomes. Generic Claude aliases are not assigned model-specific public scores because they do not pin a version.

To update the snapshot, verify primary model results plus an independent methodology source, preserve source URLs and exact model IDs, increment `version` and `asOf`, then run the registry validator, offline evaluation, and full tests. Never download or rewrite priors during routing, and never activate a learned policy without explicit approval.
