Write-Host "=== DeepTrace Local Setup (Windows) ===" -ForegroundColor Cyan

# Check Python
if (!(Get-Command "python" -ErrorAction SilentlyContinue)) {
    Write-Host "X Error: Python 3 is required." -ForegroundColor Red
    exit 1
}
Write-Host "v Python found." -ForegroundColor Green

# Create virtual environment
Write-Host "Creating virtual environment in backend\venv..."
python -m venv backend\venv

# Install requirements
Write-Host "Installing Python dependencies..."
& backend\venv\Scripts\python -m pip install -r backend\requirements.txt

# Check Java
if (!(Get-Command "javac" -ErrorAction SilentlyContinue)) {
    Write-Host "! Warning: 'javac' not found. Java profiling will be disabled." -ForegroundColor Yellow
} else {
    Write-Host "v Java compiler found." -ForegroundColor Green
}

# Note about strace
Write-Host "i Note: strace is a Linux utility. On Windows, system calls will be estimated using psutil." -ForegroundColor Gray

# Create jobs dir
$jobsDir = "$env:TEMP\deeptrace_jobs"
if (!(Test-Path $jobsDir)) {
    New-Item -ItemType Directory -Force -Path $jobsDir | Out-Null
}
Write-Host "v Created $jobsDir for temporary profiling files." -ForegroundColor Green

Write-Host "`n=== Setup Complete ===" -ForegroundColor Cyan
Write-Host "To run DeepTrace locally:`n"
Write-Host "1. Start the Backend:"
Write-Host "   cd backend"
Write-Host "   .\venv\Scripts\activate"
Write-Host "   uvicorn main:app --reload`n"
Write-Host "2. Start the Frontend (in a new terminal):"
Write-Host "   cd frontend"
Write-Host "   python -m http.server 3000`n"
Write-Host "Then open http://localhost:3000 in your browser."
