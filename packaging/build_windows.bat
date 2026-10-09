@echo off
rem Build the portable OMRChecker exe on Windows 7 SP1 / 10 / 11 (x64).
rem Requires Python 3.8.10 x64 (the last release that runs on Windows 7):
rem   https://www.python.org/downloads/release/python-3810/
rem Usage:  packaging\build_windows.bat [onedir|onefile|both]
setlocal
cd /d "%~dp0\.."

set MODE=%1
if "%MODE%"=="" set MODE=both
set PY=py -3.8
%PY% --version >nul 2>&1 || set PY=python

%PY% -c "import sys; assert sys.version_info[:2]==(3,8), 'Python 3.8 is required for a Windows 7 compatible build'" || exit /b 1

if not exist build\venv38 (
  %PY% -m venv build\venv38 || exit /b 1
)
call build\venv38\Scripts\activate.bat
python -m pip install --upgrade "pip<25" wheel || exit /b 1
python -m pip install -r packaging\requirements-win7.txt || exit /b 1

rem Tesseract, the OCR models (English, Hindi; PaddleOCR mobile and server for
rem printed text and handwriting) and cloudflared are always bundled
python packaging\prepare_bundle.py || exit /b 1

set OMR_BUILD_MODE=%MODE%
pyinstaller --noconfirm --clean --distpath dist --workpath build\pyinstaller packaging\omr.spec || exit /b 1

rem Smoke tests
if exist dist\OMRChecker\OMRChecker.exe (
  if not exist dist\OMRChecker\tesseract\tesseract.exe ( echo Tesseract missing from the build & exit /b 1 )
  if not exist dist\OMRChecker\cloudflared\cloudflared.exe ( echo cloudflared missing from the build & exit /b 1 )
  dist\OMRChecker\OMRChecker.exe --version || exit /b 1
  dist\OMRChecker\OMRChecker.exe --selftest || exit /b 1
  powershell -NoProfile -Command "Compress-Archive -Force -Path dist\OMRChecker -DestinationPath dist\OMRChecker-portable-win64.zip" || exit /b 1
)
if exist dist\OMRChecker-onefile.exe (
  dist\OMRChecker-onefile.exe --selftest || exit /b 1
)
echo.
echo Built: dist\OMRChecker\ (portable folder), dist\OMRChecker-portable-win64.zip, dist\OMRChecker-onefile.exe
endlocal
