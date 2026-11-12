@echo off
REM Builds dist\DSD2FLAC.exe. Run this from the app folder.
REM The Python environment and PyInstaller work files live outside this folder
REM The environment is private, so the program does not pick up libraries from this computer.

set "WORK=%LOCALAPPDATA%\DSD2FLAC-build"
cd /d "%~dp0"
if not exist "%WORK%\venv\Scripts\python.exe" (
    python -m venv "%WORK%\venv" || goto :fail
)
set "PY=%WORK%\venv\Scripts\python.exe"
"%PY%" -m pip install -r requirements.txt || goto :fail
"%PY%" -m unittest discover -s tests || goto :fail
"%PY%" -m PyInstaller --noconfirm --onefile --windowed --name DSD2FLAC --icon "%~dp0assets\app.ico" --add-data "%~dp0assets\app.ico;assets" --collect-all tkinterdnd2 --distpath dist --workpath "%WORK%\pyi" --specpath "%WORK%" src\main.py || goto :fail
echo.
echo Done: dist\DSD2FLAC.exe
exit /b 0

:fail
echo.
echo Build failed. Read the messages above.
exit /b 1
