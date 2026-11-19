<#
.SYNOPSIS
    同步 W1 的 8 个 VRM 形象 + 6 条 VRMA 动作到 frontend/public/avatars，并生成 catalog.json / NOTICE.md。

.DESCRIPTION
    形象与动作的**事实来源**是开发机上的 vrm-playground 工作副本（默认取用户主目录下的 vrm-playground；本机路径不同时用 -SourceRoot 传入。入库的 catalog/NOTICE 只记源目录名，不落绝对路径）。
    本脚本把它们拷进前端的静态目录，并产出前端唯一读的目录文件 catalog.json：

      - models/     8 个 .vrm（女 5 / 男 3），体积均 ≤15.8MB，满足 FR-3D-02 的首帧预算；
      - animations/ 6 条新增 .vrma（静息 / 倾听变体 / 说话 / 坐姿说话 / 展示 / 才艺），
                    既有的 idle.vrma、idle-talking.vrma、nod.vrma 不覆盖、只登记为 bundled。

    为什么要有这个脚本而不是手工拷文件：catalog.json 里的 sha256 必须与磁盘上的字节一致，
    手抄一次就会漂移；同时「源目录不存在」「个别文件缺失」都要能被**如实记录**，
    让前端拿到的是「实际拷到了什么」，而不是一份一厢情愿的清单。

    容错口径（MUST）：
      - 源目录整体不存在：不抛错，只登记全部条目为 skipped，catalog 仍会生成（只含 bundled 条目），
        退出码 0，由调用方看 Summary 决定要不要重跑；
      - 个别文件缺失：跳过该条，写入 catalog 的 `skipped` 数组并在 NOTICE.md 里点名；
      - 目标已存在同名文件：哈希一致则复用（不重拷），不一致且没给 -Force 时**跳过并告警**，
        绝不静默覆盖（动画目录里已有同名文件说明有人手工改过，覆盖会丢改动）。

    版权红线（AGENTS §4.11 / 计划 §2.4）：这些模型是原神 / 星穹铁道角色，**仅限本地演示**，
    不得入库、不得对外分发。脚本在 NOTICE.md 里写清归属，并且 `.gitignore` 已忽略
    *.vrm / *.glb / *.vrma（模型与新增动作都只留本机，`catalog.json` / `NOTICE.md` 在册）；
    本脚本不负责、也绝不能绕过这条红线。

.EXAMPLE
    pwsh -File scripts/sync_avatar_assets.ps1

.EXAMPLE
    pwsh -File scripts/sync_avatar_assets.ps1 -DryRun
