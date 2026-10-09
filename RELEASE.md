# Публикация на PyPI

## Однократная настройка (нужно сделать руками)

Публикация идёт через **Trusted Publishing** (OIDC), поэтому токен
в секретах репозитория не нужен и хранить его не нужно.

1. Зарегистрировать проект на <https://pypi.org/project/fastmem/>.
   Если имя уже занято другим — придётся сменить `name` в
   `pyproject.toml`.

2. Открыть проект → **Manage → Publishing → Add a new publisher**.

3. Заполнить:

   | Поле | Значение |
   |---|---|
   | PyPI project name | `fastmem` |
   | Owner | `slitter-tech` |
   | Repository name | `fastmem` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

   Имя окружения обязано совпадать с тем, что указано в
   `.github/workflows/publish.yml` (секция `environment`). При
   создании окружения на GitHub отметьте **Prevent self-review** —
   пусть approve делает другой человек.

## Релиз

```bash
git tag v0.2.0
git push origin v0.2.0
```

Тег сам создаст GitHub Release, соберёт wheel и sdist и опубликует
их на PyPI. Версия в теге и в `pyproject.toml` сверяется в CI: при
расхождении публикация не состоится.

## Что собирается

| Артефакт | Содержимое |
|---|---|
| `fastmem-0.2.0-cp3X-cp3X-win_amd64.whl` | готовое C-расширение |
| `fastmem-0.2.0.tar.gz` | исходники, расширение собирается при установке |

Wheel собирается под тег или вручную (вкладка **Actions → CI →
Build wheels → Run workflow**).

## Проверка пакета

```bash
pip install fastmem
python -c "from fastmem import Process, backend; print(backend.backend_name())"
```

Ожидается `c-extension` для x64 и `python-ctypes` для x86 и ARM64:
wheel собирается только под x64, остальные платформы получат sdist и
соберут расширение сами либо останутся на чистом Python.

## Если нужно добавить платформу

Сборка идёт только на Windows. Чтобы добавить x86 или ARM64,
добавьте в `.github/workflows/ci.yml` в `build-wheels` отдельные
шаги `cibuildwheel` либо матрицу runner'ов. `pythonXY.lib`
генерируется автоматически там, где его нет.
