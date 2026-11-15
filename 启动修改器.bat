@echo off
setlocal
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if not exist "%VENV_PY%" goto setup
if not exist "%~dp0.venv\ready.marker" goto setup
goto launch

:setup
set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PYTHON%" (
    echo Python 3.12 was not found: %PYTHON%
    pause
    exit /b 1
)

where uv >nul 2>nul
if errorlevel 1 (
    echo uv was not found. It is required to prepare the trainer environment.
    pause
    exit /b 1
)

if not exist "%VENV_PY%" (
    echo First run: creating an isolated Python environment...
    uv venv --python "%PYTHON%" ".venv"
    if errorlevel 1 (
        echo Failed to create venv.
        pause
        exit /b 1
    )
)

echo Preparing the IL2CPP runtime component...
uv pip install --python "%VENV_PY%" "frida==17.26.3"
if errorlevel 1 (
    echo Failed to install frida.
    pause
    exit /b 1
)

echo. > "%~dp0.venv\ready.marker"

:launch
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0trainer.py"
exit /b 0
