@echo off
REM AirOne Ground Station installer — Windows
REM Run as Administrator for best results

echo [AirOne] Installing dependencies from requirements_run.txt...
pip install -r "%~dp0requirements_run.txt"

echo [AirOne] Creating desktop shortcut...
set SCRIPT_DIR=%~dp0
set SHORTCUT=%USERPROFILE%\Desktop\AirOne Ground Station.lnk

powershell -NoProfile -Command "$ws = New-Object -ComObject WScript.Shell; $sc = $ws.CreateShortcut('%SHORTCUT%'); $sc.TargetPath = 'python'; $sc.Arguments = '\"%SCRIPT_DIR%run.py\"'; $sc.WorkingDirectory = '%SCRIPT_DIR%'; $sc.Description = 'AirOne CanSat Ground Station'; $sc.Save()"

echo.
echo [AirOne] Install complete.
echo [AirOne] Run with:  python run.py
echo [AirOne] Or double-click the desktop shortcut.
pause
