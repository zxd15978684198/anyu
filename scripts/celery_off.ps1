<#
.SYNOPSIS
  停用 Celery 车道，回到「backend 进程内 APScheduler」的单车道。

.DESCRIPTION
  停掉并删除 worker/beat 容器（不是只 stop）：留着旧容器下次 `--profile celery up`
  可能又跑旧镜像。删除后 backend 的 APScheduler 会接管全部 16 个 job。

.EXAMPLE
  ./scripts/celery_off.ps1
  ./scripts/celery_off.ps1 -DryRun
#>
param([switch]$DryRun)

$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repo

$env:CELERY_ENABLED = "false"
Write-Host "CELERY_ENABLED=false（本次进程内生效）" -ForegroundColor Yellow

Write-Host "==> 重建 backend（让它把 6 个 job 收回来）" -ForegroundColor Cyan
if (-not $DryRun) {
  docker compose up -d --force-recreate backend
  if ($LASTEXITCODE -ne 0) { throw "backend 重建失败" }
}

Write-Host "==> 停掉并删除 celery-worker / celery-beat" -ForegroundColor Cyan
if (-not $DryRun) {
  docker compose --profile celery stop celery-worker celery-beat 2>$null
  docker compose --profile celery rm -f celery-worker celery-beat
}

Write-Host "==> 确认已无 celery 容器" -ForegroundColor Cyan
if (-not $DryRun) {
  $left = docker ps -a --format "{{.Names}}" | Select-String "celery"
  if ($left) { Write-Warning "仍有残留容器：$left" } else { Write-Host "已清空。" -ForegroundColor Green }
}
