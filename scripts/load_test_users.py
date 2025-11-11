# scripts/load_test_users.py
# -*- coding: utf-8 -*-
"""
批量生成测试用户数据
用法:
    python scripts/load_test_users.py 50
默认生成 20 个用户
"""

import sys
from models.db import init_db, get_session
from models.user import get_or_create_user


def main(count: int = 20):
    init_db()
    with get_session() as s:
        for i in range(1, count + 1):
            tg_id = 90000 + i
            username = f"testuser{i}"
            u = get_or_create_user(s, tg_id=tg_id, username=username, lang="zh")
            print(f"✅ Created user {u.tg_id} ({u.username})")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    main(n)
