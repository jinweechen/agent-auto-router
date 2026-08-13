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
    [ValidateSet('inherit', 'read-only', 'workspace-write', 'danger-full-access')]
    [string]$Sandbox = 'inherit',
    [ValidateSet('lean', 'full')]
    [string]$ContextMode = 'lean',
    [ValidateSet('auto', 'off')]
    [string]$RepositoryContextMode = 'off',
    [ValidateSet('direct', 'recommend', 'auto')]
    [string]$OrchestrationPolicy = 'direct',
    [ValidateSet('auto', 'off')]
    [string]$ModelAffinity = 'off',
    [switch]$ConfirmHighRiskOrchestration,
    [ValidateSet('cli', 'desktop')]
    [string]$ExecutionBackend = 'cli',
    [string[]]$DesktopAvailableModels = @(),
    [int]$DesktopMaxParallelChildren = 0,
    [string]$HostPermissionsJson = '',
    [string]$Workdir = (Get-Location).Path,
    [string]$StateDir = $(if ($env:CODEX_AUTO_ROUTER_STATE_DIR) { $env:CODEX_AUTO_ROUTER_STATE_DIR } else { Join-Path $HOME '.codex\auto-router' }),
    [string]$FeedbackFile = '',
    [string[]]$ValidationCommand = @(),
    [switch]$EscalateOnValidationFailure,
    [switch]$EnableLearningPolicy,
    [switch]$EnableFeedback,
    [switch]$NoFeedback,
    [switch]$DryRun,
    [switch]$Explain,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
$selectorPath = Join-Path $PSScriptRoot 'select_auto_model.py'
$learningPath = Join-Path $PSScriptRoot 'policy_learning.py'
$guardedPath = Join-Path $PSScriptRoot 'guarded_auto.py'
$runnerPath = Join-Path $PSScriptRoot 'single_task_runner.py'
$desktopPath = Join-Path $PSScriptRoot 'desktop_execution.py'
if (-not (Test-Path -LiteralPath $selectorPath -PathType Leaf)) { throw "Auto model selector not found: $selectorPath" }
if (-not (Test-Path -LiteralPath $learningPath -PathType Leaf)) { throw "Policy learning helper not found: $learningPath" }
if (-not (Test-Path -LiteralPath $guardedPath -PathType Leaf)) { throw "Guarded learning helper not found: $guardedPath" }
if ($ExecutionBackend -eq 'cli' -and -not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) { throw "Single task runner not found: $runnerPath" }
if ($ExecutionBackend -eq 'desktop' -and -not (Test-Path -LiteralPath $desktopPath -PathType Leaf)) { throw "Desktop execution planner not found: $desktopPath" }
if (-not (Test-Path -LiteralPath $Workdir -PathType Container)) { throw "Workdir not found: $Workdir" }
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) { throw 'Python 3 is required for deterministic Auto routing.' }
if ($ExecutionBackend -eq 'desktop' -and $DesktopAvailableModels.Count -eq 0) {
    throw 'ExecutionBackend=desktop requires -DesktopAvailableModels from the current Desktop runtime.'
}
if ($ExecutionBackend -eq 'desktop' -and ($DesktopMaxParallelChildren -lt 1 -or $DesktopMaxParallelChildren -gt 32)) {
    throw 'ExecutionBackend=desktop requires -DesktopMaxParallelChildren between 1 and 32 from the current Desktop runtime.'
}
if ($ExecutionBackend -eq 'desktop' -and ($EscalateOnValidationFailure -or $ValidationCommand.Count -gt 0)) {
    throw 'Desktop v3 emits a bounded staged-agent plan and does not support validation-driven escalation.'
}
if ($ExecutionBackend -eq 'desktop' -and $ContextMode -ne 'lean') {
    throw 'Desktop v3 does not consume CLI ContextMode; use the default lean value.'
}
if ($ExecutionBackend -eq 'desktop' -and $FeedbackFile) {
    throw 'Desktop v3 cannot write execution feedback; -FeedbackFile is CLI-only.'
}
if ($EnableFeedback -and $NoFeedback) {
    throw '-EnableFeedback and -NoFeedback cannot be used together.'
}
if ($FeedbackFile -and -not $EnableFeedback) {
    throw '-FeedbackFile requires -EnableFeedback.'
}
$feedbackEnabled = [bool]$EnableFeedback
$resolvedWorkdir = (Resolve-Path -LiteralPath $Workdir).Path
if ($EscalateOnValidationFailure -and $ModelChoice -ne 'auto') {
    throw 'Validation-driven escalation requires -Model auto so the trusted route determines the next tier.'
}
if ($EscalateOnValidationFailure -and $ValidationCommand.Count -eq 0) {
    throw 'Validation-driven escalation requires -ValidationCommand as an argv array.'
}

