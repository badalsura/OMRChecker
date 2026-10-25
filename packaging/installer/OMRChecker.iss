; OMR Checker online installer (Inno Setup 6.5 or later).
;
; Two installers come from this script:
;   app     (default)    the app window, for distribution: OMRChecker-setup-*.exe
;   server  (/DServer=1) the launcher with remote access, for our own use:
;                        OMRChecker-server-setup-*.exe, its own folder and entry
;
; Setup.exe itself is small: while installing it downloads the program and its
; heavy files from a GitHub release, checks each one's SHA-256 and unpacks it
; into the install folder (<Prefix> is OMRChecker or OMRChecker-server):
;   <Prefix>-program.zip     the program (Python runtime, engine, screens)
;   <Prefix>-ocr-models.zip  Tesseract and PaddleOCR models, ocr_build.json
;   <Prefix>-tools.zip       Tesseract (server: and cloudflared)
; and for the app, only on the computers that need them:
;   OMRChecker-webview2-109.zip    WebView2 runtime 109: Windows 7/8.1, and
;                                  Windows 10/11 without WebView2 installed
;   ndp48-x86-x64-allos-enu.exe    .NET Framework 4.8 when older than 4.7.2
;                                  (Windows 7/8.1); needs an admin install
; Without internet, put those files next to Setup.exe: they are used instead.
;
; Built by the "installer" job of .github/workflows/build-windows.yml, which
; passes the release address and each zip's hash and unpacked size:
;   ISCC /DAppVersion=1.2.3 /DBaseUrl=https://github.com/<repo>/releases/download/<tag>
;        /DAppHash=... /DAppSize=... /DModelsHash=... /DModelsSize=...
;        /DToolsHash=... /DToolsSize=... /DPrefix=OMRChecker
;        [/DServer=1 | /DWebView2Hash=... /DWebView2Size=... /DDotNetHash=... /DDotNetSize=...]
;        OMRChecker.iss
;
; Program files go to Program Files (or the user's own programs folder when
; installed without admin rights); scans, templates and settings stay in
; %LOCALAPPDATA%\OMRChecker and survive uninstalling and upgrading.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef Prefix
  #define Prefix "OMRChecker"
#endif
#ifndef BaseUrl
  #error Pass /DBaseUrl=<release download address>
#endif

; AppName and AppPublisher: branding\branding.json, written out by
; packaging\apply_branding.py. The exe, install folder and data folder keep
; the name OMRChecker so a rename never moves anyone's results.
#include "..\..\branding\branding.iss"
#define AppExe "OMRChecker.exe"
#ifdef Server
  ; Its own entry and folder, so it installs beside the app; same data folder
  #define ServerName AppName + " Server"
  #undef AppName
  #define AppName ServerName
  #define AppGuid "{{2B9E7C11-8F4A-4D63-A1C5-7E30D9B4F682}"
  #define AppDir "OMRChecker Server"
#else
  #define AppGuid "{{6C1F4E2A-5B7D-4C1B-9E52-0D9A8F3B7C41}"
  #define AppDir "OMRChecker"
#endif

