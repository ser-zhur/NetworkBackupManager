; =========================================================
; NetworkBackupManager — Inno Setup script
; =========================================================
; Сборка: ISCC.exe installer.iss
; Выход:  dist\NetworkBackupManager-Setup.exe
; =========================================================

#define MyAppName "Network Backup Manager"
; Версия приходит из build_exe.bat (core\version.py): ISCC /DMyAppVersion=X.Y.Z
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif
#define MyAppPublisher "Network Backup Manager"
#define MyAppExeName "NetworkBackupManager.exe"
#define ServiceName "NetworkBackupManager"
#define ServiceDisplayName "Network Backup Manager"
#define ServiceDescription "Служба автоматического резервного копирования сетевого оборудования"

[Setup]
AppId={{8E9F0A1B-2C3D-4E5F-6789-ABCDEF012345}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\NetworkBackupManager
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=no
OutputDir=dist
OutputBaseFilename=NetworkBackupManager-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupLogging=yes
CloseApplications=no
RestartApplications=no
MinVersion=6.1sp1

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"; Flags: unchecked

[Dirs]
; Рабочие каталоги приложения. Пользовательские данные
; (data/, backups/, logs/, routers.json, ключи) создаются
; самим приложением и НЕ отслеживаются Inno Setup — они
; переживают uninstall, пока пользователь явно не согласится
; их удалить (см. CurUninstallStepChanged → usPostUninstall).
Name: "{app}\data"
Name: "{app}\logs"
Name: "{app}\backups"
Name: "{app}\plugins"
Name: "{app}\storages"

[Files]
; Основной исполняемый файл и его окружение
Source: "dist\NetworkBackupManager\NetworkBackupManager.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\NetworkBackupManager\_internal\*";          DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

; Web-UI: шаблоны Jinja2 и статика (tailwind.js, favicon.ico)
Source: "dist\NetworkBackupManager\web\*";                DestDir: "{app}\web";       Flags: ignoreversion recursesubdirs createallsubdirs

; Storage-бэкенды: git.py, ftp.py и будущие
Source: "dist\NetworkBackupManager\storages\*";           DestDir: "{app}\storages";  Flags: ignoreversion recursesubdirs createallsubdirs

; Плагины устройств. build_exe.bat кладёт в dist только plugin.py —
; routers.json и SSH-ключи пользователя НЕ попадают в дистрибутив.
; Wildcard покрывает текущие (Mikrotik, Pfsense) и будущие плагины
; без правок этого файла.
Source: "dist\NetworkBackupManager\plugins\*";            DestDir: "{app}\plugins";   Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}";           Filename: "{app}\{#MyAppExeName}"; Comment: "Запустить Network Backup Manager"
Name: "{group}\Открыть веб-панель";     Filename: "http://127.0.0.1:8080"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";     Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Открыть веб-панель после установки (пользователь может снять галку).
; Внимание: служба стартует в ssPostInstall секундой ранее, uvicorn
; поднимается не мгновенно — при первом клике браузер может показать
; «не удаётся получить доступ». Через 2–3 секунды обновление страницы
; решает проблему.
Filename: "http://127.0.0.1:8080"; Description: "Открыть веб-панель в браузере"; Flags: shellexec nowait postinstall skipifsilent

; =========================================================
; ВАЖНО: секция [UninstallDelete] удалена намеренно.
;
; Ранее здесь было:
;     Type: files; Name: "{app}\secret.key"
;
; Это удаляло Fernet-ключ БЕЗУСЛОВНО, до показа диалога
; «Удалить пользовательские данные?» в usPostUninstall.
; Если пользователь отвечал «Нет» (хотел сохранить настройки
; и backups для переустановки), routers.json оставался, но
; все зашифрованные в нём ssh_pass теряли возможность
; расшифровки — secret.key уже стёрт, Fernet.decrypt() вернёт "".
;
; Теперь secret.key удаляется только в usPostUninstall,
; по явному согласию пользователя, вместе с остальными
; пользовательскими данными (data/, backups/, logs/, ключи).
; =========================================================

[Code]

function NssmPath(): String;
begin
  Result := ExpandConstant('{app}\_internal\bin\nssm.exe');
end;

function RunNssm(Args: String; var ResultCode: Integer): Boolean;
var
  Nssm: String;
begin
  Nssm := NssmPath();
  if not FileExists(Nssm) then
  begin
    Result := False;
    Exit;
  end;
  Result := Exec(Nssm, Args, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure StopAndRemoveService();
var
  ResultCode: Integer;
begin
  // Игнорируем ошибки — службы может и не быть (первая установка).
  RunNssm('stop {#ServiceName}', ResultCode);
  Sleep(1500);
  RunNssm('remove {#ServiceName} confirm', ResultCode);
end;

function InstallAndStartService(): Boolean;
var
  Nssm: String;
  ServiceExe: String;
  AppDir: String;
  LogsDir: String;
  ResultCode: Integer;
begin
  Result := False;

  Nssm := NssmPath();
  if not FileExists(Nssm) then
  begin
    MsgBox('Не найден NSSM: ' + Nssm + #13#10 +
           'Установка службы невозможна. Запустите приложение вручную.',
           mbError, MB_OK);
    Exit;
  end;

  ServiceExe := ExpandConstant('{app}\{#MyAppExeName}');
  AppDir := ExpandConstant('{app}');
  LogsDir := ExpandConstant('{app}\logs');

  // --- Критичные параметры ---
  if (not RunNssm('install {#ServiceName} "' + ServiceExe + '" --service', ResultCode))
     or (ResultCode <> 0) then
  begin
    MsgBox('Не удалось зарегистрировать службу через NSSM.' + #13#10 +
           'Код возврата: ' + IntToStr(ResultCode),
           mbError, MB_OK);
    Exit;
  end;

  RunNssm('set {#ServiceName} AppDirectory "' + AppDir + '"', ResultCode);
  RunNssm('set {#ServiceName} Start SERVICE_AUTO_START', ResultCode);
  RunNssm('set {#ServiceName} AppExit 0 Exit', ResultCode);
  RunNssm('set {#ServiceName} AppExit Default Restart', ResultCode);

  // --- Необязательные параметры (ошибки не критичны) ---
  RunNssm('set {#ServiceName} Description "{#ServiceDescription}"', ResultCode);
  RunNssm('set {#ServiceName} AppRestartDelay 5000', ResultCode);
  RunNssm('set {#ServiceName} AppThrottle 1500', ResultCode);
  RunNssm('set {#ServiceName} AppStdout "' + LogsDir + '\service_stdout.log"', ResultCode);
  RunNssm('set {#ServiceName} AppStderr "' + LogsDir + '\service_stderr.log"', ResultCode);
  RunNssm('set {#ServiceName} AppRotateFiles 1', ResultCode);
  RunNssm('set {#ServiceName} AppRotateBytes 10485760', ResultCode);
  RunNssm('set {#ServiceName} AppRotateOnline 1', ResultCode);

  // --- Запуск ---
  RunNssm('start {#ServiceName}', ResultCode);

  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    // Останавливаем и удаляем старую версию службы (если есть).
    // Файлы старой версии на этом шаге ещё на месте.
    StopAndRemoveService();
  end
  else if CurStep = ssPostInstall then
  begin
    InstallAndStartService();
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
  Nssm: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    // Остановить и удалить службу до удаления файлов.
    Nssm := NssmPath();
    if FileExists(Nssm) then
    begin
      Exec(Nssm, 'stop {#ServiceName}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      Sleep(2000);
      Exec(Nssm, 'remove {#ServiceName} confirm', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
  end
  else if CurUninstallStep = usPostUninstall then
  begin
    if MsgBox(
      'Удалить пользовательские данные?' + #13#10 + #13#10 +
      'К ним относятся:' + #13#10 +
      '  • data\          — настройки, состояние, пароль' + #13#10 +
      '  • backups\       — все резервные копии (git-репозитории)' + #13#10 +
      '  • logs\          — логи приложения' + #13#10 +
      '  • secret.key     — ключ шифрования паролей' + #13#10 +
      '  • plugins\*\routers.json — список устройств' + #13#10 +
      '  • plugins\*\*.key        — SSH-ключи' + #13#10 + #13#10 +
      'Если планируете переустановить — ответьте «Нет».',
      mbConfirmation, MB_YESNO) = IDYES
    then
    begin
      DelTree(ExpandConstant('{app}\data'),    True, True, True);
      DelTree(ExpandConstant('{app}\backups'), True, True, True);
      DelTree(ExpandConstant('{app}\logs'),    True, True, True);

      // secret.key удаляется ТОЛЬКО здесь, по явному согласию.
      // Без него невозможно расшифровать ssh_pass в routers.json —
      // поэтому при «Нет» ключ сохраняется вместе с конфигами.
      DeleteFile(ExpandConstant('{app}\secret.key'));

      DeleteFile(ExpandConstant('{app}\plugins\Mikrotik\routers.json'));
      DeleteFile(ExpandConstant('{app}\plugins\Pfsense\routers.json'));

      // SSH-ключи (по маске)
      DeleteFile(ExpandConstant('{app}\plugins\Mikrotik\*.key'));
      DeleteFile(ExpandConstant('{app}\plugins\Mikrotik\*.pem'));
      DeleteFile(ExpandConstant('{app}\plugins\Mikrotik\*.ppk'));
      DeleteFile(ExpandConstant('{app}\plugins\Pfsense\*.key'));
      DeleteFile(ExpandConstant('{app}\plugins\Pfsense\*.pem'));
      DeleteFile(ExpandConstant('{app}\plugins\Pfsense\*.ppk'));
    end;
  end;
end;