@echo off
setlocal EnableExtensions

title NetworkBackupManager - Build

echo.
echo ========================================
echo   NetworkBackupManager EXE BUILD
echo ========================================
echo.

cd /d "%~dp0"

echo Project directory:
echo %CD%
echo.


REM ========================================================
REM 1. CLEAN
REM ========================================================

echo [1/7] Cleaning old build...

if exist build (
    rmdir /s /q build
)

if exist dist (
    rmdir /s /q dist
)

if exist NetworkBackupManager.spec (
    del /f /q NetworkBackupManager.spec
)

echo OK.
echo.


REM ========================================================
REM 2. CHECK PYINSTALLER
REM ========================================================

echo [2/7] Checking PyInstaller...

pyinstaller --version

if errorlevel 1 (
    echo.
    echo ERROR:
    echo PyInstaller is not available.
    echo.
    pause
    exit /b 1
)

echo.
echo OK.
echo.


REM ========================================================
REM 3. BUILD EXE
REM ========================================================

echo [3/7] Building NetworkBackupManager.exe...
echo.

pyinstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --noconsole ^
  --name NetworkBackupManager ^
  --add-data "web;web" ^
  --add-data "core;core" ^
  --hidden-import core ^
  --hidden-import core.context ^
  --hidden-import core.secure_storage ^
  --hidden-import core.scheduler ^
  --hidden-import core.plugin_manager ^
  --hidden-import core.service_installer ^
  --hidden-import fastapi ^
  --hidden-import fastapi.templating ^
  --hidden-import fastapi.staticfiles ^
  --hidden-import starlette.templating ^
  --hidden-import starlette.staticfiles ^
  --hidden-import jinja2 ^
  --hidden-import uvicorn ^
  --hidden-import uvicorn.logging ^
  --hidden-import uvicorn.loops.auto ^
  --hidden-import uvicorn.protocols.http.auto ^
  --hidden-import uvicorn.protocols.websockets.auto ^
  --hidden-import scp ^
  --collect-all paramiko ^
  --collect-all cryptography ^
  --collect-all bcrypt ^
  --collect-all nacl ^
  main.py

if errorlevel 1 (
    echo.
    echo ========================================
    echo   BUILD FAILED
    echo ========================================
    echo.
    echo Check the error above.
    echo.
    pause
    exit /b 1
)

echo.
echo EXE created successfully.
echo.


REM ========================================================
REM 4. CHECK EXE
REM ========================================================

echo [4/7] Checking executable...

if not exist "dist\NetworkBackupManager.exe" (
    echo.
    echo ERROR:
    echo NetworkBackupManager.exe was not created.
    echo.
    pause
    exit /b 1
)

for %%F in (
    "dist\NetworkBackupManager.exe"
) do (
    echo EXE size:
    echo %%~zF bytes
)

echo.
echo OK.
echo.


REM ========================================================
REM 5. CREATE EXTERNAL DIRECTORIES
REM ========================================================

echo [5/7] Preparing external directories...

mkdir "dist\data" 2>nul
mkdir "dist\plugins" 2>nul
mkdir "dist\plugins\Mikrotik" 2>nul
mkdir "dist\plugins\Pfsense" 2>nul
mkdir "dist\Backups" 2>nul
mkdir "dist\logs" 2>nul

echo OK.
echo.


REM ========================================================
REM 6. COPY ONLY PLUGIN.PY
REM ========================================================

echo [6/7] Copying plugin files...

REM --------------------------------------------------------
REM MIKROTIK
REM --------------------------------------------------------

if exist "plugins\Mikrotik\plugin.py" (

    copy /Y ^
        "plugins\Mikrotik\plugin.py" ^
        "dist\plugins\Mikrotik\plugin.py" >nul

    if errorlevel 1 (
        echo ERROR copying Mikrotik plugin.py
        pause
        exit /b 1
    )

    echo   [OK] plugins\Mikrotik\plugin.py

) else (

    echo   [WARNING] plugins\Mikrotik\plugin.py not found

)


REM --------------------------------------------------------
REM PFSENSE
REM --------------------------------------------------------

if exist "plugins\Pfsense\plugin.py" (

    copy /Y ^
        "plugins\Pfsense\plugin.py" ^
        "dist\plugins\Pfsense\plugin.py" >nul

    if errorlevel 1 (
        echo ERROR copying Pfsense plugin.py
        pause
        exit /b 1
    )

    echo   [OK] plugins\Pfsense\plugin.py

) else (

    echo   [WARNING] plugins\Pfsense\plugin.py not found

)

echo.
echo Only plugin.py files were copied.
echo routers.json and SSH keys were NOT copied.
echo.


REM ========================================================
REM REMOVE PYTHON CACHE
REM ========================================================

echo Removing Python cache files...

for /r "dist\plugins" %%F in (*.pyc) do (
    del /f /q "%%F" >nul 2>&1
)

for /d /r "dist\plugins" %%D in (__pycache__) do (
    if exist "%%D" (
        rmdir /s /q "%%D" >nul 2>&1
    )
)

echo OK.
echo.


REM ========================================================
REM DO NOT COPY CONFIG / AUTH / KEYS
REM ========================================================

echo.
echo IMPORTANT:
echo.
echo The following files are NOT copied from source plugins:
echo.
echo   routers.json
echo   id_rsa.key
echo   *.key
echo   *.pem
echo   *.ppk
echo   any other plugin files
echo.
echo data\auth.json is intentionally NOT copied.
echo.
echo On a new computer create the user with:
echo.
echo   NetworkBackupManager.exe --create-user
echo.
echo.


REM ========================================================
REM 7. FINAL INFORMATION
REM ========================================================

echo [7/7] Build completed.
echo.

echo ========================================
echo   BUILD SUCCESSFUL
echo ========================================
echo.

echo Distribution:
echo.
echo   dist\
echo   ^|
echo   +-- NetworkBackupManager.exe
echo   ^|
echo   +-- plugins\
echo   ^|   +-- Mikrotik\
echo   ^|   ^|   +-- plugin.py
echo   ^|   ^|
echo   ^|   +-- Pfsense\
echo   ^|       +-- plugin.py
echo   ^|
echo   +-- data\
echo   +-- Backups\
echo   +-- logs\
echo.

echo ========================================
echo.
echo SSH KEY POLICY:
echo.
echo * SSH keys are NOT included in EXE.
echo * SSH keys are NOT copied to dist.
echo * routers.json files are NOT copied to dist.
echo * SSH keys remain external files.
echo * Installer does NOT change key permissions.
echo * Installer does NOT change key ownership.
echo * No icacls.
echo * No chmod.
echo * No takeown.
echo.
echo ========================================
echo.

pause
exit /b 0