$routeEffort = if ($Effort) { $Effort } else { 'auto' }
$selectorArguments = @(
    $selectorPath, '--strategy', $Strategy, '--stdin', '--effort', $routeEffort,
    '--state-dir', $StateDir, '--model-choice', $ModelChoice,
    '--workdir', $resolvedWorkdir, '--repository-context', $RepositoryContextMode,
    '--orchestration-policy', $OrchestrationPolicy, '--model-affinity', $ModelAffinity
)
if ($FeedbackFile) { $selectorArguments += @('--feedback-file', $FeedbackFile) }
if ($ValidationCommand.Count -gt 0) {
    $selectorArguments += '--validation-configured'
}
if ($ConfirmHighRiskOrchestration) {
    $selectorArguments += '--confirm-high-risk-orchestration'
}
if ($EnableLearningPolicy) {
    $selectorArguments += '--use-active-policy'
}
if (-not $DryRun -and -not $HostPermissionsJson -and $Sandbox -eq 'inherit') {
    throw 'Automatic permission inheritance requires -HostPermissionsJson from the current host runtime.'
}
if ($ExecutionBackend -eq 'desktop' -and -not $HostPermissionsJson) {
    throw 'ExecutionBackend=desktop requires -HostPermissionsJson from the current Desktop turn.'
}
# Both built-in execution backends are Codex-only. Desktop availability comes
# from the current runtime metadata and must never be inferred from CLI PATH.
$selectorArguments += @('--available-backends', 'codex')
$usesProtectedRouterState = $EnableLearningPolicy -or $feedbackEnabled -or $ModelAffinity -ne 'off'
if (-not $DryRun -and $ExecutionBackend -eq 'cli' -and $usesProtectedRouterState) {
    $boundaryPermissionsJson = $HostPermissionsJson
    if (-not $boundaryPermissionsJson) {
        $explicitRoots = if ($Sandbox -eq 'workspace-write') { @($resolvedWorkdir) } else { @() }
        $boundaryPermissionsJson = [ordered]@{
            schema = 'agent-auto-router.host-permissions.v1'
            source = 'router-explicit-sandbox'
            sandbox = $Sandbox
            approvalPolicy = 'on-request'
            networkAccess = $null
            writableRoots = $explicitRoots
            canRequestPermissions = $true
        } | ConvertTo-Json -Compress
    }
    $boundaryArguments = @(
        $guardedPath, 'check-boundary', '--state-dir', $StateDir,
        '--host-permissions-json', $boundaryPermissionsJson,
        '--requested-sandbox', $Sandbox, '--model-affinity', $ModelAffinity
    )
    if ($FeedbackFile) { $boundaryArguments += @('--feedback-file', $FeedbackFile) }
    $boundaryResult = & $python.Source @boundaryArguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Guarded automatic learning boundary is unsafe: $($boundaryResult -join ' ')"
    }
}
$previousOutputEncoding = $OutputEncoding
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
    $routeRaw = $Task | & $python.Source @selectorArguments
    $routeExitCode = $LASTEXITCODE
} finally {
    $OutputEncoding = $previousOutputEncoding
}
if ($routeExitCode -ne 0) { throw 'Auto model selection failed.' }

