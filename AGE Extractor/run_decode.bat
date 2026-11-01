@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
rem Только PNG: одна папка etc\png\, без подпапок и служебных файлов
python "%~dp0acgf_tool.py" --extract "%~dp0etc\png" "%~dp0etc\acgf" --png-only
echo.
if errorlevel 1 (
  echo Не удалось: проверьте, что рядом лежат acgf_tool.py и папка etc\acgf\
) else (
  echo Готово: PNG в папке etc\png\
)
pause