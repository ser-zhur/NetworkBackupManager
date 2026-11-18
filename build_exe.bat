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

echo [0/9] Checking project files...

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

if not exist "installer.iss" (
    echo ERROR: installer.iss not found in %CD%
    >>"%LOG_FILE%" echo ERROR: installer.iss not found in %CD%
    pause
    exit /b 1
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

REM Inno Setup 7 — для сборки инсталлятора.
set "ISCC="
if exist "%LOCALAPPDATA%\Programs\Inno Setup 7\ISCC.exe" (
    set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 7\ISCC.exe"
)
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 7\ISCC.exe" (
    set "ISCC=%ProgramFiles%\Inno Setup 7\ISCC.exe"
)

if not defined ISCC (
    echo ERROR: Inno Setup 7 not found.
    echo        Install from https://jrsoftware.org/isdl.php
    >>"%LOG_FILE%" echo ERROR: Inno Setup 7 not found.
    pause
    exit /b 1
)

echo Inno Setup: %ISCC%
>>"%LOG_FILE%" echo Inno Setup: %ISCC%

echo OK.
echo.
>>"%LOG_FILE%" echo [OK] Project files checked.
>>"%LOG_FILE%" echo.


REM ========================================================
REM 1. CLEAN
REM ========================================================

echo [1/9] Cleaning old build...

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

echo [2/9] Checking Python...

python --version
if errorlevel 1 (
    echo ERROR: Python is not available in PATH.
    >>"%LOG_FILE%" echo ERROR: Python is not available in PATH.
    pause
    exit /b 1
)

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

echo [3/9] Checking dependencies...

python -c "import fastapi, uvicorn, jinja2, paramiko, scp, nacl, cryptography, bcrypt, dulwich, py7zr" 2>nul
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

echo [4/9] Checking PyInstaller...

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

echo [5/9] Building NetworkBackupManager...
echo.

>>"%LOG_FILE%" echo [5/9] Building NetworkBackupManager...
>>"%LOG_FILE%" echo.

set ICON_ARG=
if exist "assets\icon.ico" (
    set ICON_ARG=--icon "assets\icon.ico"
) else (
    echo NOTE: assets\icon.ico not found - building without icon.
    >>"%LOG_FILE%" echo NOTE: assets\icon.ico not found - building without icon.
)

REM Version: single source core\version.py -> EXE properties + installer
set APP_VERSION=
for /f "delims=" %%V in ('python tools\version_info.py "build\version_info.txt"') do set APP_VERSION=%%V
if not defined APP_VERSION (
    echo ERROR: tools\version_info.py failed.
    >>"%LOG_FILE%" echo ERROR: tools\version_info.py failed.
    pause
    exit /b 1
)
set VERSION_ARG=--version-file "build\version_info.txt"
echo App version: !APP_VERSION!
>>"%LOG_FILE%" echo App version: !APP_VERSION!

echo.

set "PYI_TEMP=%TEMP%\nbm_pyi_output.txt"

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
  --add-data "storages;storages" ^
  --hidden-import core ^
  --hidden-import core.context ^
  --hidden-import core.service_installer ^
  --hidden-import core.plugin_manager ^
  --hidden-import core.secure_storage ^
  --hidden-import core.scheduler ^
  --hidden-import core.host_keys ^
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
  --collect-all dulwich ^
  --collect-all py7zr ^
  --collect-all Cryptodome ^
  main.py > "%PYI_TEMP%" 2>&1

set "PYI_EXIT=%ERRORLEVEL%"

type "%PYI_TEMP%"

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

echo [6/9] Checking executable...

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

echo [7/9] Preparing external directories...

if not exist "dist\NetworkBackupManager\data"              mkdir "dist\NetworkBackupManager\data"
if not exist "dist\NetworkBackupManager\plugins"           mkdir "dist\NetworkBackupManager\plugins"
if not exist "dist\NetworkBackupManager\plugins\Mikrotik"  mkdir "dist\NetworkBackupManager\plugins\Mikrotik"
if not exist "dist\NetworkBackupManager\plugins\Pfsense"   mkdir "dist\NetworkBackupManager\plugins\Pfsense"
if not exist "dist\NetworkBackupManager\backups"           mkdir "dist\NetworkBackupManager\backups"
if not exist "dist\NetworkBackupManager\logs"              mkdir "dist\NetworkBackupManager\logs"
if not exist "dist\NetworkBackupManager\storages"          mkdir "dist\NetworkBackupManager\storages"

echo OK.
echo.
>>"%LOG_FILE%" echo [OK] External directories prepared.
>>"%LOG_FILE%" echo.


REM ========================================================
REM 8. COPY PLUGINS AND STORAGES
REM ========================================================

echo [8/9] Copying plugin and storage files...

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

REM --------------------------------------------------------
REM STORAGE BACKENDS
REM --------------------------------------------------------

