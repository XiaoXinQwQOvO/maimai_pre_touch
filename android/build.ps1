# Build the maimai_pre_touch phone APK without Gradle.
#
# Needs the toolchain from tools/setup_android_toolchain.ps1:
#   .tools/jdk17            (Temurin JDK 17)
#   .tools/android-sdk      (build-tools;34.0.0, platforms;android-34)
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File android\build.ps1
#   powershell -ExecutionPolicy Bypass -File android\build.ps1 -Install   # adb install -r

param(
    [string]$OutputName = 'maimai_touchpanel.apk',
    [switch]$Install
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here

# aapt2 (AOSP base) cannot open directories whose path contains non-ASCII
# characters on Windows, so build through an ASCII-only junction when needed.
if ($root -match '[^\x00-\x7F]') {
    $asciiLink = Join-Path $env:LOCALAPPDATA 'maimai_pre_touch_ascii'
    $needsCreate = $true
    if (Test-Path $asciiLink) {
        $item = Get-Item $asciiLink -Force
        if ($item.LinkType -eq 'Junction' -and ($item.Target -contains $root -or $item.Target -eq $root)) {
            $needsCreate = $false
        } else {
            Remove-Item $asciiLink -Recurse -Force
        }
    }
    if ($needsCreate) {
        New-Item -ItemType Junction -Path $asciiLink -Target $root | Out-Null
    }
    Write-Host "[i] non-ASCII repo path, building through $asciiLink" -ForegroundColor DarkGray
    $root = $asciiLink
    $here = Join-Path $root 'android'
}

$tools = Join-Path $root '.tools'
$jdk = Join-Path $tools 'jdk17'
$sdk = Join-Path $tools 'android-sdk'
$buildToolsVersion = '34.0.0'
$buildTools = Join-Path $sdk "build-tools\$buildToolsVersion"
$platform = 'android-34'
$androidJar = Join-Path $sdk "platforms\$platform\android.jar"

$aapt2 = Join-Path $buildTools 'aapt2.exe'
$zipalign = Join-Path $buildTools 'zipalign.exe'
$apksigner = Join-Path $buildTools 'apksigner.bat'
$d8 = Join-Path $buildTools 'd8.bat'
$javac = Join-Path $jdk 'bin\javac.exe'
$keytool = Join-Path $jdk 'bin\keytool.exe'

foreach ($tool in @($aapt2, $zipalign, $apksigner, $d8, $javac, $keytool, $androidJar)) {
    if (-not (Test-Path $tool)) {
        throw "missing tool: $tool`nrun tools/setup_android_toolchain.ps1 first"
    }
}

$env:JAVA_HOME = $jdk
$env:PATH = "$jdk\bin;$buildTools;$env:PATH"

$src = Join-Path $here 'app'
$build = Join-Path $here 'build'
$genDir = Join-Path $build 'gen'
$classDir = Join-Path $build 'classes'
$dexDir = Join-Path $build 'dex'
$resZip = Join-Path $build 'resources.zip'
$baseApk = Join-Path $build 'base.apk'
$unsignedApk = Join-Path $build 'unsigned.apk'
$alignedApk = Join-Path $build 'aligned.apk'
$finalApk = Join-Path $here $OutputName
$keystore = Join-Path $here 'debug.keystore'

if (Test-Path $build) { Remove-Item -Recurse -Force $build }
New-Item -ItemType Directory -Force -Path $build, $genDir, $classDir, $dexDir | Out-Null

Write-Host '[1/7] compiling resources' -ForegroundColor Cyan
& $aapt2 compile --dir (Join-Path $src 'res') -o $resZip
if ($LASTEXITCODE -ne 0) { throw 'aapt2 compile failed' }

Write-Host '[2/7] linking resources' -ForegroundColor Cyan
& $aapt2 link -o $baseApk -I $androidJar --manifest (Join-Path $src 'AndroidManifest.xml') `
    -R $resZip --java $genDir --min-sdk-version 26 --target-sdk-version 34 --auto-add-overlay
if ($LASTEXITCODE -ne 0) { throw 'aapt2 link failed' }

Write-Host '[3/7] compiling java' -ForegroundColor Cyan
$javaFiles = Get-ChildItem -Path (Join-Path $src 'java'), $genDir -Recurse -Filter *.java | ForEach-Object { $_.FullName }
& $javac -encoding UTF-8 --release 8 -nowarn -d $classDir -cp $androidJar $javaFiles
if ($LASTEXITCODE -ne 0) { throw 'javac failed' }

Write-Host '[4/7] dexing' -ForegroundColor Cyan
$classFiles = Get-ChildItem -Path $classDir -Recurse -Filter *.class | ForEach-Object { $_.FullName }
& $d8 --lib $androidJar --min-api 26 --output $dexDir @classFiles
if ($LASTEXITCODE -ne 0) { throw 'd8 failed' }

Write-Host '[5/7] packaging' -ForegroundColor Cyan
Copy-Item $baseApk $unsignedApk -Force
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open($unsignedApk, 'Update')
try {
    $existing = $zip.GetEntry('classes.dex')
    if ($existing) { $existing.Delete() }
    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
        $zip, (Join-Path $dexDir 'classes.dex'), 'classes.dex', 'Optimal') | Out-Null
} finally {
    $zip.Dispose()
}

Write-Host '[6/7] aligning' -ForegroundColor Cyan
& $zipalign -f -p 4 $unsignedApk $alignedApk
if ($LASTEXITCODE -ne 0) { throw 'zipalign failed' }

Write-Host '[7/7] signing' -ForegroundColor Cyan
if (-not (Test-Path $keystore)) {
    & $keytool -genkeypair -keystore $keystore -alias androiddebugkey -storepass android -keypass android `
        -keyalg RSA -keysize 2048 -validity 10000 -dname 'CN=Android Debug,O=Android,C=US'
    if ($LASTEXITCODE -ne 0) { throw 'keytool failed' }
}
& $apksigner sign --ks $keystore --ks-pass pass:android --key-pass pass:android `
    --ks-key-alias androiddebugkey --out $finalApk $alignedApk
if ($LASTEXITCODE -ne 0) { throw 'apksigner failed' }

& $apksigner verify --print-certs $finalApk | Out-String -Stream | Select-Object -First 3 | ForEach-Object { Write-Host $_ }

$size = [math]::Round((Get-Item $finalApk).Length / 1KB, 1)
Write-Host "`nAPK ready: $finalApk ($size KB)" -ForegroundColor Green

if ($Install) {
    $adb = Join-Path $tools 'android-sdk\platform-tools\adb.exe'
    if (-not (Test-Path $adb)) { $adb = 'adb' }
    & $adb install -r $finalApk
}

exit 0
