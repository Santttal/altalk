; Inno Setup script for the Windows installer, compiled under Wine by packaging/build-windows.sh
; with /DAppVersion, /DSourceDir (PyInstaller's folder build), /DIconFile and /DOutputDir.
; Installs for the current user only, into %LOCALAPPDATA%\Programs, so it needs no admin rights.
#define AppName "Altalk"

[Setup]
AppId={{665C240A-903A-4B54-90A9-3E34CE89775A}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Andrei Bogdanov
AppPublisherURL=https://github.com/Santttal/altalk
AppSupportURL=https://github.com/Santttal/altalk/issues
DefaultDirName={autopf}\{#AppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=altalk-{#AppVersion}-setup-x64
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\altalk.exe
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; An update closes the running Altalk; the finish page starts the new one.
CloseApplications=force
RestartApplications=no

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\altalk.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\altalk.exe"; Tasks: desktopicon

[Registry]
; Written by «Добавить в автозапуск» in the app's settings; removed together with the app.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "Altalk"; Flags: uninsdeletevalue dontcreatekey
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"; ValueType: none; ValueName: "Altalk"; Flags: uninsdeletevalue dontcreatekey

[Run]
Filename: "{app}\altalk.exe"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/im altalk.exe /f"; Flags: runhidden; RunOnceId: "StopAltalk"
