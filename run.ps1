# 本地 API 测试台 - PowerShell 启动器
# 用法:
#   .\run.ps1                     # 打开 GUI
#   .\run.ps1 --help              # 透传参数给 api_tester.py
#   .\run.ps1 --base-url http://127.0.0.1:18080/v1 --api-key sk-test --all
#   .\run.ps1 --gui

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

# 控制台强制 UTF-8, 否则中文输出在 GBK 代码页下全是乱码
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUTF8 = '1'
} catch { }

function Resolve-Python {
    foreach ($cand in @('python', 'python3', 'py')) {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if ($cmd) {
            if ($cand -eq 'py') { return @($cmd.Source, '-3') }
            return @($cmd.Source)
        }
    }
    return $null
}

$py = @(Resolve-Python)   # 必须用 @() 包住: 单元素数组会被 PS 自动解包成字符串, 之后 $py[0] 就取到第一个"字符"(C)
if ($py.Count -eq 0 -or -not $py[0]) {
    Write-Host ''
    Write-Host '[错误] PATH 里找不到 python / python3 / py' -ForegroundColor Red
    Write-Host '       请安装 Python 3.8+ 并勾选 "Add Python to PATH"' -ForegroundColor Red
    Write-Host ''
    exit 1
}

Write-Host '============================================' -ForegroundColor DarkGray
Write-Host '  Local API Tester  -  starting...' -ForegroundColor Cyan
Write-Host '============================================' -ForegroundColor DarkGray
Write-Host ("  解释器: " + ($py -join ' ')) -ForegroundColor DarkGray

$exe = [string]$py[0]
$pre = @()
if ($py.Count -gt 1) { $pre = @($py[1..($py.Count - 1)]) }

$script = Join-Path $PSScriptRoot 'api_tester.py'
if (-not (Test-Path -LiteralPath $script)) {
    Write-Host "[错误] 找不到 $script" -ForegroundColor Red
    exit 1
}
if ($Args.Count -eq 0) {
    & $exe @pre $script --gui
} else {
    & $exe @pre $script @Args
}

exit $LASTEXITCODE
