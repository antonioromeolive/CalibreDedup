@echo off
setlocal
cd /d "%~dp0"

rem Python: the project's .venv, else Python 3.13 for this user, else the py launcher, else PATH.
if exist ".venv\Scripts\python.exe" (
    set PYTHON="%~dp0.venv\Scripts\python.exe"
    set PYTHONW="%~dp0.venv\Scripts\pythonw.exe"
) else if exist "%LocalAppData%\Programs\Python\Python313\python.exe" (
    set PYTHON="%LocalAppData%\Programs\Python\Python313\python.exe"
    set PYTHONW="%LocalAppData%\Programs\Python\Python313\pythonw.exe"
) else (
    where py >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON=py -3"
        set "PYTHONW=pyw -3"
    ) else (
        set "PYTHON=python"
        set "PYTHONW=pythonw"
    )
)

rem --cli: run in this console and wait (it prints its results here).
echo(%* | findstr /i /c:"--cli" >nul
if not errorlevel 1 goto cli

rem The window: started without a console, and this script ends at once.
rem If nothing appears, run it from a console to see the error:  python -m calibre_dedup.review_app
start "" %PYTHONW% -m calibre_dedup.review_app %*
exit /b 0

:cli
%PYTHON% -m calibre_dedup.review_app %*
if errorlevel 1 (
    echo.
    echo Calibre Metadata Review ended with an error.
    pause
)
