<#
.SYNOPSIS
  启用 Celery 车道（worker + beat + backend 摘掉 APScheduler 里的 6 个 job）。

.DESCRIPTION
  为什么必须有这个脚本（2026-11-13 真机验证教训）：
  worker/beat 在 `profile: celery` 里，平时 `docker compose up -d` 根本不会碰它们。
  重建镜像之后容器还是老的 → 「代码修好了，跑的却是旧代码」：设备离线巡检连续 15 分钟
  报 KeyError、全部失败，而人看日志以为是业务 bug。

  所以启用/重启只能走这里：build → force-recreate → ping 自检，一步都不能省。

.EXAMPLE
  ./scripts/celery_on.ps1            # 全量：重建镜像 + 重建容器 + 自检
  ./scripts/celery_on.ps1 -SkipBuild # 只重建容器（镜像没变时用）
  ./scripts/celery_on.ps1 -DryRun    # 只打印将执行的命令
#>
param(
  [switch]$SkipBuild,
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repo

function Invoke-Step([string]$Text, [scriptblock]$Action) {
  Write-Host "==> $Text" -ForegroundColor Cyan
  if ($DryRun) { return }
  & $Action
  if ($LASTEXITCODE -ne 0) { throw "命令失败（exit $LASTEXITCODE）：$Text" }
}

if (-not $DryRun) {
  docker info *> $null
  if ($LASTEXITCODE -ne 0) { throw "Docker 没在运行，先启动 Docker Desktop。" }
}

if (-not $SkipBuild) {
  Invoke-Step "重建 backend 镜像（worker/beat 复用同一个镜像标签）" { docker compose build backend }
}

# 单点开关：backend 与 worker/beat 必须读到同一个值，否则会「beat 投递 + backend 也跑」双跑。
$env:CELERY_ENABLED = "true"
Write-Host "CELERY_ENABLED=true（本次进程内生效）" -ForegroundColor Yellow

Invoke-Step "重建并启动 backend / celery-worker / celery-beat" {
  docker compose --profile celery up -d --force-recreate backend celery-worker celery-beat
}

if (-not $DryRun) {
  Write-Host "==> 等待 worker 就绪（最多 90s）" -ForegroundColor Cyan
  $ok = $false
  for ($i = 0; $i -lt 18; $i++) {
    Start-Sleep -Seconds 5
    docker exec anyu-celery-worker celery -A app.tasks.celery_app inspect ping *> $null
    if ($LASTEXITCODE -eq 0) { $ok = $true; break }
  }
  if (-not $ok) { throw "worker 90s 内没应答 inspect ping —— 先看 docker logs anyu-celery-worker" }

  Write-Host "==> backend 日志确认已把 6 个 job 交给 Celery" -ForegroundColor Cyan
  docker logs anyu-backend --since 2m 2>&1 | Select-String "handed over to celery" | Select-Object -Last 2
}

Invoke-Step "当前状态" { docker compose ps backend celery-worker celery-beat }
Write-Host "完成。" -ForegroundColor Green
