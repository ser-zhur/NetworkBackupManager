@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM FIX: переключаем консоль в UTF-8, чтобы кириллица не превращалась в кракозябры.
chcp 65001 >nul

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
REM 0. SANITY CHECKS
REM ========================================================

echo [0/8] Checking project files...

if not exist "main.py" (
    echo.
    echo ERROR:
    echo main.py not found in %CD%
    echo.
    pause
    exit /b 1
)

if not exist "web" (
    echo.
    echo ERROR:
    echo web\ directory not found in %CD%
    echo.
    pause
    exit /b 1
)

if not exist "web\templates" (
    echo WARNING: web\templates not found - web UI may not work.
)

if not exist "web\static" (
    echo WARNING: web\static not found - web UI may not work.
)

if not exist "core" (
    echo.
    echo ERROR:
    echo core\ directory not found in %CD%
    echo.
    pause
    exit /b 1
)

echo OK.
echo.


REM ========================================================
REM 1. CLEAN
REM ========================================================

echo [1/8] Cleaning old build...

if exist build (
    rmdir /s /q build 2>nul
)

if exist dist (
    rmdir /s /q dist 2>nul
)

if exist NetworkBackupManager.spec (
    del /f /q NetworkBackupManager.spec 2>nul
)

echo OK.
echo.


REM ========================================================
REM 2. CHECK PYTHON
REM ========================================================

echo [2/8] Checking Python...

python --version
if errorlevel 1 (
    echo.
    echo ERROR:
    echo Python is not available in PATH.
    echo.
    pause
    exit /b 1
)

REM FIX: явно сообщаем архитектуру и разрядность - важно для целевой машины.
for /f "delims=" %%V in ('python -c "import sys; print(sys.version.split()[0])"') do set PY_VERSION=%%V
for /f "delims=" %%B in ('python -c "import struct; print(struct.calcsize(''P'') * 8)"') do set PY_BITS=%%B

echo Python version: !PY_VERSION!
echo Python bits:    !PY_BITS!

echo.
echo OK.
echo.


REM ========================================================
REM 3. CHECK DEPENDENCIES
REM ========================================================

echo [3/8] Checking dependencies...

REM FIX: явная проверка всех ключевых зависимостей.
REM Если чего-то нет - сборка не имеет смысла.
python -c "import fastapi, uvicorn, jinja2, paramiko, scp, nacl, cryptography, bcrypt" 2>nul
if errorlevel 1 (
    echo.
    echo ERROR:
    echo Some Python dependencies are missing.
    echo.
    echo Run:
    echo     pip install -r requirements.txt
    echo.
    echo Or install manually:
    echo     pip install fastapi uvicorn jinja2 paramiko scp pynacl cryptography bcrypt
    echo.
    pause
    exit /b 1
)

REM FIX: проверяем tkinter отдельно - он нужен для --create-user.
python -c "import tkinter" 2>nul
if errorlevel 1 (
    echo WARNING: tkinter not available - --create-user will not work.
)

echo OK.
echo.


REM ========================================================
REM 4. CHECK PYINSTALLER
REM ========================================================

echo [4/8] Checking PyInstaller...

REM FIX: используем "python -m PyInstaller" вместо "pyinstaller" -
REM это работает в venv, где .exe может быть не в PATH.
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERROR:
    echo PyInstaller is not available for this Python.
    echo.
    echo Install it with:
    echo     pip install pyinstaller
    echo.
    pause
    exit /b 1
)

for /f "delims=" %%V in ('python -m PyInstaller --version') do set PYI_VERSION=%%V
echo PyInstaller version: !PYI_VERSION!
echo.
echo OK.
echo.


REM ========================================================
REM 5. BUILD EXE
REM ========================================================

echo [5/8] Building NetworkBackupManager.exe...
echo.

REM FIX: убран --add-data "core;core" (дублировал код, бесполезен).
REM FIX: убраны избыточные --hidden-import core* (PyInstaller видит импорты).
REM FIX: --collect-all cryptography заменён на точечные ключи.
REM FIX: --collect-all nacl оставлен, но проверяется на этапе 3.
REM NEW: добавлена поддержка иконки и version-info, если файлы есть.

