$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)
Write-Host "Starting Cadence locally on http://127.0.0.1:8002/"
Write-Host "Keep this PowerShell window open while testing."

$env:PYTHON_DOTENV_DISABLED = "1"
Get-Content .env | ForEach-Object {
    if ($_ -match "^\s*([^#][^=]+)=(.*)$") {
        $name = $Matches[1].Trim()
        $value = $Matches[2]
        if ($name -ne "DATABASE_URL") {
            Set-Item -Path "Env:$name" -Value $value
        }
    }
}
Remove-Item Env:\DATABASE_URL -ErrorAction SilentlyContinue

& .\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8002 --noreload