echo.
echo Copying storage backends...

for %%F in (storages\*.py) do (
    if not "%%~nxF"=="__init__.py" (
        copy /Y "%%F" "dist\NetworkBackupManager\storages\%%~nxF" >nul 2>&1
        if errorlevel 1 (
            echo ERROR copying %%F
            >>"%LOG_FILE%" echo ERROR copying %%F
            pause
            exit /b 1
        )
        echo   [OK] storages\%%~nxF
        >>"%LOG_FILE%" echo   [OK] storages\%%~nxF
    )
)

REM --------------------------------------------------------
REM WEB FILES (templates + static)
REM --------------------------------------------------------

echo.
echo Copying web files...

if not exist "web\templates" (
    echo ERROR: web\templates not found in project
    >>"%LOG_FILE%" echo ERROR: web\templates not found in project
    pause
    exit /b 1
)

if not exist "web\static" (
    echo ERROR: web\static not found in project
    >>"%LOG_FILE%" echo ERROR: web\static not found in project
    pause
    exit /b 1
)

xcopy /E /I /Y "web\templates" "dist\NetworkBackupManager\web\templates" >nul
if errorlevel 1 (
    echo ERROR copying web\templates
    >>"%LOG_FILE%" echo ERROR copying web\templates
    pause
    exit /b 1
)
echo   [OK] web\templates
>>"%LOG_FILE%" echo   [OK] web\templates

xcopy /E /I /Y "web\static" "dist\NetworkBackupManager\web\static" >nul
if errorlevel 1 (
    echo ERROR copying web\static
    >>"%LOG_FILE%" echo ERROR copying web\static
    pause
    exit /b 1
)
echo   [OK] web\static
>>"%LOG_FILE%" echo   [OK] web\static

REM ========================================================
REM REMOVE PYTHON CACHE
REM ========================================================

echo Removing Python cache files...

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
REM 9. BUILD INSTALLER (Inno Setup)
REM ========================================================

echo [9/9] Building installer with Inno Setup...
echo.

>>"%LOG_FILE%" echo [9/9] Building installer with Inno Setup...
>>"%LOG_FILE%" echo.

set "ISCC_TEMP=%TEMP%\nbm_iscc_output.txt"

"%ISCC%" /DMyAppVersion=%APP_VERSION% "installer.iss" > "%ISCC_TEMP%" 2>&1
set "ISCC_EXIT=%ERRORLEVEL%"

type "%ISCC_TEMP%"

>>"%LOG_FILE%" echo --- ISCC output begin ---
type "%ISCC_TEMP%" >> "%LOG_FILE%"
>>"%LOG_FILE%" echo --- ISCC output end ---
>>"%LOG_FILE%" echo.

del /f /q "%ISCC_TEMP%" 2>nul

if not "%ISCC_EXIT%"=="0" (
    echo.
    echo ========================================
    echo   INSTALLER BUILD FAILED
    echo ========================================
    echo.
    >>"%LOG_FILE%" echo ========================================
    >>"%LOG_FILE%" echo   INSTALLER BUILD FAILED
    >>"%LOG_FILE%" echo ========================================
    >>"%LOG_FILE%" echo.
    pause
    exit /b 1
)

if not exist "dist\NetworkBackupManager-Setup.exe" (
    echo ERROR: Installer was not created.
    >>"%LOG_FILE%" echo ERROR: Installer was not created.
    pause
    exit /b 1
)

for %%F in ("dist\NetworkBackupManager-Setup.exe") do (
    echo   Installer: dist\NetworkBackupManager-Setup.exe ^(%%~zF bytes^)
    >>"%LOG_FILE%" echo   Installer: dist\NetworkBackupManager-Setup.exe ^(%%~zF bytes^)
)

echo OK.
echo.
>>"%LOG_FILE%" echo [OK] Installer created.
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
echo   +-- NetworkBackupManager-Setup.exe   ^<- единый установщик
echo   ^|
echo   +-- NetworkBackupManager\            ^<- распакованная сборка
echo.
echo Установщик готов. Передавайте пользователю
echo только dist\NetworkBackupManager-Setup.exe.
echo.

>>"%LOG_FILE%" echo Distribution:
>>"%LOG_FILE%" echo   dist\
>>"%LOG_FILE%" echo   +-- NetworkBackupManager-Setup.exe
>>"%LOG_FILE%" echo   +-- NetworkBackupManager\
>>"%LOG_FILE%" echo.

>>"%LOG_FILE%" echo ========================================
>>"%LOG_FILE%" echo   BUILD COMPLETE
>>"%LOG_FILE%" echo   Finished: %DATE% %TIME%
>>"%LOG_FILE%" echo ========================================

echo Лог сохранён в:
echo   %LOG_FILE%
echo.

if /i "%~1"=="--no-pause" (
    exit /b 0
)

pause
exit /b 0