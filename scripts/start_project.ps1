param(
  [string]$Agent = "project-founder",
  [switch]$SkipDoctor,
  [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Push-Location $RepoRoot
try {
  $python = Get-Command python -ErrorAction SilentlyContinue
  if (-not $python) {
    $python = Get-Command python3 -ErrorAction SilentlyContinue
  }
  if (-not $python) {
    throw "Python 3 is required so the SQLite memory layer can run."
  }

  $grok = Get-Command grok -ErrorAction SilentlyContinue
  if (-not $grok) {
    throw "Grok CLI is required. Install it and run 'grok login' first."
  }

  & $python.Source scripts/ensure_memory.py

  if (-not $SkipDoctor) {
    & $grok.Source mcp doctor agent_memory
  }

  if ($CheckOnly) {
    Write-Host ""
    Write-Host "Preflight OK. SQLite memory and Grok MCP wiring are ready."
    exit 0
  }

  Write-Host ""
  Write-Host "Starting Grok with agent '$Agent'."
  Write-Host "When the prompt opens, type: /project-kickoff"
  Write-Host ""

  & $grok.Source --experimental-memory --agent $Agent
}
finally {
  Pop-Location
}
