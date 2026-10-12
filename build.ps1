$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (!(Test-Path .venv/Scripts/python.exe)) {
    python -m venv .venv
    if ($LASTEXITCODE) { throw 'Environment creation failed' }
}
& .venv/Scripts/python.exe -m ensurepip --upgrade
if ($LASTEXITCODE) { throw 'pip setup failed' }
& .venv/Scripts/python.exe -m pip install -r requirements-build.txt
if ($LASTEXITCODE) { throw 'Dependency installation failed' }
& .venv/Scripts/python.exe -m playwright install chromium
if ($LASTEXITCODE) { throw 'Browser installation failed' }
& .venv/Scripts/python.exe -m unittest -v test_app
if ($LASTEXITCODE) { throw 'Tests failed' }
& .venv/Scripts/python.exe -m PyInstaller --noconfirm --onefile --windowed --name AutoMooch --distpath releases --workpath build/pyinstaller --specpath build --collect-all playwright app.py
if ($LASTEXITCODE) { throw 'Executable build failed' }
$digest = (Get-FileHash releases/AutoMooch.exe -Algorithm SHA256).Hash.ToLowerInvariant()
"$digest  AutoMooch.exe" | Set-Content -Encoding ascii releases/SHA256SUMS.txt
