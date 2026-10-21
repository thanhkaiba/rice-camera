# Camera attendance: install or update on this Windows computer. Started by Install.bat.
#
# Installs into %LOCALAPPDATA%\FaceAttendance with its own Python (no admin rights needed),
# downloads the models, starts the program now and at every login, and opens the Setup page.
# Running it again from a newer copy updates the program; settings, records and photos are kept.
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Src = Split-Path -Parent $PSScriptRoot
$AppHome = if ($env:FACE_ATTENDANCE_HOME) { $env:FACE_ATTENDANCE_HOME } else { Join-Path $env:LOCALAPPDATA "FaceAttendance" }
$Port = if ($env:FACE_ATTENDANCE_PORT) { $env:FACE_ATTENDANCE_PORT } else { "8000" }
$Tools = Join-Path $AppHome "tools"
$Venv = Join-Path $AppHome "venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$Config = Join-Path $AppHome "config.yaml"
$Models = Join-Path $AppHome "models"

function Step([string]$Text) { Write-Host ""; Write-Host "== $Text" -ForegroundColor Cyan }
function Invoke-Checked([string]$Exe, [string[]]$ArgList) {
    & $Exe @ArgList
    if ($LASTEXITCODE -ne 0) { throw "$([IO.Path]::GetFileName($Exe)) failed (exit code $LASTEXITCODE)" }
}
function Test-Models([string[]]$Names) {
    foreach ($n in $Names) { if (-not (Test-Path (Join-Path $Models $n))) { return $false } }
    return $true
}

try {
    New-Item -ItemType Directory -Force (Join-Path $AppHome "data") | Out-Null
    Set-Location $AppHome
    Write-Host "Camera attendance: installing from $Src"
    Write-Host "into $AppHome"

    # A running copy keeps its files open, and Windows would refuse to replace them.
    Get-CimInstance Win32_Process |
        Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine.Contains($AppHome) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    Step "1/6 Python"
    $Uv = Join-Path $Tools "uv.exe"
    if (-not (Test-Path $Uv) -and -not (Test-Path (Join-Path $Tools "bin\uv.exe"))) {
        Write-Host "Downloading uv (it installs Python just for this program)..."
        $env:UV_INSTALL_DIR = $Tools
        $env:UV_NO_MODIFY_PATH = "1"
        $env:INSTALLER_NO_MODIFY_PATH = "1"
        Invoke-Checked "powershell.exe" @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
            "irm https://astral.sh/uv/install.ps1 | iex")
    }
    if (-not (Test-Path $Uv)) { $Uv = Join-Path $Tools "bin\uv.exe" }
    if (-not (Test-Path $Uv)) { throw "could not install uv" }
    if (-not (Test-Path $Py)) { Invoke-Checked $Uv @("venv", "--python", "3.12", $Venv) }

    Step "2/6 Program and libraries"
    Invoke-Checked $Py @((Join-Path $Src "attendance\cli\launcher.py"), "--install", $Uv, $Src)

    Step "3/6 Settings file"
    if (Test-Path $Config) {
        Write-Host "Keeping the existing config.yaml"
    } else {
        $Ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
               Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
               Select-Object -First 1).IPAddress
        if (-not $Ip) { $Ip = "localhost" }
        $Zones = @{ "SE Asia Standard Time" = "Asia/Ho_Chi_Minh"; "Singapore Standard Time" = "Asia/Singapore";
                    "China Standard Time" = "Asia/Shanghai"; "Tokyo Standard Time" = "Asia/Tokyo";
                    "Korea Standard Time" = "Asia/Seoul"; "UTC" = "UTC" }
        $Tz = $Zones[[TimeZoneInfo]::Local.Id]
        if (-not $Tz) { $Tz = "Asia/Ho_Chi_Minh" }
        $Text = @"
# Created by the installer. Most settings are on the admin page (Settings); see config.example.yaml
# in the program folder for everything else. Restart the program after editing this file.
timezone: $Tz
data_dir: data
employees_file: employees.csv
recognition:
  backend: insightface   # or opencv (free for commercial use, less accurate); both are downloaded
web:
  port: $Port
  public_url: "http://${Ip}:$Port"
"@
        [IO.File]::WriteAllText($Config, $Text)  # UTF-8 without a byte order mark
        Write-Host "Created config.yaml (time zone $Tz)"
    }

    Step "4/6 Recognition models (about 300 MB the first time)"
    if (-not (Test-Models @("det_10g.onnx", "w600k_r50.onnx"))) {
        Invoke-Checked $Py @("-m", "attendance", "--config", $Config, "download-models", "--backend", "insightface")
    }
    if (-not (Test-Models @("face_detection_yunet_2023mar.onnx", "face_recognition_sface_2021dec.onnx"))) {
        Invoke-Checked $Py @("-m", "attendance", "--config", $Config, "download-models", "--backend", "opencv")
    }
    if (-not (Test-Models @("yolox_tiny.onnx"))) {
        Invoke-Checked $Py @("-m", "attendance", "--config", $Config, "download-models", "--person")
    }
    if (-not (Test-Models @("u2netp.onnx"))) {
        Invoke-Checked $Py @("-m", "attendance", "--config", $Config, "download-models", "--cutout")
    }
    Write-Host "Models are in $Models"

    Step "5/6 Start now and at every login"
    $Runner = Join-Path $AppHome "run.ps1"
    $RunnerText = @"
# Started at login by the shortcut in the Startup folder; restarts the program if it stops.
while (`$true) {
    Start-Process -FilePath "$Py" -ArgumentList '-m', 'attendance', '--config', '"$Config"', 'run' ``
        -WorkingDirectory "$AppHome" -NoNewWindow -Wait -RedirectStandardError "$AppHome\data\service.log"
    Start-Sleep -Seconds 10
}
"@
    [IO.File]::WriteAllText($Runner, $RunnerText)
    $PowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $RunArgs = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`""
    $Shell = New-Object -ComObject WScript.Shell
    $Link = $Shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Startup")) "Camera attendance.lnk"))
    $Link.TargetPath = $PowerShell
    $Link.Arguments = $RunArgs
    $Link.WorkingDirectory = $AppHome
    $Link.WindowStyle = 7
    $Link.Save()
    Start-Process -FilePath $PowerShell -ArgumentList $RunArgs -WindowStyle Hidden
    try { powercfg /change standby-timeout-ac 0 | Out-Null; Write-Host "Sleep on mains power turned off." }
    catch { Write-Host "Turn off sleep yourself: Settings > System > Power > Sleep: Never." }
    Write-Host "Started. It also starts by itself after every login."

    Step "6/6 Opening the admin page"
    [IO.File]::WriteAllText((Join-Path ([Environment]::GetFolderPath("Desktop")) "Camera attendance.url"),
        "[InternetShortcut]`r`nURL=http://localhost:$Port/`r`n")
    for ($i = 0; $i -lt 90; $i++) {
        try { $c = New-Object Net.Sockets.TcpClient("localhost", [int]$Port); $c.Close(); break }
        catch { Start-Sleep -Seconds 1 }
    }
    Start-Process "http://localhost:$Port/setup"

    Write-Host ""
    Write-Host "Done. The admin page is http://localhost:$Port (shortcut on the Desktop)." -ForegroundColor Green
    Write-Host "From other computers: see 'Address of this page' on the Setup page."
    Write-Host "If Windows Firewall asks about Python, allow it on private networks."
    Write-Host "Program log: $AppHome\data\attendance.log"
} catch {
    Write-Host ""
    Write-Host "Installation failed: $_" -ForegroundColor Red
    Write-Host "Check the internet connection and run Install.bat again."
    exit 1
}
