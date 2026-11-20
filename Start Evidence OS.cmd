@echo off
setlocal
pushd "%~dp0" || exit /b 1
set "mode=start"
set "python_flags="
if not "%~3"=="" goto usage
if "%~1"=="" goto preflight
if /i not "%~1"=="doctor" goto usage
set "mode=doctor"
if "%~2"=="" goto preflight
if /i not "%~2"=="--fix" goto usage
set "mode=fix"
:preflight
if not exist "scripts\practice_setup.py" goto incomplete
if not exist "uv.lock" goto incomplete
if not exist "pyproject.toml" goto incomplete
set "PATH=%PATH%;%USERPROFILE%\.local\bin;%LOCALAPPDATA%\Microsoft\WinGet\Links"
set "uv_bin=%CD%\.evidence-tools\uv.exe"
"%uv_bin%" sync --help 2>nul | findstr /L /C:"--check" >nul && goto ready
set "uv_bin=uv.exe"
uv.exe sync --help 2>nul | findstr /L /C:"--check" >nul && goto ready
echo NEEDS ATTENTION  A working copy of the uv setup tool is missing.
if "%mode%"=="doctor" goto suggest
where winget.exe >nul 2>nul || goto standalone
choice /C YN /N /M "Install uv with Windows Package Manager and continue? [Y/N] "
if errorlevel 2 goto missing
if not errorlevel 1 goto missing
winget install --id=astral-sh.uv -e
uv.exe sync --help 2>nul | findstr /L /C:"--check" >nul && goto ready
:standalone
choice /C YN /N /M "Install uv from Astral into this folder and continue? [Y/N] "
if errorlevel 2 goto missing
if not errorlevel 1 goto missing
set "uv_bin=%CD%\.evidence-tools\uv.exe"
set "UV_UNMANAGED_INSTALL=%CD%\.evidence-tools"
powershell.exe -NoProfile -NonInteractive -Command "$ErrorActionPreference='Stop'; if ((Test-Path -LiteralPath $env:UV_UNMANAGED_INSTALL) -and ((Get-Item -LiteralPath $env:UV_UNMANAGED_INSTALL).Attributes -band [IO.FileAttributes]::ReparsePoint)) {throw 'Extract the ZIP into a fresh folder'}; $f=Join-Path ([IO.Path]::GetTempPath()) (([guid]::NewGuid()).ToString()+'.ps1'); try {Invoke-WebRequest -UseBasicParsing 'https://astral.sh/uv/0.11.15/install.ps1' -OutFile $f; & $f} finally {Remove-Item -LiteralPath $f -ErrorAction SilentlyContinue}"
if errorlevel 1 goto missing
"%uv_bin%" sync --help 2>nul | findstr /L /C:"--check" >nul || goto missing
:ready
"%uv_bin%" --version
if not "%mode%"=="doctor" goto run
"%uv_bin%" python find --system --offline --no-python-downloads 3.12 >nul 2>nul || goto python_missing
set "python_flags=--offline --no-python-downloads"
:run
"%uv_bin%" run --no-project --isolated --python 3.12 %python_flags% -- python -I -B scripts/practice_setup.py "%mode%" "%uv_bin%"
if errorlevel 1 goto failed
popd
exit /b 0
:incomplete
echo Setup files are missing. Extract the entire ZIP into a fresh folder.
goto stop
:python_missing
echo NEEDS ATTENTION  Python 3.12 is missing.
:suggest
echo Open Doctor Evidence OS to install missing software and check again.
goto stop
:missing
echo Install uv through IT, then reopen this file. Guide: https://docs.astral.sh/uv/getting-started/installation/
goto stop
:failed
echo Setup could not finish. Read the first failed check above. Check network and disk space for download errors.
:stop
if "%mode%"=="start" pause
popd
exit /b 1
:usage
echo Usage: Start Evidence OS.cmd [doctor [--fix]]
popd
exit /b 2
