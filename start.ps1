# youyoumonitor 一键启动（Windows PowerShell）
#
#   .\start.ps1                 # 建环境 → 装依赖 → 自检 → 启动采集+看板
#   .\start.ps1 --setup         # 只准备环境
#   .\start.ps1 --check         # 只自检
#   .\start.ps1 --daemon        # 后台常驻
#   .\start.ps1 --stop          # 停止后台实例
#   .\start.ps1 --status        # 查看状态
#
# 若提示「禁止运行脚本」，用下面任一方式：
#   powershell -ExecutionPolicy Bypass -File .\start.ps1
#   或直接双击 start.cmd
#
# 编码说明：本文件保存为 **UTF-8 with BOM**。
#   Windows PowerShell 5.1 读取无 BOM 的 .ps1 时会按系统 ANSI（中文系统即 GBK）
#   解码，中文注释会变成乱码，严重时字节错位还会破坏语法。
#   保留 BOM 是让 5.1 与 7+ 都能正确读取的唯一简单办法。

[CmdletBinding()]
param(
    # 不要用 $Args 作参数名 —— 它是 PowerShell 的自动变量，会和未绑定参数的
    # 收集机制打架。叫 ExtraArgs 更明确。
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Write-Say  { param($m) Write-Host $m }
function Write-Ok   { param($m) Write-Host "  " -NoNewline; Write-Host "OK" -ForegroundColor Green -NoNewline; Write-Host " $m" }
function Write-Warn2{ param($m) Write-Host "  " -NoNewline; Write-Host "!" -ForegroundColor Yellow -NoNewline; Write-Host " $m" }
function Write-Err2 { param($m) Write-Host "  " -NoNewline; Write-Host "X" -ForegroundColor Red -NoNewline; Write-Host " $m" }

# ── 找 Python 3.10+ ────────────────────────────────────────
# 优先复用已建好的 venv，第二次启动会快很多。
# 注意：新版 Python 可能带 Windows Store 的 App Execution Alias 存根，
# 那种「解释器」执行时会弹商店页面，所以必须真的跑一次 -c 才算数。

function Test-Python {
    param([string]$Exe, [string[]]$Pre = @())
    try {
        $probe = $Pre + @('-c', 'import sys;print(1 if sys.version_info>=(3,10) else 0)')
        $out = & $Exe @probe 2>$null
        return ("$out".Trim() -eq '1')
    } catch { return $false }
}

function Find-Python {
    $venvPy = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPy) { return @{ Exe = $venvPy; Pre = @() } }

    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($ver in @('-3.13', '-3.12', '-3.11', '-3.10')) {
            if (Test-Python -Exe 'py' -Pre @($ver)) {
                return @{ Exe = 'py'; Pre = @($ver) }
            }
        }
    }

    foreach ($name in @('python3', 'python')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and (Test-Python -Exe $cmd.Source)) {
            return @{ Exe = $cmd.Source; Pre = @() }
        }
    }
    return $null
}

$found = Find-Python
if (-not $found) {
    Write-Err2 "找不到 Python 3.10 或更高版本"
    Write-Say ""
    Write-Say "  安装方式（任选其一）："
    Write-Say "    1) 微软商店搜索 Python 3.12 安装"
    Write-Say "    2) 官网下载 https://www.python.org/downloads/  安装时务必勾选 Add python.exe to PATH"
    Write-Say ""
    Write-Say "  装完关闭并重新打开终端，再执行 .\start.ps1"
    exit 2
}

# 透传所有参数给 bootstrap.py（含 --setup / --daemon / --stop 等）
& $found.Exe @($found.Pre) (Join-Path $PSScriptRoot 'bootstrap.py') @ExtraArgs
exit $LASTEXITCODE
