$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location -LiteralPath $projectRoot

# --- Clean up SQLite WAL files from previous run ---
$dataDir = Join-Path $projectRoot "data"
if (Test-Path -LiteralPath $dataDir) {
    $walFiles = @("trading_bot.sqlite3-shm", "trading_bot.sqlite3-wal")
    foreach ($f in $walFiles) {
        $full = Join-Path $dataDir $f
        if (Test-Path -LiteralPath $full) {
            Write-Host "       Removing previous $f..." -ForegroundColor Yellow
            Remove-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Host "=== Meme Project Launcher ===" -ForegroundColor Cyan

# --- Python venv ---
if (-not (Test-Path -LiteralPath ".venv")) {
    Write-Host "[1/5] Creating Python virtual environment..." -ForegroundColor Yellow
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: Failed to create .venv" -ForegroundColor Red; exit 1 }
} else {
    Write-Host "[1/5] Virtual environment .venv already exists." -ForegroundColor Green
}

# --- Activate venv ---
$activateScript = ".venv\Scripts\Activate.ps1"
if (-not (Test-Path -LiteralPath $activateScript)) {
    Write-Host "ERROR: Activate script not found at $activateScript" -ForegroundColor Red
    exit 1
}
. $activateScript

# --- Python dependencies ---
Write-Host "[2/5] Installing Python dependencies..." -ForegroundColor Yellow
pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARNING: pip install had non-zero exit code. Continuing anyway..." -ForegroundColor DarkYellow
} else {
    Write-Host "       Python dependencies ready." -ForegroundColor Green
}

# --- Frontend dependencies ---
Write-Host "[3/5] Checking frontend dependencies..." -ForegroundColor Yellow
if (-not (Test-Path -LiteralPath "frontend\node_modules")) {
    Write-Host "       Installing frontend npm packages..." -ForegroundColor Yellow
    Push-Location frontend
    npm install
    if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: npm install failed" -ForegroundColor Red; Pop-Location; exit 1 }
    Pop-Location
} else {
    Write-Host "       frontend/node_modules already exists." -ForegroundColor Green
}

# --- Start Backend ---
Write-Host "[4/5] Starting backend (uvicorn)..." -ForegroundColor Yellow
$backendCmd = '$Host.UI.RawUI.WindowTitle = ''Meme Backend''; Set-Location -LiteralPath ''' + $projectRoot + '''; .\.venv\Scripts\Activate.ps1; uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --reload'
Start-Process powershell -ArgumentList "-NoExit", "-Command", $backendCmd
Write-Host "       Backend launched on http://127.0.0.1:8000" -ForegroundColor Green

# --- Wait for backend ready ---
Write-Host "       Waiting for backend to be ready..." -ForegroundColor Yellow
$maxWait = 30
$ready = $false
for ($i = 0; $i -lt $maxWait; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch {}
    Start-Sleep -Seconds 1
}
if (-not $ready) {
    Write-Host "ERROR: Backend did not become ready within ${maxWait}s" -ForegroundColor Red
    Write-Host "Check the Meme Backend window for errors." -ForegroundColor Yellow
    pause
    exit 1
}
Write-Host "       Backend is ready." -ForegroundColor Green

# --- Start Frontend ---
Write-Host "[5/5] Starting frontend (vite)..." -ForegroundColor Yellow
$frontendCmd = '$Host.UI.RawUI.WindowTitle = ''Meme Frontend''; Set-Location -LiteralPath ''' + $projectRoot + '\frontend''; npm run dev'
Start-Process powershell -ArgumentList "-NoExit", "-Command", $frontendCmd
Write-Host "       Frontend launched on http://localhost:5173" -ForegroundColor Green

# --- Wait for frontend then open browser ---
Write-Host "       Waiting for frontend to be ready..." -ForegroundColor Yellow
$maxWait = 15
$fready = $false
for ($i = 0; $i -lt $maxWait; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:5173" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
        if ($r.StatusCode -eq 200) { $fready = $true; break }
    } catch {}
    Start-Sleep -Seconds 1
}
if ($fready) {
    Write-Host "       Frontend ready, opening browser..." -ForegroundColor Green
    Start-Process "http://localhost:5173"
} else {
    Write-Host "WARNING: Frontend not responding, but it may still be starting." -ForegroundColor DarkYellow
    Start-Process "http://localhost:5173"
}

Write-Host "=== All services launched ===" -ForegroundColor Cyan
Start-Sleep -Seconds 2
