[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Task,
    [ValidateSet('intelligence', 'balance', 'cost')]
    [string]$Strategy = 'balance',
    [ValidateSet('auto', 'A', 'B', 'C', 'D', 'E', 'F')]
    [string]$Variant = 'auto',
    [ValidateSet('', 'none', 'low', 'medium', 'high', 'xhigh', 'max')]
    [string]$Effort = '',
    [string[]]$AcceptanceCriteria = @(),
    [ValidateRange(1, 32)]
    [int]$MaxWorkers = 2,
    [ValidateRange(1, 86400)]
    [int]$Timeout = 600,
    [ValidateRange(1, 86400)]
    [int]$TotalTimeout = 1800,
    [ValidateRange(1, 100)]
    [int]$MaxModelCalls = 7,
    [ValidateRange(0, 2147483647)]
    [int]$MaxTotalTokens = 0,
    [ValidateSet('auto', 'always', 'never')]
    [string]$GraderPolicy = 'auto',
    [ValidateSet('lean', 'full')]
    [string]$ContextMode = 'lean',
    [ValidateSet('read-only', 'workspace-write')]
    [string]$Sandbox = 'workspace-write',
    [string]$Workdir = (Get-Location).Path,
    [string]$ResultsDir = '',
    [ValidateSet('', 'none', 'low', 'medium', 'high', 'xhigh', 'max')]
    [string]$PlannerEffort = '',
    [ValidateSet('', 'none', 'low', 'medium', 'high', 'xhigh', 'max')]
    [string]$DispatcherEffort = '',
    [ValidateSet('', 'none', 'low', 'medium', 'high', 'xhigh', 'max')]
    [string]$WorkerEffort = '',
    [ValidateSet('', 'none', 'low', 'medium', 'high', 'xhigh', 'max')]
    [string]$ReviewerEffort = '',
    [ValidateSet('', 'none', 'low', 'medium', 'high', 'xhigh', 'max')]
    [string]$GraderEffort = '',
    [switch]$DryRun,
    [switch]$AllowDirty,
    [switch]$AllowNoChanges,
    [switch]$Explain,
    [switch]$Json
    ,[switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot 'invoke_orchestrated_task.py'
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw "Orchestration runner not found: $runner"
}
if (-not (Test-Path -LiteralPath $Workdir -PathType Container)) {
    throw "Workdir not found: $Workdir"
}
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) { throw 'Python 3 is required for multi-model orchestration.' }

$arguments = @(
    $runner, '--stdin', '--strategy', $Strategy, '--variant', $Variant,
    '--max-workers', $MaxWorkers, '--timeout', $Timeout,
    '--total-timeout', $TotalTimeout, '--max-model-calls', $MaxModelCalls,
    '--grader-policy', $GraderPolicy,
    '--context-mode', $ContextMode,
    '--sandbox', $Sandbox, '--workdir', (Resolve-Path -LiteralPath $Workdir).Path
)
if ($Effort) { $arguments += @('--effort', $Effort) }
if ($MaxTotalTokens -gt 0) { $arguments += @('--max-total-tokens', $MaxTotalTokens) }
if ($ResultsDir) { $arguments += @('--results-dir', $ResultsDir) }
foreach ($roleEffort in @(
    @('planner', $PlannerEffort), @('dispatcher', $DispatcherEffort),
    @('worker', $WorkerEffort), @('reviewer', $ReviewerEffort),
    @('grader', $GraderEffort)
)) {
    if ($roleEffort[1]) { $arguments += @("--$($roleEffort[0])-effort", $roleEffort[1]) }
}
foreach ($criterion in $AcceptanceCriteria) {
    $arguments += @('--acceptance-criterion', $criterion)
}
if ($DryRun) { $arguments += '--dry-run' }
if ($AllowDirty) { $arguments += '--allow-dirty' }
if ($AllowNoChanges) { $arguments += '--allow-no-changes' }
if ($Explain) { $arguments += '--explain' }
if ($Json) { $arguments += '--json' }
if ($Quiet) { $arguments += '--no-progress' }

$previousOutputEncoding = $OutputEncoding
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
    $Task | & $python.Source @arguments
    $exitCode = $LASTEXITCODE
}
finally {
    $OutputEncoding = $previousOutputEncoding
}
exit $exitCode
