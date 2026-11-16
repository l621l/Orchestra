@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" || exit /b 1

if not defined ORCHESTRA_VENV set "ORCHESTRA_VENV=%LOCALAPPDATA%\Orchestra\venv"
for %%I in ("%ORCHESTRA_VENV%") do set "ORCHESTRA_VENV=%%~fI"
set "ORCHESTRA_PYTHON=%ORCHESTRA_VENV%\Scripts\python.exe"

"%ORCHESTRA_PYTHON%" -m pip --version >nul 2>&1
if not errorlevel 1 goto :dependencies

call :find_python
if not defined ORCHESTRA_BASE_PYTHON (
    call :install Python.Python.3.13 --scope user
    if errorlevel 1 goto :failed
    call :find_python
    if not defined ORCHESTRA_BASE_PYTHON goto :reopen
)
"%ORCHESTRA_BASE_PYTHON%" -m venv "%ORCHESTRA_VENV%"
if errorlevel 1 goto :failed

:dependencies
fc /b requirements.txt "%ORCHESTRA_VENV%\.orchestra-requirements.txt" >nul 2>&1
if errorlevel 1 goto :install_dependencies
fc /b requirements-qt.txt "%ORCHESTRA_VENV%\.orchestra-requirements-qt.txt" >nul 2>&1
if errorlevel 1 goto :install_dependencies
"%ORCHESTRA_PYTHON%" -B -c "import PySide6.QtWidgets, openai, regex, rich" >nul 2>&1
if not errorlevel 1 goto :launch

:install_dependencies
"%ORCHESTRA_PYTHON%" -m pip install -r requirements-qt.txt
if errorlevel 1 goto :failed
copy /y requirements.txt "%ORCHESTRA_VENV%\.orchestra-requirements.txt" >nul
if errorlevel 1 goto :failed
copy /y requirements-qt.txt "%ORCHESTRA_VENV%\.orchestra-requirements-qt.txt" >nul
if errorlevel 1 goto :failed

:launch
set "VIRTUAL_ENV=%ORCHESTRA_VENV%"
set "PATH=%ORCHESTRA_VENV%\Scripts;%PATH%"
"%ORCHESTRA_PYTHON%" -B orchestra_qt.py %*
set "ORCHESTRA_EXIT=%errorlevel%"
popd
exit /b %ORCHESTRA_EXIT%

:find_python
set "ORCHESTRA_BASE_PYTHON="
for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable if sys.version_info[:2] >= (3, 11) else '')" 2^>nul') do set "ORCHESTRA_BASE_PYTHON=%%P"
if defined ORCHESTRA_BASE_PYTHON exit /b 0
for %%P in ("%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%ProgramFiles%\Python313\python.exe") do if exist "%%~P" set "ORCHESTRA_BASE_PYTHON=%%~P"
if defined ORCHESTRA_BASE_PYTHON exit /b 0
for /f "delims=" %%P in ('where python.exe 2^>nul') do call :try_python "%%P"
exit /b 0

:try_python
if defined ORCHESTRA_BASE_PYTHON exit /b 0
if /i "%~dp1"=="%LOCALAPPDATA%\Microsoft\WindowsApps\" exit /b 0
"%~1" -c "import sys; sys.exit(sys.version_info[:2] < (3, 11))" >nul 2>&1
if not errorlevel 1 set "ORCHESTRA_BASE_PYTHON=%~1"
exit /b 0

:install
where winget.exe >nul 2>&1
if errorlevel 1 (
    echo WinGet is unavailable. Install App Installer or install Python 3.11+ manually. 1>&2
    exit /b 1
)
winget install --id %1 --exact --source winget --accept-package-agreements --accept-source-agreements %2 %3
exit /b %errorlevel%

:reopen
echo Open a new terminal and run start-windows.cmd again to pick up the new installation. 1>&2
goto :exit_error

:failed
echo Orchestra setup failed. See the error above. 1>&2

:exit_error
popd
exit /b 1
