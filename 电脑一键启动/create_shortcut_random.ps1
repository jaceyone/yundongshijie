# 创建「校园跑随机开跑」桌面快捷方式
#
# 运行：
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\create_shortcut_random.ps1

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$App = Join-Path $ScriptDir "随机开跑.pyw"
$Icon = Join-Path $ScriptDir "assets\app.ico"

if (-not (Test-Path $App)) {
    Write-Error "找不到 $App"
}

$pythonw = $null
$cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($cmd) {
    $pythonw = $cmd.Source
}

if (-not $pythonw) {
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        $candidate = Join-Path (Split-Path $python.Source) "pythonw.exe"
        if (Test-Path $candidate) {
            $pythonw = $candidate
        }
    }
}

if (-not $pythonw -or -not (Test-Path $pythonw)) {
    Write-Error "找不到 pythonw.exe，请确认 Python 已安装并加入 PATH。"
}

$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $Desktop "校园跑随机开跑.lnk"

$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $pythonw
$Shortcut.Arguments = '"' + $App + '"'
$Shortcut.WorkingDirectory = $ScriptDir
$Shortcut.Description = "校园跑随机开跑（距离 3000-4000 / 路线 / 锚点 / 配速全随机）"
if (Test-Path $Icon) {
    $Shortcut.IconLocation = "$Icon,0"
}
$Shortcut.Save()

Write-Host ""
Write-Host "已创建桌面快捷方式："
Write-Host "  $ShortcutPath"
Write-Host ""
Write-Host "双击桌面上的「校园跑随机开跑」即会自动跑今天的一次。"
