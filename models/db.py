# models/db.py
# -*- coding: utf-8 -*-
"""
数据库基座：
- SQLAlchemy Engine / Session / Base
- 兼容 SQLite / Postgres / MySQL
- 提供 init_db() 初始化与 get_session() 上下文管理器

新增/加强（本次改动要点）：
1) 新增强幂等表：gsheet_membership_logged(chat_id BIGINT, user_id BIGINT, PRIMARY KEY(chat_id, user_id))
   - 跨进程/重启也能保证 “每群每人仅一条” 的写表唯一性
   - 提供 mark_member_logged_once / is_member_logged / clear_member_logged 三个对外函数
2) DECIMAL(scale=6) 工厂：在 SQLite 用 TEXT 存 Decimal，读回再转 Decimal；
   在非 SQLite（Postgres/MySQL）用 NUMERIC(18, scale, asdecimal=True)。
3) 轻量迁移 _ensure_column()：在 Base.create_all() 之后，为历史库自动补齐缺失列。
   envelopes 表补齐：
     - is_finished
     - mvp_dm_sent
     - cover_channel_id / cover_message_id / cover_file_id / cover_meta
   users 表补齐（与最新模型一致）：
     - usdt_balance / ton_balance / point_balance / energy_balance
     - last_target_chat_id / last_target_chat_title / language / role
   recharge_orders 表补齐（与最新模型一致）：
     - finished_at / tx_hash / note / pay_address / pay_currency / pay_amount
     - network / invoice_id / payment_id / payment_url / purchase_id / qr_b64
"""
from __future__ import annotations
from contextlib import contextmanager
from typing import Iterator
from decimal import Decimal
import logging
import time
import os

from sqlalchemy import create_engine, event, text, inspect
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from sqlalchemy.types import TypeDecorator, String, Numeric

log = logging.getLogger("db")

# ---------- Engine ----------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data.sqlite").strip()

_is_sqlite = DATABASE_URL.startswith("sqlite")
_engine_kwargs = {
    "echo": False,
    "future": True,
    "pool_pre_ping": True,
}
# SQLite 在多线程（例如 aiogram webhook / polling）下需要关闭同线程检查
if _is_sqlite:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **_engine_kwargs)

# SQLite: 开启外键支持
if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):  # pragma: no cover
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()

# ---------- Base & Session ----------
Base = declarative_base()

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,  # ✅ 提交后不使对象过期，避免 DetachedInstanceError
    future=True,
)

@contextmanager
def get_session() -> Iterator[Session]:
    """
    用法：
        with get_session() as s:
            # 读写操作
            s.commit()  # 建议显式提交
    行为：
        - 正常退出：若调用方未显式提交，这里会做一次兜底 commit()
        - 异常退出：rollback 并继续抛出异常
    """
    session: Session = SessionLocal()
    try:
        yield session
        # 调用方可能已手动提交；这里再提交一次作为兜底（无害）
        try:
            session.commit()
        except Exception:
            # 若这里提交失败，交由 except 分支统一回滚
            raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

# 兼容：某些旧代码可能从 db 导入 session_scope
session_scope = get_session

