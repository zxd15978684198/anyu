# 用途：断网 / 演示现场按需在**前台**起一个本机 Prometheus，抓 backend 的 /metrics，验「指标文本可被抓取」。
# 依赖：Windows + PowerShell 7；一个 prometheus 可执行文件（本脚本不联网下载、不装服务、不写系统配置）。
#Requires -Version 7.0
<#
.SYNOPSIS
    前台起本机 Prometheus，把 127.0.0.1:8000/metrics 抓成可看的曲线上限，用完 Ctrl-C 即退。

.DESCRIPTION
    为什么要这个兜底脚本：正式口径是「metrics 经 OTLP 直推 Grafana Cloud，本机零常驻」
    （设计记录（可观测性接入） §4.10 / D7）。但现场会有两种情况让云侧看不了：
      1) 断网 —— 完全没有出口，云端面板就是空白；
      2) 还没拿到 Grafana Cloud 凭据 —— 面板存在，但数据没上去。
    这时需要的不是第二条数据通道，而是把同一个 /metrics 就地呈现出来：本脚本只做这一件事。

    刻意**不做**的事：不下载二进制（现场断网，下载必然失败，写个假成功更糟）、
    不注册 Windows 服务、不设开机自启、不写系统级配置、不拉 Grafana。
    Prometheus 作为前台子进程运行：Ctrl-C 打给它，脚本随之退出，不留常驻。

    查找顺序（先到先得）：
      1) $env:PROMETHEUS_BIN —— 指到 prometheus.exe 或它所在的 tools 目录；
      2) 仓库内 tools/prometheus/prometheus.exe —— 现场「解压即用」的位置；
      3) PATH 里的 prometheus。
    三者都没有时打印官方下载地址与放置指引后以退出码 1 结束，不尝试联网。

    每次启动都会重写 .data/prometheus/prometheus.yml（该目录已在 .gitignore），
    时序数据落 .data/prometheus/data，保留 7 天；端口固定绑 127.0.0.1:9090（不对外）。

.PARAMETER DryRun
    只解析二进制、生成配置、打印将要执行的命令行，不启动 Prometheus。
    用于验证配置生成是否正确，以及在没有 prometheus 二进制的机器上确认提示文案。

.EXAMPLE
    pwsh -File scripts/obs-local.ps1

.EXAMPLE
    pwsh -File scripts/obs-local.ps1 -DryRun

.EXAMPLE
    $env:PROMETHEUS_BIN = 'C:\path\to\prometheus.exe'
    pwsh -File scripts/obs-local.ps1