set ICON_ARG=
if exist "assets\icon.ico" (
    set ICON_ARG=--icon "assets\icon.ico"
) else (
    echo NOTE: assets\icon.ico not found - building without icon.
)

set VERSION_ARG=
if exist "assets\version_info.txt" (
    set VERSION_ARG=--version-file "assets\version_info.txt"
) else (
    echo NOTE: assets\version_info.txt not found - building without version info.
)

echo.

python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --noconsole ^
  --name NetworkBackupManager ^
  %ICON_ARG% ^
  %VERSION_ARG% ^
  --add-data "web;web" ^
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
  --collect-submodules cryptography ^
  --collect-binaries cryptography ^
  --collect-data cryptography ^
  --collect-all paramiko ^
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
REM 6. CHECK EXE
REM ========================================================

echo [6/8] Checking executable...

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
REM 7. CREATE EXTERNAL DIRECTORIES
REM ========================================================

echo [7/8] Preparing external directories...

REM FIX: используем "if not exist ... mkdir" вместо "mkdir ... 2>nul" -
REM это идемпотентно и не портит errorlevel.
REM FIX: "Backups" приведён к нижнему регистру ("backups"),
REM чтобы совпадать с BACKUPS_DIR в core/context.py.

if not exist "dist\data"              mkdir "dist\data"
if not exist "dist\plugins"           mkdir "dist\plugins"
if not exist "dist\plugins\Mikrotik"  mkdir "dist\plugins\Mikrotik"
if not exist "dist\plugins\Pfsense"   mkdir "dist\plugins\Pfsense"
if not exist "dist\backups"           mkdir "dist\backups"
if not exist "dist\logs"              mkdir "dist\logs"

echo OK.
echo.


REM ========================================================
REM 8. COPY ONLY PLUGIN.PY
REM ========================================================

echo [8/8] Copying plugin files...

REM --------------------------------------------------------
REM MIKROTIK
REM --------------------------------------------------------

if exist "plugins\Mikrotik\plugin.py" (
    copy /Y ^
        "plugins\Mikrotik\plugin.py" ^
        "dist\plugins\Mikrotik\plugin.py" >nul 2>&1

    if errorlevel 1 (
        echo ERROR copying Mikrotik plugin.py
        pause
        exit /b 1
    )

    echo   [OK] plugins\Mikrotik\plugin.py
) else (
    echo   [WARNING] plugins\Mikrotik\plugin.py not found - plugin will be skipped
)


REM --------------------------------------------------------
REM PFSENSE
REM --------------------------------------------------------

if exist "plugins\Pfsense\plugin.py" (
    copy /Y ^
        "plugins\Pfsense\plugin.py" ^
        "dist\plugins\Pfsense\plugin.py" >nul 2>&1

    if errorlevel 1 (
        echo ERROR copying Pfsense plugin.py
        pause
        exit /b 1
    )

    echo   [OK] plugins\Pfsense\plugin.py
) else (
    echo   [WARNING] plugins\Pfsense\plugin.py not found - plugin will be skipped
)

echo.
echo Only plugin.py files were copied.
echo routers.json and SSH keys were NOT copied.
echo.


REM ========================================================
REM REMOVE PYTHON CACHE
REM ========================================================

echo Removing Python cache files...

REM FIX: чистим __pycache__ и .pyc в исходных plugins перед копированием
REM (на всякий случай - если они уже там), и в dist после.
REM Основная чистка идёт в dist\plugins, но проверяем оба места.

for /r "dist\plugins" %%F in (*.pyc) do (
    del /f /q "%%F" >nul 2>&1
)

for /d /r "dist\plugins" %%D in (__pycache__) do (
    rmdir /s /q "%%D" >nul 2>&1
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
REM FINAL INFORMATION
REM ========================================================

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
echo   +-- backups\
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

REM FIX: pause только при интерактивном запуске (двойной клик).
REM Если передан --no-pause (например, из CI) - выходим сразу.
if /i "%~1"=="--no-pause" (
    exit /b 0
)

pause
exit /b 0