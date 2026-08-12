; Clawworker Windows 安装包定义(Inno Setup 6+)
; 用 build_installers.ps1 编译两次,分别产出 管理端 / 用户端 两个 Setup.exe。
; 手动编译单个:  iscc /DMyRole=admin  clawworker-setup.iss
;                 iscc /DMyRole=client clawworker-setup.iss

#ifndef MyRole
  #define MyRole "client"
#endif

#if MyRole == "admin"
  #define AppName "Clawworker 管理端"
  #define RoleArg "admin"
#else
  #define AppName "Clawworker 用户端"
  #define RoleArg "client"
#endif

; 1.6.0:数据库授权后免逐次审批；未授权结构隐藏且后端拒绝；管理端新增使用记录；用户端结果先本地加密再分析。
; 1.5.0:管理端新增远程企业数据库、结构目录、表/字段权限、安全只读查询与审计；用户端新增授权数据提取入口。
; 1.4.2:本机就绪探测强制直连 loopback，避免系统代理劫持 127.0.0.1 后误报超时。
; 1.4.1:修复慢机器 HTTP 就绪探测误判、重复拉起和用户端日志路径。
; 1.4.0:用户端改为 WebView2 独立桌面窗口;管理端仍使用浏览器。
; 1.3.6:精确区分 Word 阅读问答与按 Word 公式计算;点名指标必须成为 Excel 结果列。
; 1.3.5:Word+Excel 联合任务先读取公式/规则文档,再触发 Excel 密态分析;支持提取 OMML 公式。
; 1.3.4:多管理端扫描不再自动选中旧/本机管理端;候选标出本机,登录错误显示实际目标。
; 1.3.3:安装版自动替换旧源码守护进程;修标称 30 秒实际等待约 150 秒。
; 1.3.2:用户端扫描/手输地址强制归一为局域网 HTTPS :8443,迁移误存的本机 :8442。
; 1.3:完整离线包内置 Python 3.11、依赖 wheels 与 HE 库;目标机无需 Python/网络。
;      本机 Admin/Client 改 loopback HTTP;Host 双监听(:8442 HTTP + :8443 HTTPS)。
; 1.2:修致命打包缺陷 —— 离线 wheel 包漏了 cryptography(+cffi/pycparser),
;      导致目标机装完生成不了 TLS 证书、服务起不来、点图标无反应。已补齐并全量离线自检。
; 1.1:SPKI 公钥指纹信任(换网重签不再误报中间人)、登录页扫描内网、
;      supervisor 角色接管、证书信任步骤改可见、20+ 项口径/安全修复
#define AppVersion "1.6.0"
#define Pub "Clawworker"

