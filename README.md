# GTS — Games Tools Set

Набор автономных консольных утилит для разбора игровых архивов и ресурсов.
Каждый инструмент самодостаточен: только стандартная библиотека Python, без
внешних зависимостей. Windows-обёртки `*.bat` запускают всё «в один клик».

| Каталог | Что разбирает | Документация |
|---|---|---|
| [`AGE Extractor/`](<AGE Extractor/README.md>) | тома `*.ALF` и бандлы `*.acgf` (движок AGE): WAV/XWMA, шрифты, MPEG, контейнеры `SYS4472`, картинки → PNG | [`AGE Extractor/README.md`](<AGE Extractor/README.md>) |
| [`Unrpa 3/`](<Unrpa 3/README.md>) | архивы Ren'Py `*.rpa` / `*.rpi` (RPA-1.0, RPA-2.0, RPA-3.0) | [`Unrpa 3/README.md`](<Unrpa 3/README.md>) |

## Структура

```
GTS/
  README.md          этот файл
  .gitignore         что не публикуется (локальные данные и результаты)
  AGE Extractor/
    acgf_tool.py     бандлы *.acgf: структура блоков, маска ACIF, лист page -> PNG
    alf_extract.py   тома *.ALF: манифест, извлечение ресурсов, отчёт о «зазорах»
    acgf2png.bat     все *.acgf -> PNG с цветом и альфой (RGBA)
    run_decode.bat   все *.acgf -> PNG только маски ACIF (8-битный серый)
    run.bat          тома *.ALF из etc\example\ -> ресурсы в etc\out\
    README.md        документация по форматам ALF и ACGF
  Unrpa 3/
    unrpa_3          распаковщик RPA (Python 3 порт оригинального unrpa)
    unpack.bat       обёртка для Windows: unpack.bat [архив.rpa] [каталог]
    README.md        документация по формату RPA
```

## Требования

* Python 3.9 или новее в `PATH` (`AGE Extractor/`); проверено на 3.13 (`Unrpa 3/`).
* Windows — для `*.bat`-обёрток; сами скрипты кроссплатформенные.

## Быстрый старт

```bat
:: AGE Extractor: все тома из etc\example\ -> etc\out\ (+ манифест manifest.csv)
cd "AGE Extractor"
run.bat

:: AGE Extractor: все *.acgf из etc\acgf\ -> PNG с цветом и альфой в etc\png_rgba\
acgf2png.bat

:: AGE Extractor: своя папка или один файл — просто перетащить на acgf2png.bat

:: Unrpa 3: mygame.rpa -> .\out
cd "..\Unrpa 3"
unpack.bat mygame.rpa out
```

Подробности по форматам, опциям командной строки, проверкам целостности
разбора и ограничениям — в README внутри каждого каталога.

## Лицензия

GTS распространяется на условиях **GNU General Public License v3 или более
поздней** (`GPL-3.0-or-later`); полный текст — в файле [`LICENSE`](LICENSE).

* `Unrpa 3/unrpa_3` — порт [`unrpa`](https://github.com/Lattyware/unrpa)
  (© Gareth Latty и участники проекта, GPL-3.0-or-later); атрибуция сохранена
  в заголовке файла.
* `AGE Extractor/` — код проекта GTS, та же лицензия (`GPL-3.0-or-later`).

Инструменты можно свободно использовать, изучать, изменять и распространять при
условии сохранения этой же лицензии для производных работ.
