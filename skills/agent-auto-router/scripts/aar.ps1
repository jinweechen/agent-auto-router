[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('run', 'doctor')]
    [string]$Command = 'run',
    [Parameter(Position = 1)]
    [string]$Task = '',
    [ValidateSet('safe', 'standard')]
    [string]$Profile = 'standard',
    [string]$Workdir = (Get-Location).Path,
    [switch]$DryRun,
    [switch]$Explain,
    [switch]$Json,
    [switch]$NoFeedback
)

$ErrorActionPreference = 'Stop'
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) { throw 'Python 3 is required for Agent Auto Router.' }

$doctorPath = Join-Path $PSScriptRoot 'doctor.py'
$profilePath = Join-Path $PSScriptRoot 'quick_profiles.py'
$runnerPath = Join-Path $PSScriptRoot 'invoke_auto_task.ps1'

if ($Command -eq 'doctor') {
    $doctorArguments = @($doctorPath)
    if ($Json) { $doctorArguments += '--json' }
    & $python.Source @doctorArguments
    exit $LASTEXITCODE
}

if (-not $Task.Trim()) {
    throw "The run command requires a task. Example: aar.ps1 run 'Fix the failing test'"
}
if (-not (Test-Path -LiteralPath $Workdir -PathType Container)) {
    throw "Workdir not found: $Workdir"
}

$profileRaw = & $python.Source $profilePath --profile $Profile
if ($LASTEXITCODE -ne 0 -or -not $profileRaw) {
    throw "Quick profile validation failed: $Profile"
}
$selected = ($profileRaw -join [Environment]::NewLine) | ConvertFrom-Json
if ($selected.schema -ne 'agent-auto-router.quick-profiles') {
    throw 'Quick profile helper returned an unsupported schema.'
}
$settings = $selected.profile
$runnerParameters = @{
    Task = $Task
    ExecutionBackend = 'cli'
    ModelChoice = 'auto'
    Strategy = [string]$settings.strategy
    Sandbox = [string]$settings.sandbox
    ContextMode = [string]$settings.contextMode
    RepositoryContextMode = [string]$settings.repositoryContextMode
    OrchestrationPolicy = 'recommend'
    ModelAffinity = [string]$settings.modelAffinity
    Workdir = (Resolve-Path -LiteralPath $Workdir).Path
}
if ($DryRun) { $runnerParameters.DryRun = $true }
if ($Explain) { $runnerParameters.Explain = $true }
if ($Json) { $runnerParameters.Json = $true }
if ($NoFeedback -or [bool]$settings.noFeedback) {
    $runnerParameters.NoFeedback = $true
}

if ($DryRun) {
    $result = & $runnerPath @runnerParameters
    if ($null -eq $result) { throw 'Quick DryRun returned no route.' }
    $result | Add-Member -NotePropertyName quickProfile -NotePropertyValue $Profile
    $result | Add-Member -NotePropertyName effectiveSandbox -NotePropertyValue $settings.sandbox
    $result | Add-Member -NotePropertyName effectiveModelAffinity -NotePropertyValue $settings.modelAffinity
    $result | Add-Member -NotePropertyName feedbackOnExecution -NotePropertyValue (-not ($NoFeedback -or [bool]$settings.noFeedback))
    if ($Json) {
        $result | ConvertTo-Json -Depth 12
    } else {
        $result | Write-Output
    }
    exit 0
}

& $runnerPath @runnerParameters
exit $LASTEXITCODE
