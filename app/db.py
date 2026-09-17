"""SQLite 数据库初始化与连接管理。"""
import os
import json
import sqlite3
from datetime import datetime

# 数据库路径：Docker 下通过环境变量指向挂载卷 /data/ledger.db，本地默认 ./data/ledger.db
DEFAULT_DB = os.environ.get(
    "LEDGER_DB",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "ledger.db"),
)


def db_path() -> str:
    p = DEFAULT_DB
    d = os.path.dirname(p)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)
    return p


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT NOT NULL,
            amount      REAL NOT NULL,
            direction   TEXT,
            category    TEXT,
            subcategory TEXT,
            account     TEXT,
            counterparty TEXT,
            note        TEXT,
            source      TEXT,
            raw         TEXT,
            created_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tx_date     ON transactions(date);
        CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category);
        CREATE INDEX IF NOT EXISTS idx_tx_dir      ON transactions(direction);

        CREATE TABLE IF NOT EXISTS categories (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT UNIQUE NOT NULL,
            type     TEXT,
            keywords TEXT,
            icon     TEXT,
            color    TEXT
        );

        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            created_at TEXT,
            expires_at TEXT
        );
        """
    )
    conn.commit()

    # ---- 兼容旧库的轻量迁移 ----
    # 老版本 sessions 表没有 username 列。已有 NAS 上跑着旧库，这里补列，
    # 避免为了加一个「会话归属账户名」就要求用户删库重建。
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "username" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN username TEXT")
        conn.commit()

    # 仅当分类表为空时播种默认分类（含关键词规则，供规则引擎使用）
    from .categories import DEFAULT_CATEGORIES

    cur = conn.execute("SELECT COUNT(*) AS c FROM categories")
    if cur.fetchone()["c"] == 0:
        for c in DEFAULT_CATEGORIES:
            conn.execute(
                "INSERT INTO categories (name, type, keywords, icon, color) "
                "VALUES (?,?,?,?,?)",
                (
                    c["name"],
                    c["type"],
                    json.dumps(c["keywords"], ensure_ascii=False),
                    c.get("icon", "●"),
                    c.get("color", "#888888"),
                ),
            )
        conn.commit()
    conn.close()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
