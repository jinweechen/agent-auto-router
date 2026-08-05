[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Task,
    [ValidateSet('auto', 'sol', 'terra', 'luna', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna')]
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
    [string]$Workdir = (Get-Location).Path,
    [switch]$DryRun,
    [switch]$Explain,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
$selectorPath = Join-Path $PSScriptRoot 'select_auto_model.py'
if (-not (Test-Path -LiteralPath $selectorPath -PathType Leaf)) { throw "Auto model selector not found: $selectorPath" }
if (-not (Test-Path -LiteralPath $Workdir -PathType Container)) { throw "Workdir not found: $Workdir" }
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) { throw 'Python 3 is required for deterministic Auto routing.' }
$codex = Get-Command codex -ErrorAction SilentlyContinue
if (-not $codex -and -not $DryRun) { throw 'Codex CLI is required to execute an Auto task.' }

$routeEffort = if ($Effort) { $Effort } else { 'medium' }
$previousOutputEncoding = $OutputEncoding
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
    $routeRaw = $Task | & $python.Source $selectorPath --strategy $Strategy --stdin --effort $routeEffort
    $routeExitCode = $LASTEXITCODE
} finally {
    $OutputEncoding = $previousOutputEncoding
}
if ($routeExitCode -ne 0) { throw 'Auto model selection failed.' }

$route = $routeRaw | ConvertFrom-Json
$selectorModel = [string]$route.decision.model
$model = switch ($ModelChoice) {
    'auto' { $selectorModel }
    'sol' { 'gpt-5.6-sol' }
    'terra' { 'gpt-5.6-terra' }
    'luna' { 'gpt-5.6-luna' }
    default { $ModelChoice }
}
if ($model -notin @('gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna')) {
    throw "Selector chose a model outside the allowlist: $model"
}
$resolvedEffort = $Effort
if (-not $resolvedEffort) {
    $resolvedEffort = switch ($model) {
        'gpt-5.6-sol' { 'high' }
        'gpt-5.6-terra' { 'medium' }
        default { 'medium' }
    }
}
$explanation = [pscustomobject]@{
    strategy = $Strategy
    model = $model
    effort = $resolvedEffort
    reason = if ($ModelChoice -eq 'auto') { $route.decision.reason } else { 'explicit_model' }
    selectorModel = $selectorModel
    features = [pscustomobject]@{
        promptChars = $route.decision.prompt_chars
        highRiskHits = $route.decision.high_risk_hits
        complexHits = $route.decision.complex_hits
        simpleHits = $route.decision.simple_hits
    }
    modifiesCodexConfig = $false
    localProxyReceivesCredential = $false
    routeModelCalls = 0
}
if ($DryRun) { $explanation; return }
if ($Explain) { $explanation | ConvertTo-Json -Depth 6 | Write-Host }

$arguments = @(
    'exec', '--skip-git-repo-check', '--sandbox', $Sandbox,
    '-C', (Resolve-Path -LiteralPath $Workdir).Path,
    '-m', $model, '-c', "model_reasoning_effort='$resolvedEffort'"
)
if ($ContextMode -eq 'lean' -and $Sandbox -eq 'read-only') {
    $arguments = @('exec', '--ignore-user-config') + $arguments[1..($arguments.Count - 1)]
}
if ($Json) { $arguments += '--json' }
$arguments += '-'

$previousOutputEncoding = $OutputEncoding
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
    $Task | & $codex.Source @arguments
    $codexExitCode = $LASTEXITCODE
} finally {
    $OutputEncoding = $previousOutputEncoding
}
exit $codexExitCode
