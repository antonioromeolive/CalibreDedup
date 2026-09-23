@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=%~dp0.venv\Scripts\python.exe"
) else if exist "%LocalAppData%\Programs\Python\Python313\python.exe" (
    set "PYTHON=%LocalAppData%\Programs\Python\Python313\python.exe"
) else (
    where py >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON=py -3"
    ) else (
        set "PYTHON=python"
    )
)

%PYTHON% -m calibre_dedup %*
if errorlevel 1 (
    echo.
    echo Calibre Duplicate Remover could not start.
    pause
)