#>
[CmdletBinding()]
param(
    # vrm-playground 仓库根（public/assets/vrm 的上一级）。
    [string]$SourceRoot = "$env:USERPROFILE\vrm-playground",

    # 前端静态资源目录；默认相对本脚本定位，避免依赖调用者的当前目录。
    [string]$DestRoot = '',

    # 目标文件已存在且哈希不一致时，允许覆盖。
    [switch]$Force,

    # 只打印计划，不写文件。
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ([string]::IsNullOrWhiteSpace($DestRoot)) {
    $DestRoot = Join-Path (Split-Path -Parent $PSScriptRoot) 'frontend\public\avatars'
}

# public/assets/vrm 是 playground 的静态资源根；模型在其下，动作分 common/ 与根目录两处。
$SourceVrm = Join-Path $SourceRoot 'public\assets\vrm'
$SourceAnimations = Join-Path $SourceVrm 'animations'

# ---------------------------------------------------------------- 清单（唯一事实来源）
#
# `Scene` 取值与前端 `AvatarScene` 对齐：idle / listen / talking / talking_sit / nod / show / talent。
# `Weight` 供低配平板挑选（计划 §2.3 第 4 步）：light ≤8MB、normal 其余。
# 排序即前端取用优先级：同一场景有多条动作时，先出现的先被挑中。
$ModelSpecs = @(
    [pscustomobject]@{ Id = 'lisa';        File = 'Lisa.vrm';        DisplayName = '丽莎';   Gender = 'female'; Character = '原神 / Genshin Impact' }
    [pscustomobject]@{ Id = 'shenhe';      File = 'Shenhe.vrm';      DisplayName = '申鹤';   Gender = 'female'; Character = '原神 / Genshin Impact' }
    [pscustomobject]@{ Id = 'firefly';     File = 'Firefly.vrm';     DisplayName = '流萤';   Gender = 'female'; Character = '星穹铁道 / Honkai: Star Rail' }
    [pscustomobject]@{ Id = 'sparkle';     File = 'Sparkle.vrm';     DisplayName = '花火';   Gender = 'female'; Character = '星穹铁道 / Honkai: Star Rail' }
    [pscustomobject]@{ Id = 'ningguang';   File = 'Ningguang.vrm';   DisplayName = '凝光';   Gender = 'female'; Character = '原神 / Genshin Impact' }
    [pscustomobject]@{ Id = 'jingyuan';    File = 'Jingyuan.vrm';    DisplayName = '景元';   Gender = 'male';   Character = '星穹铁道 / Honkai: Star Rail' }
    [pscustomobject]@{ Id = 'danheng';     File = 'Danheng.vrm';     DisplayName = '丹恒';   Gender = 'male';   Character = '星穹铁道 / Honkai: Star Rail' }
    [pscustomobject]@{ Id = 'neuvillette'; File = 'Neuvillette.vrm'; DisplayName = '那维莱特'; Gender = 'male'; Character = '原神 / Genshin Impact' }
)

$AnimationSpecs = @(
    [pscustomobject]@{ Id = 'neutral';           File = 'neutral.vrma';           Source = 'common\neutral.vrma';           DisplayName = '静息';     Scene = 'idle' }
    [pscustomobject]@{ Id = 'idle-2';            File = 'idle-2.vrma';            Source = 'common\idle-2.vrma';            DisplayName = '倾听变体'; Scene = 'listen' }
    [pscustomobject]@{ Id = 'idle-talking-6';    File = 'idle-talking-6.vrma';    Source = 'common\idle-talking-6.vrma';    DisplayName = '说话';     Scene = 'talking' }
    [pscustomobject]@{ Id = 'world-sit-talking'; File = 'world-sit-talking.vrma'; Source = 'common\world-sit-talking.vrma'; DisplayName = '坐姿说话'; Scene = 'talking_sit' }
    [pscustomobject]@{ Id = 'vrma-01';           File = 'VRMA_01.vrma';           Source = 'VRMA_01.vrma';                  DisplayName = '展示动作'; Scene = 'show' }
    [pscustomobject]@{ Id = 'dance-28';          File = 'ダンス28.vrma';          Source = 'ダンス28.vrma';                 DisplayName = '才艺舞蹈'; Scene = 'talent' }
)

# 仓库里已有的三条动作：只登记、不拷贝、不覆盖（README.md §5 第 3 条）。
$BundledAnimations = @(
    [pscustomobject]@{ Id = 'bundled-idle';    File = 'idle.vrma';         DisplayName = '内置静息'; Scene = 'idle' }
    [pscustomobject]@{ Id = 'bundled-talking'; File = 'idle-talking.vrma'; DisplayName = '内置说话'; Scene = 'talking' }
    [pscustomobject]@{ Id = 'bundled-nod';     File = 'nod.vrma';          DisplayName = '点头';     Scene = 'nod' }
)

$ModelUrlPrefix = '/avatars/models'
$AnimationUrlPrefix = '/avatars/animations'

# ---------------------------------------------------------------- 工具函数

$script:Skipped = [System.Collections.Generic.List[object]]::new()
$script:Copied = [System.Collections.Generic.List[string]]::new()

function Stop-WithError {
    param([string]$Message)
    Write-Host "[sync-avatars][error] $Message" -ForegroundColor Red
    exit 2
}

function Get-Sha256Hex {
    param([Parameter(Mandatory)][string]$Path)
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-RelativeUrl {
    param([string]$Prefix, [string]$FileName)
    # 前端 fetch 的 URL 必须百分号编码（ダンス28.vrma 这类非 ASCII 文件名在部分代理上会 400）。
    "$Prefix/$([uri]::EscapeDataString($FileName))"
}

function Add-Skipped {
    param([string]$Id, [string]$Kind, [string]$File, [string]$Reason)
    $script:Skipped.Add([ordered]@{ id = $Id; kind = $Kind; file = $File; reason = $Reason })
    Write-Host "[sync-avatars][skip] $Kind/$Id ($File): $Reason" -ForegroundColor Yellow
}

function Copy-Asset {
    <#
        拷贝单个文件并返回哈希；源不存在返回 $null 并登记 skipped。
        目标已存在时：哈希一致 → 复用；不一致 → 默认跳过（-Force 才覆盖）。
    #>
    param(
        [string]$SourcePath,
        [string]$DestPath,
        [string]$Id,
        [string]$Kind,
        [string]$File
    )

    if (-not (Test-Path -LiteralPath $SourcePath -PathType Leaf)) {
        Add-Skipped -Id $Id -Kind $Kind -File $File -Reason "源文件缺失: $SourcePath"
        return $null
    }

    $sourceHash = Get-Sha256Hex -Path $SourcePath
    $sourceSize = (Get-Item -LiteralPath $SourcePath).Length

    if (Test-Path -LiteralPath $DestPath -PathType Leaf) {
        $destHash = Get-Sha256Hex -Path $DestPath
        if ($destHash -eq $sourceHash) {
            Write-Host "[sync-avatars][reuse] $Kind/$Id 已是最新（$sourceSize bytes）"
            return [pscustomobject]@{ Sha256 = $sourceHash; Bytes = $sourceSize; Copied = $false }
        }
        if (-not $Force) {
            Add-Skipped -Id $Id -Kind $Kind -File $File -Reason '目标已存在且内容不同；未加 -Force，拒绝覆盖'
            return $null
        }
        Write-Host "[sync-avatars][overwrite] $Kind/$Id 内容不同，-Force 覆盖" -ForegroundColor Yellow
    }

    $destDir = Split-Path -Parent $DestPath
    if (-not (Test-Path -LiteralPath $destDir -PathType Container)) {
        if (-not $DryRun) { New-Item -ItemType Directory -Path $destDir -Force | Out-Null }
    }

    if ($DryRun) {
        Write-Host "[sync-avatars][dry-run] 将拷贝 $SourcePath -> $DestPath"
    }
    else {
        Copy-Item -LiteralPath $SourcePath -Destination $DestPath -Force
        # 拷完重算，防止跨盘拷贝截断（体积不符时以目标盘为准并告警）。
        $destHash = Get-Sha256Hex -Path $DestPath
        if ($destHash -ne $sourceHash) {
            Stop-WithError "拷贝后哈希不一致: $DestPath（源 $sourceHash / 目标 $destHash）"
        }
    }
    $script:Copied.Add("$Kind/$Id")
    return [pscustomobject]@{ Sha256 = $sourceHash; Bytes = $sourceSize; Copied = $true }
}

# ---------------------------------------------------------------- 主流程

Write-Host "[sync-avatars] source = $SourceRoot"
Write-Host "[sync-avatars] dest   = $DestRoot"

$sourceAvailable = Test-Path -LiteralPath $SourceVrm -PathType Container
if (-not $sourceAvailable) {
    Write-Host "[sync-avatars][warn] 源目录不存在: $SourceVrm —— 全部条目记为 skipped，catalog 只含 bundled 条目" -ForegroundColor Yellow
}

$modelDestDir = Join-Path $DestRoot 'models'
$animationDestDir = Join-Path $DestRoot 'animations'
if (-not $DryRun) {
    foreach ($dir in @($DestRoot, $modelDestDir, $animationDestDir)) {
        if (-not (Test-Path -LiteralPath $dir -PathType Container)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
    }
}

$models = [System.Collections.Generic.List[object]]::new()
foreach ($spec in $ModelSpecs) {
    $result = Copy-Asset `
        -SourcePath (Join-Path $SourceVrm $spec.File) `
        -DestPath (Join-Path $modelDestDir $spec.File) `
        -Id $spec.Id -Kind 'model' -File $spec.File
    if ($null -eq $result) { continue }

    # 低配平板优先挑 light：>8MB 的一律 normal（计划 §2.3）。
    $weight = if ($result.Bytes -le 8MB) { 'light' } else { 'normal' }
    $models.Add([ordered]@{
            id           = $spec.Id
            display_name = $spec.DisplayName
            gender       = $spec.Gender
            character    = $spec.Character
            file         = $spec.File
            url          = (Get-RelativeUrl -Prefix $ModelUrlPrefix -FileName $spec.File)
            bytes        = $result.Bytes
            weight       = $weight
            license      = 'local-demo-only'
            sha256       = $result.Sha256
        })
}

$animations = [System.Collections.Generic.List[object]]::new()
$newAnimationCount = 0
$bundledAnimationCount = 0
foreach ($spec in $AnimationSpecs) {
    $sourcePath = if ([string]::IsNullOrEmpty($spec.Source)) { Join-Path $SourceAnimations $spec.File } else { Join-Path $SourceAnimations $spec.Source }
    $result = Copy-Asset `
        -SourcePath $sourcePath `
        -DestPath (Join-Path $animationDestDir $spec.File) `
        -Id $spec.Id -Kind 'animation' -File $spec.File
    if ($null -eq $result) { continue }

    $animations.Add([ordered]@{
            id           = $spec.Id
            display_name = $spec.DisplayName
            scene        = $spec.Scene
            file         = $spec.File
            url          = (Get-RelativeUrl -Prefix $AnimationUrlPrefix -FileName $spec.File)
            bytes        = $result.Bytes
            license      = 'Apache-2.0'
            bundled      = $false
            sha256       = $result.Sha256
        })
    $newAnimationCount++
}

foreach ($spec in $BundledAnimations) {
    $path = Join-Path $animationDestDir $spec.File
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        Add-Skipped -Id $spec.Id -Kind 'animation' -File $spec.File -Reason '仓库内置动作缺失（不应发生，请检查 frontend/public/avatars）'
        continue
    }
    $hash = Get-Sha256Hex -Path $path
    $bytes = (Get-Item -LiteralPath $path).Length
    $animations.Add([ordered]@{
            id           = $spec.Id
            display_name = $spec.DisplayName
            scene        = $spec.Scene
            file         = $spec.File
            url          = (Get-RelativeUrl -Prefix $AnimationUrlPrefix -FileName $spec.File)
            bytes        = $bytes
            license      = 'Apache-2.0'
            bundled      = $true
            sha256       = $hash
        })
    $bundledAnimationCount++
}

$catalog = [ordered]@{
    version      = 1
    generated_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    source       = (Split-Path -Leaf $SourceRoot)
    models       = @($models)
    animations   = @($animations)
    skipped      = @($script:Skipped)
}

# ---------------------------------------------------------------- 写 catalog.json
if ($DryRun) {
    Write-Host '[sync-avatars][dry-run] catalog.json 预览：'
    $catalog | ConvertTo-Json -Depth 6
}
else {
    $catalogPath = Join-Path $DestRoot 'catalog.json'
    $json = $catalog | ConvertTo-Json -Depth 6
    # UTF-8 无 BOM：带 BOM 的 JSON 在部分 fetch/JSON.parse 组合里会报 "Unexpected token"。
    [System.IO.File]::WriteAllText($catalogPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "[sync-avatars] 写入 $catalogPath"
}

# ---------------------------------------------------------------- 写 NOTICE.md
$noticeLines = [System.Collections.Generic.List[string]]::new()
$noticeLines.Add('# 3D 形象与动作资产声明（NOTICE）')
$noticeLines.Add('')
$noticeLines.Add('> 本文件由 `scripts/sync_avatar_assets.ps1` 自动生成，请勿手改；要更新清单就重跑脚本。')
$noticeLines.Add('')
$noticeLines.Add("生成时间（UTC）：$((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))　｜　源目录：$((Split-Path -Leaf $SourceRoot))（本机绝对路径不入库）")
$noticeLines.Add('')
$noticeLines.Add('## 1. 版权与分发红线')
$noticeLines.Add('')
$noticeLines.Add('`models/` 下的 8 个 VRM 形象分别是 **原神（Genshin Impact）** 与 **星穹铁道（Honkai: Star Rail）** 的角色模型，')
$noticeLines.Add('角色形象与模型版权归 **miHoYo / HoYoverse** 所有，模型由社区作者制作并经 vrm-playground 收集。')
$noticeLines.Add('')
$noticeLines.Add('- **仅限本机演示与验收**，不得随仓库提交、不得对外分发、不得用于任何商业用途；')
$noticeLines.Add('- 仓库 `.gitignore` 已忽略 `*.vrm` / `*.glb` / `*.vrma`，`frontend/public/avatars/models/` 与新增动作文件都只存在于开发机本地；')
$noticeLines.Add('- **动作文件同模型一样不入库**：换机器、换 clone 后需重跑 `scripts/sync_avatar_assets.ps1` 重新同步，前端只认在册的 `catalog.json`；')
$noticeLines.Add('- 若要对外演示或开源，必须换成可再分发的模型（如 VRoid Studio 自制 / CC0），')
$noticeLines.Add('  届时只需重跑本脚本更换源目录，前端读的是 `catalog.json`，无需改代码。')
$noticeLines.Add('')
$noticeLines.Add('`animations/` 下有两类动作，入库口径不同：')
$noticeLines.Add('')
$noticeLines.Add('- **6 条新增动作**（`bundled: false`）：来自 vrm-playground 的 `common/` 与 animations 根目录，')
$noticeLines.Add('  按 **Apache License 2.0** 引入（许可证全文见 `animations/LICENSE-Apache-2.0.txt`），但文件本身**不入库**，只留本机；')
$noticeLines.Add('- **3 条既有动作**（`bundled: true`：`idle.vrma` / `idle-talking.vrma` / `nod.vrma`）：历史已 tracked、')
$noticeLines.Add('  沿用原 README 的授权口径，本脚本只登记不覆盖，删掉会破前端 `DEFAULT_*_ANIMATION_URL` 契约。')
$noticeLines.Add('')
$noticeLines.Add('## 2. 本次同步结果')
$noticeLines.Add('')
$noticeLines.Add("已就位：模型 **$($models.Count)/$($ModelSpecs.Count)**，动作 **$($animations.Count)** 条" +
    "（新增 $newAnimationCount 条 + 内置 $bundledAnimationCount 条）。")
$noticeLines.Add('')
$noticeLines.Add("**入库口径**：入库模型 **0/$($models.Count)**（全部本地）；入库动作 **$bundledAnimationCount/$($animations.Count)**" +
    "（内置的 $bundledAnimationCount 条在册，新增的 $newAnimationCount 条本地未入库）。")
$noticeLines.Add('')
$noticeLines.Add('### 2.1 形象（`models/`）')
$noticeLines.Add('')
$noticeLines.Add('| id | 显示名 | 性别 | 文件 | 体积 | 权重档 | sha256（前 12 位） |')
$noticeLines.Add('| --- | --- | --- | --- | --- | --- | --- |')
foreach ($row in $models) {
    $noticeLines.Add("| ``$($row.id)`` | $($row.display_name) | $($row.gender) | ``$($row.file)`` | $([math]::Round($row.bytes / 1MB, 2)) MB | $($row.weight) | ``$($row.sha256.Substring(0, 12))`` |")
}
$noticeLines.Add('')
$noticeLines.Add('### 2.2 动作（`animations/`）')
$noticeLines.Add('')
$noticeLines.Add('| id | 场景 | 显示名 | 文件 | 体积 | 来源 |')
$noticeLines.Add('| --- | --- | --- | --- | --- | --- |')
foreach ($row in $animations) {
    $origin = if ($row.bundled) { '仓库内置' } else { 'playground common/' }
    $noticeLines.Add("| ``$($row.id)`` | $($row.scene) | $($row.display_name) | ``$($row.file)`` | $([math]::Round($row.bytes / 1KB, 0)) KB | $origin |")
}
$noticeLines.Add('')
if ($script:Skipped.Count -gt 0) {
    $noticeLines.Add('### 2.3 未同步条目（源缺失 / 目标冲突）')
    $noticeLines.Add('')
    $noticeLines.Add('| kind | id | 文件 | 原因 |')
    $noticeLines.Add('| --- | --- | --- | --- |')
    foreach ($row in $script:Skipped) {
        $noticeLines.Add("| $($row.kind) | ``$($row.id)`` | ``$($row.file)`` | $($row.reason) |")
    }
    $noticeLines.Add('')
}
else {
    $noticeLines.Add('### 2.3 未同步条目')
    $noticeLines.Add('')
    $noticeLines.Add('无。全部清单条目均已就位。')
    $noticeLines.Add('')
}
$noticeLines.Add('## 3. 前端怎么用')
$noticeLines.Add('')
$noticeLines.Add('前端只读 `catalog.json`：`frontend/src/pad/avatar/loadAvatar.ts` 拉取并校验它，')
$noticeLines.Add('按 `scene` 字段挑动作、按 `weight` 字段挑低配优先的形象；catalog 缺失或解析失败时')
$noticeLines.Add('回落到代码里的硬编码默认值（`DEFAULT_AVATAR_URL` 等），不会白屏。')
$noticeLines.Add('')
$noticeLines.Add('动作同理：`catalog.json` 里登记了、但本机没有对应文件的场景（新 clone / 没跑同步脚本），')
$noticeLines.Add('`loadVrmAnimation` 会失败并被 `ensureSceneAction` 静默吞掉，角色退回内置的')
$noticeLines.Add('`idle.vrma` / `idle-talking.vrma` / `nod.vrma` 三条，不会卡住渲染循环、也不触发 5s 降级。')
$noticeLines.Add('')

if ($DryRun) {
    Write-Host '[sync-avatars][dry-run] NOTICE.md 预览：'
    $noticeLines | ForEach-Object { Write-Host $_ }
}
else {
    $noticePath = Join-Path $DestRoot 'NOTICE.md'
    [System.IO.File]::WriteAllText($noticePath, ($noticeLines -join "`n") + "`n", (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "[sync-avatars] 写入 $noticePath"
}

# ---------------------------------------------------------------- 汇总
Write-Host ''
Write-Host '[sync-avatars] Summary'
Write-Host ("  models     : {0}/{1}" -f $models.Count, $ModelSpecs.Count)
Write-Host ("  animations : {0}（新增 {1} / 内置 {2}）" -f $animations.Count, $newAnimationCount, $bundledAnimationCount)
Write-Host ("  copied     : {0}" -f $script:Copied.Count)
Write-Host ("  skipped    : {0}" -f $script:Skipped.Count)
if (-not $sourceAvailable) {
    Write-Host '[sync-avatars][warn] 源目录不存在，本次只登记了仓库内置动作；形象需要源目录可用时重跑。' -ForegroundColor Yellow
}
foreach ($row in $script:Skipped) {
    Write-Host ("  - {0}/{1}: {2}" -f $row.kind, $row.id, $row.reason) -ForegroundColor Yellow
}