[Setup]
AppId={#AppGuid}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppDir}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Admin install for all users by default; the user may pick "only for me"
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 7 SP1, the oldest Windows the program runs on
MinVersion=6.1sp1
; .zip extraction needs the "full" 7-Zip method
ArchiveExtraction=full
OutputDir=..\..\dist
OutputBaseFilename={#Prefix}-setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\..\branding\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; An upgrade replaces the program files (the data folder is elsewhere)
Type: filesandordirs; Name: "{app}\tesseract"
Type: filesandordirs; Name: "{app}\tessdata"
Type: filesandordirs; Name: "{app}\models"
Type: filesandordirs; Name: "{app}\cloudflared"
Type: filesandordirs; Name: "{app}\webview2"
; Shortcuts of versions named "OMR Checker" (now named from branding.json)
Type: files; Name: "{autoprograms}\OMR Checker.lnk"
Type: files; Name: "{autodesktop}\OMR Checker.lnk"

[Files]
; Downloaded, unless the zip is next to Setup.exe
Source: "{#BaseUrl}/{#Prefix}-program.zip"; DestDir: "{app}"; DestName: "{#Prefix}-program.zip"; \
  ExternalSize: {#AppSize}; Hash: "{#AppHash}"; Check: not LocalZip('{#Prefix}-program.zip'); \
  Flags: external download extractarchive ignoreversion recursesubdirs createallsubdirs
Source: "{src}\{#Prefix}-program.zip"; DestDir: "{app}"; \
  ExternalSize: {#AppSize}; Hash: "{#AppHash}"; Check: LocalZip('{#Prefix}-program.zip'); \
  Flags: external extractarchive ignoreversion recursesubdirs createallsubdirs

Source: "{#BaseUrl}/{#Prefix}-ocr-models.zip"; DestDir: "{app}"; DestName: "{#Prefix}-ocr-models.zip"; \
  ExternalSize: {#ModelsSize}; Hash: "{#ModelsHash}"; Check: not LocalZip('{#Prefix}-ocr-models.zip'); \
  Flags: external download extractarchive ignoreversion recursesubdirs createallsubdirs
Source: "{src}\{#Prefix}-ocr-models.zip"; DestDir: "{app}"; \
  ExternalSize: {#ModelsSize}; Hash: "{#ModelsHash}"; Check: LocalZip('{#Prefix}-ocr-models.zip'); \
  Flags: external extractarchive ignoreversion recursesubdirs createallsubdirs

Source: "{#BaseUrl}/{#Prefix}-tools.zip"; DestDir: "{app}"; DestName: "{#Prefix}-tools.zip"; \
  ExternalSize: {#ToolsSize}; Hash: "{#ToolsHash}"; Check: not LocalZip('{#Prefix}-tools.zip'); \
  Flags: external download extractarchive ignoreversion recursesubdirs createallsubdirs
Source: "{src}\{#Prefix}-tools.zip"; DestDir: "{app}"; \
  ExternalSize: {#ToolsSize}; Hash: "{#ToolsHash}"; Check: LocalZip('{#Prefix}-tools.zip'); \
  Flags: external extractarchive ignoreversion recursesubdirs createallsubdirs

#ifndef Server
; WebView2 runtime 109 next to the program (packaging/omr_app.py uses it when present)
Source: "{#BaseUrl}/OMRChecker-webview2-109.zip"; DestDir: "{app}\webview2"; DestName: "OMRChecker-webview2-109.zip"; \
  ExternalSize: {#WebView2Size}; Hash: "{#WebView2Hash}"; Check: NeedsWebView2 and not LocalZip('OMRChecker-webview2-109.zip'); \
  Flags: external download extractarchive ignoreversion recursesubdirs createallsubdirs
Source: "{src}\OMRChecker-webview2-109.zip"; DestDir: "{app}\webview2"; \
  ExternalSize: {#WebView2Size}; Hash: "{#WebView2Hash}"; Check: NeedsWebView2 and LocalZip('OMRChecker-webview2-109.zip'); \
  Flags: external extractarchive ignoreversion recursesubdirs createallsubdirs

; .NET Framework 4.8 (Microsoft's offline installer), run before the program starts
Source: "{#BaseUrl}/ndp48-x86-x64-allos-enu.exe"; DestDir: "{tmp}"; DestName: "ndp48.exe"; \
  ExternalSize: {#DotNetSize}; Hash: "{#DotNetHash}"; Check: NeedsDotNet and not LocalZip('ndp48-x86-x64-allos-enu.exe'); \
  Flags: external download ignoreversion deleteafterinstall
Source: "{src}\ndp48-x86-x64-allos-enu.exe"; DestDir: "{tmp}"; DestName: "ndp48.exe"; \
  ExternalSize: {#DotNetSize}; Hash: "{#DotNetHash}"; Check: NeedsDotNet and LocalZip('ndp48-x86-x64-allos-enu.exe'); \
  Flags: external ignoreversion deleteafterinstall
#endif

[INI]
; Tells the program it is installed: data goes to %LOCALAPPDATA%\OMRChecker,
; never into the program folder (packaging/omr_desktop.py)
Filename: "{app}\installed.ini"; Section: "OMRChecker"; Key: "installed"; String: "{#AppVersion}"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
#ifndef Server
Filename: "{tmp}\ndp48.exe"; Parameters: "/q /norestart"; StatusMsg: "Installing .NET Framework 4.8 (this can take several minutes)..."; \
  Check: NeedsDotNet; Flags: waituntilterminated
; Windows 10/11 run WebView2's sandboxed parts as app containers: they must be
; able to read a runtime in the program folder (Microsoft's documented step)
Filename: "icacls.exe"; Parameters: """{app}\webview2"" /grant *S-1-15-2-2:(OI)(CI)(RX) /Q"; \
  Check: NeedsWebView2 and IsWindows10; Flags: runhidden waituntilterminated
Filename: "icacls.exe"; Parameters: """{app}\webview2"" /grant *S-1-15-2-1:(OI)(CI)(RX) /Q"; \
  Check: NeedsWebView2 and IsWindows10; Flags: runhidden waituntilterminated
#endif
; Not before the restart .NET Framework asks for
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Check: not NeedsDotNet; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Unpacked archives are not in the uninstall log: remove the program folder.
; Safe because setup only installs into an empty folder or an earlier install.
Type: filesandordirs; Name: "{app}"

[Code]
var
  DotNetNeeded, WebView2Needed: Boolean;

function IsWindows10: Boolean;
begin
  Result := GetWindowsVersion >= $0A000000;
end;

// .NET 4.7.2 or later (Release 461808): what pythonnet needs for WebView2
function DotNetPresent: Boolean;
var
  Release: Cardinal;
begin
  Result := RegQueryDWordValue(HKLM, 'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full', 'Release', Release)
    and (Release >= 461808);
end;

// The WebView2 runtime Windows installs and updates (Evergreen)
function EvergreenWebView2: Boolean;
var
  Version: String;
begin
  Result := False;
  if RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version)
    or RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) then
    Result := (Version <> '') and (Version <> '0.0.0.0');
end;

function NeedsDotNet: Boolean;
begin
  Result := DotNetNeeded;
end;

function NeedsWebView2: Boolean;
begin
  Result := WebView2Needed;
end;

function InitializeSetup: Boolean;
begin
#ifdef Server
  DotNetNeeded := False;
  WebView2Needed := False;
#else
  DotNetNeeded := not DotNetPresent;
  // Runtime 109 is the last for Windows 7/8.1; newer Windows use the installed one
  WebView2Needed := (not IsWindows10) or (not EvergreenWebView2);
#endif
  Result := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if DotNetNeeded and not IsAdminInstallMode then
    Result := 'This computer needs .NET Framework 4.8, which only an administrator can install. ' +
      'Run Setup again and choose to install for all users, or have an administrator install .NET Framework 4.8 first.';
end;

// The "Ready to install" page says what else Setup will do on this computer
function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
begin
  Result := '';
  if MemoDirInfo <> '' then Result := Result + MemoDirInfo + NewLine + NewLine;
  if MemoGroupInfo <> '' then Result := Result + MemoGroupInfo + NewLine + NewLine;
  if MemoTasksInfo <> '' then Result := Result + MemoTasksInfo + NewLine + NewLine;
  if DotNetNeeded or WebView2Needed then
  begin
    Result := Result + 'Also needed on this computer:' + NewLine;
    if DotNetNeeded then
      Result := Result + Space + 'Microsoft .NET Framework 4.8 (installed automatically; Windows may need to restart afterwards, and can take several minutes)' + NewLine;
    if WebView2Needed then
      Result := Result + Space + 'Microsoft Edge WebView2 Runtime 109 (placed in the program folder)' + NewLine;
  end;
end;

function NeedRestart: Boolean;
begin
  // .NET Framework usually finishes after a restart
  Result := DotNetNeeded;
end;

function LocalZip(Name: String): Boolean;
begin
  Result := FileExists(ExpandConstant('{src}\') + Name);
end;

function FolderIsEmpty(Dir: String): Boolean;
var
  Find: TFindRec;
begin
  Result := True;
  if FindFirst(AddBackslash(Dir) + '*', Find) then
  try
    repeat
      if (Find.Name <> '.') and (Find.Name <> '..') then
      begin
        Result := False;
        Exit;
      end;
    until not FindNext(Find);
  finally
    FindClose(Find);
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Dir: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    Dir := WizardDirValue;
    // Uninstalling removes the whole folder, so never install into one with other files
    if DirExists(Dir) and not FolderIsEmpty(Dir) and not FileExists(AddBackslash(Dir) + '{#AppExe}') then
    begin
      MsgBox('The folder ' + Dir + ' already holds other files. Choose an empty or new folder.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;
