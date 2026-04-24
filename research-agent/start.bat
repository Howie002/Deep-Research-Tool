@echo off
setlocal enabledelayedexpansion
title Research Agent

:: Set working directory to script location
cd /d "%~dp0"

:: Create virtual environment if it doesn't exist
if not exist ".venv\Scripts\activate.bat" (
    echo Virtual environment not found. Setting up now...
    echo.

    call :find_python
    if not defined PYBOOT (
        echo No compatible Python ^(3.10-3.13^) found.
        echo   ^(crewai and other dependencies do not yet support Python 3.14+.^)
        where winget >nul 2>&1
        if not errorlevel 1 (
            echo.
            set /p install_ans="Install Python 3.13 via winget now? [Y/n]: "
            if /i not "!install_ans!"=="n" (
                winget install --id Python.Python.3.13 -e --accept-source-agreements --accept-package-agreements
                if errorlevel 1 (
                    echo ERROR: winget install Python.Python.3.13 failed.
                    pause
                    exit /b 1
                )
                :: Refresh PATH so newly-installed py/python is visible this session.
                for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul ^| find "Path"') do set "USERPATH=%%B"
                for /f "tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul ^| find "Path"') do set "SYSPATH=%%B"
                if defined SYSPATH set "PATH=!SYSPATH!;!USERPATH!"
                call :find_python
            )
        ) else (
            echo   winget not found. Install a supported Python from
            echo   https://www.python.org/downloads/ and re-run this script.
        )
    )
    if not defined PYBOOT (
        echo ERROR: Still no compatible Python on PATH. Aborting.
        pause
        exit /b 1
    )
    echo Using !PYBOOT!
    !PYBOOT! -m venv .venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
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
        if exist ".env.example" (
            copy .env.example .env >nul
            echo Created .env from .env.example -- edit it to set your LM_STUDIO_MODEL.
            echo.
        )
    )
    echo Setup complete!
    echo.
)

call .venv\Scripts\activate.bat

:: Read API_PORT from .env so the URL we print matches what api_server.py binds.
:: config.py defaults to 8765 but .env.example ships API_PORT=8000 — the two drift.
set "API_PORT=8765"
if exist ".env" (
    for /f "usebackq tokens=1,2 delims==" %%A in (".env") do (
        set "KEY=%%A"
        set "VAL=%%B"
        call :trim KEY
        call :trim VAL
        if /i "!KEY!"=="API_PORT" if not "!VAL!"=="" set "API_PORT=!VAL!"
    )
)

:: Show menu
echo.
echo =========================================
echo   Research Agent
echo =========================================
echo.
echo  [1] Start API Server  ^(http://localhost:!API_PORT!^)
echo  [2] Start MCP Server
echo  [3] Run a Query
echo  [4] Exit
echo.
set /p choice="Select an option: "

if "%choice%"=="1" (
    echo Starting REST API server on http://localhost:!API_PORT! ...
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
    python run.py query "!question!"
    pause
) else if "%choice%"=="4" (
    exit /b 0
) else (
    echo Invalid option.
    pause
)
exit /b 0

:: ------------------------------------------------------------------
:: :trim <varname>
:: Strips leading/trailing whitespace and surrounding quotes from the
:: named variable. Used for parsing .env key=value pairs.
:: ------------------------------------------------------------------
:trim
call set "VALUE=%%%1%%"
for /f "tokens=* delims= " %%a in ("!VALUE!") do set "VALUE=%%a"
if "!VALUE:~-1!"==" " set "VALUE=!VALUE:~0,-1!"
if defined VALUE (
    if "!VALUE:~0,1!"=="\"" set "VALUE=!VALUE:~1!"
    if "!VALUE:~-1!"=="\"" set "VALUE=!VALUE:~0,-1!"
)
set "%1=!VALUE!"
exit /b 0

:: ------------------------------------------------------------------
:: :find_python
:: Sets PYBOOT to a launcher invocation that yields Python 3.10-3.13,
:: or leaves it undefined. Prefers py -X.Y, falls back to `python` if
:: its version is in range.
:: ------------------------------------------------------------------
:find_python
set "PYBOOT="
where py >nul 2>&1
if not errorlevel 1 (
    for %%V in (3.13 3.12 3.11 3.10) do (
        if not defined PYBOOT (
            py -%%V -c "import sys" >nul 2>&1
            if not errorlevel 1 set "PYBOOT=py -%%V"
        )
    )
)
if not defined PYBOOT (
    where python >nul 2>&1
    if not errorlevel 1 (
        for /f "tokens=*" %%v in ('python -c "import sys;print(str(sys.version_info.major)+'.'+str(sys.version_info.minor))" 2^>nul') do set "PYVER=%%v"
        if "!PYVER!"=="3.10" set "PYBOOT=python"
        if "!PYVER!"=="3.11" set "PYBOOT=python"
        if "!PYVER!"=="3.12" set "PYBOOT=python"
        if "!PYVER!"=="3.13" set "PYBOOT=python"
    )
)
exit /b 0
