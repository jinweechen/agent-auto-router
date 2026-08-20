[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Task,
    [ValidateSet('intelligence', 'balance', 'cost')]
    [string]$Strategy = 'balance',
    [ValidateSet('auto', 'A', 'B', 'C', 'D', 'E', 'F')]
    [string]$Variant = 'auto',
    [ValidateSet('direct', 'recommend', 'auto')]
    [string]$OrchestrationPolicy = 'auto',
    [ValidateSet('session', 'sticky', 'auto', 'off')]
    [string]$ModelAffinity = 'auto',
    [string]$ConversationKeyHash = '',
    [string]$PinnedModel = '',
    [ValidateSet('', 'none', 'low', 'medium', 'high', 'xhigh', 'max')]
    [string]$PinnedEffort = '',
    [ValidateRange(-1, 2147483647)]
    [int]$PinTurns = -1,
    [ValidateRange(-1, 2147483647)]
    [int]$LastSwitchAgeSeconds = -1,
    [switch]$CheckpointReached,
    [switch]$ConfirmPinDowngrade,
    [string[]]$AvailableModels = @(),
    [switch]$ConfirmHighRiskOrchestration,
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
    [ValidateSet('adaptive', 'auto', 'off')]
    [string]$RepositoryContextMode = 'auto',
    [ValidateSet('inherit', 'read-only', 'workspace-write', 'danger-full-access')]
    [string]$Sandbox = 'inherit',
    [string]$HostPermissionsJson = '',
    [string]$Backend = '',
    [string]$Workdir = (Get-Location).Path,
    [string]$ResultsDir = '',
    [switch]$IncludeOutputInReport,
    [string]$StateDir = $(if ($env:CODEX_AUTO_ROUTER_STATE_DIR) { $env:CODEX_AUTO_ROUTER_STATE_DIR } else { Join-Path $HOME '.codex\auto-router' }),
    [string]$FeedbackFile = '',
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
    [switch]$Json,
    [switch]$Quiet,
    [switch]$NoFeedback
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
if (-not $DryRun -and -not $HostPermissionsJson -and $Sandbox -eq 'inherit') {
    throw 'Automatic permission inheritance requires -HostPermissionsJson from the current host runtime.'
}
if ($IncludeOutputInReport -and -not $ResultsDir) {
    throw '-IncludeOutputInReport requires -ResultsDir.'
}
if ($ModelAffinity -eq 'sticky') {
    if ($ConversationKeyHash -notmatch '^[0-9a-f]{64}$' -or -not $PinnedModel) {
        throw '-ModelAffinity sticky requires a lowercase HMAC-SHA256 -ConversationKeyHash and -PinnedModel.'
    }
} elseif ($ConversationKeyHash -or $PinnedModel -or $PinnedEffort -or $PinTurns -ge 0 -or $LastSwitchAgeSeconds -ge 0 -or $CheckpointReached -or $ConfirmPinDowngrade) {
    throw 'Conversation pin state requires -ModelAffinity sticky.'
}

$arguments = @(
    $runner, '--stdin', '--strategy', $Strategy, '--variant', $Variant,
    '--orchestration-policy', $OrchestrationPolicy, '--model-affinity', $ModelAffinity,
    '--max-workers', $MaxWorkers, '--timeout', $Timeout,
    '--total-timeout', $TotalTimeout, '--max-model-calls', $MaxModelCalls,
    '--grader-policy', $GraderPolicy,
    '--context-mode', $ContextMode,
    '--repository-context', $RepositoryContextMode,
    '--state-dir', $StateDir,
    '--sandbox', $Sandbox, '--workdir', (Resolve-Path -LiteralPath $Workdir).Path
)
if ($HostPermissionsJson) { $arguments += @('--host-permissions-json', $HostPermissionsJson) }
if ($Effort) { $arguments += @('--effort', $Effort) }
if ($Backend) { $arguments += @('--backend', $Backend) }
if ($MaxTotalTokens -gt 0) { $arguments += @('--max-total-tokens', $MaxTotalTokens) }
if ($ResultsDir) { $arguments += @('--results-dir', $ResultsDir) }
if ($IncludeOutputInReport) { $arguments += '--include-output-in-report' }
if ($FeedbackFile) { $arguments += @('--feedback-file', $FeedbackFile) }
if ($ModelAffinity -eq 'sticky') {
    $arguments += @('--conversation-key-hash', $ConversationKeyHash, '--pinned-model', $PinnedModel)
    if ($PinnedEffort) { $arguments += @('--pinned-effort', $PinnedEffort) }
    if ($PinTurns -ge 0) { $arguments += @('--pin-turns', $PinTurns) }
    if ($LastSwitchAgeSeconds -ge 0) { $arguments += @('--last-switch-age-seconds', $LastSwitchAgeSeconds) }
    if ($CheckpointReached) { $arguments += '--checkpoint-reached' }
    if ($ConfirmPinDowngrade) { $arguments += '--confirm-pin-downgrade' }
}
foreach ($availableModel in $AvailableModels) {
    $arguments += @('--available-model', $availableModel)
}
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
if ($ConfirmHighRiskOrchestration) { $arguments += '--confirm-high-risk-orchestration' }
if ($AllowDirty) { $arguments += '--allow-dirty' }
if ($AllowNoChanges) { $arguments += '--allow-no-changes' }
if ($Explain) { $arguments += '--explain' }
if ($Json) { $arguments += '--json' }
if ($Quiet) { $arguments += '--no-progress' }
if ($NoFeedback) { $arguments += '--no-feedback' }

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
