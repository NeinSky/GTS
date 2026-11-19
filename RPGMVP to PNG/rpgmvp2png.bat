@echo off
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8

rem ============================================================
rem  *.rpgmvp  ->  обычный PNG
rem
rem  Файл картинки RPG Maker MV -- это PNG, у которого первые 16 байт
rem  лежат под XOR с ключом игры, а перед ними стоит 16-байтовый заголовок.
rem  Ключ восстанавливается из самого файла, поэтому указывать его не нужно.
rem
rem  Использование:
rem     rpgmvp2png.bat                     input\ -> output\
rem     rpgmvp2png.bat <вход> <выход>      свои каталоги
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
set "DST=%~2"
if "%SRC%"=="" set "SRC=%~dp0input"

echo Вход:  %SRC%
if "%DST%"=="" (
  echo Выход: %~dp0output
) else (
  echo Выход: %DST%
)
echo.

if "%DST%"=="" (
  python "%~dp0rpgmvp2png.py" "%SRC%" --overwrite
) else (
  python "%~dp0rpgmvp2png.py" "%SRC%" --out "%DST%" --overwrite
)
if errorlevel 1 goto fail

echo.
echo Готово: PNG лежат в каталоге результатов
goto end

:fail
echo.
echo Не удалось. Проверьте, что рядом с этим файлом лежит rpgmvp2png.py,
echo а во входном каталоге есть файлы *.rpgmvp.

:end
echo.
pause
