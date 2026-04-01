# initial_setup_script.ps1
# Sets up N Claude Max credential profiles in ~/.claude-profiles/
# Each profile gets its own OAuth session via `claude` CLI.
#
# Usage:
#   .\initial_setup_script.ps1
#   .\initial_setup_script.ps1 -N 3
#   .\initial_setup_script.ps1 -Names alice,bob,carol

[CmdletBinding()]
param(
    [int]$N = 0,
    [string[]]$Names = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Resolve claude binary ──────────────────────────────────────────────────────

function Find-Claude {
    $candidate = Get-Command claude -ErrorAction SilentlyContinue
    if ($candidate) { return $candidate.Source }

    # Common npm global install locations
    $npm_paths = @(
        "$env:APPDATA\npm\claude.cmd",
        "$env:APPDATA\npm\claude",
        "$env:LOCALAPPDATA\Programs\nodejs\claude.cmd"
    )
    foreach ($p in $npm_paths) {
        if (Test-Path $p) { return $p }
    }

    throw "claude CLI not found. Install it with: npm install -g @anthropic-ai/claude-code"
}

$ClaudeBin = Find-Claude
Write-Host "Found claude: $ClaudeBin" -ForegroundColor DarkGray

# ── Determine account names ────────────────────────────────────────────────────

if ($Names.Count -gt 0) {
    $accountNames = $Names
} elseif ($N -gt 0) {
    $accountNames = 1..$N | ForEach-Object { "acct-$_" }
} else {
    $raw = Read-Host "How many accounts to set up?"
    $count = [int]$raw
    $accountNames = 1..$count | ForEach-Object { "acct-$_" }
}

$ProfilesRoot = Join-Path $env:USERPROFILE ".claude-profiles"

Write-Host ""
Write-Host "Will set up $($accountNames.Count) profile(s) in: $ProfilesRoot" -ForegroundColor Cyan
Write-Host "Accounts: $($accountNames -join ', ')" -ForegroundColor Cyan
Write-Host ""

# ── Set up each account ────────────────────────────────────────────────────────

foreach ($name in $accountNames) {
    $profileDir = Join-Path $ProfilesRoot $name
    $claudeDir  = Join-Path $profileDir ".claude"
    $credsFile  = Join-Path $claudeDir ".credentials.json"
    $labelFile  = Join-Path $profileDir "label.txt"

    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor DarkGray
    Write-Host "Account: $name" -ForegroundColor Yellow

    # Skip if already authenticated
    if (Test-Path $credsFile) {
        Write-Host "  Already has credentials — skipping." -ForegroundColor Green
        Write-Host "  (Delete $credsFile to re-authenticate)" -ForegroundColor DarkGray
        continue
    }

    # Create directories
    New-Item -ItemType Directory -Force -Path $claudeDir | Out-Null

    # Write label
    if (-not (Test-Path $labelFile)) {
        $name | Set-Content -Path $labelFile -Encoding UTF8
    }

    Write-Host "  Starting auth session. A browser link will appear below." -ForegroundColor Cyan
    Write-Host "  Sign in with the Claude Max account for '$name', then return here." -ForegroundColor Cyan
    Write-Host ""

    # Launch claude with an isolated config dir so it never touches ~/.claude.
    # Save and restore CLAUDE_CONFIG_DIR so this script doesn't pollute the
    # caller's shell session, even if it exits early or throws.
    $savedConfigDir = $env:CLAUDE_CONFIG_DIR
    try {
        $env:CLAUDE_CONFIG_DIR = $claudeDir
        # With no credentials in $claudeDir, the CLI will print an OAuth URL
        # and wait. The user clicks it, authenticates, and .credentials.json
        # is written into $claudeDir — never touching ~/.claude.
        & $ClaudeBin --dangerously-skip-permissions /login
    } catch {
        # /login exits non-zero after successful auth on some versions — ignore
    } finally {
        if ($null -eq $savedConfigDir) {
            Remove-Item Env:\CLAUDE_CONFIG_DIR -ErrorAction SilentlyContinue
        } else {
            $env:CLAUDE_CONFIG_DIR = $savedConfigDir
        }
    }

    if (Test-Path $credsFile) {
        Write-Host ""
        Write-Host "  Credentials saved for '$name'." -ForegroundColor Green
    } else {
        Write-Host ""
        Write-Host "  WARNING: credentials file not found after login for '$name'." -ForegroundColor Red
        Write-Host "  Expected: $credsFile" -ForegroundColor Red
        Write-Host "  You may need to re-run this script for this account." -ForegroundColor Red
    }

    Write-Host ""
}

# ── Summary ────────────────────────────────────────────────────────────────────

Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor DarkGray
Write-Host "Setup complete. Profile summary:" -ForegroundColor Cyan
Write-Host ""

foreach ($name in $accountNames) {
    $credsFile = Join-Path $ProfilesRoot $name ".claude" ".credentials.json"
    if (Test-Path $credsFile) {
        Write-Host "  [OK] $name" -ForegroundColor Green
    } else {
        Write-Host "  [MISSING] $name — re-run to authenticate" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "Profiles root: $ProfilesRoot" -ForegroundColor DarkGray
Write-Host "Bind-mount this directory into your dev containers as /root/.claude-profiles/" -ForegroundColor DarkGray
