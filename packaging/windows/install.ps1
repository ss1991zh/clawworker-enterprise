<#
  Clawworker Windows 安装脚本
  作用:建 venv → 装依赖 → 装 4 个 HE 库 → 在桌面生成图标(双击即开界面)。

  用法(在 PowerShell 7 / Windows Terminal 里,项目根目录执行):
      # 管理端机器:
      powershell -ExecutionPolicy Bypass -File packaging\windows\install.ps1 -Role admin
      # 用户端机器:
      powershell -ExecutionPolicy Bypass -File packaging\windows\install.ps1 -Role client
      # 单机一体(两个图标都建):
      powershell -ExecutionPolicy Bypass -File packaging\windows\install.ps1 -Role both

  前置:① 已装 Python 3.11+(勾选 Add to PATH);
        ② 把你的 4 个 HE 库源码目录(crypto_toolkit-64_dev / henumpy-dev /
           pandaseal-dev / helearn-dev,内含 win64 DLL)放进 packaging\windows\he_libs\。
#>
param(
    [ValidateSet("admin", "client", "both")]
    [string]$Role = "client",
    [switch]$NoShortcut    # 由 .exe 安装包调用时传:仅建环境,快捷方式交给安装包管理(便于卸载)
)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

# ---- 路径 ----
$Here    = Split-Path -Parent $MyInvocation.MyCommand.Path        # packaging\windows
$Project = (Resolve-Path (Join-Path $Here "..\..")).Path          # 项目根
$Venv    = Join-Path $Project ".venv"
$PyW     = Join-Path $Venv "Scripts\pythonw.exe"
$Py      = Join-Path $Venv "Scripts\python.exe"
$Launch  = Join-Path $Here "clawworker_launch.py"
$Icon    = Join-Path $Here "clawworker.ico"
$HeLibs  = Join-Path $Here "he_libs"

Write-Host "==== Clawworker 安装 ($Role) ====" -ForegroundColor Cyan
Write-Host "项目根: $Project"

# ---- 1. 找系统 Python(真正验证可运行)----
function Test-PyCmd($exe, $verArg) {
    try {
        if ($verArg) { & $exe $verArg --version *> $null } else { & $exe --version *> $null }
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}
$pyExe = $null; $pyArg = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($v in @("-3.11", "-3.12", "-3.13", "-3")) {
        if (Test-PyCmd "py" $v) { $pyExe = "py"; $pyArg = $v; break }
    }
}
if (-not $pyExe -and (Get-Command python -ErrorAction SilentlyContinue) -and (Test-PyCmd "python" $null)) {
    $pyExe = "python"
}
if (-not $pyExe) { throw "未找到可用的 Python 3.11+。请先安装并勾选 Add to PATH。" }
Write-Host "系统 Python: $pyExe $pyArg"

# ---- 2. 建 venv(PS 自动处理含空格/中文的路径)----
if (-not (Test-Path $Py)) {
    Write-Host "创建虚拟环境 .venv ..." -ForegroundColor Yellow
    if ($pyArg) { & $pyExe $pyArg -m venv $Venv } else { & $pyExe -m venv $Venv }
    if (-not (Test-Path $Py)) { throw "创建 venv 失败:$Venv" }
}
& $Py -m pip install --upgrade pip --quiet

# ---- 3. 装依赖 ----
Write-Host "安装依赖(requirements.txt)..." -ForegroundColor Yellow
& $Py -m pip install -r (Join-Path $Here "requirements.txt") --quiet

# ---- 4. 装 4 个 HE 库 ----
$libs = @("crypto_toolkit-64_dev", "henumpy-dev", "pandaseal-dev", "helearn-dev")
if (Test-Path $HeLibs) {
    foreach ($l in $libs) {
        $d = Join-Path $HeLibs $l
        if (Test-Path $d) { Write-Host "安装 HE 库 $l ..." -ForegroundColor Yellow; & $Py -m pip install -e $d --quiet }
        else { Write-Warning "缺少 HE 库目录: $d" }
    }
} else {
    Write-Warning "未找到 $HeLibs —— 请把 4 个 HE 库源码目录放进去后重跑(否则密态功能无法初始化)。"
}

# ---- 5. 桌面图标 ----
function New-Shortcut($name, $roleArg) {
    $desktop = [Environment]::GetFolderPath("Desktop")
    $lnk = Join-Path $desktop "$name.lnk"
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnk)
    $sc.TargetPath = $PyW
    $sc.Arguments = "`"$Launch`" $roleArg"
    $sc.WorkingDirectory = $Project
    if (Test-Path $Icon) { $sc.IconLocation = $Icon }
    $sc.Description = "Clawworker $name"
    $sc.Save()
    Write-Host "桌面图标已创建: $lnk" -ForegroundColor Green
}

if (-not $NoShortcut) {
    if ($Role -eq "admin" -or $Role -eq "both") { New-Shortcut "Clawworker 管理端" "admin" }
    if ($Role -eq "client" -or $Role -eq "both") { New-Shortcut "Clawworker 用户端" "client" }
}

Write-Host ""
Write-Host "==== 安装完成 ====" -ForegroundColor Cyan
Write-Host "双击桌面图标即可启动并打开界面(首次启动需几十秒初始化密钥)。"
Write-Host "可选:开机自启 → 在界面「设置→自启/运维」开启,或运行 schtasks 自启(见 README)。"