$route = $routeRaw | ConvertFrom-Json
$routeDecision = $route.routeDecision
if (-not $routeDecision -or $routeDecision.schema -ne 'agent-auto-router.route-decision.v2') {
    throw 'Auto model selection returned an unsupported route-decision schema.'
}
$routeId = [string]$routeDecision.routeId
$selectorModel = [string]$routeDecision.selectorModel
$model = [string]$routeDecision.selectedModel
$resolvedEffort = $Effort
if (-not $resolvedEffort) {
    $resolvedEffort = [string]$routeDecision.executionPlan.effort
}
$explanation = [pscustomobject]@{
    executionBackend = $ExecutionBackend
    strategy = $Strategy
    model = $model
    effort = $resolvedEffort
    reason = [string]$routeDecision.reasonCode
    selectorModel = $selectorModel
    targetTier = [string]$routeDecision.targetTier
    selectedTier = [string]$routeDecision.selectedTier
    features = [pscustomobject]@{
        promptChars = $routeDecision.features.prompt_chars
        highRiskHits = $routeDecision.features.high_risk_hits
        complexHits = $routeDecision.features.complex_hits
        simpleHits = $routeDecision.features.simple_hits
    }
    matchedSignals = $routeDecision.matchedSignals
    repositoryInspection = [pscustomobject]@{
        mode = $RepositoryContextMode
        durationMs = $routeDecision.repository.metadata.scan_duration_ms
        truncated = $routeDecision.repository.metadata.scan_truncated
    }
    routeId = $routeId
    policyVersion = [string]$routeDecision.policy.version
    policyDigest = [string]$routeDecision.policy.digest
    policySource = [string]$routeDecision.policy.source
    registryDigest = [string]$routeDecision.registry.digest
    executionPlan = $routeDecision.executionPlan
    modifiesCodexConfig = $false
    localProxyReceivesCredential = $false
    routeModelCalls = 0
}
if ($DryRun -and $ExecutionBackend -eq 'cli') { $explanation; return }
if ($Explain -and $ExecutionBackend -eq 'cli') { $explanation | ConvertTo-Json -Depth 6 | Write-Host }

if ($ExecutionBackend -eq 'desktop') {
    $desktopArguments = @(
        $desktopPath, '--sandbox', $Sandbox, '--workdir', $resolvedWorkdir,
        '--host-permissions-json', $HostPermissionsJson,
        '--max-parallel-children', $DesktopMaxParallelChildren
    )
    foreach ($availableModel in $DesktopAvailableModels) {
        $desktopArguments += @('--available-model', $availableModel)
    }
    if ($DryRun) { $desktopArguments += '--dry-run' }
    if (-not $feedbackEnabled) { $desktopArguments += '--no-feedback' }
    $previousOutputEncoding = $OutputEncoding
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    try {
        $desktopPlanRaw = $routeRaw | & $python.Source @desktopArguments
        $desktopExitCode = $LASTEXITCODE
    } finally {
        $OutputEncoding = $previousOutputEncoding
    }
    if (-not $desktopPlanRaw) { throw 'Desktop execution planning failed without a plan.' }
    $desktopPlan = $desktopPlanRaw | ConvertFrom-Json
    if ($desktopExitCode -eq 0 -and -not $DryRun -and $usesProtectedRouterState) {
        $boundaryArguments = @(
            $guardedPath, 'check-boundary', '--state-dir', $StateDir,
            '--host-permissions-json', $HostPermissionsJson,
            '--requested-sandbox', $Sandbox, '--model-affinity', $ModelAffinity
        )
        $boundaryResult = & $python.Source @boundaryArguments 2>&1
        $boundaryExitCode = $LASTEXITCODE
        if ($boundaryExitCode -ne 0) {
            $boundaryText = $boundaryResult -join [Environment]::NewLine
            try {
                $boundaryBlock = $boundaryText | ConvertFrom-Json
            } catch {
                $boundaryBlock = [pscustomobject]@{
                    reason = 'guarded-auto-boundary-invalid-response'
                    message = 'Guarded automatic learning boundary returned an invalid response.'
                    modelCalls = 0
                }
            }
            $desktopPlan.status = 'blocked'
            $desktopPlan.executionRequested = $false
            $desktopPlan.plannedAgentCalls = 0
            $desktopPlan.agents = @()
            $desktopPlan.stages = @()
            $desktopPlan.hostContract.action = 'blocked'
            $desktopPlan.hostContract.maxAgents = 0
            $desktopPlan.hostContract.maxParallelAgents = 0
            $desktopPlan.hostContract.onlyWriter = $null
            $desktopPlan.blocked = [pscustomobject]@{
                code = [string]$boundaryBlock.reason
                message = [string]$boundaryBlock.message
                modelCalls = 0
            }
            $desktopPlan | ConvertTo-Json -Depth 32 -Compress | Write-Output
            exit 2
        }
    }
    $desktopPlanRaw | Write-Output
    exit $desktopExitCode
}

