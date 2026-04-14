@echo off
title Research Agent

:: Set working directory to script location
cd /d "%~dp0"

:: Create virtual environment if it doesn't exist
if not exist ".venv\Scripts\activate.bat" (
    echo Virtual environment not found. Setting up now...
    echo.
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment. Make sure Python 3.10+ is installed.
        pause
        exit /b 1
    )
    call .venv\Scripts\activate.bat
    echo Installing dependencies...
    pip install --quiet --upgrade pip
    pip install --quiet -r requirements.txt
    if errorlevel 1 (
        echo ERROR: Failed to install dependencies.
        pause
        exit /b 1
    )
    if not exist ".env" (
        copy .env.example .env >nul
        echo Created .env from .env.example -- edit it to set your LM_STUDIO_MODEL.
        echo.
    )
    echo Setup complete!
    echo.
)

call .venv\Scripts\activate.bat

:: Show menu
echo.
echo =========================================
echo   Research Agent
echo =========================================
echo.
echo  [1] Start API Server
echo  [2] Start MCP Server
echo  [3] Run a Query
echo  [4] Exit
echo.
set /p choice="Select an option: "

if "%choice%"=="1" (
    echo Starting REST API server...
    python run.py api
) else if "%choice%"=="2" (
    echo Starting MCP server...
    python run.py mcp
    if errorlevel 1 (
        echo.
        echo ERROR: MCP server failed to start. See output above.
        pause
    )
) else if "%choice%"=="3" (
    echo.
    set /p question="Enter your research question: "
    python run.py query "%question%"
    pause
) else if "%choice%"=="4" (
    exit /b 0
) else (
    echo Invalid option.
    pause
)