[Setup]
AppId={{C1AW-{#RoleArg}-0007}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#Pub}
; 两个角色目录隔离,允许同机分别安装/卸载,避免共享目录互相覆盖。
DefaultDirName={autopf}\Clawworker\{#RoleArg}
DefaultGroupName=Clawworker
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=Clawworker-{#RoleArg}-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
#if MyRole == "admin"
; SQL Server ODBC Driver 18 是系统级驱动，管理端安装时需要一次 UAC 确认。
PrivilegesRequired=admin
#else
PrivilegesRequired=lowest
#endif
SetupIconFile=clawworker.ico
UninstallDisplayIcon={app}\packaging\windows\clawworker.ico
; 1.3.1 强制迁移到角色隔离目录。旧版可能仍记着共享目录 Clawworker\,
; 若复用旧目录会继续启动旧 launcher 并打开错误的 http://localhost:8443。
DirExistsWarning=no
UsePreviousAppDir=no

[Languages]
Name: "cn"; MessagesFile: "compiler:Default.isl"

[Files]
; ---- 应用源码(项目根的各目录)----
Source: "..\..\host\*";              DestDir: "{app}\host";            Excludes: "__pycache__\*,*.pyc"; Flags: recursesubdirs createallsubdirs
Source: "..\..\client\*";            DestDir: "{app}\client";          Excludes: "__pycache__\*,*.pyc"; Flags: recursesubdirs createallsubdirs
Source: "..\..\shared\*";            DestDir: "{app}\shared";          Excludes: "__pycache__\*,*.pyc"; Flags: recursesubdirs createallsubdirs
; ---- 文档(含 LLM 系统 prompt,运行时会读 docs\llm_system_prompt.md)----
Source: "..\..\docs\*";              DestDir: "{app}\docs";            Flags: recursesubdirs createallsubdirs
Source: "..\..\skill_packs\*";       DestDir: "{app}\skill_packs";     Flags: recursesubdirs createallsubdirs
Source: "..\..\supervisor.py";       DestDir: "{app}"
Source: "..\..\client_supervisor.py"; DestDir: "{app}"
; ---- 打包工具(启动器 / 图标 / 依赖 / 安装脚本)----
Source: "clawworker_launch.py";      DestDir: "{app}\packaging\windows"
Source: "clawworker.ico";            DestDir: "{app}\packaging\windows"
Source: "requirements.txt";          DestDir: "{app}\packaging\windows"
Source: "install.ps1";               DestDir: "{app}\packaging\windows"
#if MyRole == "admin"
Source: "db_drivers\vc_redist.x64.exe"; DestDir: "{app}\packaging\windows\db_drivers"
Source: "db_drivers\msodbcsql18-x64.msi"; DestDir: "{app}\packaging\windows\db_drivers"
#endif
#if MyRole == "client"
Source: "clawworker_desktop.py";     DestDir: "{app}\packaging\windows"
Source: "requirements-desktop.txt";  DestDir: "{app}\packaging\windows"
Source: "webview2\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"; DestDir: "{app}\packaging\windows\webview2"
#endif
; ---- HE 库(含 win64 DLL,装包前放进 he_libs\)----
; 密钥/证书/字典类文件绝不打进安装包(skf / user_authorization / dictf / evk / sk.bin)
Source: "he_libs\*";                 DestDir: "{app}\packaging\windows\he_libs"; Excludes: "skf,*user_authorization*,dictf*,evk*,sk.bin"; Flags: recursesubdirs createallsubdirs
; ---- 完全离线包:随包 Python 安装器 + 全部依赖 wheel(目标机无需装 Python、无需联网)----
Source: "python-3.11.9-amd64.exe";   DestDir: "{app}\packaging\windows"; Flags: skipifsourcedoesntexist
#if MyRole == "client"
Source: "wheels\*";                  DestDir: "{app}\packaging\windows\wheels"; Flags: recursesubdirs createallsubdirs skipifsourcedoesntexist
#else
Source: "wheels\*";                  DestDir: "{app}\packaging\windows\wheels"; Excludes: "pywebview-*,pythonnet-*,clr_loader-*,proxy_tools-*,bottle-*"; Flags: recursesubdirs createallsubdirs skipifsourcedoesntexist
#endif

[Icons]
; 桌面 + 开始菜单图标:启动器(无窗口的 pythonw)+ 角色参数 + 自带图标
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\packaging\windows\clawworker_launch.py"" {#RoleArg}"; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\clawworker.ico"
Name: "{group}\{#AppName}";       Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\packaging\windows\clawworker_launch.py"" {#RoleArg}"; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\clawworker.ico"
#if MyRole == "client"
Name: "{group}\Clawworker 用户端（浏览器诊断）"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\packaging\windows\clawworker_launch.py"" client --browser"; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\clawworker.ico"
#endif

[Run]
; 安装后用随包 Python 3.11 建 venv + 离线装依赖 + HE 库;不重复建快捷方式
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -NoProfile -File ""{app}\packaging\windows\install.ps1"" -Role {#RoleArg} -NoShortcut"; \
  StatusMsg: "正在安装依赖与密态库(可能需要几分钟)..."; Flags: runhidden waituntilterminated
; 安装完成后可选:立即启动并打开界面
Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\packaging\windows\clawworker_launch.py"" {#RoleArg}"; \
  Description: "立即启动 {#AppName}"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"

[Code]
// 覆盖安装:先停掉与本安装目录相关的服务进程,否则文件被锁 → 复制失败/残留旧文件。
// 注意:此处只能用 // 注释 —— Pascal 的花括号注释会被安装目录常量里的右花括号提前闭合。
//
// 两点讲究:
// 1) 不能只按「可执行文件在安装目录下」匹配 —— 服务可能是用**别的 Python**(如系统
//    Python311)启动的,其 exe 路径在安装目录之外,但它加载了安装目录下的 HE DLL,
//    照样锁着文件。故同时按**命令行**是否引用安装目录来匹配。
// 2) supervisor 会自动重拉子进程 —— 杀一遍可能被它救活,故连杀两轮。
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  R: Integer;
  AppDir: String;
begin
  AppDir := ExpandConstant('{app}');
  Exec('powershell.exe',
    '-NoProfile -Command "$p=''' + AppDir + '''; 1..2 | ForEach-Object { ' +
    'Get-CimInstance Win32_Process | Where-Object { ' +
    '($_.Name -eq ''python.exe'' -or $_.Name -eq ''pythonw.exe'') -and ' +
    '($_.ExecutablePath -like ($p+''*'') -or $_.CommandLine -like (''*''+$p+''*'')) } | ' +
    'ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; ' +
    'Start-Sleep -Milliseconds 700 }"',
    '', SW_HIDE, ewWaitUntilTerminated, R);
  Result := '';
end;

