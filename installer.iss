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
AppPublisherURL=https://github.com/ser-zhur/NetworkBackupManager
AppCopyright=© 2026 Жураченко. Лицензия AGPL-3.0
; Текст лицензии на странице установщика (в тихом режиме не показывается)
LicenseFile=LICENSE
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
; Python 3.13+ (внутри EXE) — только Windows 10 и новее
MinVersion=10.0

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"; Flags: unchecked

[Dirs]
; Пользовательские данные: data\ (устройства, SSH-ключи, настройки,
; secret.key), backups\, logs\. Обновление их не трогает, удаление
; программы — только по явному согласию (usPostUninstall).
Name: "{app}\data";    Flags: uninsneveruninstall
Name: "{app}\logs";    Flags: uninsneveruninstall
Name: "{app}\backups"; Flags: uninsneveruninstall
Name: "{app}\plugins"
Name: "{app}\storages"

[InstallDelete]
; Обновление: код прежней версии убираем целиком. Иначе в _internal\
; остались бы библиотеки старой сборки PyInstaller (смесь версий —
; частая причина падений после обновления). Данные не затрагиваются.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\web"

[Files]
; Основной исполняемый файл и его окружение
Source: "dist\NetworkBackupManager\NetworkBackupManager.exe"; DestDir: "{app}"; Flags: ignoreversion

; Лицензия программы и лицензии сторонних компонентов
Source: "dist\NetworkBackupManager\LICENSE.txt";              DestDir: "{app}"; Flags: ignoreversion
Source: "dist\NetworkBackupManager\THIRD_PARTY_LICENSES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\NetworkBackupManager\_internal\*";          DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

; Web-UI: шаблоны Jinja2 и статика (tailwind.js, favicon.ico)
Source: "dist\NetworkBackupManager\web\*";                DestDir: "{app}\web";       Flags: ignoreversion recursesubdirs createallsubdirs

; Storage-бэкенды: git.py, ftp.py и будущие
Source: "dist\NetworkBackupManager\storages\*";           DestDir: "{app}\storages";  Flags: ignoreversion recursesubdirs createallsubdirs

; Плагины устройств: plugin.py и profiles\*.json каждого плагина
; (build_exe.bat). Свои плагины пользователя в plugins\ не удаляются.
Source: "dist\NetworkBackupManager\plugins\*";            DestDir: "{app}\plugins";   Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Ярлык запускает программу: без прав администратора она открывает
; веб-интерфейс по адресу, который записала служба (порт и HTTPS —
; как в настройках), а не по зашитому http://127.0.0.1:8080
Name: "{group}\{#MyAppName}";           Filename: "{app}\{#MyAppExeName}"; Comment: "Открыть веб-интерфейс Network Backup Manager"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";     Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Открыть веб-интерфейс после установки (пользователь может снять
; галку). Программа от имени вошедшего пользователя дожидается
; запуска службы и открывает её настоящий адрес.
Filename: "{app}\{#MyAppExeName}"; Description: "Открыть веб-интерфейс в браузере"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallDelete]
; Адрес веб-интерфейса для ярлыка (пишет служба). data\secret.key
; (ключ шифрования паролей) здесь нет намеренно: он удаляется только
; вместе с остальными данными и по согласию пользователя
; (usPostUninstall).
Type: files; Name: "{app}\web-address.txt"

[Code]

// ---------------------------------------------------------
// Пользователь веб-интерфейса
// ---------------------------------------------------------
// Страница «Логин и пароль» — при первой установке (нет
// data\auth.json). Пользователя создаёт сама программа
// (--init-user) из NBM_ADMIN_USER / NBM_ADMIN_PASSWORD: пароль
// передаётся через окружение, а не командной строкой, чтобы не
// попасть в список процессов. Хэширует программа, как и везде.
//
// Тихая установка: страница не показывается; если в окружении
// установщика задан NBM_ADMIN_PASSWORD (и, по желанию,
// NBM_ADMIN_USER), пользователь создаётся из него.
//
// На той же странице — порт веб-интерфейса (проверяется, не
// занят ли): занятый порт — служба перезапускалась бы по кругу,
// а сменить его в интерфейсе было бы нельзя. --init-user
// записывает его из NBM_PORT в config.json.

var
  UserPage: TInputQueryWizardPage;

function SetEnvironmentVariable(lpName: String; lpValue: String): Boolean;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';
// Второе объявление той же функции: lpValue = NULL удаляет переменную
function DeleteEnvironmentVariable(lpName: String; lpValue: Cardinal): Boolean;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';