$runnerResultPath = Join-Path ([System.IO.Path]::GetTempPath()) ("codex-auto-run-{0}.json" -f [guid]::NewGuid().ToString('N'))
$runnerArguments = @(
    $runnerPath, '--model', $model, '--effort', $resolvedEffort,
    '--sandbox', $Sandbox, '--context-mode', $ContextMode,
    '--workdir', $resolvedWorkdir,
    '--result-file', $runnerResultPath,
    '--input-format', 'route-envelope',
    '--repo-map-tokens', [int]$routeDecision.executionPlan.context.repoMapTokens,
    '--max-candidate-files', [int]$routeDecision.executionPlan.context.maxCandidateFiles
)
if ($HostPermissionsJson) { $runnerArguments += @('--host-permissions-json', $HostPermissionsJson) }
if ($Json) { $runnerArguments += '--emit-json' }

function New-RunnerInputEnvelope {
    param([string]$TaskText)
    return [ordered]@{
        schema = 'agent-auto-router.runner-input.v1'
        task = $TaskText
        repositoryContext = [string]$route.repositoryContext.text
        repositoryMetadata = $route.repositoryContext.metadata
    } | ConvertTo-Json -Depth 8 -Compress
}

$previousOutputEncoding = $OutputEncoding
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
try {
    (New-RunnerInputEnvelope -TaskText $Task) | & $python.Source @runnerArguments
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
if ($needsEscalation -and [bool]$routeDecision.executionPlan.escalation.eligible) {
    $nextModel = [string]$routeDecision.executionPlan.escalation.nextModel
    $nextEffort = [string]$routeDecision.executionPlan.escalation.nextEffort
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
        '--input-format', 'route-envelope',
        '--repo-map-tokens', [int]$routeDecision.executionPlan.context.repoMapTokens,
        '--max-candidate-files', [int]$routeDecision.executionPlan.context.maxCandidateFiles
    )
    if ($HostPermissionsJson) { $escalationArguments += @('--host-permissions-json', $HostPermissionsJson) }
    if ($Json) { $escalationArguments += '--emit-json' }
    $escalationTask = "$Task`n`nA previous lower-tier attempt did not pass the user-provided deterministic validation. Inspect the current workspace state, fix the remaining issue, and run relevant validation."
    (New-RunnerInputEnvelope -TaskText $escalationTask) | & $python.Source @escalationArguments
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
$observedCacheWrite = 0L
$observedOutput = 0L
$observedReasoning = 0L
$usageAvailable = $false
foreach ($attempt in $attemptResults) {
    if ($attempt.usageAvailable) {
        $usageAvailable = $true
        $observedInput += [int64]$attempt.observedTokens.input
        $observedCached += [int64]$attempt.observedTokens.cached_input
        $observedCacheWrite += [int64]$attempt.observedTokens.cache_write
        $observedOutput += [int64]$attempt.observedTokens.output
        $observedReasoning += [int64]$attempt.observedTokens.reasoning_output
    }
}
$selectedModelObservedTokens = $null
if ($attemptResults.Count -gt 0) {
    $selectedAttempt = $attemptResults[$attemptResults.Count - 1]
    if ($selectedAttempt.usageAvailable) {
        $selectedModelObservedTokens = [ordered]@{
            input = [int64]$selectedAttempt.observedTokens.input
            cached_input = [int64]$selectedAttempt.observedTokens.cached_input
            cache_write = [int64]$selectedAttempt.observedTokens.cache_write
            output = [int64]$selectedAttempt.observedTokens.output
            reasoning_output = [int64]$selectedAttempt.observedTokens.reasoning_output
            total = [int64]$selectedAttempt.observedTokens.total
        }
    }
}

if ($feedbackEnabled) {
    $resolvedFeedbackFile = if ($FeedbackFile) { $FeedbackFile } else { Join-Path $StateDir 'feedback.jsonl' }
    $feedbackPayload = [ordered]@{
        route_id = $routeId
        strategy = $Strategy
        effort = $finalEffort
        selector_model = $selectorModel
        selected_model = $finalModel
        target_tier = [string]$routeDecision.targetTier
        reason = [string]$routeDecision.reasonCode
        features = [ordered]@{
            prompt_chars = [int]$routeDecision.features.prompt_chars
            criteria_count = [int]$routeDecision.features.criteria_count
            complexity_score = [int]$routeDecision.features.complexity_score
            risk_score = [int]$routeDecision.features.risk_score
            clarity_score = [int]$routeDecision.features.clarity_score
            high_risk = [bool]$routeDecision.features.high_risk
            constrained = [bool]$routeDecision.features.constrained
            parallelizable = [bool]$routeDecision.features.parallelizable
            dependency_ambiguity = [bool]$routeDecision.features.dependency_ambiguity
            orchestration_eligible = [bool]$routeDecision.features.orchestration_eligible
            complex_debugging = [bool]$routeDecision.features.complex_debugging
            long_context = [bool]$routeDecision.features.long_context
            multi_file = [bool]$routeDecision.features.multi_file
            computer_use = [bool]$routeDecision.features.computer_use
            validated_bounded = [bool]$routeDecision.features.validated_bounded
            scope_hits = [int]$routeDecision.features.scope_hits
            algorithm_hits = [int]$routeDecision.features.algorithm_hits
            repo_files = [int]$routeDecision.repository.metadata.repo_files
            source_files = [int]$routeDecision.repository.metadata.source_files
            test_files = [int]$routeDecision.repository.metadata.test_files
            language_count = [int]$routeDecision.repository.metadata.language_count
            manifest_count = [int]$routeDecision.repository.metadata.manifest_count
            large_repo = [bool]$routeDecision.repository.metadata.large_repo
            monorepo = [bool]$routeDecision.repository.metadata.monorepo
            dirty_worktree = [bool]$routeDecision.repository.metadata.dirty_worktree
            is_git_repo = [bool]$routeDecision.repository.metadata.is_git_repo
            task_has_path_hint = [bool]$routeDecision.repository.metadata.task_has_path_hint
            validation_configured = ($ValidationCommand.Count -gt 0)
            validation_passed = ($null -ne $validationExitCode -and $validationExitCode -eq 0)
            escalated = $escalated
        }
        policy_version = [string]$routeDecision.policy.version
        policy_digest = [string]$routeDecision.policy.digest
        registry_digest = [string]$routeDecision.registry.digest
        feature_schema_version = [int]$routeDecision.featureSchemaVersion
        explicit_override = [bool]$routeDecision.explicitOverride
        workspace_key = [string]$routeDecision.workspaceKey
        topology = [string]$routeDecision.executionPlan.topology
        variant = [string]$routeDecision.executionPlan.variant
        role_model_policy = [string]$routeDecision.executionPlan.roleModelPolicy
        estimated_role_tier_switches = [int]$routeDecision.executionPlan.orchestrationRecommendation.utility.estimatedRoleTierSwitches
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
                cache_write = $observedCacheWrite
                output = $observedOutput
                reasoning_output = $observedReasoning
                total = $observedInput + $observedOutput
            }
        } else { $null }
        selected_model_observed_tokens = $selectedModelObservedTokens
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
    } else {
        $guardedStatus = $null
        try {
            $recordResult = ($recordOutput -join [Environment]::NewLine) | ConvertFrom-Json
            $guardedStatus = $recordResult.guardedAuto
        } catch {
            if ($Explain) {
                Write-Warning "Route feedback was recorded, but its learning summary could not be parsed. Route ID: $routeId"
            }
        }
        if ($null -ne $recordResult -and $recordResult.recorded -eq $false) {
            if ($Explain) {
                Write-Host "Route feedback not persisted. Learning mode: $($recordResult.learningMode)."
            }
        } elseif ($null -ne $guardedStatus -and $guardedStatus.status -eq 'error') {
            Write-Warning "Route feedback was recorded, but guarded automatic learning did not advance. Error: $($guardedStatus.errorType)"
        } elseif ($Explain) {
            $learningSummary = if ($null -ne $guardedStatus) {
                " Guarded auto: $($guardedStatus.status)/$($guardedStatus.action)."
            } else { '' }
            Write-Host "Route feedback recorded. Route ID: $routeId.$learningSummary"
        }
    }
}
exit $finalExitCode
