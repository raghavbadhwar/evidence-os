@echo off
call "%~dp0Start Evidence OS.cmd" doctor --fix
set "doctor_result=%errorlevel%"
pause
exit /b %doctor_result%
