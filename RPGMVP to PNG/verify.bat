@echo off
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8

rem ============================================================
rem  Проверка *.rpgmvp без записи: расшифровать каждый файл
rem  и сверить структуру PNG (включая CRC всех чанков).
rem
rem  Использование:
rem     verify.bat                     проверка input\
rem     verify.bat <вход>              проверка своего каталога
rem     (папку или один *.rpgmvp можно просто перетащить на этот файл)
rem ============================================================

where python >nul 2>nul
if errorlevel 1 (
  echo Python не найден в PATH. Установите Python 3.9 или новее и повторите.
  echo.
  pause
  exit /b 1
)

set "SRC=%~1"
if "%SRC%"=="" set "SRC=%~dp0input"

echo Проверка без записи: %SRC%
echo.

python "%~dp0rpgmvp2png.py" "%SRC%" --dry-run -v
if errorlevel 1 goto fail

echo.
echo Все файлы читаются корректно.
goto end

:fail
echo.
echo Есть ошибки: список выше. Битые или чужие файлы можно посмотреть
echo отдельно: python rpgmvp2png.py ^<файл^> --dry-run

:end
echo.
pause
