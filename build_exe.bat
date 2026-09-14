@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM Переключаем консоль в UTF-8, чтобы кириллица не превращалась в кракозябры.
chcp 65001 >nul

title NetworkBackupManager - Build

REM ========================================================
REM ЕДИНЫЙ ЛОГ
REM ========================================================
REM
REM Весь вывод build.bat дублируется в build.log.
REM Файл перезаписывается при каждом запуске.
REM
REM Для запуска без паузы передайте --no-pause.
REM

set "LOG_FILE=%~dp0build.log"

REM Очищаем лог
> "%LOG_FILE%" echo ========================================
>>"%LOG_FILE%" echo   NetworkBackupManager BUILD LOG
>>"%LOG_FILE%" echo   Started: %DATE% %TIME%
>>"%LOG_FILE%" echo ========================================
>>"%LOG_FILE%" echo.

echo.
echo ========================================
echo   NetworkBackupManager EXE BUILD
echo ========================================
echo.

cd /d "%~dp0"

echo Project directory:
echo %CD%
echo.

>>"%LOG_FILE%" echo Project directory: %CD%
>>"%LOG_FILE%" echo.


REM ========================================================
REM 0. SANITY CHECKS
REM ========================================================

echo [0/8] Checking project files...

if not exist "main.py" (
    echo ERROR: main.py not found in %CD%
    >>"%LOG_FILE%" echo ERROR: main.py not found in %CD%
    pause
    exit /b 1
)

if not exist "web" (
    echo ERROR: web\ directory not found in %CD%
    >>"%LOG_FILE%" echo ERROR: web\ directory not found in %CD%
    pause
    exit /b 1
)

if not exist "web\templates" (
    echo WARNING: web\templates not found - web UI may not work.
    >>"%LOG_FILE%" echo WARNING: web\templates not found - web UI may not work.
)

if not exist "web\static" (
    echo WARNING: web\static not found - web UI may not work.
    >>"%LOG_FILE%" echo WARNING: web\static not found - web UI may not work.
)

if not exist "core" (
    echo ERROR: core\ directory not found in %CD%
    >>"%LOG_FILE%" echo ERROR: core\ directory not found in %CD%
    pause
    exit /b 1
)

if not exist "core\__init__.py" (
    echo WARNING: core\__init__.py not found - creating empty file.
    >>"%LOG_FILE%" echo WARNING: core\__init__.py not found - creating empty file.
    type nul > "core\__init__.py"
)

REM NSSM обязателен для установки службы Windows.
if not exist "bin\nssm.exe" (
    echo ERROR: bin\nssm.exe not found.
    echo        NSSM is required for Windows service installation.
    echo        Download from https://nssm.cc/download and place
    echo        nssm.exe [win64] into bin\nssm.exe.
    >>"%LOG_FILE%" echo ERROR: bin\nssm.exe not found.
    >>"%LOG_FILE%" echo NSSM is required for Windows service installation.
    pause
    exit /b 1
)

echo OK.
echo.
>>"%LOG_FILE%" echo [OK] Project files checked.
>>"%LOG_FILE%" echo.


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
>>"%LOG_FILE%" echo [OK] Clean complete.
>>"%LOG_FILE%" echo.


REM ========================================================
REM 2. CHECK PYTHON
REM ========================================================

echo [2/8] Checking Python...

python --version
if errorlevel 1 (
    echo ERROR: Python is not available in PATH.
    >>"%LOG_FILE%" echo ERROR: Python is not available in PATH.
    pause
    exit /b 1
)

REM Убираем двойные кавычки внутри строки Python.
REM cmd.exe не экранирует ''P'' так, как PowerShell.
for /f "delims=" %%V in ('python -c "import sys; print(sys.version.split()[0])"') do set PY_VERSION=%%V
for /f "delims=" %%B in ('python -c "import struct; print(struct.calcsize('P') * 8)"') do set PY_BITS=%%B

echo Python version: !PY_VERSION!
echo Python bits:    !PY_BITS!
echo.

>>"%LOG_FILE%" echo Python version: !PY_VERSION!
>>"%LOG_FILE%" echo Python bits:    !PY_BITS!
>>"%LOG_FILE%" echo.

echo OK.
echo.


REM ========================================================
REM 3. CHECK DEPENDENCIES
REM ========================================================

echo [3/8] Checking dependencies...

