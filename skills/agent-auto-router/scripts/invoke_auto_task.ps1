[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Task,
    [Alias('Model')]
    [string]$ModelChoice = 'auto',
    [ValidateSet('intelligence', 'balance', 'cost')]
    [string]$Strategy = 'balance',
    [ValidateSet('', 'none', 'low', 'medium', 'high', 'xhigh', 'max')]
    [string]$Effort = '',
    [ValidateSet('read-only', 'workspace-write', 'danger-full-access')]
    [string]$Sandbox = 'workspace-write',
    [ValidateSet('lean', 'full')]
    [string]$ContextMode = 'lean',
    [ValidateSet('cli', 'desktop')]
    [string]$ExecutionBackend = 'cli',
    [string[]]$DesktopAvailableModels = @(),
    [string]$Workdir = (Get-Location).Path,
    [string]$StateDir = $(if ($env:CODEX_AUTO_ROUTER_STATE_DIR) { $env:CODEX_AUTO_ROUTER_STATE_DIR } else { Join-Path $HOME '.codex\auto-router' }),
    [string]$FeedbackFile = '',
    [string[]]$ValidationCommand = @(),
    [switch]$EscalateOnValidationFailure,
    [switch]$NoFeedback,
    [switch]$DryRun,
    [switch]$Explain,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
$selectorPath = Join-Path $PSScriptRoot 'select_auto_model.py'
$learningPath = Join-Path $PSScriptRoot 'policy_learning.py'
$runnerPath = Join-Path $PSScriptRoot 'single_task_runner.py'
$desktopPath = Join-Path $PSScriptRoot 'desktop_execution.py'
if (-not (Test-Path -LiteralPath $selectorPath -PathType Leaf)) { throw "Auto model selector not found: $selectorPath" }
if (-not (Test-Path -LiteralPath $learningPath -PathType Leaf)) { throw "Policy learning helper not found: $learningPath" }
if ($ExecutionBackend -eq 'cli' -and -not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) { throw "Single task runner not found: $runnerPath" }
if ($ExecutionBackend -eq 'desktop' -and -not (Test-Path -LiteralPath $desktopPath -PathType Leaf)) { throw "Desktop execution planner not found: $desktopPath" }
if (-not (Test-Path -LiteralPath $Workdir -PathType Container)) { throw "Workdir not found: $Workdir" }
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) { throw 'Python 3 is required for deterministic Auto routing.' }
$codex = if ($ExecutionBackend -eq 'cli') { Get-Command codex -ErrorAction SilentlyContinue } else { $null }
if ($ExecutionBackend -eq 'cli' -and -not $codex -and -not $DryRun) { throw 'Codex CLI is required for ExecutionBackend=cli.' }
if ($ExecutionBackend -eq 'desktop' -and $DesktopAvailableModels.Count -eq 0) {
    throw 'ExecutionBackend=desktop requires -DesktopAvailableModels from the current Desktop runtime.'
}
if ($ExecutionBackend -eq 'desktop' -and ($EscalateOnValidationFailure -or $ValidationCommand.Count -gt 0)) {
    throw 'Desktop v1 emits one direct-agent plan and does not support validation-driven escalation.'
}
if ($ExecutionBackend -eq 'desktop' -and $ContextMode -ne 'lean') {
    throw 'Desktop v1 does not consume CLI ContextMode; use the default lean value.'
}
if ($ExecutionBackend -eq 'desktop' -and $FeedbackFile) {
    throw 'Desktop v1 cannot write execution feedback; -FeedbackFile is CLI-only.'
}
$resolvedWorkdir = (Resolve-Path -LiteralPath $Workdir).Path
if ($EscalateOnValidationFailure -and $ModelChoice -ne 'auto') {
    throw 'Validation-driven escalation requires -Model auto so the trusted route determines the next tier.'
}
if ($EscalateOnValidationFailure -and $ValidationCommand.Count -eq 0) {
    throw 'Validation-driven escalation requires -ValidationCommand as an argv array.'
}

