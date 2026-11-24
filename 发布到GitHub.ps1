<#
.SYNOPSIS
  把「刷校园跑」按发布清单同步到本地仓库目录，脱敏后提交并推送到 GitHub。

.DESCRIPTION
  发布清单（与 快速开始.md 的「本仓库包含 / 不包含」一致）：
    不发布：*.exe 及打包产物、config*.json、session*.json、device_id、logs/、cache/、
            真实 route_library.json / anchor_points.json、项目记忆.md、
            原始脱壳资料/、_analysis/、__pycache__/、.idea/
    发布：  协议/（Python 源码 + 校园数据）、电脑一键启动/（全部 .py/.pyw）、
            工具脚本、设计文档、.claude/、node_modules（自动极验依赖）
  脱敏：文档里出现的真实手机号替换成 账号N 再提交。

.PARAMETER Message
  提交说明；省略时用「更新：yyyy-MM-dd HH:mm」。

.PARAMETER NoPush
  只同步 + 提交，不推送（先看看 git status 时用）。

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\发布到GitHub.ps1 -NoPush
  powershell -NoProfile -ExecutionPolicy Bypass -File .\发布到GitHub.ps1 -Message "更新路线编辑器说明"
#>
param(
    [string]$Message = "",
    [switch]$NoPush
)

$ErrorActionPreference = "Continue"

$Src = $PSScriptRoot
$Dst = Join-Path (Split-Path $Src -Parent) "yundongshijie-git"
$Remote = "https://github.com/jaceyone/yundongshijie.git"
$Branch = "main"
$Placeholder = "13800000000"

Write-Host "源目录: $Src"
Write-Host "发布目录: $Dst"

if (-not (Test-Path $Dst)) { New-Item -ItemType Directory -Path $Dst | Out-Null }

# ---------------------------------------------------------------- 1. 同步
$excludeDirs = @(
    "原始脱壳资料", "_analysis", "__pycache__", ".idea", "cache", "logs",
    ".pytest_cache", ".git", "yundongshijie-git"
)
$excludeFiles = @(
    "项目记忆.md", "config.json", "config_b.json", "config.json.bak-*",
    "session.json", "session_b.json", "session*.json", "device_id",
    "route_library.json", "route_library.json.bak-*", "route_history.jsonl",
    "anchor_points.json", "*.pyc", "*.exe", "*.msi", "*.tmp"
)
robocopy $Src $Dst /E /NFL /NDL /NJH /NJS /NP /XD @excludeDirs /XF @excludeFiles 2>&1 | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy 失败，退出码 $LASTEXITCODE" }

# ---------------------------------------------------------------- 2. 脱敏
$phonePattern = '\b1[3-9]\d{9}\b'
$script:phoneMap = @{}
$script:phoneCount = 0
$files = Get-ChildItem -Recurse -Force -File $Dst -Include *.md, *.txt, *.py, *.pyw, *.json, *.ps1 |
    Where-Object { $_.FullName -notmatch '\\\.git\\|\\node_modules\\' }

foreach ($file in $files) {
    $text = Get-Content $file.FullName -Raw -Encoding UTF8
    if ($null -eq $text) { continue }
    $new = [regex]::Replace($text, $phonePattern, {
            param($m)
            $value = $m.Value
            if ($value -eq $Placeholder) { return $value }
            if (-not $script:phoneMap.ContainsKey($value)) {
                $script:phoneCount++
                $script:phoneMap[$value] = "账号" + [char](64 + $script:phoneCount)
            }
            return $script:phoneMap[$value]
        })
    if ($new -ne $text) {
        Set-Content $file.FullName -Value $new -Encoding UTF8 -NoNewline
        Write-Host ("脱敏: " + $file.FullName.Substring($Dst.Length + 1))
    }
}
if ($script:phoneMap.Count -gt 0) {
    Write-Host "手机号映射:"
    $script:phoneMap.GetEnumerator() | Sort-Object Value | ForEach-Object {
        Write-Host ("  " + $_.Key + " -> " + $_.Value)
    }
}

# ---------------------------------------------------------------- 3. 提交并推送
Push-Location $Dst
try {
    if (-not (Test-Path (Join-Path $Dst ".git"))) {
        git init -b $Branch 2>&1 | Out-Null
    }
    git config core.quotepath false

    $remotes = git remote 2>&1
    if (-not ($remotes | Select-String -SimpleMatch "origin")) {
        git remote add origin $Remote 2>&1 | Out-Null
    }

    git add -A 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "git add 失败（退出码 $LASTEXITCODE）" }

    $pending = @(git status --porcelain 2>&1 | Where-Object { $_ -match '^\s*[AMDRCU?!]' })
    if ($pending.Count -eq 0) {
        Write-Host "没有变化，跳过提交。"
        return
    }
    Write-Host ("待提交变更: " + $pending.Count + " 个文件")

    if (-not $Message) { $Message = "更新：" + (Get-Date -Format "yyyy-MM-dd HH:mm") }
    $tmpMessage = Join-Path ([System.IO.Path]::GetTempPath()) ("commit-" + [guid]::NewGuid().ToString("N") + ".txt")
    [System.IO.File]::WriteAllText($tmpMessage, $Message, [System.Text.UTF8Encoding]::new($false))
    git commit -F $tmpMessage --quiet 2>&1 | Out-Null
    $commitExit = $LASTEXITCODE
    Remove-Item $tmpMessage -Force -ErrorAction SilentlyContinue
    if ($commitExit -ne 0) { throw "git commit 失败（退出码 $commitExit）" }
    Write-Host ("已提交: " + (git log --oneline -1))

    if ($NoPush) {
        Write-Host "已跳过推送（-NoPush）。"
        return
    }

    $env:GIT_TERMINAL_PROMPT = "0"
    $env:GCM_INTERACTIVE = "never"
    git push origin $Branch
    if ($LASTEXITCODE -ne 0) { throw "推送失败（退出码 $LASTEXITCODE）" }
    Write-Host "已推送到 $Remote ($Branch)"
}
finally {
    Pop-Location
}
