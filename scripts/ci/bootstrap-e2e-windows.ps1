$ErrorActionPreference = 'Stop'

$WhisperCppUrl = 'https://github.com/ggml-org/whisper.cpp.git'
$WhisperCppRevision = '0c7be696371069e0baa2515ed7aa08d5a5473aa6'
$RuntimeRoot = $env:SPEECHSCOPE_WHISPERCPP_CACHE
if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) {
    throw 'SPEECHSCOPE_WHISPERCPP_CACHE must point to the hosted runtime cache.'
}
$SourceDir = Join-Path $RuntimeRoot 'whisper.cpp'
$BuildDir = Join-Path $SourceDir 'build'

$missingPackages = @()
foreach ($commandName in @('git', 'cmake', 'ffmpeg', 'espeak-ng')) {
    if (-not (Get-Command $commandName -ErrorAction SilentlyContinue)) {
        $missingPackages += $commandName
    }
}
if ($missingPackages.Count -gt 0) {
    if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
        throw "Missing E2E prerequisites ($($missingPackages -join ', ')) and Chocolatey is unavailable."
    }
    & choco install --yes --no-progress @missingPackages
    if ($LASTEXITCODE -ne 0) {
        throw "Chocolatey failed to install E2E prerequisites (exit $LASTEXITCODE)."
    }
    $chocolateyProfile = Join-Path $env:ChocolateyInstall 'helpers\chocolateyProfile.psm1'
    if (Test-Path $chocolateyProfile) {
        Import-Module $chocolateyProfile -ErrorAction SilentlyContinue
        if (Get-Command refreshenv -ErrorAction SilentlyContinue) {
            refreshenv
        }
    }
}
foreach ($commandName in @('git', 'cmake', 'ffmpeg', 'espeak-ng')) {
    if (-not (Get-Command $commandName -ErrorAction SilentlyContinue)) {
        throw "Required Windows E2E prerequisite is missing: $commandName"
    }
}

New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
$CachedRevision = ''
if (Test-Path (Join-Path $SourceDir '.git')) {
    $CachedRevision = (& git -C $SourceDir rev-parse HEAD 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        $CachedRevision = ''
    }
}
$WhisperCli = $null
if (Test-Path $BuildDir) {
    $WhisperCli = Get-ChildItem -Path $BuildDir -Filter 'whisper-cli.exe' -File -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
}

if ($CachedRevision -ne $WhisperCppRevision -or -not $WhisperCli) {
    if (Test-Path $SourceDir) {
        Remove-Item -LiteralPath $SourceDir -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $SourceDir | Out-Null
    & git -C $SourceDir init
    if ($LASTEXITCODE -ne 0) { throw "git init failed (exit $LASTEXITCODE)." }
    & git -C $SourceDir remote add origin $WhisperCppUrl
    if ($LASTEXITCODE -ne 0) { throw "git remote add failed (exit $LASTEXITCODE)." }
    & git -C $SourceDir fetch --depth 1 origin $WhisperCppRevision
    if ($LASTEXITCODE -ne 0) { throw "git fetch failed (exit $LASTEXITCODE)." }
    & git -C $SourceDir checkout --detach FETCH_HEAD
    if ($LASTEXITCODE -ne 0) { throw "git checkout failed (exit $LASTEXITCODE)." }

    $CheckedOutRevision = (& git -C $SourceDir rev-parse HEAD | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $CheckedOutRevision -ne $WhisperCppRevision) {
        throw "whisper.cpp revision mismatch: expected $WhisperCppRevision, got $CheckedOutRevision."
    }
    & cmake -S $SourceDir -B $BuildDir -DCMAKE_BUILD_TYPE=Release -DWHISPER_BUILD_TESTS=OFF
    if ($LASTEXITCODE -ne 0) { throw "CMake configure failed (exit $LASTEXITCODE)." }
    & cmake --build $BuildDir --config Release --target whisper-cli --parallel 2
    if ($LASTEXITCODE -ne 0) { throw "CMake build failed (exit $LASTEXITCODE)." }

    $WhisperCli = Get-ChildItem -Path $BuildDir -Filter 'whisper-cli.exe' -File -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $WhisperCli -or -not (Test-Path $WhisperCli -PathType Leaf)) {
    throw "Could not discover whisper-cli.exe in $BuildDir."
}
& $WhisperCli --help *> $null
if ($LASTEXITCODE -ne 0) {
    throw "whisper-cli failed its --help check (exit $LASTEXITCODE)."
}

if ($env:GITHUB_ENV) {
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::AppendAllText($env:GITHUB_ENV, "SPEECHSCOPE_WHISPERCPP_BIN=$WhisperCli`n", $utf8)
}
if ($env:GITHUB_PATH) {
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::AppendAllText($env:GITHUB_PATH, "$(Split-Path -Parent $WhisperCli)`n", $utf8)
}
Write-Host "Built whisper.cpp $WhisperCppRevision at $WhisperCli"