$routeEffort = if ($Effort) { $Effort } else { 'auto' }
$previousOutputEncoding = $OutputEncoding
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
    $routeRaw = $Task | & $python.Source $selectorPath --strategy $Strategy --stdin --effort $routeEffort --state-dir $StateDir --model-choice $ModelChoice --workdir $resolvedWorkdir
    $routeExitCode = $LASTEXITCODE
} finally {
    $OutputEncoding = $previousOutputEncoding
}
if ($routeExitCode -ne 0) { throw 'Auto model selection failed.' }

$route = $routeRaw | ConvertFrom-Json
$routeId = [string]$route.routeId
$selectorModel = [string]$route.decision.model
$model = [string]$route.selectedModel
$resolvedEffort = $Effort
if (-not $resolvedEffort) {
    $resolvedEffort = if ($ModelChoice -eq 'auto') {
        [string]$route.executionPlan.effort
    } else {
        [string]$route.selectedDefaultEffort
    }
}
$explanation = [pscustomobject]@{
    executionBackend = $ExecutionBackend
    strategy = $Strategy
    model = $model
    effort = $resolvedEffort
    reason = if ($ModelChoice -eq 'auto') { $route.decision.reason } else { 'explicit_model' }
    selectorModel = $selectorModel
    targetTier = [string]$route.decision.target_tier
    selectedTier = [string]$route.selectedTier
    features = [pscustomobject]@{
        promptChars = $route.decision.prompt_chars
        highRiskHits = $route.decision.high_risk_hits
        complexHits = $route.decision.complex_hits
        simpleHits = $route.decision.simple_hits
    }
    routeId = $routeId
    policyVersion = [string]$route.policy.version
    policyDigest = [string]$route.policy.digest
    policySource = [string]$route.policy.source
    registryDigest = [string]$route.registry.digest
    executionPlan = $route.executionPlan
    modifiesCodexConfig = $false
    localProxyReceivesCredential = $false
    routeModelCalls = 0
}
if ($DryRun -and $ExecutionBackend -eq 'cli') { $explanation; return }
if ($Explain -and $ExecutionBackend -eq 'cli') { $explanation | ConvertTo-Json -Depth 6 | Write-Host }

if ($ExecutionBackend -eq 'desktop') {
    $desktopArguments = @($desktopPath, '--sandbox', $Sandbox, '--workdir', $resolvedWorkdir)
    foreach ($availableModel in $DesktopAvailableModels) {
        $desktopArguments += @('--available-model', $availableModel)
    }
    if ($DryRun) { $desktopArguments += '--dry-run' }
    $previousOutputEncoding = $OutputEncoding
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    try {
        $desktopPlanRaw = $routeRaw | & $python.Source @desktopArguments
        $desktopExitCode = $LASTEXITCODE
    } finally {
        $OutputEncoding = $previousOutputEncoding
    }
    if (-not $desktopPlanRaw) { throw 'Desktop execution planning failed without a plan.' }
    $desktopPlanRaw | Write-Output
    exit $desktopExitCode
}

$runnerResultPath = Join-Path ([System.IO.Path]::GetTempPath()) ("codex-auto-run-{0}.json" -f [guid]::NewGuid().ToString('N'))
$runnerArguments = @(
    $runnerPath, '--model', $model, '--effort', $resolvedEffort,
    '--sandbox', $Sandbox, '--context-mode', $ContextMode,
    '--workdir', $resolvedWorkdir,
    '--result-file', $runnerResultPath,
    '--repo-map-tokens', [int]$route.executionPlan.context.repoMapTokens,
    '--max-candidate-files', [int]$route.executionPlan.context.maxCandidateFiles
)
if ($Json) { $runnerArguments += '--emit-json' }

