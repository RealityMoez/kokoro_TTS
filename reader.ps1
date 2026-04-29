$ErrorActionPreference = "Stop"

if (!(Test-Path ".\\.venv\\Scripts\\python.exe")) {
  Write-Error "Virtual env not found. Run setup.ps1 first."
  exit 1
}

& .\.venv\Scripts\python -u .\reader.py
