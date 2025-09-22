import os
import sys

# Добавляем корень проекта в PYTHONPATH, чтобы импортировать botApp
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Для тестов используем локальный тестовый DB_PATH
TEST_DB = os.path.join(os.path.dirname(__file__), "test.db")
os.environ.setdefault("DB_PATH", TEST_DB)

