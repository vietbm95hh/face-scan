@echo off
REM ============================================================
REM  Wrapper cho Windows Task Scheduler: quet bai viet dinh ky
REM
REM  DB o xa (PG_HOST trong .env khac 127.0.0.1/localhost) thi KHONG
REM  goi start_db.cmd; DB local thi tu bat PostgreSQL 2024 truoc.
REM ============================================================
setlocal
cd /d "%~dp0.."

set LOG_DIR=logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set STAMP=%date:~-4%-%date:~3,2%-%date:~0,2%
set LOG=%LOG_DIR%\scan-%STAMP%.log

set "PY=python"
where python >nul 2>&1 || set "PY=D:\laragon\bin\python\python-3.13\python.exe"

echo [%date% %time%] bat dau >> "%LOG%"
if /i "%FBSCAN_SKIP_DB_START%"=="1" goto :scan

call "%~dp0start_db.cmd" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] DB khong san sang, bo qua lan quet >> "%LOG%"
    exit /b 1
)

:scan
"%PY%" "%~dp0run_scan.py" --all >> "%LOG%" 2>&1
set RC=%errorlevel%
echo [%date% %time%] ket thuc, exit=%RC% >> "%LOG%"
exit /b %RC%
