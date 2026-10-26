@echo off
set "SCRIPT_DIR=%~dp0"
title Telegram Big Bet - Simulation
if exist "%SCRIPT_DIR%.env" goto run
echo ERROR: .env was not found in the script folder.
echo Copy .env.example to .env and complete the settings first.
pause
exit /b 1

:run
"%SCRIPT_DIR%runtime\python\python.exe" -u "%SCRIPT_DIR%tg_big_bet.py"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Program exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%