#>
[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

# 仓库内约定的位置（与 .gitignore 的 .data/prometheus/ 对应）
$localExe = Join-Path $repoRoot 'tools/prometheus/prometheus.exe'

# 相对仓库根的统一入口：先 Push-Location，后面所有相对路径都按仓库根解析。
$configRel = './.data/prometheus/prometheus.yml'
$storageRel = './.data/prometheus/data'
$listenAddress = '127.0.0.1:9090'
$scrapeTarget = '127.0.0.1:8000'

function Resolve-PrometheusBinary {
    <# 按 PROMETHEUS_BIN → tools/prometheus/ → PATH 的顺序找可执行文件。找不到返回 $null。 #>
    if ($env:PROMETHEUS_BIN) {
        $candidate = $env:PROMETHEUS_BIN
        # 允许指向目录：方便把整个解压目录塞进环境变量
        if (Test-Path -LiteralPath $candidate -PathType Container) {
            $candidate = Join-Path $candidate 'prometheus.exe'
        }
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
        Write-Host "[warn] PROMETHEUS_BIN=$($env:PROMETHEUS_BIN) 不存在，继续往下找。" -ForegroundColor Yellow
    }

    if (Test-Path -LiteralPath $localExe -PathType Leaf) {
        return $localExe
    }

    $onPath = Get-Command prometheus -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($onPath) {
        return $onPath.Source
    }

    return $null
}

function Write-NotFoundHelp {
    Write-Host ''
    Write-Host '找不到 prometheus 可执行文件（三个位置都没有）。' -ForegroundColor Red
    Write-Host ''
    Write-Host '现场自备二进制的三条路，任选一条：' -ForegroundColor Cyan
    Write-Host '  1. 下载 windows-amd64 压缩包（需联网，且别在断网现场做）：'
    Write-Host '     https://github.com/prometheus/prometheus/releases'
    Write-Host "     解压后把目录放到 tools\prometheus\，确认存在：$localExe"
    Write-Host '  2. 已有二进制：$env:PROMETHEUS_BIN = ''C:\path\to\prometheus.exe'''
    Write-Host '     或指向解压目录：$env:PROMETHEUS_BIN = ''C:\path\to\prometheus-3.x.windows-amd64'''
    Write-Host '  3. 把它所在目录加进 PATH，让 prometheus 命令能直接找到。'
    Write-Host ''
    Write-Host '本脚本不会联网下载，也不会安装任何东西。' -ForegroundColor Yellow
    Write-Host ''
}

# ---------------------------------------------------------------------------
# 1) 定位二进制
# ---------------------------------------------------------------------------
$prometheus = Resolve-PrometheusBinary
if (-not $prometheus) {
    Write-NotFoundHelp
    exit 1
}

# ---------------------------------------------------------------------------
# 2) 生成抓取配置（每次都重写：配置是脚本产物，不靠人手改）
# ---------------------------------------------------------------------------
$configText = @"
# 由 scripts/obs-local.ps1 生成，每次启动都会重写 —— 要改抓取口径请改脚本，不要改这个文件。
#
# 只有一条 job：backend 暴露的 /metrics（Prometheus 文本格式）。
# scrape_interval 与规划文档一致的 15s：够看见分钟级的请求/延迟变化，又不至于把本地 SQLite 写爆。
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: anyu-backend
    metrics_path: /metrics
    static_configs:
      - targets: ['$scrapeTarget']
"@

$configPath = Join-Path $repoRoot '.data/prometheus/prometheus.yml'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $configPath) | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $repoRoot '.data/prometheus/data') | Out-Null
Set-Content -LiteralPath $configPath -Value $configText -Encoding utf8NoBOM

# ---------------------------------------------------------------------------
# 3) 前台启动（相对路径按仓库根解析，故先 Push-Location）
# ---------------------------------------------------------------------------
$promArgs = @(
    "--config.file=$configRel"
    "--storage.tsdb.path=$storageRel"
    '--storage.tsdb.retention.time=7d'
    "--web.listen-address=$listenAddress"
)

Write-Host ''
Write-Host '本机 Prometheus（兜底视图，非交付口径）' -ForegroundColor Cyan
Write-Host "  二进制   : $prometheus"
Write-Host "  配置     : $configPath"
Write-Host "  抓取目标 : http://$scrapeTarget/metrics（每 15s）"
Write-Host "  数据目录 : .data/prometheus/data（保留 7 天）"
Write-Host "  面板     : http://$listenAddress （targets: /targets）"
Write-Host '  退出     : Ctrl-C'
Write-Host ''

if ($DryRun) {
    Write-Host '--- DryRun：不启动，仅打印将要执行的命令 ---' -ForegroundColor Yellow
    Write-Host "& '$prometheus' $($promArgs -join ' ')"
    Write-Host ''
    exit 0
}

Write-Host '提示：backend 没起时 targets 会是 DOWN，先确认 http://127.0.0.1:8000/health 通。' -ForegroundColor Yellow
Write-Host ''

Push-Location $repoRoot
try {
    # 前台运行：Ctrl-C 交给 prometheus，它收到 SIGINT 会自行 flush 关闭。
    & $prometheus @promArgs
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

Write-Host ''
Write-Host "[bye] prometheus 已退出（exit=$exitCode）。时序数据留在 .data/prometheus/data，可删。" -ForegroundColor Cyan
exit $exitCode
