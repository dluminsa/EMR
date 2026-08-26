@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" convert_data.py
) else (
    py convert_data.py
)

echo.
if errorlevel 1 (
    echo No app data was updated. Correct the error above and try again.
) else (
    echo Conversion complete.
)
pause
