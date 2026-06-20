; HOKWorldScript Windows 安装器(Inno Setup 6)。
; 由 build_installer.ps1 调用:ISCC /DMyAppVersion=<版本> /DMyDistDir=<onedir 目录> installer\HOKWorldScript.iss
; 特性:单用户安装(默认不需要管理员)、可选安装位置、开始菜单 + 可选桌面快捷方式、
;       覆盖升级、正常卸载;用户配置/日志在 %LOCALAPPDATA%\HOKWorldScript,卸载与升级都不动。

#define MyAppName "HOKWorld"
#define MyAppPublisher "Peiyu Yuan"
#define MyAppExeName "HOKWorld.exe"
#define MyDataName "HOKWorldScript"
#define MyAppURL "https://github.com/yueanipy/HOKWorld"

#ifndef MyAppVersion
  #define MyAppVersion "0.0.1"
#endif
#ifndef MyDistDir
  #define MyDistDir "..\dist\HOKWorld"
#endif

[Setup]
; 固定 AppId 保证覆盖升级被识别为同一程序(勿修改)
AppId={{8F2A6C31-7B4E-4D9A-9C1F-2E5B6A0D3C77}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
VersionInfoVersion={#MyAppVersion}
; 默认装到用户目录(PrivilegesRequired=lowest 时 {autopf}=%LOCALAPPDATA%\Programs),不需要管理员
DefaultDirName={autopf}\{#MyDataName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename={#MyDataName}-{#MyAppVersion}-Setup
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 升级时若程序在运行,自动请求关闭(配合主程序「下载更新→退出→安装器覆盖」流程)
CloseApplications=yes
CloseApplicationsFilter=*.exe
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; 整个 onedir 产物(HOKWorld.exe + _internal 资源)拷到安装目录
Source: "{#MyDistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

; 注意:这里不写 [UninstallDelete]——用户配置 / 日志 / 缓存在 %LOCALAPPDATA%\HOKWorldScript,
; 不在安装目录内,卸载只删程序文件,用户数据保留。
