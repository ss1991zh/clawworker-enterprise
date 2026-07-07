<#
  把项目打成两个 Windows 安装包(管理端 + 用户端 Setup.exe)。
  在 Windows 上运行,前置:已装 Inno Setup 6+(https://jrsoftware.org/isdl.php)。

  用法(项目根):
      powershell -ExecutionPolicy Bypass -File packaging\windows\build_installers.ps1

  产物:packaging\windows\dist\
      Clawworker-admin-Setup-0.7.exe   ← 装在管理端机器
      Clawworker-client-Setup-0.7.exe  ← 装在每台用户机器
#>
$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Iss  = Join-Path $Here "clawworker-setup.iss"

# 找 Inno Setup 编译器 ISCC.exe
$iscc = $null
foreach ($p in @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
    if (Test-Path $p) { $iscc = $p; break }
}
if (-not $iscc -and (Get-Command iscc -ErrorAction SilentlyContinue)) { $iscc = "iscc" }
if (-not $iscc) { throw "未找到 Inno Setup(ISCC.exe)。请先安装 Inno Setup 6+:https://jrsoftware.org/isdl.php" }

# 检查 HE 库已就位
$heLibs = Join-Path $Here "he_libs"
if (-not (Test-Path $heLibs)) {
    Write-Warning "缺少 $heLibs —— 安装包将不含密态库,目标机无法初始化密态。"
    Write-Warning "请把 4 个 HE 库目录(crypto_toolkit-64_dev/henumpy-dev/pandaseal-dev/helearn-dev,含 win64 DLL)放进去后重跑。"
}

foreach ($role in @("admin", "client")) {
    Write-Host "==== 编译 $role 安装包 ====" -ForegroundColor Cyan
    & $iscc "/DMyRole=$role" $Iss
    if ($LASTEXITCODE -ne 0) { throw "$role 安装包编译失败(ISCC 退出码 $LASTEXITCODE)" }
}

Write-Host ""
Write-Host "==== 完成 ====" -ForegroundColor Green
Write-Host "安装包在: $(Join-Path $Here 'dist')"
Get-ChildItem (Join-Path $Here "dist") -Filter "*.exe" | ForEach-Object { Write-Host "  $($_.Name)" }