function AuthFile(): String;
begin
  Result := ExpandConstant('{app}\data\auth.json');
end;

procedure InitializeWizard();
begin
  UserPage := CreateInputQueryPage(wpSelectDir,
    'Пользователь веб-интерфейса',
    'Логин и пароль для входа в веб-интерфейс.',
    'Пароль — не короче 8 символов. Сменить его потом можно в командной строке ' +
    'от имени администратора: NetworkBackupManager.exe --create-user');
  UserPage.Add('Логин:', False);
  UserPage.Add('Пароль:', True);
  UserPage.Add('Повторите пароль:', True);
  UserPage.Add('Порт веб-интерфейса:', False);
  UserPage.Values[0] := 'admin';
  UserPage.Values[3] := '8080';
end;

// Порт слушает другая программа (не прежняя копия NBM).
// Нет Get-NetTCPConnection или ошибка — считаем свободным.
function PortBusy(Port: Integer): Boolean;
var
  ResultCode: Integer;
begin
  Result := False;
  if Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
          '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' +
          '$c = Get-NetTCPConnection -State Listen -LocalPort ' + IntToStr(Port) +
          ' -ErrorAction SilentlyContinue | Where-Object { ' +
          '(Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName -ne ''NetworkBackupManager'' }; ' +
          'if ($c) { exit 1 } else { exit 0 }"',
          '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Result := (ResultCode = 1);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  // Пропускаем:
  //  • обновление или переустановку с сохранёнными данными —
  //    пользователь уже есть;
  //  • тихую установку — мастер в ней сам нажимает «Далее»,
  //    и проверка пустого пароля (MsgBox) ждала бы ответа
  //    вечно; пароль тогда — из окружения (CreateWebUser).
  if (UserPage <> nil) and (PageID = UserPage.ID) then
    Result := WizardSilent() or FileExists(AuthFile());
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Login: String;
  Port: Integer;
