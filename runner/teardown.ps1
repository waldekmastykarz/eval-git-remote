#Requires -Version 7.2
<#
.SYNOPSIS
  Records the run's git state and removes the per-run GitHub repo.

.DESCRIPTION
  Runs after the agent stops and before SCOPE snapshots the run folder, so
  everything written to ../_agent_files ends up in the snapshot:

    git-state.txt    branches, status, stash and recent history, captured
                     before the harness stages the workspace for its diff
    remote-refs.txt  the remote's refs, including pushes that didn't update
                     local remote-tracking refs

  Never fails: a failing teardown marks the SCOPE runner as poisoned.
#>
param(
  [string]$Owner = $env:SCOPE_GITHUB_OWNER
)

$ErrorActionPreference = 'Continue'

try {
  $workspace = (Get-Location).Path
  $agentFiles = Join-Path (Split-Path $workspace -Parent) '_agent_files'
  New-Item -ItemType Directory -Force -Path $agentFiles | Out-Null

  $fixturePath = Join-Path $agentFiles 'fixture.json'
  $fixture = if (Test-Path -LiteralPath $fixturePath) { Get-Content -Raw -LiteralPath $fixturePath | ConvertFrom-Json } else { $null }

  $state = foreach ($command in @(
      @('status', '--porcelain=v1', '--branch', '--untracked-files=all'),
      @('branch', '-vv', '--all'),
      @('stash', 'list'),
      @('log', '--all', '--graph', '--format=%h %d %s', '-n', '40'),
      @('reflog', '-n', '60', '--date=iso'))) {
    "### git $($command -join ' ')"
    & git -C $workspace --no-pager @command 2>&1 | ForEach-Object { "$_" }
    ''
  }
  $state | Set-Content -LiteralPath (Join-Path $agentFiles 'git-state.txt') -Encoding utf8

  if ($fixture -and $fixture.remoteUrl) {
    & git ls-remote $fixture.remoteUrl 2>&1 | ForEach-Object { "$_" } |
      Set-Content -LiteralPath (Join-Path $agentFiles 'remote-refs.txt') -Encoding utf8
  }

  if ($fixture -and $fixture.variant -eq 'github' -and $fixture.repoName) {
    $owner = if ($fixture.owner) { $fixture.owner } else { $Owner }
    if ([string]::IsNullOrWhiteSpace($owner)) {
      throw 'Cannot delete the run repository: fixture owner and SCOPE_GITHUB_OWNER are missing'
    }
    gh repo delete "$owner/$($fixture.repoName)" --yes 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Warning "Could not delete $owner/$($fixture.repoName); setup will remove it once it's stale" }
  }
}
catch {
  Write-Warning "Teardown error: $_"
}

exit 0
