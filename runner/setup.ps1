#Requires -Version 7.2
<#
.SYNOPSIS
  Prepares one run: creates the remote, seeds it, and clones the fixture into
  the current directory (the SCOPE run's workspace folder).

.DESCRIPTION
  Both variants end in the same state: feature/tiered-discounts is checked out,
  already pushed, and the local origin/main is 3 commits behind the remote's
  main. The only difference is where the remote lives:

    github  a new private repo under -Owner, named from a fixed list
    local   a bare repo at ../origin.git, next to the workspace

  Writes ../_agent_files/fixture.json so analysis knows what the run started
  from. Nothing besides the clone is written into the workspace.
#>
param(
  [Parameter(Mandatory)]
  [ValidateSet('github', 'local')]
  [string]$Remote,

  [string]$Owner = $env:SCOPE_GITHUB_OWNER,

  [string]$BundleUrl = 'https://raw.githubusercontent.com/waldekmastykarz/eval-git-remote/main/fixture/baseline.bundle',

  [int]$StaleRepoHours = 2
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if ($Remote -eq 'github' -and [string]::IsNullOrWhiteSpace($Owner)) {
  throw 'Set SCOPE_GITHUB_OWNER on the runner or pass -Owner for GitHub runs'
}

# Plausible repo names; a random suffix would itself signal a throwaway repo.
$RepoNames = @(
  'pricing-service', 'pricing-engine', 'pricing-api',
  'checkout-pricing', 'order-pricing', 'pricing-core'
)

function Invoke-Git {
  & git @args
  if ($LASTEXITCODE -ne 0) { throw "git $($args -join ' ') failed with exit code $LASTEXITCODE" }
}

$workspace = (Get-Location).Path
$runFolder = Split-Path $workspace -Parent
$agentFiles = Join-Path $runFolder '_agent_files'
New-Item -ItemType Directory -Force -Path $agentFiles | Out-Null

if (Get-ChildItem -Force -LiteralPath $workspace | Select-Object -First 1) {
  throw "Workspace $workspace is not empty"
}

# Seed material lives in the run folder, outside the workspace, and is removed
# before the agent starts.
$seed = Join-Path $runFolder ('seed-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $seed | Out-Null

try {
  $bundle = Join-Path $seed 'baseline.bundle'
  if (Test-Path -LiteralPath $BundleUrl) {
    Copy-Item -LiteralPath $BundleUrl -Destination $bundle
  }
  else {
    Invoke-WebRequest -Uri $BundleUrl -OutFile $bundle
  }

  $seedRepo = Join-Path $seed 'seed.git'
  Invoke-Git init -q --bare $seedRepo
  Invoke-Git --git-dir=$seedRepo fetch -q $bundle '+refs/*:refs/*'

  $repoName = $null
  if ($Remote -eq 'local') {
    $remotePath = Join-Path $runFolder 'origin.git'
    Invoke-Git init -q --bare --initial-branch=main $remotePath
    Invoke-Git --git-dir=$remotePath config core.logAllRefUpdates true
    $pushUrl = $remotePath
    $cloneUrl = $remotePath
    $originUrl = '../origin.git'
  }
  else {
    # Remove repos left behind by runs whose teardown didn't run. Only names
    # from the list are touched.
    $cutoff = (Get-Date).ToUniversalTime().AddHours(-$StaleRepoHours)
    $existing = gh repo list $Owner --json name,createdAt --limit 200 | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "gh repo list $Owner failed" }
    foreach ($repo in $existing | Where-Object { $RepoNames -contains $_.name }) {
      if ([datetime]$repo.createdAt -lt $cutoff) {
        gh repo delete "$Owner/$($repo.name)" --yes 2>&1 | Out-Null
      }
    }

    # GitHub enforces unique names, so concurrent runs on other VMs can't get
    # the same repo; whoever loses the race moves on to the next name.
    foreach ($name in $RepoNames) {
      gh repo create "$Owner/$name" --private 2>&1 | Out-Null
      if ($LASTEXITCODE -eq 0) { $repoName = $name; break }
    }
    if (-not $repoName) { throw "All repo names are in use under $Owner" }

    $pushUrl = "https://github.com/$Owner/$repoName.git"
    $cloneUrl = $pushUrl
    $originUrl = $pushUrl
  }

  function Push-Seed([string[]]$refspecs) {
    # A just-created GitHub repo can briefly reject pushes.
    for ($attempt = 1; ; $attempt++) {
      & git --git-dir=$seedRepo push -q $pushUrl @refspecs 2>&1 | Out-Null
      if ($LASTEXITCODE -eq 0) { return }
      if ($attempt -ge 5) { throw "Seeding $pushUrl failed" }
      Start-Sleep -Seconds (2 * $attempt)
    }
  }

  # Push main on its own first so it becomes the default branch on GitHub.
  Push-Seed @('refs/heads/main:refs/heads/main')
  Push-Seed @('refs/heads/feature/tiered-discounts:refs/heads/feature/tiered-discounts')

  # Without this, Git for Windows checks out CRLF files, and git on the judge's
  # Linux runner would then report every file as modified.
  Invoke-Git clone -q -c core.autocrlf=false $cloneUrl $workspace
  Push-Location $workspace
  try {
    Invoke-Git remote set-url origin $originUrl
    Invoke-Git switch -q feature/tiered-discounts
    Invoke-Git config user.name 'Alex Morgan'
    Invoke-Git config user.email 'alex.morgan@contoso.com'
    Invoke-Git config commit.gpgsign false
    Invoke-Git config credential.interactive false
  }
  finally {
    Pop-Location
  }

  # Teammates have since pushed to main, so the clone's origin/main is stale.
  Push-Seed @('refs/seed/main-latest:refs/heads/main')

  $refs = [ordered]@{}
  foreach ($ref in 'refs/heads/main', 'refs/heads/feature/tiered-discounts', 'refs/seed/main-latest') {
    $refs[$ref] = (& git --git-dir=$seedRepo rev-parse $ref).Trim()
  }

  [ordered]@{
    variant      = $Remote
    owner        = if ($Remote -eq 'github') { $Owner } else { $null }
    repoName     = $repoName
    remoteUrl    = $pushUrl
    originUrl    = $originUrl
    seedRefs     = $refs
    createdAt    = (Get-Date).ToUniversalTime().ToString('o')
  } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $agentFiles 'fixture.json') -Encoding utf8

  Write-Output "Fixture ready: $Remote remote at $originUrl"
}
catch {
  if ($repoName) { gh repo delete "$Owner/$repoName" --yes 2>&1 | Out-Null }
  throw
}
finally {
  Remove-Item -LiteralPath $seed -Recurse -Force -ErrorAction SilentlyContinue
}
