@echo off
rem Build OMRChecker on Windows 7 SP1 / 10 / 11 (x64).
rem Requires Python 3.8.10 x64 (the last release that runs on Windows 7):
rem   https://www.python.org/downloads/release/python-3810/
rem
rem Usage:  packaging\build_windows.bat [server|app|all] [onedir|onefile|both]
rem   server  the server build (ours): launcher, remote access, LAN, --bulk,
rem           with Tesseract, the OCR models and cloudflared. Mode applies here:
rem           onedir, onefile or both (default both).
rem           -> dist\server\OMRChecker\, dist\server\OMRChecker-portable-win64.zip,
rem              dist\server\OMRChecker-onefile.exe
rem   app     the app for distribution (WebView2 window), always onedir:
rem           -> dist\app\OMRChecker-app-modern-win64.zip  (Windows 10/11)
rem              dist\app\OMRChecker-app-legacy-win64.zip  (Windows 7/8.1, WebView2 109 inside)
rem              dist\app\OMRChecker-webview2-109.zip      (the runtime alone)
rem              and the unzipped app in dist\app\OMRChecker\
rem   all     both of the above
rem The old form "build_windows.bat onedir|onefile|both" builds the server.
setlocal
cd /d "%~dp0\.."

set KIND=%1
set MODE=%2
if /i "%KIND%"=="onedir" ( set MODE=onedir& set KIND=server )
if /i "%KIND%"=="onefile" ( set MODE=onefile& set KIND=server )
if /i "%KIND%"=="both" ( set MODE=both& set KIND=server )
if "%KIND%"=="" set KIND=server
if "%MODE%"=="" set MODE=both
if /i not "%KIND%"=="server" if /i not "%KIND%"=="app" if /i not "%KIND%"=="all" (
  echo Unknown build "%KIND%": use server, app or all
  exit /b 1
)

set PY=py -3.8
%PY% --version >nul 2>&1 || set PY=python

%PY% -c "import sys; assert sys.version_info[:2]==(3,8), 'Python 3.8 is required for a Windows 7 compatible build'" || exit /b 1

if not exist build\venv38 (
  %PY% -m venv build\venv38 || exit /b 1
)
call build\venv38\Scripts\activate.bat
python -m pip install --upgrade "pip<25" wheel || exit /b 1
python -m pip install -r packaging\requirements-win7.txt || exit /b 1
if /i not "%KIND%"=="server" (
  python -m pip install -r packaging\requirements-app.txt || exit /b 1
)

rem Tesseract and the OCR models (English, Hindi; PaddleOCR mobile and server for
rem printed text and handwriting) are always bundled; cloudflared in the server
if /i "%KIND%"=="app" (
  python packaging\prepare_bundle.py --only tesseract models || exit /b 1
) else (
  python packaging\prepare_bundle.py || exit /b 1
)
rem Name and icon from branding\branding.json and icon.png (icon.ico, branding.iss)
python packaging\apply_branding.py || echo Branding not refreshed: the committed icon.ico and branding.iss are used

rem ------------------------------------------------------------------ server
if /i "%KIND%"=="app" goto app
set OMR_APP=0
set OMR_BUILD_MODE=%MODE%
pyinstaller --noconfirm --clean --distpath dist\server --workpath build\pyinstaller-server packaging\omr.spec || exit /b 1
if exist dist\server\OMRChecker\OMRChecker.exe (
  if not exist dist\server\OMRChecker\tesseract\tesseract.exe ( echo Tesseract missing from the build & exit /b 1 )
  if not exist dist\server\OMRChecker\cloudflared\cloudflared.exe ( echo cloudflared missing from the build & exit /b 1 )
  dist\server\OMRChecker\OMRChecker.exe --version || exit /b 1
  dist\server\OMRChecker\OMRChecker.exe --selftest || exit /b 1
  powershell -NoProfile -Command "Compress-Archive -Force -Path dist\server\OMRChecker -DestinationPath dist\server\OMRChecker-portable-win64.zip" || exit /b 1
)
if exist dist\server\OMRChecker-onefile.exe (
  dist\server\OMRChecker-onefile.exe --selftest || exit /b 1
)
echo Server build: dist\server\
if /i "%KIND%"=="server" goto done

rem ------------------------------------------------------------------ app
:app
set OMR_APP=1
set OMR_BUILD_MODE=onedir
pyinstaller --noconfirm --clean --distpath dist\app --workpath build\pyinstaller-app packaging\omr.spec || exit /b 1
if not exist dist\app\OMRChecker\tesseract\tesseract.exe ( echo Tesseract missing from the build & exit /b 1 )
rem A windowed exe: wait for it, the result is its exit code (details in app.log)
start "" /wait dist\app\OMRChecker\OMRChecker.exe --selftest --data-dir "%CD%\build\app_selftest"
if errorlevel 1 ( echo The app selftest failed: see build\app_selftest\app.log & exit /b 1 )
rem WebView2 109 for the legacy zip (fetched once, signatures checked), then the zips
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\pack_app.ps1 || exit /b 1
echo App build: dist\app\

:done
endlocal
exit /b 0
