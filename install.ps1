# Установка Огонька на Windows 10 и 11. Два способа:
#  1) скачали архив: распакуйте и дважды щёлкните «Установить.cmd»;
#  2) одной строкой в PowerShell (Win+R, powershell):
#     $f="$env:TEMP\ogonyok.ps1"; irm https://raw.githubusercontent.com/Muzharov-D/brief/main/install.ps1 -OutFile $f; powershell -ExecutionPolicy Bypass -File $f
# Повторный запуск безопасен: готовое пропускается, ваши настройки и файлы не трогаются.
# Continue, а не Stop: в PowerShell 5 любой вывод программы в stderr иначе обрывает установку.
# Ошибки проверяем сами: $LASTEXITCODE после программ, try и -ErrorAction Stop у загрузок.
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$Repo = "Muzharov-D/brief"
$Dest = Join-Path $HOME "Огонёк"
$HomeB = if ($env:BRIEF_HOME) { $env:BRIEF_HOME } else { Join-Path $HOME ".brief" }

function Step($t) { Write-Host ""; Write-Host $t -ForegroundColor Cyan }
function Fail($t) {
    Write-Host ""; Write-Host "Остановлено: $t" -ForegroundColor Red
    Write-Host "Закройте окно и запустите установку ещё раз. Если повторится, пришлите этот текст тому, кто дал вам Огонька."
    Read-Host "Enter, чтобы закрыть" | Out-Null; exit 1
}

Write-Host "Огонёк: личный помощник на этом компьютере."
Write-Host "Сейчас поставлю нужные программы (5-15 минут, один раз), потом откроется страница настройки."
if ([Environment]::OSVersion.Version.Major -lt 10) { Fail "нужна Windows 10 или 11." }

# Код Огонька живёт в домашней папке: туда же кладёт обновления `brief update`
$Root = if ($PSCommandPath) { Split-Path -Parent $PSCommandPath } else { "" }
if (-not $Root -or -not (Test-Path (Join-Path $Root "core\brief.py"))) {
    Step "Скачиваю Огонька с GitHub"
    $tmp = Join-Path $env:TEMP ("ogonyok-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    try {
        Invoke-WebRequest "https://codeload.github.com/$Repo/zip/refs/heads/main" -OutFile "$tmp\brief.zip" -UseBasicParsing -ErrorAction Stop
        Expand-Archive "$tmp\brief.zip" -DestinationPath $tmp -Force -ErrorAction Stop
    } catch { Fail "не скачался архив Огонька. Проверьте интернет." }
    $Root = (Get-ChildItem $tmp -Directory | Select-Object -First 1).FullName
}
if ((Resolve-Path $Root).Path.TrimEnd("\") -ne $Dest.TrimEnd("\")) {
    Step "Переношу Огонька в $Dest"
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    Copy-Item -Path (Join-Path $Root "*") -Destination $Dest -Recurse -Force
    Get-ChildItem $Dest -Recurse | Unblock-File -ErrorAction SilentlyContinue
    $Root = $Dest
}

Step "1/4 Python и библиотеки (Telegram, Outlook, запись и расшифровка голоса)"
New-Item -ItemType Directory -Force -Path (Join-Path $HomeB "bin") | Out-Null
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $HOME ".local\bin\uv.exe" }
if (-not (Test-Path $uv)) {
    $env:UV_NO_MODIFY_PATH = "1"
    try { Invoke-RestMethod https://astral.sh/uv/install.ps1 -ErrorAction Stop | Invoke-Expression } catch { Fail "не скачался установщик Python (uv). Проверьте интернет." }
    $uv = Join-Path $HOME ".local\bin\uv.exe"
}
$Venv = Join-Path $HomeB "venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$Pyw = Join-Path $Venv "Scripts\pythonw.exe"
if (-not (Test-Path $Py)) { & $uv venv -q --python 3.12 $Venv; if ($LASTEXITCODE) { Fail "не создалось окружение Python" } }
if (-not (Test-Path $Pyw)) { $Pyw = $Py }  # без pythonw ярлыки откроют ещё и чёрное окно, но работать будут
& $uv pip install -q --python $Py telethon qrcode faster-whisper pywin32 soundcard "numpy<2"
if ($LASTEXITCODE) { Fail "не поставились библиотеки" }

Step "2/4 Модель распознавания речи (около 0,5 ГБ, работает без интернета)"
& $Py -X utf8 -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')" 2>$null
if ($LASTEXITCODE) { Write-Host "   Не скачалась, это не страшно: голосовые заработают, когда интернет будет стабильнее." }

Step "3/4 Команда brief, ярлыки на рабочем столе и в меню Пуск"
$BriefPy = Join-Path $Root "core\brief.py"
# brief.cmd для окна cmd и PowerShell: кодировка консоли, иначе путь с кириллицей не прочитается
$oem = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage)
[IO.File]::WriteAllText((Join-Path $HomeB "bin\brief.cmd"), "@`"$Py`" -I `"$BriefPy`" %*`r`n", $oem)
# brief без расширения для Git Bash: через него Claude Code вызывает `brief run`
function Posix($p) { $p = $p -replace "\\", "/"; if ($p -match "^([A-Za-z]):/(.*)$") { "/" + $Matches[1].ToLower() + "/" + $Matches[2] } else { $p } }
$sh = "#!/bin/sh`nexec `"$(Posix $Py)`" -I `"$(Posix $BriefPy)`" `"`$@`"`n"
[IO.File]::WriteAllText((Join-Path $HomeB "bin\brief"), $sh, (New-Object Text.UTF8Encoding $false))
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$binDir = Join-Path $HomeB "bin"
if (-not (($userPath -split ";") -contains $binDir)) {
    [Environment]::SetEnvironmentVariable("Path", (($userPath.TrimEnd(";") + ";" + $binDir).TrimStart(";")), "User")
}
$env:Path = "$binDir;$env:Path"

$shell = New-Object -ComObject WScript.Shell
$icon = Join-Path $Root "core\ogonyok.ico"
$places = @([Environment]::GetFolderPath("Desktop"), (Join-Path ([Environment]::GetFolderPath("Programs")) "Огонёк"))
foreach ($dir in $places) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $l = $shell.CreateShortcut((Join-Path $dir "Огонёк.lnk"))
    $l.TargetPath = $Pyw; $l.Arguments = "-I `"$BriefPy`" setup"; $l.WorkingDirectory = $Root
    $l.IconLocation = "$icon,0"; $l.Description = "Огонёк: настройка, лента и расписание"; $l.Save()
    $l = $shell.CreateShortcut((Join-Path $dir "Запись созвона.lnk"))
    $l.TargetPath = $Pyw; $l.Arguments = "-I `"$(Join-Path $Root 'connectors\calls-windows\record.pyw')`""
    $l.WorkingDirectory = $Root; $l.IconLocation = "$icon,0"; $l.Description = "Огонёк: записать созвон для протокола"; $l.Save()
}

Step "4/4 Открываю страницу настройки в браузере"
Write-Host "   Дальше всё на странице: Огонёк спросит по шагам и объяснит, что и зачем."
Write-Host "   Потом настройку можно открыть ярлыком «Огонёк» на рабочем столе."
Start-Process -FilePath $Pyw -ArgumentList @("-I", "`"$BriefPy`"", "setup") -WorkingDirectory $Root
Start-Sleep -Seconds 3