# ✅ 新增：FastAPI 依赖（yield 版 Session）
def get_db():
    """
    FastAPI 依赖用法：
        def endpoint(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
        # 这里不做 commit，交由调用端按需决定；若已经 commit 也不冲突
    finally:
        db.close()

# ---------- Decimal 安全类型（解决 SQLite 精度/告警问题） ----------
class _SqliteDecimal(TypeDecorator):
    """
    在 SQLite 上使用 TEXT 保存 Decimal，避免浮点转换误差。
    仅在 SQLite 方言下生效；其他方言不应使用此类型。
    """
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        # 统一转为字符串表示；支持传入 str/float/Decimal
        return str(Decimal(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return Decimal(value)

def DECIMAL(scale: int = 6):
    """
    列类型工厂：
      - 在 SQLite 使用 _SqliteDecimal()（TEXT）保证精度与无告警
      - 在其他数据库使用 Numeric(18, scale, asdecimal=True)
    用法示例（模型中）：
      amount = Column(DECIMAL(6), nullable=False)
    """
    if _is_sqlite:
        return _SqliteDecimal()
    return Numeric(18, scale, asdecimal=True)

# ---------- 轻量迁移工具 ----------
def _column_exists(table: str, column: str) -> bool:
    """
    使用 SQLAlchemy Inspector 检查列是否存在（兼容 SQLite/Postgres/MySQL）
    """
    try:
        insp = inspect(engine)
        cols = insp.get_columns(table)
        names = {c["name"] for c in cols}
        return column in names
    except Exception as e:
        log.warning("inspect columns failed: table=%s err=%s", table, e)
        # 退化到 SQLite PRAGMA（若为 sqlite）
        if _is_sqlite:
            with engine.connect() as conn:
                rows = conn.execute(text(f"PRAGMA table_info({table})")).mappings().all()
                return any(r["name"] == column for r in rows)
        return False

def _ensure_column(table: str, ddl_by_dialect: dict) -> None:
    """
    若指定列不存在，则执行 "ALTER TABLE {table} ADD COLUMN {DDL}"。
    ddl_by_dialect:
        {
            "sqlite":   "is_finished INTEGER NOT NULL DEFAULT 0",
            "postgres": "is_finished BOOLEAN NOT NULL DEFAULT FALSE",
            "mysql":    "is_finished TINYINT(1) NOT NULL DEFAULT 0",
            "default":  "is_finished BOOLEAN NOT NULL DEFAULT 0"
        }
    说明：
    - 通过示例 DDL 的第一个 token 自动推断列名（如: is_finished）。
    - 可安全重复执行：存在即跳过；并发冲突时捕获并仅记录 warning。
    """
    # 从任意一个 DDL 字符串里解析列名（第一个 token）
    sample = next(iter(ddl_by_dialect.values()))
    col_name = sample.split()[0]

    if _column_exists(table, col_name):
        return

    name = engine.dialect.name.lower()
    if name == "postgresql":
        key = "postgres"
    elif name == "mysql":
        key = "mysql"
    elif name == "sqlite":
        key = "sqlite"
    else:
        key = "default"

    ddl = ddl_by_dialect.get(key) or ddl_by_dialect.get("default")
    if not ddl:
        log.warning("no DDL provided for dialect=%s table=%s", name, table)
        return

    try:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
        log.info("migrate: add column %s to %s done (dialect=%s)", col_name, table, name)
    except Exception as e:
        # 若并发或重复执行导致失败，给出告警但不中断主流程
        log.warning("migrate: add column failed (maybe exists): table=%s col=%s err=%s",
                    table, col_name, e)

# ---------- 初始化 ----------
def init_db() -> None:
    """
    创建所有模型对应的数据表。
    生产环境建议使用 Alembic 迁移，这里仅用于快速落地。
    """
    # 在此处导入模型，确保 Base 已知所有表，否则不会创建
    import models.user      # noqa: F401
    import models.envelope  # noqa: F401
    import models.invite    # noqa: F401
    import models.ledger    # noqa: F401
    import models.recharge  # noqa: F401
    if (os.getenv("FLAG_ENABLE_PUBLIC_GROUPS", "").strip().lower() in {"1", "true", "yes", "on"}):
        import models.public_group  # noqa: F401
    # 封面表（如果你已根据方案增加了 models/cover.py，这里会一并创建）
    try:
        import models.cover   # noqa: F401
    except Exception:
        # 若未提供 cover 模型，忽略，不影响其他表创建
        pass

    # 先创建已知结构
    Base.metadata.create_all(bind=engine)

    # ✅ 轻量迁移：为历史库补齐 envelopes 表的新增列
    _ensure_column(
        table="envelopes",
        ddl_by_dialect={
            "sqlite":   "is_finished INTEGER NOT NULL DEFAULT 0",
            "postgres": "is_finished BOOLEAN NOT NULL DEFAULT FALSE",
            "mysql":    "is_finished TINYINT(1) NOT NULL DEFAULT 0",
            "default":  "is_finished BOOLEAN NOT NULL DEFAULT 0",
        },
    )
    _ensure_column(
        table="envelopes",
        ddl_by_dialect={
            "sqlite":   "mvp_dm_sent INTEGER NOT NULL DEFAULT 0",
            "postgres": "mvp_dm_sent BOOLEAN NOT NULL DEFAULT FALSE",
            "mysql":    "mvp_dm_sent TINYINT(1) NOT NULL DEFAULT 0",
            "default":  "mvp_dm_sent BOOLEAN NOT NULL DEFAULT 0",
        },
    )
    _ensure_column(
        table="envelopes",
        ddl_by_dialect={
            "sqlite":   "cover_channel_id INTEGER",
            "postgres": "cover_channel_id BIGINT",
            "mysql":    "cover_channel_id BIGINT",
            "default":  "cover_channel_id BIGINT",
        },
    )
    _ensure_column(
        table="envelopes",
        ddl_by_dialect={
            "sqlite":   "cover_message_id INTEGER",
            "postgres": "cover_message_id BIGINT",
            "mysql":    "cover_message_id BIGINT",
            "default":  "cover_message_id BIGINT",
        },
    )
    _ensure_column(
        table="envelopes",
        ddl_by_dialect={
            "sqlite":   "cover_file_id TEXT",
            "postgres": "cover_file_id TEXT",
            "mysql":    "cover_file_id TEXT",
            "default":  "cover_file_id TEXT",
        },
    )
    _ensure_column(
        table="envelopes",
        ddl_by_dialect={
            "sqlite":   "cover_meta TEXT",
            "postgres": "cover_meta JSONB",
            "mysql":    "cover_meta JSON",
            "default":  "cover_meta TEXT",
        },
    )

    # ✅ 轻量迁移：为历史库补齐 users 表关键列
    _ensure_column(
        table="users",
        ddl_by_dialect={
            "sqlite":   "usdt_balance TEXT",        # Decimal 在 SQLite 用 TEXT
            "postgres": "usdt_balance NUMERIC(18,6) DEFAULT 0",
            "mysql":    "usdt_balance DECIMAL(18,6) DEFAULT 0",
            "default":  "usdt_balance NUMERIC(18,6) DEFAULT 0",
        },
    )
    _ensure_column(
        table="users",
        ddl_by_dialect={
            "sqlite":   "ton_balance TEXT",
            "postgres": "ton_balance NUMERIC(18,6) DEFAULT 0",
            "mysql":    "ton_balance DECIMAL(18,6) DEFAULT 0",
            "default":  "ton_balance NUMERIC(18,6) DEFAULT 0",
        },
    )
    _ensure_column(
        table="users",
        ddl_by_dialect={
            "sqlite":   "point_balance INTEGER DEFAULT 0",
            "postgres": "point_balance INTEGER DEFAULT 0",
            "mysql":    "point_balance INT DEFAULT 0",
            "default":  "point_balance INTEGER DEFAULT 0",
        },
    )
    _ensure_column(
        table="users",
        ddl_by_dialect={
            "sqlite":   "energy_balance INTEGER DEFAULT 0",
            "postgres": "energy_balance INTEGER DEFAULT 0",
            "mysql":    "energy_balance INT DEFAULT 0",
            "default":  "energy_balance INTEGER DEFAULT 0",
        },
    )
    _ensure_column(
        table="users",
        ddl_by_dialect={
            "sqlite":   "last_target_chat_id INTEGER",
            "postgres": "last_target_chat_id BIGINT",
            "mysql":    "last_target_chat_id BIGINT",
            "default":  "last_target_chat_id BIGINT",
        },
    )
    _ensure_column(
        table="users",
        ddl_by_dialect={
            "sqlite":   "last_target_chat_title TEXT",
            "postgres": "last_target_chat_title VARCHAR(128)",
            "mysql":    "last_target_chat_title VARCHAR(128)",
            "default":  "last_target_chat_title VARCHAR(128)",
        },
    )
    _ensure_column(
        table="users",
        ddl_by_dialect={
            "sqlite":   "language VARCHAR(8)",
            "postgres": "language VARCHAR(8)",
            "mysql":    "language VARCHAR(8)",
            "default":  "language VARCHAR(8)",
        },
    )
    _ensure_column(
        table="users",
        ddl_by_dialect={
            "sqlite":   "role VARCHAR(16) DEFAULT 'user'",  # ENUM 在迁移里以 VARCHAR 兜底
            "postgres": "role VARCHAR(16) DEFAULT 'user'",
            "mysql":    "role VARCHAR(16) DEFAULT 'user'",
            "default":  "role VARCHAR(16) DEFAULT 'user'",
        },
    )

    # ✅ 轻量迁移：为历史库补齐 recharge_orders 表关键列
    _ensure_column(
        table="recharge_orders",
        ddl_by_dialect={
            "sqlite":   "finished_at DATETIME",
            "postgres": "finished_at TIMESTAMP",
            "mysql":    "finished_at DATETIME",
            "default":  "finished_at TIMESTAMP",
        },
    )
    _ensure_column(
        table="recharge_orders",
        ddl_by_dialect={
            "sqlite":   "tx_hash VARCHAR(128)",
            "postgres": "tx_hash VARCHAR(128)",
            "mysql":    "tx_hash VARCHAR(128)",
            "default":  "tx_hash VARCHAR(128)",
        },
    )
    _ensure_column(
        table="recharge_orders",
        ddl_by_dialect={
            "sqlite":   "note VARCHAR(255)",
            "postgres": "note VARCHAR(255)",
            "mysql":    "note VARCHAR(255)",
            "default":  "note VARCHAR(255)",
        },
    )
    _ensure_column(
        table="recharge_orders",
        ddl_by_dialect={
            "sqlite":   "pay_address VARCHAR(128)",
            "postgres": "pay_address VARCHAR(128)",
            "mysql":    "pay_address VARCHAR(128)",
            "default":  "pay_address VARCHAR(128)",
        },
    )
    _ensure_column(
        table="recharge_orders",
        ddl_by_dialect={
            "sqlite":   "pay_currency VARCHAR(32)",
            "postgres": "pay_currency VARCHAR(32)",
            "mysql":    "pay_currency VARCHAR(32)",
            "default":  "pay_currency VARCHAR(32)",
        },
    )
    _ensure_column(
        table="recharge_orders",
        ddl_by_dialect={
            "sqlite":   "pay_amount TEXT",  # 与模型保持 String 存储，避免方言差异
            "postgres": "pay_amount VARCHAR(32)",
            "mysql":    "pay_amount VARCHAR(32)",
            "default":  "pay_amount VARCHAR(32)",
        },
    )
    _ensure_column(
        table="recharge_orders",
        ddl_by_dialect={
            "sqlite":   "network VARCHAR(32)",
            "postgres": "network VARCHAR(32)",
            "mysql":    "network VARCHAR(32)",
            "default":  "network VARCHAR(32)",
        },
    )
    _ensure_column(
        table="recharge_orders",
        ddl_by_dialect={
            "sqlite":   "invoice_id VARCHAR(64)",
            "postgres": "invoice_id VARCHAR(64)",
            "mysql":    "invoice_id VARCHAR(64)",
            "default":  "invoice_id VARCHAR(64)",
        },
    )
    _ensure_column(
        table="recharge_orders",
        ddl_by_dialect={
            "sqlite":   "payment_id VARCHAR(64)",
            "postgres": "payment_id VARCHAR(64)",
            "mysql":    "payment_id VARCHAR(64)",
            "default":  "payment_id VARCHAR(64)",
        },
    )
    _ensure_column(
        table="recharge_orders",
        ddl_by_dialect={
            "sqlite":   "payment_url TEXT",
            "postgres": "payment_url VARCHAR(255)",
            "mysql":    "payment_url VARCHAR(255)",
            "default":  "payment_url VARCHAR(255)",
        },
    )
    _ensure_column(
        table="recharge_orders",
        ddl_by_dialect={
            "sqlite":   "purchase_id VARCHAR(64)",
            "postgres": "purchase_id VARCHAR(64)",
            "mysql":    "purchase_id VARCHAR(64)",
            "default":  "purchase_id VARCHAR(64)",
        },
    )
    _ensure_column(
        table="recharge_orders",
        ddl_by_dialect={
            "sqlite":   "qr_b64 TEXT",
            "postgres": "qr_b64 TEXT",
            "mysql":    "qr_b64 TEXT",
            "default":  "qr_b64 TEXT",
        },
    )

    # ✅ 新增：创建强幂等表（若不存在）
    _ensure_membership_table()

    # 可选：简单自检（查询一次确保连通）
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

# ---------- 强幂等：gsheet_membership_logged ----------
def _ensure_membership_table() -> None:
    """
    创建 gsheet_membership_logged 表（若不存在）。
    结构：chat_id BIGINT, user_id BIGINT, PRIMARY KEY(chat_id, user_id)
    """
    name = engine.dialect.name.lower()
    if name == "postgresql":
        ddl = """
        CREATE TABLE IF NOT EXISTS gsheet_membership_logged (
            chat_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        )
        """
    elif name == "mysql":
        ddl = """
        CREATE TABLE IF NOT EXISTS gsheet_membership_logged (
            chat_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        ) ENGINE=InnoDB
        """
    else:  # sqlite / default
        ddl = """
        CREATE TABLE IF NOT EXISTS gsheet_membership_logged (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        )
        """

    try:
        with engine.begin() as conn:
            conn.execute(text(ddl))
        log.info("ensure table gsheet_membership_logged ok (dialect=%s)", name)
    except Exception as e:
        log.warning("ensure table gsheet_membership_logged failed: %s", e)

def mark_member_logged_once(chat_id: int, user_id: int) -> bool:
    """
    尝试标记 (chat_id, user_id) 为“已写入 Google 表格”。
    - 插入成功 => 返回 True
    - 已存在/违反唯一约束 => 返回 False
    """
    name = engine.dialect.name.lower()
    if name == "postgresql":
        sql = """
        INSERT INTO gsheet_membership_logged (chat_id, user_id)
        VALUES (:chat_id, :user_id)
        ON CONFLICT (chat_id, user_id) DO NOTHING
        """
    elif name == "mysql":
        sql = """
        INSERT IGNORE INTO gsheet_membership_logged (chat_id, user_id)
        VALUES (:chat_id, :user_id)
        """
    else:  # sqlite & default
        sql = """
        INSERT OR IGNORE INTO gsheet_membership_logged (chat_id, user_id)
        VALUES (:chat_id, :user_id)
        """

    try:
        with engine.begin() as conn:
            r = conn.execute(text(sql), {"chat_id": int(chat_id), "user_id": int(user_id)})
            # sqlite/mysql: IGNORE 发生重复时 rowcount=0；postgres on conflict do nothing 同理
            return getattr(r, "rowcount", 0) > 0
    except Exception as e:
        # 若出现临时异常，为避免阻塞主流程，降级为“未成功标记”（让上层自行决定是否继续）
        log.warning("mark_member_logged_once failed: chat_id=%s user_id=%s err=%s",
                    chat_id, user_id, e)
        return False

def is_member_logged(chat_id: int, user_id: int) -> bool:
    """
    查询 (chat_id, user_id) 是否已被标记。
    """
    try:
        with engine.begin() as conn:
            r = conn.execute(
                text(
                    "SELECT 1 FROM gsheet_membership_logged "
                    "WHERE chat_id=:chat_id AND user_id=:user_id LIMIT 1"
                ),
                {"chat_id": int(chat_id), "user_id": int(user_id)},
            ).first()
            return bool(r)
    except Exception as e:
        log.warning("is_member_logged failed: chat_id=%s user_id=%s err=%s", chat_id, user_id, e)
        return False

def clear_member_logged(chat_id: int, user_id: int) -> int:
    """
    运维辅助：删除 (chat_id, user_id) 的标记（返回删除的行数）。
    """
    try:
        with engine.begin() as conn:
            r = conn.execute(
                text(
                    "DELETE FROM gsheet_membership_logged "
                    "WHERE chat_id=:chat_id AND user_id=:user_id"
                ),
                {"chat_id": int(chat_id), "user_id": int(user_id)},
            )
            return int(getattr(r, "rowcount", 0) or 0)
    except Exception as e:
        log.warning("clear_member_logged failed: chat_id=%s user_id=%s err=%s", chat_id, user_id, e)
        return 0

# ✅ 新增：显式导出，避免外部误导入错误对象
__all__ = [
    "engine",
    "Base",
    "SessionLocal",
    "get_session",
    "session_scope",
    "get_db",
    "DECIMAL",
    "init_db",
    "mark_member_logged_once",
    "is_member_logged",
    "clear_member_logged",
]
