; =============================================================
;  CAMA-Tools Installer Script
;  Built with Inno Setup
; =============================================================

; -------------------------------------------------------------
; SECTION: App Info
; -------------------------------------------------------------
#define MyAppName "CAMA-Tools"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "CAMA-Tools"
#define MyAppExeName "CAMA-Tools.exe"

; Change this to the actual folder where dist\CAMA-Tools.exe lives
; Example: E:\Work\CAMA-TOOLS\dist
#define SourceDistFolder "C:\IGEOSYS\CAMA-TOOLS\dist"

; -------------------------------------------------------------
; SECTION: Setup Configuration
; -------------------------------------------------------------
[Setup]
AppId={{B4C9E2A1-7F3D-4E8A-9C1B-1A2B3C4D5E6F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=.\Output
OutputBaseFilename=CAMA-Tools-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible

; -------------------------------------------------------------
; SECTION: Languages
; -------------------------------------------------------------
[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

; -------------------------------------------------------------
; SECTION: Optional Tasks (shown as checkboxes during install)
; -------------------------------------------------------------
[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

; -------------------------------------------------------------
; SECTION: Files to Install
; -------------------------------------------------------------
[Files]
Source: "{#SourceDistFolder}\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

; -------------------------------------------------------------
; SECTION: Shortcuts
; -------------------------------------------------------------
[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

; -------------------------------------------------------------
; SECTION: Post-Install Actions
; -------------------------------------------------------------
[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent unchecked
