; OMR Checker online installer (Inno Setup 6.5 or later).
;
; Setup.exe itself is small: while installing it downloads the program and its
; heavy files from a GitHub release, checks each one's SHA-256 and unpacks it
; into the install folder:
;   OMRChecker-app.zip         the program (Python runtime, engine, web GUI)
;   OMRChecker-ocr-models.zip  Tesseract and PaddleOCR models, ocr_build.json
;   OMRChecker-tools.zip       Tesseract and cloudflared
; Without internet, put the three zips next to Setup.exe: they are used instead.
;
; Built by the "installer" job of .github/workflows/build-windows.yml, which
; passes the release address and each zip's hash and unpacked size:
;   ISCC /DAppVersion=1.2.3 /DBaseUrl=https://github.com/<repo>/releases/download/<tag>
;        /DAppHash=... /DAppSize=... /DModelsHash=... /DModelsSize=...
;        /DToolsHash=... /DToolsSize=... OMRChecker.iss
;
; Program files go to Program Files (or the user's own programs folder when
; installed without admin rights); scans, templates and settings stay in
; %LOCALAPPDATA%\OMRChecker and survive uninstalling and upgrading.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef BaseUrl
  #error Pass /DBaseUrl=<release download address>
#endif

#define AppName "OMR Checker"
#define AppExe "OMRChecker.exe"

[Setup]
AppId={{6C1F4E2A-5B7D-4C1B-9E52-0D9A8F3B7C41}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=OMR Checker
DefaultDirName={autopf}\OMRChecker
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
OutputBaseFilename=OMRChecker-setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
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

[Files]
; Downloaded, unless the zip is next to Setup.exe
Source: "{#BaseUrl}/OMRChecker-app.zip"; DestDir: "{app}"; DestName: "OMRChecker-app.zip"; \
  ExternalSize: {#AppSize}; Hash: "{#AppHash}"; Check: not LocalZip('OMRChecker-app.zip'); \
  Flags: external download extractarchive ignoreversion recursesubdirs createallsubdirs
Source: "{src}\OMRChecker-app.zip"; DestDir: "{app}"; \
  ExternalSize: {#AppSize}; Hash: "{#AppHash}"; Check: LocalZip('OMRChecker-app.zip'); \
  Flags: external extractarchive ignoreversion recursesubdirs createallsubdirs

Source: "{#BaseUrl}/OMRChecker-ocr-models.zip"; DestDir: "{app}"; DestName: "OMRChecker-ocr-models.zip"; \
  ExternalSize: {#ModelsSize}; Hash: "{#ModelsHash}"; Check: not LocalZip('OMRChecker-ocr-models.zip'); \
  Flags: external download extractarchive ignoreversion recursesubdirs createallsubdirs
Source: "{src}\OMRChecker-ocr-models.zip"; DestDir: "{app}"; \
  ExternalSize: {#ModelsSize}; Hash: "{#ModelsHash}"; Check: LocalZip('OMRChecker-ocr-models.zip'); \
  Flags: external extractarchive ignoreversion recursesubdirs createallsubdirs

Source: "{#BaseUrl}/OMRChecker-tools.zip"; DestDir: "{app}"; DestName: "OMRChecker-tools.zip"; \
  ExternalSize: {#ToolsSize}; Hash: "{#ToolsHash}"; Check: not LocalZip('OMRChecker-tools.zip'); \
  Flags: external download extractarchive ignoreversion recursesubdirs createallsubdirs
Source: "{src}\OMRChecker-tools.zip"; DestDir: "{app}"; \
  ExternalSize: {#ToolsSize}; Hash: "{#ToolsHash}"; Check: LocalZip('OMRChecker-tools.zip'); \
  Flags: external extractarchive ignoreversion recursesubdirs createallsubdirs

[INI]
; Tells the program it is installed: data goes to %LOCALAPPDATA%\OMRChecker,
; never into the program folder (packaging/omr_desktop.py)
Filename: "{app}\installed.ini"; Section: "OMRChecker"; Key: "installed"; String: "{#AppVersion}"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Unpacked archives are not in the uninstall log: remove the program folder.
; Safe because setup only installs into an empty folder or an earlier install.
Type: filesandordirs; Name: "{app}"

[Code]
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
