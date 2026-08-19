@echo off
setlocal

echo ========================================
echo Building NetworkBackupManager.exe
echo ========================================

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

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
echo.
echo ========================================
echo Build completed
echo ========================================
pause