#Requires -Version 7.0
<#
.SYNOPSIS
    以「假麦克风」启动 Chrome，把一段 WAV 当成麦克风输入，用来在没有硬件麦克风的机器上验语音链路。

.DESCRIPTION
    开发机没有麦克风时，唤醒词（FR-UI-11 / AC-42）与整条 pad 语音链路都没法真跑：
    getUserMedia 拿不到设备，后面的 KWS / 建连 / VAD / 上行全都无从谈起。

    本脚本用 Chrome 自带的三个开关把这件事绕开：
      --use-fake-device-for-media-stream   用假设备代替真实麦克风
      --use-fake-ui-for-media-stream       自动通过权限弹窗（否则自动化会被卡住）
      --use-file-for-fake-audio-capture=   指定假设备播放的 WAV，Chrome 会循环播放

    这样 getUserMedia 会真返回一个 MediaStream，pad 侧代码一行不改就能跑到采集路径上。
    它验证的是「采集 → KWS → 建连」这一段；物理声学（距离、噪声、AEC）仍然验不了，
    AC-42 的现场 20 次必须真人真机补。

.PARAMETER Url
    要打开的页面。默认 vite dev 的 pad 入口。
.PARAMETER Wav
    假麦克风播放的 WAV。缺省时自动调用 scripts/make-wake-wav.py 生成。
.PARAMETER Chrome
    Chrome 可执行文件；不传则按常见路径探测。
.PARAMETER Autoconnect
    给页面加 ?autoconnect=1，跳过唤醒词直接建连（联调用；验 KWS 时不要带）。
.PARAMETER DryRun
    只打印将要执行的命令行，不启动浏览器。
.PARAMETER FreshProfile
    每次启动前清空临时配置目录（权限状态也会被重置，便于复验首次授权流程）。

.EXAMPLE
    pwsh -File scripts/dev-fake-mic.ps1
    pwsh -File scripts/dev-fake-mic.ps1 -DryRun
    pwsh -File scripts/dev-fake-mic.ps1 -Url https://localhost/kws-assets/selftest.html
#>
[CmdletBinding()]
param(
    [string]$Url = 'https://localhost:5173/index.html',
    [string]$Wav = '.tmp/voice/wake-xiaoban.wav',
    [string]$Chrome,
    [switch]$Autoconnect,
    [switch]$DryRun,
    [switch]$FreshProfile
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

function Resolve-RepoPath([string]$p) {
    if ([System.IO.Path]::IsPathRooted($p)) { return $p }
    return (Join-Path $repoRoot $p)
}

# 1) 找一个 Chrome
if (-not $Chrome) {
    $candidates = @(
        (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
    ) | Where-Object { $_ -and (Test-Path $_) }
    $Chrome = $candidates | Select-Object -First 1
}
if (-not $Chrome -or -not (Test-Path $Chrome)) {
    throw "找不到 chrome.exe。用 -Chrome 'C:\path\to\chrome.exe' 指定，或先装 Chrome。"
}

# 2) 准备唤醒词 WAV（没有就现造）
$wavPath = Resolve-RepoPath $Wav
if (-not (Test-Path $wavPath)) {
    Write-Host "[make] 未找到 $wavPath，开始合成唤醒词音频…" -ForegroundColor Yellow
    Push-Location $repoRoot
    try {
        & uv run python scripts/make-wake-wav.py --out $Wav
        if ($LASTEXITCODE -ne 0) { throw "make-wake-wav.py 退出码 $LASTEXITCODE" }
    }
    finally { Pop-Location }
}
$wavPath = (Resolve-Path $wavPath).Path

# 3) 隔离的浏览器配置目录：避免污染日常 Chrome，也避免它复用已有实例而吞掉启动参数
$profileDir = Join-Path $env:TEMP 'anyu-fake-mic'
if ($FreshProfile -and (Test-Path $profileDir)) {
    Remove-Item -Recurse -Force $profileDir
    Write-Host "[clean] 已清空 $profileDir" -ForegroundColor Yellow
}
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null

# 4) 拼参数
$target = $Url
if ($Autoconnect) {
    $sep = if ($target.Contains('?')) { '&' } else { '?' }
    $target = "$target${sep}autoconnect=1"
}

$argList = @(
    '--use-fake-device-for-media-stream',
    '--use-fake-ui-for-media-stream',
    "--use-file-for-fake-audio-capture=`"$wavPath`"",
    "--user-data-dir=`"$profileDir`"",
    '--no-first-run',
    '--no-default-browser-check',
    '--ignore-certificate-errors',          # 本地自签证书，否则 getUserMedia 会被拦
    '--autoplay-policy=no-user-gesture-required',
    $target
)

Write-Host ''
Write-Host '假麦克风启动配置' -ForegroundColor Cyan
Write-Host "  浏览器   : $Chrome"
Write-Host "  音频源   : $wavPath"
Write-Host "  配置目录 : $profileDir"
Write-Host "  目标页面 : $target"
Write-Host "  模式     : $(if ($Autoconnect) { 'autoconnect=1（跳过唤醒词）' } else { '唤醒词（等「小伴」触发）' })"
Write-Host ''

if ($DryRun) {
    Write-Host '--- 将要执行（DryRun，不启动）---' -ForegroundColor Yellow
    Write-Host "& '$Chrome' $($argList -join ' ')"
    Write-Host ''
    Write-Host '提示：KWS 单独自检页 = https://localhost:5173/kws-assets/selftest.html'
    return
}

Start-Process -FilePath $Chrome -ArgumentList $argList | Out-Null

Write-Host '[ok] 已启动。' -ForegroundColor Green
Write-Host ''
Write-Host '接下来怎么看：' -ForegroundColor Cyan
Write-Host '  1. 页面应在 KWS 监听态；WAV 每 4.5s 循环一次，约 1.5s 后应命中并自动建连。'
Write-Host '  2. 打开 DevTools Console，看 kws 的 state 是否走 listening → woken。'
Write-Host '  3. 只想验 KWS 本身：-Url https://localhost:5173/kws-assets/selftest.html'
Write-Host '  4. 想复验首次授权流程：加 -FreshProfile。'
Write-Host ''
Write-Host '注意：这是假设备，不覆盖物理声学（距离/噪声/AEC），AC-42 的现场 20 次仍需真人真机。' -ForegroundColor Yellow
