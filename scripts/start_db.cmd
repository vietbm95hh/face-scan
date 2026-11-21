@echo off
REM ============================================================
REM  Khoi dong PostgreSQL 16.6 (Laragon) cho fbscan
REM  - Data dir : D:\laragon\data\postgresql2  (cluster rieng, port 2024)
REM  - Ly do: cluster goc D:\laragon\data\postgresql bi crash
REM    (0xC0000142) ngay khi co ket noi -> tao cluster sach.
REM
REM  Script nay chi la vo boc cho PowerShell/Task Scheduler.
REM  Logic that nam o scripts\start_db.py (dung duoc tu moi shell).
REM
REM  LAN DAU (neu chua co cluster):
REM    D:\laragon\bin\postgresql\postgresql\bin\initdb.exe ^
REM      -D D:\laragon\data\postgresql2 -U postgres -A scram-sha-256 ^
REM      --pwfile=pw.txt -E UTF8
REM  (pw.txt: 1 dong chua mat khau cua user postgres)
REM ============================================================
setlocal
cd /d "%~dp0.."

set "PY=python"
where python >nul 2>&1 || set "PY=D:\laragon\bin\python\python-3.13\python.exe"

"%PY%" "%~dp0start_db.py"
exit /b %ERRORLEVEL%