$previousOutputEncoding = $OutputEncoding
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
try {
    $Task | & $python.Source @runnerArguments
    $codexExitCode = $LASTEXITCODE
} finally {
    $stopwatch.Stop()
    $OutputEncoding = $previousOutputEncoding
}
$runnerResult = $null
if (Test-Path -LiteralPath $runnerResultPath -PathType Leaf) {
    try {
        $runnerResult = Get-Content -Raw -LiteralPath $runnerResultPath | ConvertFrom-Json
    } finally {
        Remove-Item -LiteralPath $runnerResultPath -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-RouteValidation {
    param([string[]]$Command, [string]$Directory)
    if ($Command.Count -eq 0) { return $null }
    $executable = $Command[0]
    $validationArgs = if ($Command.Count -gt 1) { $Command[1..($Command.Count - 1)] } else { @() }
    Push-Location -LiteralPath $Directory
    try {
        $commandInfo = Get-Command $executable -ErrorAction Stop
        & $executable @validationArgs
        $commandSucceeded = $?
        $externalExitCode = $LASTEXITCODE
        if (-not $commandSucceeded) { return 1 }
        if ($commandInfo.CommandType -in @('Application', 'ExternalScript')) {
            return [int]$externalExitCode
        }
        return 0
    } finally {
        Pop-Location
    }
}

$validationExitCode = $null
if ($codexExitCode -eq 0 -and $ValidationCommand.Count -gt 0) {
    $validationExitCode = Invoke-RouteValidation -Command $ValidationCommand -Directory $Workdir
}
$attemptCount = 1
$escalated = $false
$finalModel = $model
$finalEffort = $resolvedEffort
$attemptResults = @()
if ($runnerResult) { $attemptResults += $runnerResult }
$needsEscalation = $EscalateOnValidationFailure -and (
    $codexExitCode -eq 0 -and
    $null -ne $validationExitCode -and
    $validationExitCode -ne 0
)
if ($needsEscalation -and [bool]$route.executionPlan.escalation.eligible) {
    $nextModel = [string]$route.executionPlan.escalation.nextModel
    $nextEffort = [string]$route.executionPlan.escalation.nextEffort
    Write-Warning "Validation failed; explicitly enabled escalation is starting model=$nextModel effort=$nextEffort."
    $escalated = $true
    $attemptCount = 2
    $finalModel = $nextModel
    $finalEffort = $nextEffort
    $escalationResultPath = Join-Path ([System.IO.Path]::GetTempPath()) ("codex-auto-run-{0}.json" -f [guid]::NewGuid().ToString('N'))
    $escalationArguments = @(
        $runnerPath, '--model', $nextModel, '--effort', $nextEffort,
        '--sandbox', $Sandbox, '--context-mode', $ContextMode,
        '--workdir', (Resolve-Path -LiteralPath $Workdir).Path,
        '--result-file', $escalationResultPath,
        '--repo-map-tokens', [int]$route.executionPlan.context.repoMapTokens,
        '--max-candidate-files', [int]$route.executionPlan.context.maxCandidateFiles
    )
    if ($Json) { $escalationArguments += '--emit-json' }
    $escalationTask = "$Task`n`nA previous lower-tier attempt did not pass the user-provided deterministic validation. Inspect the current workspace state, fix the remaining issue, and run relevant validation."
    $escalationTask | & $python.Source @escalationArguments
    $codexExitCode = $LASTEXITCODE
    $escalationResult = $null
    if (Test-Path -LiteralPath $escalationResultPath -PathType Leaf) {
        try {
            $escalationResult = Get-Content -Raw -LiteralPath $escalationResultPath | ConvertFrom-Json
        } finally {
            Remove-Item -LiteralPath $escalationResultPath -Force -ErrorAction SilentlyContinue
        }
    }
    if ($escalationResult) { $attemptResults += $escalationResult }
    $validationExitCode = $null
    if ($codexExitCode -eq 0) {
        $validationExitCode = Invoke-RouteValidation -Command $ValidationCommand -Directory $Workdir
    }
}
$finalExitCode = $codexExitCode
if ($finalExitCode -eq 0 -and $null -ne $validationExitCode) {
    $finalExitCode = [int]$validationExitCode
}
$observedInput = 0L
$observedCached = 0L
$observedOutput = 0L
$observedReasoning = 0L
$usageAvailable = $false
foreach ($attempt in $attemptResults) {
    if ($attempt.usageAvailable) {
        $usageAvailable = $true
        $observedInput += [int64]$attempt.observedTokens.input
        $observedCached += [int64]$attempt.observedTokens.cached_input
        $observedOutput += [int64]$attempt.observedTokens.output
        $observedReasoning += [int64]$attempt.observedTokens.reasoning_output
    }
}

if (-not $NoFeedback) {
    $resolvedFeedbackFile = if ($FeedbackFile) { $FeedbackFile } else { Join-Path $StateDir 'feedback.jsonl' }
    $feedbackPayload = [ordered]@{
        route_id = $routeId
        strategy = $Strategy
        effort = $finalEffort
        selector_model = $selectorModel
        selected_model = $finalModel
        target_tier = [string]$route.decision.target_tier
        reason = if ($ModelChoice -eq 'auto') { [string]$route.decision.reason } else { 'explicit_model' }
        features = [ordered]@{
            prompt_chars = [int]$route.decision.prompt_chars
            criteria_count = [int]$route.decision.criteria_count
            complexity_score = [int]$route.decision.complexity_score
            risk_score = [int]$route.decision.risk_score
            clarity_score = [int]$route.decision.clarity_score
            high_risk = [bool]$route.decision.high_risk
            constrained = [bool]$route.decision.constrained
            parallelizable = [bool]$route.decision.parallelizable
            dependency_ambiguity = [bool]$route.decision.dependency_ambiguity
            orchestration_eligible = [bool]$route.decision.orchestration_eligible
            scope_hits = [int]$route.decision.scope_hits
            algorithm_hits = [int]$route.decision.algorithm_hits
            repo_files = [int]$route.repository.repo_files
            source_files = [int]$route.repository.source_files
            test_files = [int]$route.repository.test_files
            language_count = [int]$route.repository.language_count
            manifest_count = [int]$route.repository.manifest_count
            large_repo = [bool]$route.repository.large_repo
            monorepo = [bool]$route.repository.monorepo
            dirty_worktree = [bool]$route.repository.dirty_worktree
            is_git_repo = [bool]$route.repository.is_git_repo
            task_has_path_hint = [bool]$route.repository.task_has_path_hint
            validation_configured = ($ValidationCommand.Count -gt 0)
            validation_passed = ($null -ne $validationExitCode -and $validationExitCode -eq 0)
            escalated = $escalated
        }
        policy_version = [string]$route.policy.version
        policy_digest = [string]$route.policy.digest
        registry_digest = [string]$route.registry.digest
        explicit_override = ($ModelChoice -ne 'auto')
        exit_code = [int]$finalExitCode
        validation_configured = ($ValidationCommand.Count -gt 0)
        validation_passed = if ($null -eq $validationExitCode) { $null } else { $validationExitCode -eq 0 }
        escalated = $escalated
        attempt_count = $attemptCount
        duration_ms = [int64](($attemptResults | Measure-Object -Property durationMs -Sum).Sum)
        observed_tokens = if ($usageAvailable) {
            [ordered]@{
                input = $observedInput
                cached_input = $observedCached
                output = $observedOutput
                reasoning_output = $observedReasoning
                total = $observedInput + $observedOutput
            }
        } else { $null }
    }
    $feedbackJson = $feedbackPayload | ConvertTo-Json -Depth 8 -Compress
    $previousOutputEncoding = $OutputEncoding
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    try {
        $recordOutput = $feedbackJson | & $python.Source $learningPath record --state-dir $StateDir --feedback-file $resolvedFeedbackFile --stdin
        $recordExitCode = $LASTEXITCODE
    } finally {
        $OutputEncoding = $previousOutputEncoding
    }
    if ($recordExitCode -ne 0) {
        Write-Warning "Task completed, but route feedback could not be recorded. Route ID: $routeId"
    } elseif ($Explain) {
        Write-Host "Route feedback recorded. Route ID: $routeId"
    }
}
exit $finalExitCode
