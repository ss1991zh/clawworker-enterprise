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

; 1.1:SPKI 公钥指纹信任(换网重签不再误报中间人)、登录页扫描内网、
;      supervisor 角色接管(修点管理端图标无反应)、20+ 项口径/安全修复
#define AppVersion "1.1"
#define Pub "Clawworker"

[Setup]
AppId={{C1AW-{#RoleArg}-0007}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#Pub}
DefaultDirName={autopf}\Clawworker
DefaultGroupName=Clawworker
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=Clawworker-{#RoleArg}-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
SetupIconFile=clawworker.ico
UninstallDisplayIcon={app}\packaging\windows\clawworker.ico
; 覆盖安装:同 AppId 直接装回原目录,不弹"目录已存在"警告
DirExistsWarning=no
UsePreviousAppDir=yes

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
; ---- HE 库(含 win64 DLL,装包前放进 he_libs\)----
; 密钥/证书/字典类文件绝不打进安装包(skf / user_authorization / dictf / evk / sk.bin)
Source: "he_libs\*";                 DestDir: "{app}\packaging\windows\he_libs"; Excludes: "skf,*user_authorization*,dictf*,evk*,sk.bin"; Flags: recursesubdirs createallsubdirs
; ---- 完全离线包:随包 Python 安装器 + 全部依赖 wheel(目标机无需装 Python、无需联网)----
Source: "python-3.11.9-amd64.exe";   DestDir: "{app}\packaging\windows"; Flags: skipifsourcedoesntexist
Source: "wheels\*";                  DestDir: "{app}\packaging\windows\wheels"; Flags: recursesubdirs createallsubdirs skipifsourcedoesntexist

[Icons]
; 桌面 + 开始菜单图标:启动器(无窗口的 pythonw)+ 角色参数 + 自带图标
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\packaging\windows\clawworker_launch.py"" {#RoleArg}"; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\clawworker.ico"
Name: "{group}\{#AppName}";       Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\packaging\windows\clawworker_launch.py"" {#RoleArg}"; WorkingDir: "{app}"; IconFilename: "{app}\packaging\windows\clawworker.ico"

[Run]
; 安装后建 venv + 装依赖 + 装 HE 库(需目标机已装 Python 3.11+;不重复建快捷方式)
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -NoProfile -File ""{app}\packaging\windows\install.ps1"" -Role {#RoleArg} -NoShortcut"; \
  StatusMsg: "正在安装依赖与密态库(可能需要几分钟)..."; Flags: runhidden waituntilterminated
; 安装完成后可选:立即启动并打开界面
Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\packaging\windows\clawworker_launch.py"" {#RoleArg}"; \
  Description: "立即启动 {#AppName}"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; 卸载时从受信任根移除本产品自签证书(按 CN 匹配,best-effort)
Filename: "certutil.exe"; Parameters: "-user -delstore Root ""Clawworker Enterprise Local"""; \
  Flags: runhidden; RunOnceId: "DelTlsCert"

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
