@echo off
setlocal EnableExtensions EnableDelayedExpansion
title YouTube History Archiver - Setup
cd /d "%~dp0"

echo.
echo  ==========================================
echo   YouTube History Archiver - one-time setup
echo  ==========================================
echo.

where winget >nul 2>&1
if errorlevel 1 (
    set "HAVE_WINGET=0"
    echo  Note: winget was not found, so tools can't be installed automatically.
    echo  Get "App Installer" from the Microsoft Store to enable it.
    echo.
) else (
    set "HAVE_WINGET=1"
)

REM ---------------------------------------------------------------- Python 3
echo  [1/5] Looking for Python 3.10 or newer...
call :find_python
if not defined PY (
    if "!HAVE_WINGET!"=="0" goto :no_python
    echo        Not found. Installing Python 3.12...
    winget install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements
    call :refresh_path
    call :find_python
)
if not defined PY goto :no_python
for /f "delims=" %%V in ('call !PY! -c "import sys; print(sys.version.split()[0])"') do set "PYVER=%%V"
echo        Using Python !PYVER!  [!PY!]
echo.

REM ---------------------------------------------------------------- Python packages
echo  [2/5] Installing Python packages...
!PY! -m pip install --upgrade pip --quiet --disable-pip-version-check
!PY! -m pip install --upgrade -r requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 (
    echo        Package install failed. Check your internet connection and run setup.bat again.
    goto :fail
)
echo        Done.
echo.

REM ---------------------------------------------------------------- ffmpeg + Deno
echo  [3/5] Installing ffmpeg - needed for 1080p and above...
call :ensure_tool ffmpeg Gyan.FFmpeg
echo.
echo  [4/5] Installing Deno - yt-dlp needs it for YouTube...
call :ensure_tool deno DenoLand.Deno
echo.

REM ---------------------------------------------------------------- rclone (optional)
echo  [5/5] Cloud upload is optional. It uses rclone to copy videos to
echo        Google Drive, Dropbox, OneDrive, S3 and others.
choice /c YN /n /m "        Install rclone now? [Y/N] "
if errorlevel 2 (
    echo        Skipped. You can run setup.bat again later.
) else (
    call :ensure_tool rclone Rclone.Rclone
    where rclone >nul 2>&1
    if not errorlevel 1 (
        echo.
        choice /c YN /n /m "        Connect a cloud account now with rclone config? [Y/N] "
        if not errorlevel 2 (
            echo        Follow the prompts. Remember the name you give the remote, e.g. gdrive
            rclone config
        )
    )
)
echo.

REM ---------------------------------------------------------------- shortcuts
for /f "delims=" %%P in ('call !PY! -c "import sys,os; d=os.path.dirname(sys.executable); w=os.path.join(d,'pythonw.exe'); print(w if os.path.exists(w) else sys.executable)"') do set "PYW=%%P"

> "Start YouTube Archiver.bat" (
    echo @echo off
    echo cd /d "%%~dp0"
    echo start "" "!PYW!" app.py
)

set "SC_TARGET=!PYW!"
set "SC_ARGS="%~dp0app.py""
set "SC_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([IO.Path]::Combine([Environment]::GetFolderPath('Desktop'),'YouTube History Archiver.lnk'));" ^
  "$s.TargetPath=$env:SC_TARGET; $s.Arguments=$env:SC_ARGS; $s.WorkingDirectory=$env:SC_DIR;" ^
  "$s.Description='Saves every YouTube video you watch'; $s.Save()" >nul 2>&1
if errorlevel 1 (
    echo  Couldn't create a desktop shortcut. Use "Start YouTube Archiver.bat" in this folder instead.
) else (
    echo  Added a "YouTube History Archiver" shortcut to your desktop.
)

echo.
echo  ==========================================
echo   Setup finished.
echo  ==========================================
echo.
choice /c YN /n /m " Open the app now? [Y/N] "
if not errorlevel 2 start "" "!PYW!" "%~dp0app.py"
exit /b 0


REM ================================================================ helpers
:find_python
set "PY="
REM Prefer the Windows "py" launcher, asking it for Python 3 specifically
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 ( set "PY=py -3" & exit /b 0 )
for %%C in (python3 python) do (
    if not defined PY (
        %%C -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
        if not errorlevel 1 set "PY=%%C"
    )
)
if defined PY exit /b 0
REM Freshly installed but not on PATH yet
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%~fD\python.exe" (
        "%%~fD\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
        if not errorlevel 1 set "PY="%%~fD\python.exe""
    )
)
exit /b 0

:ensure_tool
REM %1 = command name, %2 = winget package id
where %1 >nul 2>&1
if not errorlevel 1 ( echo        Already installed. & exit /b 0 )
if "!HAVE_WINGET!"=="0" ( echo        Install %1 manually - see README.md & exit /b 0 )
winget install -e --id %2 --accept-source-agreements --accept-package-agreements
call :refresh_path
where %1 >nul 2>&1
if errorlevel 1 ( echo        Installed. It will be available after you restart the app. ) else ( echo        Done. )
exit /b 0

:refresh_path
REM Pick up PATH changes made by installers without reopening the window
set "UPATH=" & set "MPATH="
for /f "tokens=2,*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "UPATH=%%B"
for /f "tokens=2,*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "MPATH=%%B"
call set "PATH=%MPATH%;%UPATH%;%LOCALAPPDATA%\Microsoft\WinGet\Links"
exit /b 0

:no_python
echo.
echo  Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/
echo  and tick "Add python.exe to PATH" during install, then run setup.bat again.
start "" "https://www.python.org/downloads/"
goto :fail

:fail
echo.
pause
exit /b 1
