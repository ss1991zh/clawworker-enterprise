# Clawworker · Windows 安装包

把企业版做成 **管理端 / 用户端** 两个 Windows 安装包,装好后**双击桌面图标即可启动并打开界面**。

- **管理端(admin)**:控制面 Host(:8443)——账户/证书/LLM 代理/admin 后台。装在中心机器。
- **用户端(client)**:数据面 Client(:8444)——密钥沙盒/加密/密态计算/解密/出 Excel。装在每台终端机器。

> 双击图标 → 启动器(无黑窗)检查服务是否在跑 → 没在跑就后台拉起(崩溃自愈)→ 等就绪 → 用默认浏览器打开界面。再次双击只是打开界面(服务已在后台)。

---

## 前置

1. **Python 3.11+**(安装时勾选 *Add Python to PATH*):https://www.python.org/downloads/
2. **HE 密态库**:把你的 4 个库源码目录放进 `packaging\windows\he_libs\`(它们内含 `lib\win64_*.dll`):
   ```
   packaging\windows\he_libs\
     crypto_toolkit-64_dev\
     henumpy-dev\
     pandaseal-dev\
     helearn-dev\
   ```
3. 打 `.exe` 安装包还需 **Inno Setup 6+**:https://jrsoftware.org/isdl.php

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

它会:建 `.venv` → 装 `requirements.txt` → 装 4 个 HE 库 → 在**桌面生成图标**(「Clawworker 管理端」/「Clawworker 用户端」)。完成后双击图标即可。

## 方式 B:打成可分发的 .exe 安装包(发给别人)

在项目根运行:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build_installers.ps1
```

产物在 `packaging\windows\dist\`:
- `Clawworker-admin-Setup-0.7.exe`
- `Clawworker-client-Setup-0.7.exe`

把对应的 `.exe` 拷到目标机器双击安装即可(安装时会自动建 venv + 装依赖 + 建桌面/开始菜单图标)。
目标机仍需预装 Python 3.11+ 与 `he_libs`(已打进安装包)。

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

- **双击图标没反应**:首次启动要几十秒(初始化密钥)。确认浏览器开了 `localhost:8443`(管理端)/`8444`(用户端)。
- **密态报错 / 初始化失败**:多半是 `he_libs` 没放或缺 win64 DLL;或密钥/字典不配套——进「密钥体检」看报告。
- **PowerShell 中文乱码**:用 PowerShell 7 或 Windows Terminal 运行;不影响功能。
- **授权到期**:界面会预警 HE 库授权剩余天数,到期前联系供应商续期。
