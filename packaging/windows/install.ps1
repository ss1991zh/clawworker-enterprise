<#
  Clawworker Windows 安装脚本
  作用:建 venv → 按角色装依赖 → 用户端装 4 个 HE 库 → 在桌面生成图标。

  用法(在 PowerShell 7 / Windows Terminal 里,项目根目录执行):
      # 管理端机器:
      powershell -ExecutionPolicy Bypass -File packaging\windows\install.ps1 -Role admin
      # 用户端机器:
      powershell -ExecutionPolicy Bypass -File packaging\windows\install.ps1 -Role client
      # 单机一体(两个图标都建):
      powershell -ExecutionPolicy Bypass -File packaging\windows\install.ps1 -Role both

  EXE 安装包已内置 Python 3.11、全部 wheel 与 4 个 HE 库,目标机无需预装 Python
  或联网。直接运行本脚本时,仍需确保 packaging\windows 下具备这些离线资源。
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
$Wheels  = Join-Path $Here "wheels"                    # 离线 wheel 目录(离线包会带上)
$PyBundle = Join-Path $Here "python-3.11.9-amd64.exe"   # 随包 Python 安装器(离线包会带上)
$RequirementsName = if ($Role -eq "both") { "requirements.txt" } else { "requirements-$Role.txt" }
$RoleRequirements = Join-Path $Here $RequirementsName
$DesktopRequirements = Join-Path $Here "requirements-desktop.txt"
$WebView2Installer = Join-Path $Here "webview2\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
$SqlServerOdbcInstaller = Join-Path $Here "db_drivers\msodbcsql18-x64.msi"
$VcRedistInstaller = Join-Path $Here "db_drivers\vc_redist.x64.exe"

Write-Host "==== Clawworker 安装 ($Role) ====" -ForegroundColor Cyan
Write-Host "项目根: $Project"

# 离线模式:检测到 wheels\ 就完全用本地 wheel 装,不联网。
$PipArgs = @()
if (Test-Path $Wheels) {
    $PipArgs = @("--no-index", "--find-links", $Wheels)
    Write-Host "离线模式:使用随包 wheels 安装依赖(无需联网)" -ForegroundColor Yellow
}

# ---- 1. 固定 Python 3.11 运行时。----
# 离线 wheel 是 cp311/win_amd64 集合,不能因为目标机已有 Python 3.12/3.13 就误用它。
# 优先复用可运行的 3.11;没有就静默安装随包 3.11。只有在没有离线包的源码安装模式下,
# 才允许回退到其他 3.11+ 版本并联网解析依赖。
function Test-PyCmd($exe, $verArg) {
    try {
        if ($verArg) { & $exe $verArg --version *> $null } else { & $exe --version *> $null }
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}
$pyExe = $null; $pyArg = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    if (Test-PyCmd "py" "-3.11") { $pyExe = "py"; $pyArg = "-3.11" }
}

