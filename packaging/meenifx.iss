; meeniFX Studios installer (Inno Setup 6).
; Version values come from packaging/version.iss, generated from meenifx/version.py.
; Installs per user, so no administrator rights are requested, and never touches projects
; or settings: those live under the user's own AppData folders.

#include "version.iss"

[Setup]
AppId={{8E4D3C52-6B7A-4F9E-9A2C-1D7B5C3E8F01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=meeniFX
AppPublisherURL=https://github.com/meeniDev/meeniFX
AppSupportURL=https://github.com/meeniDev/meeniFX/issues
AppUpdatesURL=https://github.com/meeniDev/meeniFX/releases
DefaultDirName={localappdata}\Programs\meeniFX Studios
DefaultGroupName=meeniFX Studios
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=meeniFX-Studios-{#MyAppVersion}-Windows-Setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
VersionInfoVersion={#MyAppNumericVersion}
VersionInfoCompany=meeniFX
VersionInfoDescription={#MyAppName} installer
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppNumericVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
AllowNoIcons=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\meeniFX\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Runs for silent updates too, so the new build opens after the install completes.
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait

[UninstallDelete]
; Only the files Inno Setup itself generated; user projects, presets and settings stay.
Type: files; Name: "{app}\*.log"
