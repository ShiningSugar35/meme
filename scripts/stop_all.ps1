$ErrorActionPreference = "Continue"
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)

Write-Host "=== Meme Project Stopper ===" -ForegroundColor Cyan

$stopped = @()

# --- Stop by window title (primary) ---
Write-Host "[1/2] Stopping processes by window title..." -ForegroundColor Yellow

Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.MainWindowTitle -eq "Meme Backend" -or $_.MainWindowTitle -eq "Meme Frontend"
} | ForEach-Object {
    Write-Host "       Stopping '$($_.MainWindowTitle)' (PID: $($_.Id), Name: $($_.ProcessName))" -ForegroundColor Green
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    $script:stopped += $_.Id
}

if ($stopped.Count -eq 0) {
    Write-Host "       No matching window titles found." -ForegroundColor DarkYellow
}

# --- Fallback: scan command lines for uvicorn/vite (may need admin) ---
Write-Host "[2/2] Checking for orphaned project processes (command-line scan)..." -ForegroundColor Yellow

try {
    $procs = Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        $_.CommandLine -match 'uvicorn\s+backend\.app\.main' -or
        $_.CommandLine -match 'vite'
    }
    foreach ($p in $procs) {
        if ($p.ProcessId -notin $stopped) {
            Write-Host "       Stopping orphan process (PID: $($p.ProcessId), Name: $($p.Name))" -ForegroundColor Green
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    if ($procs.Count -eq 0) {
        Write-Host "       No orphaned project processes found." -ForegroundColor Green
    }
} catch {
    Write-Host "       Command-line scan skipped (requires administrator rights)." -ForegroundColor DarkYellow
}

Write-Host "=== All services stopped ===" -ForegroundColor Cyan
