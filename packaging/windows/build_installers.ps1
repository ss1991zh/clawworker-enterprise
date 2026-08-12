<#
  把项目打成两个 Windows 安装包(管理端 + 用户端 Setup.exe)。
  在 Windows 上运行,前置:已装 Inno Setup 6+(https://jrsoftware.org/isdl.php)。

  用法(项目根):
      powershell -ExecutionPolicy Bypass -File packaging\windows\build_installers.ps1

  产物:packaging\windows\dist\
      Clawworker-admin-Setup-1.5.0.exe   ← 装在管理端机器
      Clawworker-client-Setup-1.5.0.exe  ← 装在每台用户机器
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

# 自包含安装包硬闸:Python 安装器、4 个 HE 库和所需 DLL 缺一不可。
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

# 离线 wheel 包完整性硬闸:目标机用 --no-index 纯离线装,漏一个包就装不起来
# (真实事故:漏了 cryptography → 目标机生成不了证书、服务起不来、点图标无反应)。
# 打包前用 pip 离线解析核对一遍,缺件直接中止,绝不产出装完用不了的安装包。
$wheels = Join-Path $Here "wheels"
$reqs   = Join-Path $Here "requirements.txt"
if ((Test-Path $wheels) -and (Test-Path $reqs)) {
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
    if ($py) {
        Write-Host "==== 校验离线 wheel 包完整性 ====" -ForegroundColor Cyan
        $tmp = Join-Path $env:TEMP ("cw_wheelcheck_" + [Guid]::NewGuid().ToString("N"))
        & $py -m pip install --dry-run --no-index --find-links $wheels -r $reqs --target $tmp `
            --platform win_amd64 --python-version 3.11 --implementation cp --abi cp311 `
            --only-binary=:all: 2>&1 | Out-Null
        $ok = ($LASTEXITCODE -eq 0)
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        if (-not $ok) {
            throw "离线 wheel 包无法满足 requirements.txt —— 目标机装完会缺依赖、程序起不来。" +
                  "请先补齐缺失的 wheel(如 pip download <pkg> --dest wheels)再打包。"
        }
        Write-Host "wheel 包完整(可离线满足全部 requirements)。" -ForegroundColor Green
    } else {
        Write-Warning "未找到 python,跳过 wheel 完整性校验(强烈建议在有 python 的机器上打包)。"
    }
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
