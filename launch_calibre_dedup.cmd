@echo off
REM Copyright (c) 2026 Antonio Romeo <antonioromeo@ilve.it>
REM Author: Antonio Romeo (with Claude Code et al.)
REM SPDX-License-Identifier: MIT
REM
REM Permission is hereby granted, free of charge, to any person obtaining a copy
REM of this software and associated documentation files (the "Software"), to deal
REM in the Software without restriction, including without limitation the rights
REM to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
REM copies of the Software, and to permit persons to whom the Software is
REM furnished to do so, subject to the following conditions:
REM
REM The above copyright notice and this permission notice shall be included in all
REM copies or substantial portions of the Software.
REM
REM THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
REM IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
REM FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
REM AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
REM LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
REM OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
REM SOFTWARE.
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
rem If nothing appears, run it from a console to see the error:  python -m calibre_dedup
start "" %PYTHONW% -m calibre_dedup %*
exit /b 0

:cli
%PYTHON% -m calibre_dedup %*
if errorlevel 1 (
    echo.
    echo Calibre Duplicate Remover ended with an error.
    pause
)
