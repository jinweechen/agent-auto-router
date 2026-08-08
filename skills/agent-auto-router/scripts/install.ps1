[CmdletBinding()]
param(
    [string]$Source = (Split-Path -Parent $PSScriptRoot),
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }),
    [switch]$Backup
)

$ErrorActionPreference = 'Stop'
$sourcePath = (Resolve-Path -LiteralPath $Source).Path
$targetRoot = Join-Path $CodexHome 'skills'
$targetPath = Join-Path $targetRoot 'agent-auto-router'
$backupRoot = Join-Path $CodexHome 'skill-backups\agent-auto-router'
$stagingPath = Join-Path $targetRoot ".agent-auto-router-install-$PID"
$previousPath = Join-Path $targetRoot ".agent-auto-router-previous-$PID"

if (-not (Test-Path -LiteralPath (Join-Path $sourcePath 'SKILL.md') -PathType Leaf)) {
    throw "Invalid skill source: SKILL.md was not found in $sourcePath"
}

# Legacy migration: if a codex-auto-router installation exists and the new path does not, move it.
$legacyPath = Join-Path $targetRoot 'codex-auto-router'
if ((Test-Path -LiteralPath $legacyPath) -and (-not (Test-Path -LiteralPath $targetPath))) {
    Move-Item -LiteralPath $legacyPath -Destination $targetPath
    Write-Host "Migrated legacy codex-auto-router installation to agent-auto-router"
}
elseif ((Test-Path -LiteralPath $legacyPath) -and (Test-Path -LiteralPath $targetPath)) {
    Write-Host "WARNING: A legacy codex-auto-router installation still exists at $legacyPath; the new agent-auto-router installation will be used."
}

New-Item -ItemType Directory -Path $targetRoot -Force | Out-Null
foreach ($temporaryPath in @($stagingPath, $previousPath)) {
    if (Test-Path -LiteralPath $temporaryPath) {
        Remove-Item -LiteralPath $temporaryPath -Recurse -Force
    }
}

try {
    New-Item -ItemType Directory -Path $stagingPath -Force | Out-Null
    $sourceItems = Get-ChildItem -LiteralPath $sourcePath -Force
    Copy-Item -LiteralPath $sourceItems.FullName -Destination $stagingPath -Recurse -Force
    Get-ChildItem -LiteralPath $stagingPath -Directory -Filter '__pycache__' -Recurse |
        Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $stagingPath -File -Filter '*.pyc' -Recurse |
        Remove-Item -Force
    if (-not (Test-Path -LiteralPath (Join-Path $stagingPath 'SKILL.md') -PathType Leaf)) {
        throw 'Staged skill is incomplete: SKILL.md is missing.'
    }

    if ($Backup -and (Test-Path -LiteralPath $targetPath)) {
        New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
        $backupPath = Join-Path $backupRoot (Get-Date -Format 'yyyyMMdd-HHmmss')
        Copy-Item -LiteralPath $targetPath -Destination $backupPath -Recurse -Force
    }

    if (Test-Path -LiteralPath $targetPath) {
        Move-Item -LiteralPath $targetPath -Destination $previousPath
    }
    try {
        Move-Item -LiteralPath $stagingPath -Destination $targetPath
    }
    catch {
        if (Test-Path -LiteralPath $previousPath) {
            Move-Item -LiteralPath $previousPath -Destination $targetPath
        }
        throw
    }
    if (Test-Path -LiteralPath $previousPath) {
        Remove-Item -LiteralPath $previousPath -Recurse -Force
    }
}
finally {
    foreach ($temporaryPath in @($stagingPath, $previousPath)) {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Recurse -Force
        }
    }
}

Write-Host "Installed agent-auto-router to $targetPath"
