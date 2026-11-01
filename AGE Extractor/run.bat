@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
rem Извлечь все ресурсы из томов etc\example\ в etc\out\
python "%~dp0alf_extract.py" "%~dp0etc\example" --out "%~dp0etc\out"
echo.
if errorlevel 1 (
  echo Не удалось: проверьте, что рядом лежат alf_extract.py и папка etc\example\
) else (
  echo Готово: результаты в etc\out\ ^(манифест etc\out\manifest.csv^)
)
pause