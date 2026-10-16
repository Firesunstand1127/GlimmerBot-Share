$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = if ($env:MAASHANSHUO_PYTHON) { $env:MAASHANSHUO_PYTHON } else { (Get-Command python -ErrorAction Stop).Source }
$ExtraPaths = @($env:MAASHANSHUO_PACKAGER, $env:MAASHANSHUO_RUNTIME) | Where-Object { $_ }
if ($ExtraPaths) { $env:PYTHONPATH = $ExtraPaths -join [IO.Path]::PathSeparator }
if ($env:MAASHANSHUO_RUNTIME) {
    $env:PATH = "$env:MAASHANSHUO_RUNTIME;$(Join-Path $env:MAASHANSHUO_RUNTIME 'PySide6');$env:PATH"
}
$env:PYTHONIOENCODING = 'utf-8'
$Version = & $Python -c "import runpy; print(runpy.run_path(r'$PSScriptRoot/version.py')['APP_VERSION'])"
if ($LASTEXITCODE -ne 0) { throw 'Cannot read application version' }
$Dist = Join-Path $ProjectRoot "release-v$Version"
& $Python -m PyInstaller --noconfirm --clean --windowed --name MaaShanShuoShare `
    --distpath $Dist --workpath (Join-Path $ProjectRoot 'work/pyinstaller') `
    --specpath (Join-Path $ProjectRoot 'work/spec') `
    --collect-all maa --collect-all MaaAgentBinary --collect-all strenum `
    (Join-Path $PSScriptRoot 'app.py')
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed: $LASTEXITCODE" }
$AppDir = Join-Path $Dist 'MaaShanShuoShare'
$ConflictingIcu = Join-Path $AppDir '_internal/icuuc.dll'
if (Test-Path -LiteralPath $ConflictingIcu) { Remove-Item -LiteralPath $ConflictingIcu }
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'assets/resource') -Destination (Join-Path $AppDir 'resource') -Recurse -Force
foreach ($File in @('assets/interface.json', 'README.md', 'LICENSE', 'requirements-runtime.txt', 'ui/USER_GUIDE.txt', 'docs/RELEASE_NOTES.md')) {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot $File) -Destination $AppDir -Force
}
# Package origin metadata may contain build-machine paths; it is not runtime data.
$InternalDir = (Resolve-Path -LiteralPath (Join-Path $AppDir '_internal')).Path
$MaaVersion = (Get-Content -LiteralPath (Join-Path $ProjectRoot 'requirements-runtime.txt') | Where-Object { $_ -match '^maafw==' }) -replace '^maafw==', ''
if ($env:MAASHANSHUO_RUNTIME) {
    $MaaMetadata = Join-Path $env:MAASHANSHUO_RUNTIME "maafw-$MaaVersion.dist-info"
    if (Test-Path -LiteralPath $MaaMetadata) {
        foreach ($OldMetadata in Get-ChildItem -LiteralPath $InternalDir -Directory -Filter 'maafw-*.dist-info') {
            $ResolvedMetadata = (Resolve-Path -LiteralPath $OldMetadata.FullName).Path
            if ((Split-Path -Parent $ResolvedMetadata) -ne $InternalDir) { throw 'Unexpected metadata path' }
            Remove-Item -LiteralPath $ResolvedMetadata -Recurse -Force
        }
        Copy-Item -LiteralPath $MaaMetadata -Destination $InternalDir -Recurse
    }
}
Get-ChildItem -LiteralPath (Join-Path $AppDir '_internal') -Filter direct_url.json -Recurse -File | Remove-Item
Write-Host "Built: $(Join-Path $AppDir 'MaaShanShuoShare.exe')"
