# patent-figure-kit 一键安装并启动（Windows 10/11 x64）。
#
# 用法：右键本文件 →「使用 PowerShell 运行」。首次几分钟（取决于网络），之后秒开。
# 若提示「无法加载，因为在此系统上禁止运行脚本」或文件来自网络被阻止：
#   在 PowerShell 里执行  Unblock-File -Path .\安装并启动.ps1   后再右键运行；
#   或  powershell -ExecutionPolicy Bypass -File .\安装并启动.ps1
#
# 参数： -NoStart（只安装与自检）  -Port 8791  -Python C:\路径\python.exe  -Reinstall
#        -Data D:\工作台数据（默认 %USERPROFILE%\.patent-workbench）  -NoBrowser
# 虚拟环境放在 %LOCALAPPDATA%\patent-figure-kit\（不放进仓库）；安装日志与计时也在那里。
# 安全约定：路径一律作为参数传递（支持空格与中文）；不使用 Invoke-Expression，不拼接命令字符串。
# 本文件以 UTF-8（带 BOM）保存：Windows PowerShell 5.1 才能正确读中文。

[CmdletBinding()]
param(
    [switch]$NoStart,
    [int]$Port = 8790,
    [string]$Python = $env:PFK_PYTHON,
    [switch]$Reinstall,
    [string]$Data = '',
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($env:PFK_HOME) { $StateDir = $env:PFK_HOME } else { $StateDir = Join-Path $env:LOCALAPPDATA 'patent-figure-kit' }
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
$LogFile = Join-Path $StateDir 'install.log'
$transcript = $false
try { Start-Transcript -Path $LogFile -Append | Out-Null; $transcript = $true } catch { }

function Stop-WithHint([string]$Message, [string]$Hint) {
    Write-Host ''
    Write-Host "【未完成】$Message" -ForegroundColor Red
    if ($Hint) { Write-Host "修复建议：$Hint" -ForegroundColor Yellow }
    Write-Host "完整日志：$LogFile"
    if ($transcript) { try { Stop-Transcript | Out-Null } catch { } }
    if (-not $NoStart) { Read-Host '按回车关闭窗口' | Out-Null }
    exit 1
}

Write-Host ''
Write-Host ("==== patent-figure-kit 安装并启动  {0} ====" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Write-Host "仓库：$RepoDir"
Write-Host "安装目录：$StateDir"

if (-not (Test-Path -LiteralPath (Join-Path $RepoDir 'requirements-pinned.txt')) -or
    -not (Test-Path -LiteralPath (Join-Path $RepoDir 'scripts\pfk_cli.py'))) {
    Stop-WithHint '没找到 requirements-pinned.txt / scripts\pfk_cli.py' '请在完整解压的仓库目录里运行本脚本'
}

# ---------------------------------------------------------------- 平台：只支持 x64
$arch = $env:PROCESSOR_ARCHITECTURE
if ($env:PROCESSOR_ARCHITEW6432) { $arch = $env:PROCESSOR_ARCHITEW6432 }
if ($arch -ne 'AMD64') {
    Stop-WithHint "不支持的处理器架构：$arch（几何内核 cadquery-ocp 7.7.2 只有 Windows x64 版本）" '换一台 x64 电脑'
}

# ---------------------------------------------------------------- Python 3.9–3.12
# 每个候选是「可执行文件 + 前置参数」的数组，例如 @('py', '-3.12')；调用时用 & 展开，不拼字符串。
function Get-PyVersion([string[]]$Cmd) {
    try {
        $exe = $Cmd[0]
        $pre = @()
        if ($Cmd.Count -gt 1) { $pre = $Cmd[1..($Cmd.Count - 1)] }
        $out = & $exe @pre '-c' 'import sys; print("%d.%d" % sys.version_info[:2])' 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return ($out | Select-Object -First 1).Trim()
    } catch { return $null }
}

$Supported = @('3.12', '3.11', '3.10', '3.9')
$PyCmd = $null
if ($Python) {
    $v = Get-PyVersion @($Python)
    if ($Supported -notcontains $v) { Stop-WithHint "指定的 Python 不可用或版本不在 3.9–3.12：$Python" '' }
    $PyCmd = @($Python)
} else {
    $cands = @()
    foreach ($s in $Supported) { $cands += ,@('py', "-$s") }
    $cands += ,@('python')
    $cands += ,@('python3')
    foreach ($c in $cands) {
        $found = Get-Command $c[0] -ErrorAction SilentlyContinue
        if (-not $found) { continue }
        # Microsoft Store 的 python.exe 占位程序在 WindowsApps 下：执行它只会打开商店
        if ($found.Source -and $found.Source -like '*\WindowsApps\*') { continue }
        $v = Get-PyVersion $c
        if ($Supported -contains $v) { $PyCmd = $c; break }
    }
}
if (-not $PyCmd) {
    Stop-WithHint '没找到 Python 3.9–3.12（3.13 及以上暂不支持：几何内核没有对应版本）' `
        '到 https://www.python.org/downloads/windows/ 安装 Python 3.12（64 位，勾选 Add python.exe to PATH），装好后重新运行'
}
$PyExe = $PyCmd[0]
$PyPre = @()
if ($PyCmd.Count -gt 1) { $PyPre = $PyCmd[1..($PyCmd.Count - 1)] }
$PyVer = Get-PyVersion $PyCmd
Write-Host "Python：$($PyCmd -join ' ')（$PyVer）"
$bits = & $PyExe @PyPre '-c' 'import struct; print(struct.calcsize("P") * 8)'
if ("$bits".Trim() -ne '64') { Stop-WithHint '找到的是 32 位 Python' '请安装 64 位 Python 3.12' }

# ---------------------------------------------------------------- 虚拟环境 + 锁定依赖
$Venv = Join-Path $StateDir "venv-py$PyVer"
$VPy = Join-Path $Venv 'Scripts\python.exe'
$Stamp = Join-Path $Venv '.pfk-install-stamp'
$ReqCore = Join-Path $RepoDir 'requirements-pinned.txt'
$ReqWeb = Join-Path $RepoDir 'requirements-web.txt'
$sha = [System.Security.Cryptography.SHA256]::Create()
$buf = [System.Text.Encoding]::UTF8.GetBytes($PyVer)
$buf += [System.IO.File]::ReadAllBytes($ReqCore)
$buf += [System.IO.File]::ReadAllBytes($ReqWeb)
$WantStamp = -join ($sha.ComputeHash($buf) | ForEach-Object { $_.ToString('x2') })

$upToDate = $false
if (-not $Reinstall -and (Test-Path -LiteralPath $VPy) -and (Test-Path -LiteralPath $Stamp)) {
    if ((Get-Content -LiteralPath $Stamp -Raw).Trim() -eq $WantStamp) {
        & $VPy '-c' 'import OCP, ezdxf, fastapi, pypdfium2' 2>$null
        if ($LASTEXITCODE -eq 0) { $upToDate = $true }
    }
}

if ($upToDate) {
    Write-Host '依赖已是最新（跳过安装）。'
    $InstallSeconds = 0
    $InstallKind = 'skip'
} else {
    Write-Host '安装依赖（首次约需几分钟，取决于网络；几何内核约 90 MB）…'
    $t0 = Get-Date
    if (-not (Test-Path -LiteralPath $VPy)) {
        if (Test-Path -LiteralPath $Venv) { Remove-Item -LiteralPath $Venv -Recurse -Force }
        & $PyExe @PyPre '-m' 'venv' $Venv
        if ($LASTEXITCODE -ne 0) { Stop-WithHint "建虚拟环境失败：$Venv" '检查磁盘空间与杀毒软件拦截' }
    }
    & $VPy '-m' 'pip' 'install' '--upgrade' 'pip'
    if ($LASTEXITCODE -ne 0) {
        Stop-WithHint '升级 pip 失败（网络？）' '检查网络；国内网络可先执行  $env:PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"  再重新运行'
    }
    & $VPy '-m' 'pip' 'install' '-r' $ReqCore '-r' $ReqWeb
    if ($LASTEXITCODE -ne 0) {
        Stop-WithHint '安装依赖失败' '多为网络问题：重新运行即可续装；国内网络可先执行  $env:PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"'
    }
    Set-Content -LiteralPath $Stamp -Value $WantStamp -Encoding ASCII
    $InstallSeconds = [int]((Get-Date) - $t0).TotalSeconds
    $InstallKind = 'install'
    Write-Host "依赖安装完成，用时 $InstallSeconds 秒。"
}

$pipIndex = 'pypi.org'
if ($env:PIP_INDEX_URL) { $pipIndex = $env:PIP_INDEX_URL }
$timing = [ordered]@{
    script = '安装并启动.ps1'; os = 'Windows'; os_release = [System.Environment]::OSVersion.VersionString
    arch = $arch; python = $PyVer; kind = $InstallKind; seconds = $InstallSeconds
    pip_index = $pipIndex; at = (Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz')
}
Add-Content -LiteralPath (Join-Path $StateDir 'install-timing.json') -Value ($timing | ConvertTo-Json -Compress) -Encoding UTF8
Write-Host ("计时记录：{0}" -f (Join-Path $StateDir 'install-timing.json'))

# ---------------------------------------------------------------- 自检
Write-Host ''
Write-Host '环境自检 …'
$Cli = Join-Path $RepoDir 'scripts\pfk_cli.py'
& $VPy $Cli 'paths' '--check' | Out-Null
if ($LASTEXITCODE -ne 0) { Stop-WithHint '仓库文件不完整' '重新下载 / 解压完整的仓库' }
& $VPy $Cli 'doctor' '--profile' 'web'
if ($LASTEXITCODE -ne 0) { Stop-WithHint '环境自检未通过（见上面的「修复」行）' '按提示修复后重新运行；或加 -Reinstall 强制重装依赖' }

if ($NoStart) {
    Write-Host ''
    Write-Host "安装完成（-NoStart：不启动）。以后启动：& `"$VPy`" `"$Cli`" start"
    if ($transcript) { try { Stop-Transcript | Out-Null } catch { } }
    exit 0
}

Write-Host ''
Write-Host '启动工作台（关闭本窗口或按 Ctrl+C 即停止）…'
if ($transcript) { try { Stop-Transcript | Out-Null } catch { } }
$startArgs = @('start', '--auto-port', '--port', "$Port")
if ($Data) { $startArgs += @('--data', $Data) }
if ($NoBrowser) { $startArgs += '--no-browser' }
& $VPy $Cli @startArgs
$rc = $LASTEXITCODE
if ($rc -ne 0) { Read-Host '工作台已退出（见上面的提示），按回车关闭窗口' | Out-Null }
exit $rc