# 没找到 Python 3.11 但随包带了安装器 → 静默装(仅当前用户,免管理员),再探测。
if (-not $pyExe -and (Test-Path $PyBundle)) {
    Write-Host "未检测到 Python,正在安装随包 Python 3.11(静默,可能需一两分钟)..." -ForegroundColor Yellow
    $pyInstall = Start-Process -FilePath $PyBundle -Wait -PassThru -ArgumentList `
        "/quiet","InstallAllUsers=0","PrependPath=0","AssociateFiles=0","Shortcuts=0",`
        "Include_pip=1","Include_launcher=1","Include_test=0","Include_doc=0"
    if ($pyInstall.ExitCode -ne 0) {
        throw "随包 Python 3.11 安装失败(退出码 $($pyInstall.ExitCode))。"
    }
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    if (Get-Command py -ErrorAction SilentlyContinue) {
        if (Test-PyCmd "py" "-3.11") { $pyExe = "py"; $pyArg = "-3.11" }
    }
    # launcher/PATH 可能尚未刷新 → 直接查随包 Python 的已知安装路径(per-user)
    if (-not $pyExe) {
        foreach ($cand in @(
            (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
            (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311-64\python.exe"))) {
            if ((Test-Path $cand) -and (Test-PyCmd $cand $null)) { $pyExe = $cand; break }
        }
    }
}

# 仅源码/联网安装模式允许使用其他 Python 3.11+。
if (-not $pyExe -and -not (Test-Path $Wheels)) {
    if ((Get-Command python -ErrorAction SilentlyContinue) -and (Test-PyCmd "python" $null)) {
        $pyExe = "python"
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in @("-3.13", "-3.12", "-3")) {
            if (Test-PyCmd "py" $v) { $pyExe = "py"; $pyArg = $v; break }
        }
    }
}
if (-not $pyExe) {
    throw "未找到可用的 Python 3.11,且随包 Python 安装失败或离线资源不完整。"
}
Write-Host "使用 Python: $pyExe $pyArg"

# ---- 2. 建/修复 venv(覆盖安装时不复用损坏或已被移动的旧环境)----
$venvReady = (Test-Path $Py) -and (Test-PyCmd $Py $null)
if (-not $venvReady) {
    if (Test-Path $Venv) {
        $resolvedVenv = (Resolve-Path -LiteralPath $Venv).Path
        $resolvedProject = (Resolve-Path -LiteralPath $Project).Path
        if ((Split-Path -Parent $resolvedVenv) -ne $resolvedProject -or
            (Split-Path -Leaf $resolvedVenv) -ne ".venv") {
            throw "拒绝清理项目范围外的虚拟环境:$resolvedVenv"
        }
        Write-Host "检测到旧虚拟环境不可用，正在安全重建..." -ForegroundColor Yellow
        Remove-Item -LiteralPath $resolvedVenv -Recurse -Force
    }
    Write-Host "创建虚拟环境 .venv ..." -ForegroundColor Yellow
    if ($pyArg) { & $pyExe $pyArg -m venv $Venv } else { & $pyExe -m venv $Venv }
    if (-not ((Test-Path $Py) -and (Test-PyCmd $Py $null))) { throw "创建 venv 失败:$Venv" }
}
& $Py -m pip install --upgrade pip @PipArgs --quiet
if ($LASTEXITCODE -ne 0) { throw "初始化 pip 失败(退出码 $LASTEXITCODE)。" }

# ---- 3. 装依赖 ----
if (-not (Test-Path $RoleRequirements)) {
    throw "缺少角色依赖清单:$RoleRequirements"
}
Write-Host "安装角色依赖($RequirementsName)..." -ForegroundColor Yellow
& $Py -m pip install -r $RoleRequirements @PipArgs --quiet
if ($LASTEXITCODE -ne 0) { throw "Python 依赖安装失败(退出码 $LASTEXITCODE)。" }

# 仅用户端安装原生桌面窗口依赖；管理端仍使用浏览器，不引入 pywebview/pythonnet。
if ($Role -eq "client" -or $Role -eq "both") {
    if (-not (Test-Path $DesktopRequirements)) {
        throw "缺少用户端桌面依赖清单:$DesktopRequirements"
    }
    Write-Host "安装用户端桌面窗口依赖..." -ForegroundColor Yellow
    & $Py -m pip install -r $DesktopRequirements @PipArgs --quiet
    if ($LASTEXITCODE -ne 0) { throw "用户端桌面窗口依赖安装失败(退出码 $LASTEXITCODE)。" }
}

# ---- 4. 用户端装 4 个 HE 库；管理端不携带、不安装密态计算运行库 ----
if ($Role -eq "client" -or $Role -eq "both") {
    $libs = @("crypto_toolkit-64_dev", "henumpy-dev", "pandaseal-dev", "helearn-dev")
    if (Test-Path $HeLibs) {
        foreach ($l in $libs) {
            $d = Join-Path $HeLibs $l
            if (-not (Test-Path $d)) { throw "缺少 HE 库目录: $d" }
            Write-Host "安装 HE 库 $l ..." -ForegroundColor Yellow
            & $Py -m pip install -e $d @PipArgs --quiet
            if ($LASTEXITCODE -ne 0) { throw "HE 库 $l 安装失败(退出码 $LASTEXITCODE)。" }
        }
    } else {
        throw "未找到 $HeLibs,无法安装用户端密态运行环境。"
    }
}

# 安装后冒烟检查:基础 Web/TLS 依赖与当前角色入口必须能导入,否则拒绝带病完成。
if ($Role -eq "admin" -or $Role -eq "both") {
    & $Py -c "import fastapi, uvicorn, cryptography, pymysql, psycopg, pyodbc, sqlglot; import host.gateway"
    if ($LASTEXITCODE -ne 0) { throw "管理端运行环境自检失败。" }
}
if ($Role -eq "client" -or $Role -eq "both") {
    & $Py -c "import fastapi, uvicorn, cryptography; import client.webui"
    if ($LASTEXITCODE -ne 0) { throw "用户端运行环境自检失败。" }
}

# ---- 4.4 管理端 SQL Server ODBC 运行环境。----
# pyodbc 只是 Python 绑定，真正连接 SQL Server 还需要微软 ODBC Driver 18。
# 管理端安装包内置微软签名 MSI；已有驱动则跳过，避免重复安装。
if ($Role -eq "admin" -or $Role -eq "both") {
    $odbcReady = $false
    try {
        $drivers = (& $Py -c "import pyodbc; print('\n'.join(pyodbc.drivers()))") -join "`n"
        $odbcReady = $drivers -match "ODBC Driver 18 for SQL Server"
    } catch {}
    if (-not $odbcReady) {
        if (-not (Test-Path $SqlServerOdbcInstaller)) {
            throw "未检测到 Microsoft ODBC Driver 18，且离线安装器缺失:$SqlServerOdbcInstaller"
        }
        if (-not (Test-Path $VcRedistInstaller)) {
            throw "SQL Server ODBC Driver 18 所需的 Microsoft VC++ 运行库安装器缺失:$VcRedistInstaller"
        }
        Write-Host "安装 Microsoft VC++ 运行库..." -ForegroundColor Yellow
        $vcInstall = Start-Process -FilePath $VcRedistInstaller -Wait -PassThru -ArgumentList `
            "/install","/quiet","/norestart"
        if ($vcInstall.ExitCode -notin @(0, 1638, 3010)) {
            throw "Microsoft VC++ 运行库安装失败(退出码 $($vcInstall.ExitCode))。"
        }
        Write-Host "安装 SQL Server ODBC Driver 18..." -ForegroundColor Yellow
        $odbcInstall = Start-Process -FilePath "msiexec.exe" -Wait -PassThru -ArgumentList `
            "/i",$SqlServerOdbcInstaller,"/qn","IACCEPTMSODBCSQLLICENSETERMS=YES","ADDLOCAL=ALL"
        if ($odbcInstall.ExitCode -ne 0 -and $odbcInstall.ExitCode -ne 3010) {
            throw "SQL Server ODBC Driver 18 安装失败(退出码 $($odbcInstall.ExitCode))。"
        }
        $drivers = (& $Py -c "import pyodbc; print('\n'.join(pyodbc.drivers()))") -join "`n"
        $odbcReady = $drivers -match "ODBC Driver 18 for SQL Server"
        if (-not $odbcReady) { throw "ODBC 安装完成后仍未检测到 ODBC Driver 18 for SQL Server。" }
    }
    Write-Host "SQL Server ODBC Driver 18 已就位。" -ForegroundColor Green
}

# ---- 4.25 用户端 WebView2 离线运行环境。----
# 微软官方检测口径:以下 per-machine/per-user 注册表 pv 只要有一个大于 0.0.0.0 即已安装。
function Get-WebView2Version {
    $clientId = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    $keys = @(
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$clientId",
        "HKCU:\Software\Microsoft\EdgeUpdate\Clients\$clientId",
        "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$clientId"
    )
    foreach ($key in $keys) {
        try {
            $version = (Get-ItemProperty -LiteralPath $key -Name "pv" -ErrorAction Stop).pv
            if ($version -and $version -ne "0.0.0.0") { return [string]$version }
        } catch {}
    }
    return $null
}

if ($Role -eq "client" -or $Role -eq "both") {
    $webView2Version = Get-WebView2Version
    if (-not $webView2Version) {
        if (-not (Test-Path $WebView2Installer)) {
            throw "未检测到 WebView2 Runtime，且离线安装器缺失:$WebView2Installer"
        }
        Write-Host "安装用户端桌面窗口运行环境 WebView2..." -ForegroundColor Yellow
        $webView2Install = Start-Process -FilePath $WebView2Installer -Wait -PassThru -ArgumentList `
            "/silent","/install"
        if ($webView2Install.ExitCode -ne 0) {
            throw "WebView2 Runtime 安装失败(退出码 $($webView2Install.ExitCode))。"
        }
        $webView2Version = Get-WebView2Version
        if (-not $webView2Version) {
            throw "WebView2 Runtime 安装完成后仍未检测到有效版本。"
        }
    }
    Write-Host "WebView2 Runtime 已就位: $webView2Version" -ForegroundColor Green
    # 不只检查包是否存在，实际初始化 WinForms + WebView2；挡住 pythonnet/.NET/Runtime
    # 任一环节缺失导致的“安装成功但点图标无反应”。
    & $Py -c "import importlib; gl=importlib.import_module('webview.guilib'); gui=gl.initialize('edgechromium'); assert gui.renderer == 'edgechromium', gui.renderer"
    if ($LASTEXITCODE -ne 0) {
        throw "用户端桌面窗口自检失败(WebView2 渲染器未能初始化)。"
    }
}

# ---- 4.5 仅管理主机生成局域网 TLS 证书。----
# 本机 Admin(:8442)与用户端(:8444)均走 loopback HTTP,不再向 Windows Root
# 证书库导入每机自签证书。局域网 Host(:8443)仍强制 HTTPS;gateway 启动时也会
# fail-closed 再次校验证书,失败就明确退出,不会静默降级 HTTP。
if ($Role -eq "admin" -or $Role -eq "both") {
    Write-Host "生成局域网 HTTPS 证书..." -ForegroundColor Yellow
    try {
        & $Py -c "from host import tls_cert; tls_cert.ensure_cert()" 2>$null | Out-Null
        Write-Host "局域网 HTTPS 证书已就位。" -ForegroundColor Green
    } catch {
        throw "局域网 HTTPS 证书生成失败:$_"
    }
}

# ---- 4.75 真实启动自检。----
# 仅能 import 不代表 lifespan、控制库、调度器和本地存储真正可用；用随机 loopback
# 端口启动一次角色服务并等待 /readyz，失败就拒绝完成安装并给出日志路径。
$PostInstallSmoke = Join-Path $Here "post_install_smoke.py"
if (-not (Test-Path $PostInstallSmoke)) {
    throw "缺少安装后启动自检脚本:$PostInstallSmoke"
}
$SmokeRoles = if ($Role -eq "both") { @("admin", "client") } else { @($Role) }
foreach ($SmokeRole in $SmokeRoles) {
    Write-Host "执行 $SmokeRole 真实启动自检..." -ForegroundColor Yellow
    & $Py $PostInstallSmoke --role $SmokeRole --project $Project --timeout 60
    if ($LASTEXITCODE -ne 0) {
        throw "$SmokeRole 安装后启动自检失败，请查看 ~/.agent-system/supervisor/install-smoke-$SmokeRole.log"
    }
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