python -c "import fastapi, uvicorn, jinja2, paramiko, scp, nacl, cryptography, bcrypt" 2>nul
if errorlevel 1 (
    echo ERROR: Some Python dependencies are missing.
    echo Run: pip install -r requirements.txt
    echo.
    >>"%LOG_FILE%" echo ERROR: Some Python dependencies are missing.
    >>"%LOG_FILE%" echo Run: pip install -r requirements.txt
    >>"%LOG_FILE%" echo.
    pause
    exit /b 1
)

python -c "import tkinter" 2>nul
if errorlevel 1 (
    echo WARNING: tkinter not available - --create-user will not work.
    >>"%LOG_FILE%" echo WARNING: tkinter not available - --create-user will not work.
)

echo OK.
echo.
>>"%LOG_FILE%" echo [OK] Dependencies checked.
>>"%LOG_FILE%" echo.


REM ========================================================
REM 4. CHECK PYINSTALLER
REM ========================================================

echo [4/8] Checking PyInstaller...

python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: PyInstaller is not available for this Python.
    echo Install it with: pip install pyinstaller
    echo.
    >>"%LOG_FILE%" echo ERROR: PyInstaller is not available for this Python.
    >>"%LOG_FILE%" echo Install it with: pip install pyinstaller
    >>"%LOG_FILE%" echo.
    pause
    exit /b 1
)

for /f "delims=" %%V in ('python -m PyInstaller --version') do set PYI_VERSION=%%V
echo PyInstaller version: !PYI_VERSION!
echo.

>>"%LOG_FILE%" echo PyInstaller version: !PYI_VERSION!
>>"%LOG_FILE%" echo.

echo OK.
echo.


REM ========================================================
REM 5. BUILD EXE
REM ========================================================

echo [5/8] Building NetworkBackupManager...
echo.

>>"%LOG_FILE%" echo [5/8] Building NetworkBackupManager...
>>"%LOG_FILE%" echo.

set ICON_ARG=
if exist "assets\icon.ico" (
    set ICON_ARG=--icon "assets\icon.ico"
) else (
    echo NOTE: assets\icon.ico not found - building without icon.
    >>"%LOG_FILE%" echo NOTE: assets\icon.ico not found - building without icon.
)

set VERSION_ARG=
if exist "assets\version_info.txt" (
    set VERSION_ARG=--version-file "assets\version_info.txt"
) else (
    echo NOTE: assets\version_info.txt not found - building without version info.
    >>"%LOG_FILE%" echo NOTE: assets\version_info.txt not found - building without version info.
)

echo.

REM PyInstaller пишет много вывода в stdout/stderr.
REM Дублируем его и в консоль, и в лог через временный файл.
REM Используем "2>&1" чтобы захватить и stderr.

set "PYI_TEMP=%TEMP%\nbm_pyi_output.txt"

REM --onedir выбран осознанно:
REM для службы Windows через NSSM нельзя использовать --onefile,
REM потому что bootloader-родитель ломает управление процессами
REM через SCM (Stop-Service убивает родителя, ребёнок остаётся).
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onedir ^
  --noconsole ^
  --name NetworkBackupManager ^
  %ICON_ARG% ^
  %VERSION_ARG% ^
  --add-data "web;web" ^
  --add-data "bin;bin" ^
  --hidden-import core ^
  --hidden-import core.context ^
  --hidden-import core.service_installer ^
  --hidden-import core.plugin_manager ^
  --hidden-import core.secure_storage ^
  --hidden-import core.scheduler ^
  --hidden-import web ^
  --hidden-import web.server ^
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
  --collect-all scp ^
  main.py > "%PYI_TEMP%" 2>&1

set "PYI_EXIT=%ERRORLEVEL%"

REM Показываем вывод PyInstaller в консоли
type "%PYI_TEMP%"

REM И пишем его в лог
>>"%LOG_FILE%" echo --- PyInstaller output begin ---
type "%PYI_TEMP%" >> "%LOG_FILE%"
>>"%LOG_FILE%" echo --- PyInstaller output end ---
>>"%LOG_FILE%" echo.

del /f /q "%PYI_TEMP%" 2>nul

if not "%PYI_EXIT%"=="0" (
    echo.
    echo ========================================
    echo   BUILD FAILED
    echo ========================================
    echo.
    >>"%LOG_FILE%" echo ========================================
    >>"%LOG_FILE%" echo   BUILD FAILED
    >>"%LOG_FILE%" echo ========================================
    >>"%LOG_FILE%" echo.
    pause
    exit /b 1
)

