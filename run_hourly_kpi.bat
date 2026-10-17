@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

REM ============================================================================
REM Hourly KPI Platform - Windows Task Scheduler & Interactive Runner
REM ============================================================================

REM Always switch working directory to this script's directory
cd /d "%~dp0"
title Telecom Hourly KPI Surveillance Engine

REM Detect Python executable (prefer virtual environment)
if exist ".venv\Scripts\python.exe" (
    set "PY_EXE=.venv\Scripts\python.exe"
    set "ENV_NAME=Virtualenv (.venv)"
) else if exist "venv\Scripts\python.exe" (
    set "PY_EXE=venv\Scripts\python.exe"
    set "ENV_NAME=Virtualenv (venv)"
) else (
    set "PY_EXE=python"
    set "ENV_NAME=System Python"
)

REM Check for demo flag
set "TARGET_SCRIPT=main.py"
if "%~1"=="--demo" set "TARGET_SCRIPT=demo_pipeline.py"
if "%~2"=="--demo" set "TARGET_SCRIPT=demo_pipeline.py"

echo ==============================================================================
echo   TELECOM HOURLY KPI SURVEILLANCE RUNNER
echo   Started    : %date% %time%
echo   Python Env : %ENV_NAME%
echo   Target     : %TARGET_SCRIPT%
echo ==============================================================================
echo.

"%PY_EXE%" %TARGET_SCRIPT%
set EXIT_CODE=%ERRORLEVEL%

echo.
echo ==============================================================================
if %EXIT_CODE% EQU 0 (
    echo   STATUS: EXECUTION SUCCESSFUL [Exit Code: 0]
    echo   Full audit trail saved to logs\kpi_engine.log
) else (
    echo   STATUS: EXECUTION FAILED [Exit Code: %EXIT_CODE%]
    echo   Diagnostics and failure traceback saved to logs\kpi_errors.log
)
echo   Finished: %date% %time%
echo ==============================================================================

REM If launched interactively by double-clicking (no --silent parameter),
REM pause briefly so the window doesn't disappear abruptly before you can read it.
if "%~1"=="--silent" goto end
if "%~2"=="--silent" goto end

echo.
echo   [Interactive Session] Window will close in 10 seconds (or press any key)...
timeout /t 10 >nul 2>&1

:end
exit /b %EXIT_CODE%
