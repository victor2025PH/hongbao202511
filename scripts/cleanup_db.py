# scripts/cleanup_db.py
# -*- coding: utf-8 -*-
"""
快速清理数据库中所有表数据
⚠️ 仅限开发测试使用，生产环境请勿运行！
"""

from models.db import init_db, engine, Base


def main():
    init_db()
    confirm = input("⚠️ WARNING: This will DROP ALL tables. Continue? (y/N): ")
    if confirm.lower() != "y":
        print("❌ Cancelled.")
        return

    Base.metadata.drop_all(engine)
    print("🗑️ All tables dropped.")

    Base.metadata.create_all(engine)
    print("✅ Database re-created (empty).")


if __name__ == "__main__":
    main()