echo.
echo EXE created successfully.
echo.
>>"%LOG_FILE%" echo [OK] EXE created successfully.
>>"%LOG_FILE%" echo.


REM ========================================================
REM 6. CHECK EXE
REM ========================================================

echo [6/8] Checking executable...

REM При --onedir EXE лежит в подкаталоге dist\NetworkBackupManager\.
if not exist "dist\NetworkBackupManager\NetworkBackupManager.exe" (
    echo ERROR: NetworkBackupManager.exe was not created.
    >>"%LOG_FILE%" echo ERROR: NetworkBackupManager.exe was not created.
    pause
    exit /b 1
)

for %%F in (
    "dist\NetworkBackupManager\NetworkBackupManager.exe"
) do (
    echo EXE size: %%~zF bytes
    >>"%LOG_FILE%" echo EXE size: %%~zF bytes
)

REM Проверяем, что nssm.exe попал в бандл.
if not exist "dist\NetworkBackupManager\_internal\bin\nssm.exe" (
    if not exist "dist\NetworkBackupManager\bin\nssm.exe" (
        echo WARNING: nssm.exe not found in dist - service install may fail.
        >>"%LOG_FILE%" echo WARNING: nssm.exe not found in dist - service install may fail.
    )
)

echo.
echo OK.
echo.
>>"%LOG_FILE%" echo [OK] Executable checked.
>>"%LOG_FILE%" echo.


REM ========================================================
REM 7. CREATE EXTERNAL DIRECTORIES
REM ========================================================

echo [7/8] Preparing external directories...

if not exist "dist\NetworkBackupManager\data"              mkdir "dist\NetworkBackupManager\data"
if not exist "dist\NetworkBackupManager\plugins"           mkdir "dist\NetworkBackupManager\plugins"
if not exist "dist\NetworkBackupManager\plugins\Mikrotik"  mkdir "dist\NetworkBackupManager\plugins\Mikrotik"
if not exist "dist\NetworkBackupManager\plugins\Pfsense"   mkdir "dist\NetworkBackupManager\plugins\Pfsense"
if not exist "dist\NetworkBackupManager\backups"           mkdir "dist\NetworkBackupManager\backups"
if not exist "dist\NetworkBackupManager\logs"              mkdir "dist\NetworkBackupManager\logs"

echo OK.
echo.
>>"%LOG_FILE%" echo [OK] External directories prepared.
>>"%LOG_FILE%" echo.


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
        "dist\NetworkBackupManager\plugins\Mikrotik\plugin.py" >nul 2>&1

    if errorlevel 1 (
        echo ERROR copying Mikrotik plugin.py
        >>"%LOG_FILE%" echo ERROR copying Mikrotik plugin.py
        pause
        exit /b 1
    )

    echo   [OK] plugins\Mikrotik\plugin.py
    >>"%LOG_FILE%" echo   [OK] plugins\Mikrotik\plugin.py
) else (
    echo   [WARNING] plugins\Mikrotik\plugin.py not found - plugin will be skipped
    >>"%LOG_FILE%" echo   [WARNING] plugins\Mikrotik\plugin.py not found - plugin will be skipped
)


REM --------------------------------------------------------
REM PFSENSE
REM --------------------------------------------------------

if exist "plugins\Pfsense\plugin.py" (
    copy /Y ^
        "plugins\Pfsense\plugin.py" ^
        "dist\NetworkBackupManager\plugins\Pfsense\plugin.py" >nul 2>&1

    if errorlevel 1 (
        echo ERROR copying Pfsense plugin.py
        >>"%LOG_FILE%" echo ERROR copying Pfsense plugin.py
        pause
        exit /b 1
    )

    echo   [OK] plugins\Pfsense\plugin.py
    >>"%LOG_FILE%" echo   [OK] plugins\Pfsense\plugin.py
) else (
    echo   [WARNING] plugins\Pfsense\plugin.py not found - plugin will be skipped
    >>"%LOG_FILE%" echo   [WARNING] plugins\Pfsense\plugin.py not found - plugin will be skipped
)

