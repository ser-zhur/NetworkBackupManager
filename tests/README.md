# Тесты Network Backup Manager

## Установка

```bash
pip install -r requirements-dev.txt


# Создать репу с 100 коммитами в /tmp/nbm-test
python tests/gen_history.py /tmp/nbm-test 100 --fresh

# Теперь можно открыть её в браузере (если она в backups/)
# или работать с ней из REPL / тестов


# Все тесты
pytest

# Конкретный файл
pytest tests/test_smoke.py -v

# Конкретный тест
pytest tests/test_smoke.py::test_create_and_read_history -v

# С выводом логов
pytest -s

# Только «медленные» тесты
pytest -m slow


tests/
├── conftest.py            фикстуры (make_repo и др.)
├── gen_history.py         генератор истории для ручной отладки
├── fixtures/
│   ├── sample-old.rsc     образец старого конфига
│   └── sample-new.rsc     образец нового конфига
├── test_smoke.py          smoke-тесты существующего API
├── test_git_delete.py     тесты удаления (по мере реализации)
└── test_retention.py      тесты retention (по мере реализации)


Ручная отладка через REPL
bash

python tests/gen_history.py /tmp/nbm-test 100 --fresh

