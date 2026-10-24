# Downloads the Android build toolchain into <repo>\.tools (git-ignored).
#
#   powershell -ExecutionPolicy Bypass -File tools\setup_android_toolchain.ps1
#
# Installs: Temurin JDK 17 + Android cmdline-tools + platform-tools,
#           platforms;android-34, build-tools;34.0.0
#
# NOTE: this file must stay ASCII-only. Windows PowerShell 5.1 decodes .ps1 files
# without a BOM using the system ANSI code page, so any non-ASCII literal (for
# example a CJK path) would be silently mangled.

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$root = Join-Path (Split-Path -Parent $PSScriptRoot) '.tools'
New-Item -ItemType Directory -Force -Path $root | Out-Null
Write-Host "[i] tools dir: $root"

function Get-File($url, $dest) {
    if (Test-Path $dest) { Write-Host "[=] exists: $dest"; return }
    Write-Host "[>] download $url"
    $sw = [Diagnostics.Stopwatch]::StartNew()
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -TimeoutSec 1800
    $sw.Stop()
    Write-Host ("[+] done {0:n1}s  {1:n1} MB" -f $sw.Elapsed.TotalSeconds, ((Get-Item $dest).Length / 1MB))
}

# 1) JDK 17 (Temurin)
$jdkZip = Join-Path $root 'jdk17.zip'
Get-File 'https://api.adoptium.net/v3/binary/latest/17/ga/windows/x64/jdk/hotspot/normal/eclipse' $jdkZip
$jdkDir = Join-Path $root 'jdk17'
if (-not (Test-Path (Join-Path $jdkDir 'bin\javac.exe'))) {
    Expand-Archive -Path $jdkZip -DestinationPath $root -Force
    $inner = Get-ChildItem $root -Directory | Where-Object { $_.Name -like 'jdk-17*' } | Select-Object -First 1
    if ($inner) { Move-Item $inner.FullName $jdkDir -Force }
}
Write-Host "[i] JAVA_HOME: $jdkDir"
& (Join-Path $jdkDir 'bin\java.exe') -version 2>&1 | Out-String | Write-Host

# 2) Android command line tools
$cmdZip = Join-Path $root 'cmdline-tools.zip'
Get-File 'https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip' $cmdZip
$sdk = Join-Path $root 'android-sdk'
if (-not (Test-Path (Join-Path $sdk 'cmdline-tools\latest\bin\sdkmanager.bat'))) {
    $tmp = Join-Path $root 'cmdline-tmp'
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    Expand-Archive -Path $cmdZip -DestinationPath $tmp -Force
    New-Item -ItemType Directory -Force -Path (Join-Path $sdk 'cmdline-tools') | Out-Null
    Move-Item (Join-Path $tmp 'cmdline-tools') (Join-Path $sdk 'cmdline-tools\latest') -Force
    Remove-Item $tmp -Recurse -Force
}
Write-Host "[i] sdk dir: $sdk"

$env:JAVA_HOME = $jdkDir
$env:ANDROID_SDK_ROOT = $sdk
$env:ANDROID_HOME = $sdk
$sdkmanager = Join-Path $sdk 'cmdline-tools\latest\bin\sdkmanager.bat'

$y = ("y`n" * 60)
$y | & $sdkmanager --licenses 2>&1 | Select-Object -Last 3

Write-Host "[>] installing platform-tools, platform, build-tools"
& $sdkmanager --install 'platform-tools' 'platforms;android-34' 'build-tools;34.0.0' 2>&1 | Select-Object -Last 6

Write-Host "=== verify ==="
Get-ChildItem (Join-Path $sdk 'build-tools') -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name
Get-ChildItem (Join-Path $sdk 'platforms') -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name

$checks = @(
    (Join-Path $jdkDir 'bin\javac.exe'),
    (Join-Path $sdk 'build-tools\34.0.0\aapt2.exe'),
    (Join-Path $sdk 'build-tools\34.0.0\d8.bat'),
    (Join-Path $sdk 'build-tools\34.0.0\apksigner.bat'),
    (Join-Path $sdk 'build-tools\34.0.0\zipalign.exe'),
    (Join-Path $sdk 'platforms\android-34\android.jar'),
    (Join-Path $sdk 'platform-tools\adb.exe')
)
$missing = $checks | Where-Object { -not (Test-Path $_) }
if ($missing) {
    Write-Host "[!] missing:" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "    $_" }
    exit 1
}
Write-Host "[+] toolchain ready" -ForegroundColor Green
