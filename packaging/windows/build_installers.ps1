<#
  把项目打成两个 Windows 安装包(管理端 + 用户端 Setup.exe)。
  在 Windows 上运行,前置:已装 Inno Setup 6+(https://jrsoftware.org/isdl.php)。

  用法(项目根):
      powershell -ExecutionPolicy Bypass -File packaging\windows\build_installers.ps1

  产物:packaging\windows\dist\
      Clawworker-admin-Setup-1.7.0.exe   ← 装在管理端机器
      Clawworker-client-Setup-1.7.0.exe  ← 装在每台用户机器
#>
$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Iss  = Join-Path $Here "clawworker-setup.iss"
$VersionFile = Join-Path $Here "..\..\shared\version.py"
$VersionMatch = Select-String -LiteralPath $VersionFile -Pattern '^__version__\s*=\s*"([^"]+)"$'
if (-not $VersionMatch) { throw "无法从 $VersionFile 读取产品版本。" }
$AppVersion = $VersionMatch.Matches[0].Groups[1].Value

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

# 自包含安装包硬闸:Python、管理端数据库驱动、用户端 HE 库缺一不可。
$pyBundle = Join-Path $Here "python-3.11.9-amd64.exe"
if (-not (Test-Path $pyBundle)) {
    throw "缺少随包 Python 3.11 安装器: $pyBundle"
}
$odbcBundle = Join-Path $Here "db_drivers\msodbcsql18-x64.msi"
if (-not (Test-Path $odbcBundle)) {
    throw "缺少 SQL Server ODBC Driver 18 离线安装器: $odbcBundle"
}
$odbcSig = Get-AuthenticodeSignature -LiteralPath $odbcBundle
if ($odbcSig.Status -ne "Valid" -or $odbcSig.SignerCertificate.Subject -notmatch "Microsoft Corporation") {
    throw "SQL Server ODBC 安装器签名无效或并非 Microsoft 签发,拒绝打包。"
}
$vcBundle = Join-Path $Here "db_drivers\vc_redist.x64.exe"
if (-not (Test-Path $vcBundle)) {
    throw "缺少 Microsoft VC++ x64 运行库离线安装器: $vcBundle"
}
$vcSig = Get-AuthenticodeSignature -LiteralPath $vcBundle
if ($vcSig.Status -ne "Valid" -or $vcSig.SignerCertificate.Subject -notmatch "Microsoft Corporation") {
    throw "Microsoft VC++ 运行库安装器签名无效或并非 Microsoft 签发,拒绝打包。"
}

# 检查 HE 库已就位
$heLibs = Join-Path $Here "he_libs"
if (-not (Test-Path $heLibs)) {
    throw "缺少 $heLibs,拒绝生成无法使用的安装包。"
}
$requiredHe = @("crypto_toolkit-64_dev", "henumpy-dev", "pandaseal-dev", "helearn-dev")
foreach ($lib in $requiredHe) {
    $libPath = Join-Path $heLibs $lib
    if (-not (Test-Path $libPath)) { throw "缺少 HE 库目录: $libPath" }
}
foreach ($lib in @("crypto_toolkit-64_dev", "henumpy-dev")) {
    if (-not (Get-ChildItem (Join-Path $heLibs $lib) -Recurse -File -Filter "*.dll" -ErrorAction SilentlyContinue)) {
        throw "HE 库 $lib 缺少 Windows DLL,拒绝打包。"
    }
}

# 从完整 wheel 仓按角色解析并生成两个最小离线 wheel 目录。这样管理端不再携带
# pandas/xgboost/密态计算依赖，用户端也不再携带数据库驱动绑定。
$wheels = Join-Path $Here "wheels"
if (-not (Test-Path $wheels)) { throw "缺少完整离线 wheel 仓:$wheels" }
$py = $env:CLAWWORKER_BUILD_PY
if (-not $py) {
    foreach ($c in @("$PSScriptRoot\..\..\.venv\Scripts\python.exe", "python", "py")) {
        try {
            if ((Test-Path $c) -or (Get-Command $c -ErrorAction SilentlyContinue)) {
                & $c --version *> $null
                if ($LASTEXITCODE -eq 0) { $py = $c; break }
            }
        } catch {}
    }
}
if (-not $py) { throw "未找到 Python，无法解析并验证角色离线依赖。" }

foreach ($role in @("admin", "client")) {
    $roleReq = Join-Path $Here "requirements-$role.txt"
    if (-not (Test-Path $roleReq)) { throw "缺少角色依赖清单:$roleReq" }
    $roleWheelDir = Join-Path $Here "wheels-$role"
    if (Test-Path $roleWheelDir) {
        $resolved = (Resolve-Path -LiteralPath $roleWheelDir).Path
        $expectedParent = (Resolve-Path -LiteralPath $Here).Path
        if ((Split-Path -Parent $resolved) -ne $expectedParent) {
            throw "拒绝清理非打包目录:$resolved"
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
    New-Item -ItemType Directory -Path $roleWheelDir | Out-Null

    Write-Host "==== 生成 $role 最小离线依赖 ====" -ForegroundColor Cyan
    $downloadArgs = @(
        "download", "--no-index", "--find-links", $wheels,
        "--dest", $roleWheelDir, "--platform", "win_amd64",
        "--python-version", "3.11", "--implementation", "cp", "--abi", "cp311",
        "--only-binary=:all:", "-r", $roleReq
    )
    if ($role -eq "client") {
        $downloadArgs += @("-r", (Join-Path $Here "requirements-desktop.txt"))
    }
    & $py -m pip @downloadArgs --quiet
    if ($LASTEXITCODE -ne 0) {
        throw "$role 离线依赖不完整。请先补齐 packaging\windows\wheels 后再打包。"
    }

    $tmp = Join-Path $env:TEMP ("cw_wheelcheck_" + [Guid]::NewGuid().ToString("N"))
    $checkArgs = @(
        "install", "--dry-run", "--no-index", "--find-links", $roleWheelDir,
        "--target", $tmp, "--platform", "win_amd64", "--python-version", "3.11",
        "--implementation", "cp", "--abi", "cp311", "--only-binary=:all:", "-r", $roleReq
    )
    if ($role -eq "client") {
        $checkArgs += @("-r", (Join-Path $Here "requirements-desktop.txt"))
    }
    & $py -m pip @checkArgs --quiet
    $ok = ($LASTEXITCODE -eq 0)
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    if (-not $ok) { throw "$role 角色 wheel 自检失败，拒绝生成安装包。" }
}

foreach ($role in @("admin", "client")) {
    Write-Host "==== 编译 $role 安装包 ====" -ForegroundColor Cyan
    & $iscc "/Qp" "/DMyRole=$role" "/DMyVersion=$AppVersion" $Iss
    if ($LASTEXITCODE -ne 0) { throw "$role 安装包编译失败(ISCC 退出码 $LASTEXITCODE)" }
}

$dist = Join-Path $Here "dist"
$built = @(Get-ChildItem -LiteralPath $dist -Filter "Clawworker-*-Setup-$AppVersion.exe")
if ($built.Count -ne 2) { throw "预期生成 2 个 $AppVersion 安装包，实际为 $($built.Count) 个。" }
$hashLines = @()
foreach ($file in ($built | Sort-Object Name)) {
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    $hashLines += "$hash *$($file.Name)"
}
$hashFile = Join-Path $dist "SHA256SUMS-$AppVersion.txt"
[IO.File]::WriteAllLines($hashFile, $hashLines, [Text.UTF8Encoding]::new($false))

Write-Host ""
Write-Host "==== 完成 ====" -ForegroundColor Green
Write-Host "安装包在: $dist"
$built | Sort-Object Name | ForEach-Object { Write-Host "  $($_.Name)" }
Write-Host "  $(Split-Path -Leaf $hashFile)"
