# Copy production into the local Docker database (one way: production is only read). See pull_prod_to_local.py.
$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

$dockerBin = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin"
if ((Test-Path $dockerBin) -and ($env:Path -notlike "*$dockerBin*")) {
    $env:Path = "$dockerBin;$env:Path"
}

& ".\venv\Scripts\python.exe" "scripts\pull_prod_to_local.py"
exit $LASTEXITCODE
