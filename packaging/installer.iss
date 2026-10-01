; py12306 抢票助手 —— Inno Setup 安装包脚本
; 由 CI 调用：ISCC /DMyVersion=1.0.0 packaging\installer.iss
; 需要 Inno Setup 6.x（CI 里装的是 6.7.3）

#define MyAppName "py12306 抢票助手"
#define MyAppVersion "1.0.0"
#define MyAppExeName "py12306.exe"
#define MyAppPublisher "py12306-pro"

#ifndef MyVersion
  #define MyVersion MyAppVersion
#endif

[Setup]
AppId={{8E1B6E2A-9C4B-4F1E-9A77-3B7C5A2D4E10}
AppName={#MyAppName}
AppVersion={#MyVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 未签名时安装包会触发 SmartScreen，属正常现象
OutputDir=..\dist
OutputBaseFilename=py12306-Setup-v{#MyVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=EULA.txt
; 图标（由 make_icon.py 生成）
SetupIconFile=app_icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
; 中文向导：语言包随仓库携带（Inno Setup 官方安装包不带非英文语言，
; 直接引用编译器目录会找不到文件；这份来自官方推荐的中文翻译项目，适用 6.5+）
[Languages]
Name: "chinese"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: checkedonce

[Files]
; PyInstaller 的 onedir 产物整体拷进去
Source: "..\dist\py12306\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载时保留用户数据目录（账号配置、登录态、日志都在那），只删程序文件
Type: filesandordirs; Name: "{app}"