echo.
echo Only plugin.py files were copied.
echo routers.json and SSH keys were NOT copied.
echo.
>>"%LOG_FILE%" echo.
>>"%LOG_FILE%" echo Only plugin.py files were copied.
>>"%LOG_FILE%" echo routers.json and SSH keys were NOT copied.
>>"%LOG_FILE%" echo.


REM ========================================================
REM REMOVE PYTHON CACHE
REM ========================================================

echo Removing Python cache files...

REM При --onedir плагины лежат в dist\NetworkBackupManager\plugins.
for /r "dist\NetworkBackupManager\plugins" %%F in (*.pyc) do (
    del /f /q "%%F" >nul 2>&1
)

for /d /r "dist\NetworkBackupManager\plugins" %%D in (__pycache__) do (
    rmdir /s /q "%%D" >nul 2>&1
)

echo OK.
echo.
>>"%LOG_FILE%" echo [OK] Python cache removed.
>>"%LOG_FILE%" echo.


REM ========================================================
REM DO NOT COPY CONFIG / AUTH / KEYS
REM ========================================================

>>"%LOG_FILE%" echo.
>>"%LOG_FILE%" echo IMPORTANT:
>>"%LOG_FILE%" echo The following files are NOT copied from source plugins:
>>"%LOG_FILE%" echo   routers.json
>>"%LOG_FILE%" echo   id_rsa.key
>>"%LOG_FILE%" echo   *.key
>>"%LOG_FILE%" echo   *.pem
>>"%LOG_FILE%" echo   *.ppk
>>"%LOG_FILE%" echo data\auth.json is intentionally NOT copied.
>>"%LOG_FILE%" echo.
>>"%LOG_FILE%" echo On a new computer create the user with:
>>"%LOG_FILE%" echo   NetworkBackupManager.exe --create-user
>>"%LOG_FILE%" echo.


REM ========================================================
REM FINAL INFORMATION
REM ========================================================

echo ========================================
echo   BUILD SUCCESSFUL
echo ========================================
echo.
>>"%LOG_FILE%" echo ========================================
>>"%LOG_FILE%" echo   BUILD SUCCESSFUL
>>"%LOG_FILE%" echo ========================================
>>"%LOG_FILE%" echo.

echo Distribution:
echo.
echo   dist\
echo   ^|
echo   +-- NetworkBackupManager\
echo       ^|
echo       +-- NetworkBackupManager.exe
echo       +-- _internal\        (Python runtime + nssm.exe)
echo       ^|
echo       +-- plugins\
echo       ^|   +-- Mikrotik\
echo       ^|   ^|   +-- plugin.py
echo       ^|   ^|
echo       ^|   +-- Pfsense\
echo       ^|       +-- plugin.py
echo       ^|
echo       +-- data\
echo       +-- backups\
echo       +-- logs\
echo.

>>"%LOG_FILE%" echo Distribution:
>>"%LOG_FILE%" echo   dist\
>>"%LOG_FILE%" echo   +-- NetworkBackupManager\
>>"%LOG_FILE%" echo       +-- NetworkBackupManager.exe
>>"%LOG_FILE%" echo       +-- _internal\
>>"%LOG_FILE%" echo       +-- plugins\
>>"%LOG_FILE%" echo       +-- data\
>>"%LOG_FILE%" echo       +-- backups\
>>"%LOG_FILE%" echo       +-- logs\
>>"%LOG_FILE%" echo.

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

>>"%LOG_FILE%" echo SSH KEY POLICY:
>>"%LOG_FILE%" echo * SSH keys are NOT included in EXE.
>>"%LOG_FILE%" echo * SSH keys are NOT copied to dist.
>>"%LOG_FILE%" echo * routers.json files are NOT copied to dist.
>>"%LOG_FILE%" echo * SSH keys remain external files.
>>"%LOG_FILE%" echo * No icacls.
>>"%LOG_FILE%" echo * No chmod.
>>"%LOG_FILE%" echo * No takeown.
>>"%LOG_FILE%" echo.

REM Финализация лога
>>"%LOG_FILE%" echo ========================================
>>"%LOG_FILE%" echo   BUILD COMPLETE
>>"%LOG_FILE%" echo   Finished: %DATE% %TIME%
>>"%LOG_FILE%" echo ========================================

echo Лог сохранён в:
echo   %LOG_FILE%
echo.

REM pause только при интерактивном запуске (двойной клик).
if /i "%~1"=="--no-pause" (
    exit /b 0
)

pause
exit /b 0