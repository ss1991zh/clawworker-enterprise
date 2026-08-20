# Clawworker · Windows 安装包

把企业版做成 **管理端 / 用户端** 两个 Windows 安装包,装好后**双击桌面图标即可启动并打开界面**。

- **管理端(admin)**:控制面 Host——本机管理页走 HTTP `127.0.0.1:8442`,
  局域网客户端/远程管理走 HTTPS `:8443`。装在中心机器。
- **用户端(client)**:数据面 Client——本机 HTTP `127.0.0.1:8444`,
  密钥沙盒/加密/密态计算/解密/出 Excel。装在每台终端机器。

> 双击图标 → 启动器(无黑窗)检查本机 HTTP 服务是否在跑 → 没在跑就后台拉起
> (崩溃自愈)→ 等就绪 → 用默认浏览器打开界面。浏览器不再需要安装/信任本机自签证书。
> 管理主机的 `:8443` 仍强制 HTTPS,供其他终端连接。

---

## 构建前置

目标电脑不需要预装 Python，也不需要联网。两个安装包都内置 Python 3.11，并分别
携带本角色所需的最小离线依赖。只有用户端包含 HE 密态库和数据分析依赖；管理端
只包含服务、模型代理和数据库连接依赖。

打包电脑需要:

1. **HE 密态库**:把你的 4 个库源码目录放进 `packaging\windows\he_libs\`(它们内含 `lib\win64_*.dll`):
   ```
   packaging\windows\he_libs\
     crypto_toolkit-64_dev\
     henumpy-dev\
     pandaseal-dev\
     helearn-dev\
   ```
2. **Inno Setup 6+**:https://jrsoftware.org/isdl.php
3. **可用 Python**:仅用于离线解析和核对角色 wheel；也可通过环境变量
   `CLAWWORKER_BUILD_PY` 指向具体的 `python.exe`。

---

## 方式 A:不打包,直接装(最快,本机用)

在**项目根目录**用 PowerShell(建议 PowerShell 7 / Windows Terminal,UTF-8)运行:

```powershell
# 管理端机器
powershell -ExecutionPolicy Bypass -File packaging\windows\install.ps1 -Role admin
# 用户端机器
powershell -ExecutionPolicy Bypass -File packaging\windows\install.ps1 -Role client
# 单机一体(两个图标都建)
powershell -ExecutionPolicy Bypass -File packaging\windows\install.ps1 -Role both
```

它会:建 `.venv` → 按角色安装 `requirements-admin.txt` 或
`requirements-client.txt` → 仅用户端安装 4 个 HE 库 → 在**桌面生成图标**。
完成后双击图标即可。

## 方式 B:打成可分发的 .exe 安装包(发给别人)

在项目根运行:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build_installers.ps1
```

产物在 `packaging\windows\dist\`:
- `Clawworker-admin-Setup-1.7.0.exe`
- `Clawworker-client-Setup-1.7.0.exe`
- `SHA256SUMS-1.7.0.txt`（两份安装包的完整性校验值）

v1.4.0 起，用户端默认以独立 WebView2 桌面窗口运行，不再打开浏览器标签页。
管理端仍使用浏览器。用户端开始菜单保留“浏览器诊断”入口，便于排查桌面窗口故障。
用户端离线包包含 WebView2 x64 Standalone Runtime；安装时仅在系统未安装 Runtime 时静默安装。

这些大体积离线运行库不提交到 GitHub。打包机需要准备以下微软官方文件：

- `webview2/MicrosoftEdgeWebView2RuntimeInstallerX64.exe`：WebView2 Evergreen Standalone x64。
- `db_drivers/msodbcsql18-x64.msi`：Microsoft ODBC Driver 18 for SQL Server x64。
- `db_drivers/vc_redist.x64.exe`：Visual C++ Redistributable x64。

构建脚本会检查文件是否存在，并验证数据库驱动和 VC++ 运行库的 Microsoft 数字签名；
随后从完整 `wheels` 仓库解析出 `wheels-admin` 和 `wheels-client` 两个最小集合。
任何角色缺少依赖都会停止构建，不会生成装完后打不开的安装包。
v1.4.1 修复部分较慢电脑上后台已启动、却被启动器误报超时且不打开桌面窗口的问题。
v1.4.2 进一步修复启用系统代理的电脑上，本机 `127.0.0.1` 就绪探测被代理后误报超时的问题。

v1.7.0 汇总 P0/P1/P2 稳定性优化，明确文档正文发送边界，桌面启动器等待服务真正就绪，并在安装后执行真实启动自检。

v1.6.2 增加企业数据库指标逐项可计算性检查：优先直接字段，其次按可靠公式检查间接参数；
缺少必要参数且无法推导时只跳过对应指标并说明原因，其余指标继续完成。同时修复收入/毛利
同比查询、只读子查询别名校验、数据库失败后的重新连接和桌面窗口跳转闪退。

把对应的 `.exe` 拷到目标机器双击安装即可。安装时会自动安装随包 Python 3.11、
创建隔离 venv、离线安装全部依赖与 HE 库，并建立桌面/开始菜单图标。

---

## 开机自启(可选)

进界面 **设置 → 自启 / 运维** 一键开启;或命令行注册计划任务(登录即拉起 supervisor):

```powershell
# 管理端:
$env:CLAWWORKER_MANAGED_SERVICES="host"
schtasks /Create /TN "Clawworker Supervisor" /SC ONLOGON /TR "`"<安装目录>\.venv\Scripts\pythonw.exe`" `"<安装目录>\supervisor.py`"" /F
```
(用户端把 `host` 换成 `client`。supervisor 会按 `CLAWWORKER_MANAGED_SERVICES` 只托管该角色,并做崩溃自愈。)

---

## 密钥与字典

每个用户在**用户端界面 → 设置 → 同态密钥**导入自己的 `sk` / `evk`,从主机拉取 `user_authorization`;
密钥默认也可由环境变量覆盖(`AGENT_SK_PATH` / `AGENT_DICT_DIR` / `AGENT_USER_AUTH`),否则用 HE 库自带的默认密钥。
导入后点 **密钥体检** 验证可用性。

## 排错

- **双击图标没反应**:首次启动要几十秒。确认浏览器开了
  `http://127.0.0.1:8442/admin`(管理端)或 `http://127.0.0.1:8444`(用户端)。
- **其他电脑连不上主机**:确认管理主机 `8443/TCP` 防火墙已放行,客户端填写
  `https://<管理主机IP或DNS名>:8443`。首次连接仍需核对主机证书指纹。
- **密态报错 / 初始化失败**:多半是 `he_libs` 没放或缺 win64 DLL;或密钥/字典不配套——进「密钥体检」看报告。
- **PowerShell 中文乱码**:用 PowerShell 7 或 Windows Terminal 运行;不影响功能。
- **授权到期**:界面会预警 HE 库授权剩余天数,到期前联系供应商续期。
