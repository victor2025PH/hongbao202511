# config/__init__.py
# 暴露 settings / is_admin（兼容现有导入）
from .settings import settings, is_admin  # noqa: F401
