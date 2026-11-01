@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem ============================================================
rem  unrpa_3 - unpack a Ren'Py archive (.rpa)
rem
rem  Usage:
rem    unpack.bat                 -> unpacks archive.rpa into .\extracted
rem    unpack.bat mygame.rpa      -> unpacks mygame.rpa into .\extracted
rem    unpack.bat mygame.rpa out  -> unpacks mygame.rpa into .\out
rem ============================================================

set "SCRIPT=unrpa_3"

set "ARCHIVE=%~1"
if "%ARCHIVE%"=="" set "ARCHIVE=archive.rpa"

set "OUTDIR=%~2"
if "%OUTDIR%"=="" set "OUTDIR=extracted"

if not exist "%ARCHIVE%" (
	echo [ERROR] Archive not found: "%ARCHIVE%"
	echo         Put this .bat next to the archive, or pass its path as an argument.
	pause
	exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
	echo [ERROR] Python not found in PATH. Install Python 3 and try again.
	pause
	exit /b 1
)

echo === Unpacking "%ARCHIVE%" into "%OUTDIR%" ===
echo.

python "%SCRIPT%" -p "%OUTDIR%" -m "%ARCHIVE%"
if errorlevel 1 (
	echo.
	echo [ERROR] Extraction failed.
	pause
	exit /b 1
)

echo.
echo done!
pause
