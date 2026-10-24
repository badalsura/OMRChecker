# Packs the app build (dist\app\OMRChecker) the same way as the GitHub workflow:
#   OMRChecker-app-modern-win64.zip  Windows 10/11, the WebView2 runtime Windows keeps up to date
#   OMRChecker-webview2-109.zip      the WebView2 109 runtime alone (the installer's download)
#   OMRChecker-app-legacy-win64.zip  Windows 7/8.1, with WebView2 109 inside
# WebView2 109 is fetched once into build\webview2 and every file in it must
# carry Microsoft's signature (Google's for widevinecdm.dll).
# Called by packaging\build_windows.bat app|all, from the repository folder.
$ErrorActionPreference = "Stop"
$cab = "https://github.com/westinyang/WebView2RuntimeArchive/releases/download/109.0.1518.78/Microsoft.WebView2.FixedVersionRuntime.109.0.1518.78.x64.cab"
$app = "dist\app\OMRChecker"
$out = "dist\app"
if (!(Test-Path "$app\OMRChecker.exe")) { Write-Error "$app\OMRChecker.exe is missing: build the app first" }
if (Test-Path "$app\omr_data") { Remove-Item -Recurse -Force "$app\omr_data" }

if (!(Test-Path "build\webview2\msedgewebview2.exe")) {
    Write-Host "Fetching the WebView2 109 runtime"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    if (Test-Path build\webview2_x) { Remove-Item -Recurse -Force build\webview2_x }
    New-Item -ItemType Directory -Force -Path build\webview2_x | Out-Null
    Invoke-WebRequest -Uri $cab -OutFile build\webview2.cab -UseBasicParsing
    expand.exe build\webview2.cab -F:* build\webview2_x | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Error "expand failed" }
    $exe = Get-ChildItem build\webview2_x -Recurse -Filter msedgewebview2.exe | Select-Object -First 1
    if (-not $exe) { Write-Error "msedgewebview2.exe is not in the .cab" }
    $version = $exe.VersionInfo.ProductVersion
    if (-not $version.StartsWith("109.")) { Write-Error "Expected runtime 109, got $version" }
    $bad = Get-ChildItem $exe.Directory.FullName -Recurse -Include *.exe, *.dll | ForEach-Object {
        $sig = Get-AuthenticodeSignature $_.FullName
        $signer = if ($_.Name -eq "widevinecdm.dll") { "O=Google LLC" } else { "O=Microsoft Corporation" }
        if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch $signer) { "$($_.Name): $($sig.Status) $($sig.SignerCertificate.Subject)" }
    }
    if ($bad) { $bad | Write-Host; Write-Error "Files without a valid Microsoft signature" }
    if (Test-Path build\webview2) { Remove-Item -Recurse -Force build\webview2 }
    Move-Item $exe.Directory.FullName build\webview2
    Remove-Item -Recurse -Force build\webview2_x
    Remove-Item -Force build\webview2.cab
}

# 7-Zip when installed (faster, better for large folders), else Windows' own zip
$7z = @("$env:ProgramFiles\7-Zip\7z.exe", "${env:ProgramFiles(x86)}\7-Zip\7z.exe") | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
function Pack($source, $zip) {
    if (Test-Path $zip) { Remove-Item -Force $zip }
    Write-Host "Packing $zip"
    if ($7z) {
        & $7z a -tzip -mx=7 $zip $source | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Error "7-Zip failed on $zip" }
    } else {
        Compress-Archive -Path $source -DestinationPath $zip
    }
}

Pack ".\$app" "$out\OMRChecker-app-modern-win64.zip"
Pack ".\build\webview2\*" "$out\OMRChecker-webview2-109.zip"
Copy-Item -Recurse build\webview2 "$app\webview2"
try {
    Pack ".\$app" "$out\OMRChecker-app-legacy-win64.zip"
} finally {
    Remove-Item -Recurse -Force "$app\webview2"
}
Get-ChildItem "$out\*.zip" | ForEach-Object { Write-Host "$($_.Name): $([math]::Round($_.Length / 1MB)) MB" }
