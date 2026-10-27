$ErrorActionPreference = 'Stop'
$setup = (Resolve-Path 'installer/CourseMaterialAssistant-Update.exe').Path
$install = Join-Path $env:RUNNER_TEMP 'course-update-install'
$data = Join-Path $env:RUNNER_TEMP 'course-update-data'
$env:COURSE_BRIDGE_DATA = $data
New-Item -ItemType Directory -Path $data -Force | Out-Null
$fixture = '{"automatic":false,"relay_url":"","projects":[]}'
[IO.File]::WriteAllText((Join-Path $data 'projects.json'), $fixture)
[IO.File]::WriteAllText((Join-Path $data 'pairing.json'), '{"token":"synthetic-update-test-only"}')
[IO.File]::WriteAllText((Join-Path $data 'downloaded-material.txt'), 'synthetic retained course attachment')
$before = @{}
Get-ChildItem $data -File | ForEach-Object { $before[$_.Name] = (Get-FileHash $_.FullName).Hash }
function Install-Update($language = 'chinesesimplified') {
    $process = Start-Process $setup -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/LANG=' + $language), ('/DIR="' + $install + '"')) -PassThru -Wait
    if ($process.ExitCode -ne 0) { throw "Update installer exit: $($process.ExitCode)" }
}
Install-Update
$exe = Join-Path $install 'CourseMaterialAssistant.exe'
$running = Start-Process $exe -ArgumentList '--minimized' -PassThru
$ready = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $info = Invoke-RestMethod -Uri 'http://127.0.0.1:47821/health' -Headers @{Authorization='Bearer synthetic-update-test-only'} -TimeoutSec 1
        if ($info.ok) { $ready = $true; break }
    } catch {}
}
if (-not $ready) { throw 'Installed assistant did not start' }
# A second instance must show the first and exit without stealing the local listener.
$duplicate = Start-Process $exe -ArgumentList '--minimized' -PassThru
if (-not $duplicate.WaitForExit(15000)) { throw 'A duplicate assistant remained running' }
if ($running.HasExited) { throw 'The original assistant exited during duplicate launch' }
# Reinstall while the GUI is running and minimizes on WM_CLOSE, just like v0.4.
Install-Update 'japanese'
if (-not $running.WaitForExit(15000)) { throw 'Update left the old assistant running' }
foreach ($name in $before.Keys) {
    if ((Get-FileHash (Join-Path $data $name)).Hash -ne $before[$name]) { throw "Update modified user data: $name" }
}
$check = Join-Path $env:RUNNER_TEMP 'updated-runtime.json'
$verified = Start-Process $exe -ArgumentList @('--self-check', $check) -PassThru -Wait
$result = Get-Content $check -Raw | ConvertFrom-Json
if ($verified.ExitCode -ne 0 -or $result.version -ne '0.7.0' -or $result.dpi.awareness -ne 2) { throw 'Updated executable failed runtime/DPI verification' }
Write-Output 'In-place update from Chinese to Japanese passed: running GUI replaced; settings, pairing and downloaded material preserved; updated DPI-aware executable starts.'
