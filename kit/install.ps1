# aibook-kit Windows 安装脚本
# 用法: powershell -ExecutionPolicy Bypass -File .\install.ps1 [--token ak_xxx] [--force]
[CmdletBinding()]
param(
    [string]$Root = "",
    [string]$Token = "",
    [string]$BaseUrl = "",
    [string]$SkillsHome = "",
    [string]$Model = "",
    [switch]$Force,
    [switch]$NoDeps
)

$ErrorActionPreference = "Stop"
$Kit = $PSScriptRoot
if (-not $Kit) { $Kit = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $Root) { $Root = $Kit }

$py = $null
foreach ($name in @("python", "python3", "py")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source; break }
}
if (-not $py) {
    Write-Host "[ERR] 未找到 Python (>=3.9)，请先安装并加入 PATH" -ForegroundColor Red
    exit 1
}

$argv = @((Join-Path $Kit "setup.py"), "--root", $Root)
if ($Token)     { $argv += @("--token", $Token) }
if ($BaseUrl)   { $argv += @("--base-url", $BaseUrl) }
if ($SkillsHome){ $argv += @("--skills-home", $SkillsHome) }
if ($Model)     { $argv += @("--model", $Model) }
if ($Force)     { $argv += "--force" }
if ($NoDeps)    { $argv += "--no-deps" }

Write-Host "[run] $py $($argv -join ' ')"
& $py $argv
exit $LASTEXITCODE
