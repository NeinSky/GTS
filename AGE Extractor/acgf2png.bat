@echo off
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8

rem ============================================================
rem  *.acgf  ->  PNG с цветом и альфой
rem
rem  цвет  -- из "листа" page (запись AGFBMP: палитра 256 x BGRA + индексы)
rem  альфа -- из картинки ACIF (8-битная маска движка)
rem
rem  Использование:
rem     acgf2png.bat                     etc\acgf\ -> etc\png_rgba\
rem     acgf2png.bat <вход> <выход>     свои каталоги
rem     (папку или один *.acgf можно просто перетащить на этот файл)
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
if "%SRC%"=="" set "SRC=%~dp0etc\acgf"
if "%DST%"=="" set "DST=%~dp0etc\png_rgba"

echo Вход:  %SRC%
echo Выход: %DST%  (RGBA: цвет из page + альфа из ACIF)
echo.

python "%~dp0acgf_tool.py" --extract "%DST%" "%SRC%" --png-only --color --rgba
if errorlevel 1 goto fail

echo.
echo Готово: PNG с цветом и альфой лежат в "%DST%"
goto end

:fail
echo.
echo Не удалось. Проверьте, что рядом с этим файлом лежит acgf_tool.py,
echo а во входном каталоге есть файлы *.acgf.

:end
echo.
pause
