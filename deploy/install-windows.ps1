# 把 facet 注册为 Windows 计划任务（开机自启 + 崩溃重启）
#
#   .\install-windows.ps1                 # 安装（以当前用户身份运行）
#   .\install-windows.ps1 -Uninstall      # 卸载
#   .\install-windows.ps1 -Interval 3600  # 自定义采集间隔（秒）
#
# 装好后：
#   Get-ScheduledTask facet | Get-ScheduledTaskInfo
#   Stop-ScheduledTask facet ; Start-ScheduledTask facet
#
# 说明：用「计划任务」而不是 Windows 服务，是因为服务运行在 Session 0，
#       本项目自带 Web 看板，计划任务在用户会话里跑更符合个人使用场景，
#       且不需要额外安装 nssm 之类的包装工具。

[CmdletBinding()]
param(
    [switch]$Uninstall,
    [int]$Interval = 1800,
    [string]$TaskName = 'facet'
)

$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectDir '.venv\Scripts\python.exe'
$Bootstrap = Join-Path $ProjectDir 'bootstrap.py'

function Write-Ok   { param($m) Write-Host "  " -NoNewline; Write-Host "+" -ForegroundColor Green -NoNewline; Write-Host " $m" }
function Write-Warn2{ param($m) Write-Host "  " -NoNewline; Write-Host "!" -ForegroundColor Yellow -NoNewline; Write-Host " $m" }
function Write-Err2 { param($m) Write-Host "  " -NoNewline; Write-Host "x" -ForegroundColor Red -NoNewline; Write-Host " $m" }
function Write-Dim  { param($m) Write-Host "    " -NoNewline; Write-Host $m -ForegroundColor DarkGray }

if ($Uninstall) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Ok "已卸载计划任务 $TaskName（项目文件与数据库未删除）"
    } else {
        Write-Warn2 "未找到计划任务 $TaskName"
    }
    exit 0
}

# ── 前置检查 ───────────────────────────────────────────────
if (-not (Test-Path -LiteralPath $Bootstrap)) {
    Write-Err2 "找不到 $Bootstrap"
    exit 1
}

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Warn2 "尚未创建虚拟环境，先执行首次配置…"
    & (Join-Path $ProjectDir 'start.ps1') --setup
    if (-not (Test-Path -LiteralPath $Python)) {
        Write-Err2 "虚拟环境仍不可用：$Python"
        Write-Dim "请先手动运行：.\start.ps1 --setup"
        exit 1
    }
}

# ── 注册任务 ───────────────────────────────────────────────
$action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "`"$Bootstrap`" --foreground --skip-install --interval $Interval" `
    -WorkingDirectory $ProjectDir

$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -RestartCount 999 `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Warn2 "已存在同名任务，先移除旧的"
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "facet - CS 饰品多源行情监控 (BUFF / 悠悠有品)" | Out-Null

Write-Ok "已注册计划任务 $TaskName（登录时自动启动，异常退出 2 分钟后重启）"
Write-Dim "采集间隔：$Interval 秒"

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2
$info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
Write-Ok "任务已启动（上次结果码：$($info.LastTaskResult)）"
Write-Host ""
Write-Dim "查看状态： Get-ScheduledTask $TaskName | Get-ScheduledTaskInfo"
Write-Dim "停止：     Stop-ScheduledTask $TaskName"
Write-Dim "启动：     Start-ScheduledTask $TaskName"
Write-Dim "卸载：     .\deploy\install-windows.ps1 -Uninstall"
Write-Dim "日志：     $ProjectDir\logs\facet.log"
