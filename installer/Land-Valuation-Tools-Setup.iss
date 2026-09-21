; =============================================================
;  CAMA-Tools Installer Script
;  Built with Inno Setup
; =============================================================

; -------------------------------------------------------------
; SECTION: App Info
; -------------------------------------------------------------
#define MyAppName "Land Valuation Tools"
#define MyAppVersion "1.0.1"
#define MyAppPublisher "Integrated Geosys Development, Inc."
#define MyAppExeName "Land Valuation Tools.exe"

; Folder where the built Land Valuation Tools.exe lives.
; Resolved relative to this .iss file's location, so no machine-specific path is hardcoded.
; Expected layout: <this .iss file's folder>\..\dist\Land Valuation Tools.exe
#define SourceDistFolder SourcePath + "..\dist"

; Folder containing the wizard images (wizard_sidebar.bmp, wizard_header.bmp).
; Resolved relative to this .iss file's location, so no machine-specific path is hardcoded.
; Expected layout: <this .iss file's folder>\resources\wizard_sidebar.bmp / wizard_header.bmp
#define ResourcesFolder SourcePath + "resources"

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
OutputBaseFilename=Land-Valuation-Tools-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible

; Wizard sidebar image (large, left side of wizard pages) and header image (small, top-right).
; Both are .bmp files pulled from the resources folder defined above.
WizardImageFile={#ResourcesFolder}\wizard_sidebar.bmp
WizardSmallImageFile={#ResourcesFolder}\wizard_header.bmp

; Title bar / taskbar icon for the setup window and uninstaller.
SetupIconFile={#ResourcesFolder}\installer.ico

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