begin
  Result := True;
  if (UserPage = nil) or (CurPageID <> UserPage.ID) or WizardSilent() then
    Exit;
  Login := Trim(UserPage.Values[0]);
  if (Login = '') or (Length(Login) > 100) then
  begin
    MsgBox('Укажите логин (до 100 символов).', mbError, MB_OK);
    Result := False;
  end
  else if Length(UserPage.Values[1]) < 8 then
  begin
    MsgBox('Пароль должен быть не короче 8 символов.', mbError, MB_OK);
    Result := False;
  end
  else if UserPage.Values[1] <> UserPage.Values[2] then
  begin
    MsgBox('Пароли не совпадают.', mbError, MB_OK);
    Result := False;
  end
  else
  begin
    Port := StrToIntDef(Trim(UserPage.Values[3]), 0);
    if (Port < 1) or (Port > 65535) then
    begin
      MsgBox('Порт — число от 1 до 65535.', mbError, MB_OK);
      Result := False;
    end
    else if PortBusy(Port) then
    begin
      MsgBox('Порт ' + IntToStr(Port) + ' занят другой программой. ' +
             'Укажите другой, например 8090.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure CreateWebUser();
var
  Login, Password, Reason: String;
  ResultCode: Integer;
  FromPage: Boolean;
begin
  if FileExists(AuthFile()) then
    Exit;

  // Со страницы мастера — пользователь и порт через окружение;
  // в тихой установке они уже в окружении (NBM_ADMIN_USER,
  // NBM_ADMIN_PASSWORD, NBM_PORT), --init-user возьмёт их сам
  FromPage := (UserPage <> nil) and (UserPage.Values[1] <> '');
  if FromPage then
  begin
    Login := Trim(UserPage.Values[0]);
    Password := UserPage.Values[1];
    SetEnvironmentVariable('NBM_ADMIN_USER', Login);
    SetEnvironmentVariable('NBM_ADMIN_PASSWORD', Password);
    SetEnvironmentVariable('NBM_PORT', Trim(UserPage.Values[3]));
  end;

  ResultCode := -1;
  if not Exec(ExpandConstant('{app}\{#MyAppExeName}'), '--init-user',
              ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    ResultCode := -1;

  if FromPage then
  begin
    DeleteEnvironmentVariable('NBM_ADMIN_USER', 0);
    DeleteEnvironmentVariable('NBM_ADMIN_PASSWORD', 0);
    DeleteEnvironmentVariable('NBM_PORT', 0);
  end;

  case ResultCode of
    0: begin Log('Пользователь веб-интерфейса создан'); Exit; end;
    2: begin
         // Тихая установка без пароля: пользователя создадут позже
         Log('Пользователь веб-интерфейса не задан — нужен --create-user');
         Exit;
       end;
    3: Reason := 'пароль короче 8 символов';
    4: Reason := 'неверный порт в NBM_PORT';
  else
    Reason := 'код ' + IntToStr(ResultCode);
  end;
  SuppressibleMsgBox(
    'Не удалось создать пользователя веб-интерфейса (' + Reason + ').' + #13#10 + #13#10 +
    'Создайте его после установки в командной строке от имени администратора:' + #13#10 +
    '"' + ExpandConstant('{app}\{#MyAppExeName}') + '" --create-user',
    mbError, MB_OK, IDOK);
end;

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
    SuppressibleMsgBox('Не найден NSSM: ' + Nssm + #13#10 +
           'Установка службы невозможна. Запустите приложение вручную.',
           mbError, MB_OK, IDOK);
    Exit;
  end;

  ServiceExe := ExpandConstant('{app}\{#MyAppExeName}');
  AppDir := ExpandConstant('{app}');
  LogsDir := ExpandConstant('{app}\logs');

  // --- Критичные параметры ---
  if (not RunNssm('install {#ServiceName} "' + ServiceExe + '" --service', ResultCode))
     or (ResultCode <> 0) then
  begin
    SuppressibleMsgBox('Не удалось зарегистрировать службу через NSSM.' + #13#10 +
           'Код возврата: ' + IntToStr(ResultCode),
           mbError, MB_OK, IDOK);
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

// ---------------------------------------------------------
// Права на данные
// ---------------------------------------------------------
// Папки внутри Program Files наследуют «Пользователи: чтение».
// В data\ — secret.key (расшифровывает пароли устройств),
// SSH-ключи, auth.json; в backups\ — конфигурации; в logs\ —
// адреса и ошибки. Оставляем доступ только SYSTEM (служба) и
// Администраторам: наследование отключается, вложенные файлы
// получают права от папки. SID вместо имён — имена групп
// зависят от языка Windows.

procedure ProtectDir(Dir: String);
var
  ResultCode: Integer;
begin
  if not DirExists(Dir) then
    Exit;
  if (not Exec(ExpandConstant('{sys}\icacls.exe'),
               '"' + Dir + '" /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F /Q',
               '', SW_HIDE, ewWaitUntilTerminated, ResultCode))
     or (ResultCode <> 0) then
  begin
    Log('icacls: не удалось ограничить права на ' + Dir + ', код ' + IntToStr(ResultCode));
    Exit;
  end;
  // Вложенные файлы и папки — только унаследованные права
  Exec(ExpandConstant('{sys}\icacls.exe'), '"' + Dir + '\*" /reset /T /C /Q',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure ProtectDataDirs();
begin
  ProtectDir(ExpandConstant('{app}\data'));
  ProtectDir(ExpandConstant('{app}\backups'));
  ProtectDir(ExpandConstant('{app}\logs'));
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
    // Пользователь — до запуска службы: первая же страница
    // в браузере спросит уже существующий логин
    ProtectDataDirs();
    CreateWebUser();
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
    // При тихом удалении (/VERYSILENT /SUPPRESSMSGBOXES) — «Нет»:
    // данные сохраняются.
    if SuppressibleMsgBox(
      'Удалить данные программы?' + #13#10 + #13#10 +
      '  • data\     — устройства, SSH-ключи, настройки, пароль входа' + #13#10 +
      '                и ключ шифрования паролей (secret.key)' + #13#10 +
      '  • backups\  — все резервные копии и их история' + #13#10 +
      '  • logs\     — журналы' + #13#10 + #13#10 +
      'Если планируете переустановить или перенести программу —' + #13#10 +
      'ответьте «Нет»: данные останутся в папке программы.',
      mbConfirmation, MB_YESNO, IDNO) = IDYES
    then
    begin
      DelTree(ExpandConstant('{app}\data'),    True, True, True);
      DelTree(ExpandConstant('{app}\backups'), True, True, True);
      DelTree(ExpandConstant('{app}\logs'),    True, True, True);
      // Ключ шифрования прежних версий (до переноса в data\)
      DeleteFile(ExpandConstant('{app}\secret.key'));
      // Папка программы — если пуста (свои плагины пользователя
      // в plugins\ не удаляем)
      RemoveDir(ExpandConstant('{app}'));
    end;
  end;
end;