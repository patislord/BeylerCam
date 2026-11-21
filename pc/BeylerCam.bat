@echo off
setlocal
cd /d "%~dp0"

rem pyvirtualcam su an Python 3.14'e kadar destekliyor; bu yuzden ayri bir 3.14 ortami kullanilir.
if exist ".venv\Scripts\pythonw.exe" goto run

echo [BeylerCam] Ilk kurulum yapiliyor, bu bir kac dakika surebilir...
py -V:3.14 --version >nul 2>&1
if errorlevel 1 (
    echo [BeylerCam] Python 3.14 kuruluyor...
    py install 3.14
    if errorlevel 1 goto fail
)
py -V:3.14 -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto fail
echo [BeylerCam] Kurulum tamam.

:run
start "" ".venv\Scripts\pythonw.exe" main.py
exit /b 0

:fail
echo.
echo [BeylerCam] Kurulum basarisiz oldu. Yukaridaki hatayi kontrol edin.
if exist ".venv" rmdir /s /q ".venv"
pause
exit /b 1
