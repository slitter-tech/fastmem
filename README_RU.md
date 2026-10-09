**Русский** | [English](README.md)

# fastmem

[![PyPI](https://img.shields.io/pypi/v/fastmem.svg)](https://pypi.org/project/fastmem/)
[![Python](https://img.shields.io/pypi/pyversions/fastmem.svg)](https://pypi.org/project/fastmem/)
[![CI](https://github.com/slitter-tech/fastmem/actions/workflows/ci.yml/badge.svg)](https://github.com/slitter-tech/fastmem/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Быстрое чтение памяти процессов Windows. Только `ctypes` и стандартная
библиотека. C-расширение собирается автоматически, если есть компилятор,
но никогда не требуется.

**Компилятор не нужен.** Wheels под все поддерживаемые версии Python и все
архитектуры собираются на GitHub и выкладываются на PyPI, поэтому
`pip install fastmem` обычно ставит готовый бинарник. Компилятор нужен
только при установке из sdist, и даже тогда библиотека работает без него.

Тот же движок есть ещё и как отдельная C-библиотека с header-only C++
обёрткой — см. [docs/C_API.md](docs/C_API.md).

Инструмент для reverse engineering, отладки, memory forensics и security
research.

## Установка

```bash
pip install fastmem
```

Из исходников, с локальной сборкой C-расширения:

```bash
git clone https://github.com/slitter-tech/fastmem.git
cd fastmem
pip install -e .
```

## Быстрый старт

```python
from fastmem import Process

with Process(pid) as p:
    print(p.machine_name, p.pointer_size * 8, "бит")
    data = p.read(address, 32)              # bytes
    value = p.read(address, as_="float")    # число
    obj = p.read(address)                   # размер указателя
```

В горячих циклах — `or_none=True`, возвращает `None` вместо исключения:

```python
hit = p.read(address, 8, or_none=True)
```

## API

Восемь методов покрывают всё.

| Метод | Результат | Для чего |
|---|---|---|
| `read(addr, size=None, as_=None, or_none=False)` | `bytes`, число или `None` | одиночные чтения |
| `read_many(addrs, size=None, as_=None, into=False, span=-1, threads=0)` | `list` или `bytearray` | **пачки** |
| `read_region(base, size, chunks=0, or_none=False)` | `bytes` или генератор | **сканеры** |
| `regions(min_size=0, max_size=0, ...)` | генератор `Region` | обход виртуальной памяти |
| `find(pattern, regions=None, align=1, limit=0, chunk_size=0)` | генератор адресов | **поиск значений** |
| `query(addr)` | `Region` или `None` | описание одного региона |
| `is_alive()` | `bool` | живость процесса |
| `close()` | - | освобождение handle |

### Чтение

`size=None` означает размер указателя целевого процесса, поэтому
`read(addr)` читает указатель напрямую. `as_` интерпретирует байты как
число:

| `as_` | Значение |
|---|---|
| `None` | сырые `bytes` |
| `'ptr'` | указатель по разрядности **цели** (8 на x64/ARM64, 4 на x86) |
| `'int'`, `'uint'` | беззнаковое 32-битное |
| `'i32'`, `'u32'` | знаковое / беззнаковое 32-битное |
| `'i64'`, `'u64'` | знаковое / беззнаковое 64-битное |
| `'i8'`, `'u8'`, `'i16'`, `'u16'` | узкие целые |
| `'float'`, `'f32'` | 32-битный float |
| `'double'`, `'f64'` | 64-битный float |
| `'long'`, `'ulong'` | беззнаковое 64-битное |

`int` и `long` беззнаковые намеренно: чтение памяти — это про сырые
значения (хеши, флаги, указатели), и `0xDEADBEEF`, вернувшийся как
`-559038737`, удивляет всех. Для знаковой интерпретации используйте
`i32` / `i64`.

```python
with Process(pid) as p:
    hp = p.read(addr, as_="float")
    n = p.read(addr, as_="int")
    ok = p.read(addr, as_="ptr", or_none=True)
```

### Пачки

```python
with Process(pid) as p:
    values = p.read_many(addrs, as_="ptr")   # list[int | None]
    raw = p.read_many(addrs, into=True)      # один bytearray, нули на сбоях
```

`span` объединяет адреса ближе, чем `span` байт, и читает каждую группу
одним вызовом. `-1` (по умолчанию) — четыре страницы, `0` отключает
склейку. Близкие адреса — обычное дело в реальных задачах: поля объекта,
элементы массива, узлы списка, — и склейка до **67x** быстрее поточного
чтения.

`threads` раскидывает адреса по рабочим потокам и требует C-расширения.
Потоки живут в C, создаются при первом обращении и переиспользуются, то
есть платится один раз, а не на каждом вызове. Реальный выигрыш —
**1.5x на 2 потоках и 2.6x на 4** на адресах, слишком разреженных для
склейки. Склейка всё равно выигрывает везде, где применима, поэтому
сочетайте `threads` с `span=0` для разреженных адресов.

### Регионы

```python
with Process(pid) as p:
    for reg in p.regions(max_size=64 << 20):
        blob = p.read_region(reg.base, reg.size, or_none=True)
        if blob:
            scan(blob)

    # потоковый обход большого региона без удержания его в памяти
    for chunk in p.read_region(base, size, chunks=1 << 20):
        scan(chunk)
```

### Поиск

```python
import struct
from fastmem import Process

needle = struct.pack("<Q", 0x1A2B3C4D5E6F7788)

with Process(pid) as p:
    for addr in p.find(needle, align=8, limit=1000):
        print(hex(addr))
```

Поиск идёт в C через `bytes.find`: находка в мегабайтном регионе занимает
~0.3 мкс, тогда как Python-цикл по байтам отстаёт на пять порядков.
`chunk_size` читает перекрывающимися блоками, чтобы не потерять вхождения
на стыке.

## Свойства цели

Определяются автоматически через `IsWow64Process2` → `IsWow64Process` →
архитектуру системы, поэтому `as_='ptr'` следует за **целью**, а не за
хостом.

```python
p.pointer_size   # 4 или 8
p.is_64bit       # bool
p.machine_name   # 'x86', 'x64', 'ARM64', ...
p.page_size      # из GetSystemInfo
p.backend()      # 'c-extension' или 'python-ctypes'
```

## Примеры

Обход цепочки указателей:

```python
with Process(pid) as p:
    node = p.read(root, as_="ptr")
    chain = []
    while node and len(chain) < 100:
        chain.append(node)
        node = p.read(node, or_none=True)
        if node:
            node = int.from_bytes(node, "little")
    print("длина цепочки:", len(chain))
```

Выгрузка всех читаемых регионов:

```python
with Process(pid) as p:
    total = 0
    for reg in p.regions(max_size=64 << 20):
        blob = p.read_region(reg.base, reg.size, or_none=True)
        if blob:
            total += len(blob)
    print("прочитано", total, "байт")
```

## Совместимость

* Windows 7, 8, 8.1, 10, 11, Server 2012+; x86, x64, ARM64.
* Python 3.9-3.13. PyPy работает на чистом Python.
* 32- и 64-битные цели, в том числе WOW64.
* Защищённые процессы (PPL) и PID 4 (System): при нехватке прав —
  `ProcessOpenError` с понятным текстом и кодом Windows, без краша.
* Процесс, умирающий во время чтения: `or_none=True` даёт `None`, обычный
  `read` бросает `ProcessTerminatedError` (наследник `ReadMemoryError`).
* Минимальные права доступа по умолчанию:
  `PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION` — работает без
  прав администратора для процессов текущего пользователя. Свой набор:
  `Process(pid, access=...)`.
* Никаких оффсетов структур Windows и никакого ассемблера: только
  публичные API, каждое проверяется на наличие.
* Размер страницы берётся из `GetSystemInfo`, а не предполагается.

## Потокобезопасность

Экземпляр `Process` **не потокобезопасен** (внутри переиспользуемый
буфер). Для ручного чтения в потоках открывайте по одному `Process` на
поток. Исключение — `read_many`: пул рабочих потоков общий, защищён
блокировкой и пересоздаётся по необходимости, поэтому вызывать его из
нескольких Python-потоков одновременно безопасно.

## Производительность

Python 3.11, 4 ядра, чужой процесс, 8 байт на адрес. Колонка «Python» —
та же работа через `ctypes` без расширения, то есть отличается только
место, где крутится цикл.

| Операция | Python | C | Ускорение |
|---|---|---|---|
| `read()` в try/except, битый адрес | 10.46 | - | - |
| `read(or_none=True)`, битый адрес | 3.55 | - | - |
| `read()` успех | 3.53 | - | - |
| `read(as_='int')` | 5.25 | - | - |
| пачка в `bytearray` | 5.08 | 1.40 | 3.62x |
| пачка в `list[bytes]` | 5.82 | 1.36 | 4.27x |
| пачка в `list[int]` | 5.29 | 1.47 | 3.59x |
| **склейка против поточного** | 5.12 | **0.076** | **67.2x** |

Крупные блоки, по одному вызову API:

| | мкс | МБ/с |
|---|---|---|
| `read_region(4 КиБ)` | 9.7 | 421 |
| `read_region(64 КиБ)` | 22.0 | 2977 |
| `read_region(1 МиБ)` | 817 | 1284 |

### Откуда берётся скорость

1. **Размер блока важнее языка.** Один `read_region(64 КиБ)` — ~22 мкс;
   то же самое по адресам — ~150 мс.
2. **Поиск должен быть в C.** `bytes.find` против Python-цикла по байтам
   — пять порядков разницы.
3. **Аллокации обнуляют память.** `(ctypes.c_char * n)()` делает memset:
   ~400 мкс на 1 МиБ. Переиспользуемый буфер это убирает.
4. **Исключения дорогие.** Формирование текста ошибки стоило 3.3 мкс; теперь
   текст строится лениво в `__str__`.
5. **C — это один переход вместо тысячи.** Вызов из Python в C стоит
   ~0.6 мкс, и пачка из 1000 адресов в Python платит эту цену тысячу
   раз. Цикл на C пересекает границу один раз.
6. **Передача данных в C тоже стоит денег.** `(ctypes.c_size_t * n)(*addrs)`
   занимала 330 мкс на 2000 адресов; `array.array` заполняет тот же буфер
   за 66 мкс. Само по себе это 12% многопоточного чтения.

### Потоки

Замеры на чужом процессе, без склейки, мкс на адрес:

| | 1 поток | 2 | 4 |
|---|---|---|---|
| C, пул потоков | 1.28 | 0.85 (1.51x) | 0.50 (2.57x) |
| то же, в `bytearray` | 1.26 | 0.96 (1.32x) | 0.54 (2.32x) |
| то же, разреженные адреса | 1.24 | 0.93 (1.33x) | 0.69 (1.80x) |

Раньше потоки были чистым минусом: создание объектов `threading.Thread` на
каждый вызов стоило ~812 мкс на Windows — больше, чем сама работа
~1700 мкс, которую они должны были распараллелить. Поэтому четыре потока
давали 1.11x на плотных адресах и 0.20x на разреженных, то есть хуже, чем
вообще без потоков. Теперь рабочие потоки живут в C, создаются при первом
обращении и паркуются на событии между вызовами — именно это превратило
те цифры в 2.57x и 1.80x. Повторные прогоны дают по четырём потокам
2.57-2.71x: абсолютные времена гуляют вместе с нагрузкой машины, а
соотношения — нет.

Контрольная группа подтверждает механизм: вариант, удерживающий GIL, на
четырёх потоках даёт ровно ничего (1617 мкс против 1605 у одного), значит
ограничитель — GIL, а не конкуренция внутри ядра. Пул отпускает GIL на всё
время чтения.

Читающий поток тоже берёт часть работы, поэтому `threads=4` означает
пятерых читателей. По той же причине `threads` ограничен
`len(addrs) - 1`.

Предпочитайте склейку для плотных адресов: один вызов на окно бьёт один
вызов на адрес сильнее, чем могут отыграть потоки. Потоки — когда адреса
слишком разрежены для склейки.

## Сборка из исходников

```bash
python setup.py build_ext --inplace   # необязательно
python -m tests.test_fastmem          # функциональные тесты
python -m tests.benchmarks            # бенчмарки, свой процесс
python -m tests.benchmarks --foreign  # бенчмарки, чужой процесс
```

Сборке расширения нужны MSVC и Windows SDK. SDK не всегда лежит в
дефолтном пути, поэтому `tools/msvc_paths.py` ищет его через
`WindowsSdkDir` и типовые расположения, включая `G:\Windows Kits\10`. На
некоторых сборках Python нет `pythonXY.lib` (сборка без
`Py_ENABLE_SHARED`) — тогда импортовая библиотека генерируется из
таблицы экспортов DLL через
`dumpbin` и `lib.exe`.

Переменная `FASTMEM_BUILD_ARCH` со значением `x86` или `arm64` включает
кросс-компиляцию с x64-машины — так CI собирает все три архитектуры на
одном раннере.

## C и C++

Тот же движок есть и без Python:

```bat
python tools\build_native.py --example
```

- `csrc/fastmem.h`, `csrc/fastmem.c` — чистый C99, только kernel32
- `cpp/include/fastmem/fastmem.hpp` — header-only обёртка на C++17

```cpp
#include <fastmem/fastmem.hpp>

fastmem::Process p(1234);
auto hp   = p.read<uintptr_t>(addr);
auto blob = p.read_region(base, size);
for (auto &r : p.readable_regions()) { /* ... */ }
```

Подробнее в [docs/C_API.md](docs/C_API.md).

## Лицензия

MIT. См. [LICENSE](LICENSE).
