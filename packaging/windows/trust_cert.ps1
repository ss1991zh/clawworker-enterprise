# 生成本机 TLS 证书并导入「当前用户 · 受信任的根证书颁发机构」。
#
# 为什么单独一个脚本:安装主流程(install.ps1)被 Inno 以 runhidden 调用,
# 而导入受信任根**必然弹一次 Windows 确认框**(这是授权信任本机证书,无法绕过)。
# 放在隐藏窗口里 → 用户看不到确认框、也看不到提示 → 点不了『是』 → 浏览器一直报
# "此站点连接不安全"。故本步骤单独、**可见**地运行,并用弹窗告知结果,可随时重跑。
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File trust_cert.ps1           # 安装时/手动修复
param([switch]$Quiet)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
Add-Type -AssemblyName System.Windows.Forms | Out-Null

function Show-Info($text, $title, $icon) {
    if ($Quiet) { Write-Host $text; return }
    [System.Windows.Forms.MessageBox]::Show($text, $title, 'OK', $icon) | Out-Null
}

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppRoot = Split-Path -Parent (Split-Path -Parent $Here)   # packaging\windows → 项目根
$Py = Join-Path $AppRoot ".venv\Scripts\python.exe"
$certFile = Join-Path $env:USERPROFILE ".agent-system\host-config\host_cert.pem"

# 1) 确保证书已生成(首次安装时可能还没有;交给 ensure_cert 幂等生成)
if ((Test-Path $Py) -and -not (Test-Path $certFile)) {
    Push-Location $AppRoot
    try { & $Py -c "from host import tls_cert; tls_cert.ensure_cert()" 2>$null } catch {}
    Pop-Location
}
if (-not (Test-Path $certFile)) {
    Show-Info "尚未找到本机证书。请先启动一次程序(双击桌面图标)让它生成证书,再运行本修复。" `
        "Clawworker · 证书信任" 'Warning'
    exit 1
}

# 2) 已经信任了就不打扰
$cn = "Clawworker Enterprise Local"
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 $certFile
$already = Get-ChildItem Cert:\CurrentUser\Root -ErrorAction SilentlyContinue |
    Where-Object { $_.Thumbprint -eq $cert.Thumbprint }
if ($already) {
    Show-Info "本机已信任 Clawworker 证书,浏览器不会再报『连接不安全』。" `
        "Clawworker · 证书信任" 'Information'
    exit 0
}

# 3) 导入(会弹一次 Windows 确认框,点『是』)
if (-not $Quiet) {
    $tip = "接下来 Windows 会弹出一个『是否安装此证书』的安全确认框。`n`n" +
           "请点『是』—— 这是授权本机信任 Clawworker 的本地证书;" +
           "点『是』之后浏览器就不再报『此站点连接不安全』。"
    [System.Windows.Forms.MessageBox]::Show($tip, "Clawworker · 证书信任", 'OK', 'Information') | Out-Null
}
try {
    Import-Certificate -FilePath $certFile -CertStoreLocation Cert:\CurrentUser\Root -ErrorAction Stop | Out-Null
    Show-Info "证书已导入信任库。现在刷新浏览器,『此站点连接不安全』的警告就没有了。" `
        "Clawworker · 完成" 'Information'
    exit 0
} catch {
    Show-Info ("证书未能导入(可能刚才点了『否』)。`n`n可随时从开始菜单的" +
        "『修复证书信任』重新运行本步骤;或在浏览器点『高级 → 继续前往』暂时通过" +
        "(连接仍是加密的,只是未被系统标记为受信任)。") `
        "Clawworker · 未完成" 'Warning'
    exit 1
}
