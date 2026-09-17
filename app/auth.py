"""本地鉴权：基于「账户名 + 密码」+ 会话 Cookie。零外部依赖（标准库实现）。

- 账户名存于 meta 表（key=auth_username），默认 admin，可随时在界面里改。
- 密码以 PBKDF2-HMAC-SHA256 加盐哈希存储于 meta 表（key=auth_password）。
- 首次启动若设置了环境变量 LEDGER_PASSWORD / LEDGER_USERNAME，则作为初始凭据写入 DB。
- 登录成功后签发随机会话令牌，存于 sessions 表（默认 30 天有效），通过 HttpOnly Cookie 下发。
- 未配置任何密码时（无环境变量且 DB 无记录）系统为「开放模式」，不拦截请求（向后兼容）。

设计取舍（为什么账户名也当作凭据校验）：
  单用户私有账本，账户名的意义是「标识这台机器上是谁的账 + 登录时多一道门槛」，
  不做多用户隔离。因此库里只保存**一个**账户名，登录时必须与库里的值一致。
"""
import os
import re
import hashlib
import secrets
from datetime import datetime, timedelta

from .db import get_conn

SESSION_DAYS = 30
_COOKIE = "ledger_session"

# 账户名存储位置与默认值
USERNAME_KEY = "auth_username"
DEFAULT_USERNAME = "admin"
# 长度 2-32，允许中英文、数字、下划线、点、短横线、@（禁止空格与特殊符号，避免前后端转义歧义）
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fa5.\-@]{2,32}$")


# ---------------- 密码哈希 ----------------
def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    d = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()
    return f"pbkdf2$sha256$100000${salt}${d}"


def verify_password(pw: str, stored: str) -> bool:
    if not stored or "$" not in stored:
        return False
    try:
        _, algo, it, salt, d = stored.split("$")
        calc = hashlib.pbkdf2_hmac(algo, pw.encode("utf-8"), salt.encode("utf-8"), int(it)).hex()
        return secrets.compare_digest(calc, d)
    except Exception:
        return False


# ---------------- 密码存储 ----------------
def seed_password_from_env() -> None:
    """启动时若设置了 LEDGER_PASSWORD 且 DB 尚无密码，则写入初始密码。"""
    env = os.environ.get("LEDGER_PASSWORD")
    if not env:
        return
    conn = get_conn()
    r = conn.execute("SELECT value FROM meta WHERE key='auth_password'").fetchone()
    if not (r and r["value"]):
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('auth_password', ?)",
            (hash_password(env),),
        )
        conn.commit()
    conn.close()


def get_stored_hash() -> str | None:
    conn = get_conn()
    r = conn.execute("SELECT value FROM meta WHERE key='auth_password'").fetchone()
    conn.close()
    return r["value"] if (r and r["value"]) else None


def is_auth_enabled() -> bool:
    return get_stored_hash() is not None


def set_password(pw: str) -> None:
    if not pw or len(pw) < 4:
        raise ValueError("密码至少 4 位")
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('auth_password', ?)",
        (hash_password(pw),),
    )
    conn.commit()
    conn.close()


# ---------------- 账户名 ----------------
def normalize_username(name) -> str:
    return (name or "").strip()


def get_username() -> str:
    """读取当前账户名；库里没有就返回默认值（不写库，读操作保持无副作用）。"""
    conn = get_conn()
    r = conn.execute("SELECT value FROM meta WHERE key=?", (USERNAME_KEY,)).fetchone()
    conn.close()
    return normalize_username(r["value"]) if (r and r["value"]) else DEFAULT_USERNAME


def validate_username(name) -> str:
    n = normalize_username(name)
    if not n:
        raise ValueError("账户名不能为空")
    if not _USERNAME_RE.match(n):
        raise ValueError("账户名需 2-32 位，只能用中英文、数字、下划线、点、短横线、@")
    return n


def set_username(name) -> str:
    """写入新账户名，并同步更新所有已存在会话上的账户名（改名后旧会话显示也会跟着变）。"""
    n = validate_username(name)
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (USERNAME_KEY, n))
    conn.execute("UPDATE sessions SET username=?", (n,))
    conn.commit()
    conn.close()
    return n


def verify_username(name) -> bool:
    """大小写不敏感 + 首尾空格忽略。

    注意：secrets.compare_digest 只能比对 ASCII，中文账户名直接传 str 会抛
    TypeError（表现为整站 500）。所以统一先 UTF-8 编码成 bytes 再比，
    既保留常数时间比较，也支持中文/任意 Unicode 账户名。
    """
    a = normalize_username(name).casefold()
    b = get_username().casefold()
    if not a:
        return False
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def seed_username_from_env() -> None:
    """启动时确保库里有账户名：优先用 LEDGER_USERNAME，否则写入默认 admin。

    只在 meta 里**还没有**账户名时写入——否则会出现「界面里改好了名字，
    一重启又被环境变量覆盖回去」的诡异现象。
    """
    env = normalize_username(os.environ.get("LEDGER_USERNAME"))
    try:
        name = validate_username(env) if env else DEFAULT_USERNAME
    except ValueError:
        name = DEFAULT_USERNAME          # 环境变量写得不合法就退回默认，不阻断启动
    conn = get_conn()
    r = conn.execute("SELECT value FROM meta WHERE key=?", (USERNAME_KEY,)).fetchone()
    if not (r and normalize_username(r["value"])):
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (USERNAME_KEY, name))
        conn.commit()
    conn.close()


# ---------------- 会话 ----------------
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def create_session(username: str = "") -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
    who = normalize_username(username) or get_username()   # 先取值，再开连接，避免连接套连接
    conn = get_conn()
    conn.execute(
        "INSERT INTO sessions(token, created_at, expires_at, username) VALUES(?,?,?,?)",
        (token, _now(), expires, who),
    )
    conn.commit()
    conn.close()
    return token


def session_username(token: str) -> str:
    """本次会话是以哪个账户名登录的（旧库上的历史会话可能为空，调用方自行兜底）。"""
    if not token:
        return ""
    conn = get_conn()
    r = conn.execute("SELECT username FROM sessions WHERE token=?", (token,)).fetchone()
    conn.close()
    return normalize_username(r["username"]) if r else ""


def destroy_other_sessions(keep_token: str = "") -> int:
    """清掉除当前会话之外的所有会话。

    改密码后调用：其它设备（或曾经借出去的浏览器）上已登录的会话立即失效，
    只有当前这台设备继续保持登录，不用重新输密码。
    """
    conn = get_conn()
    if keep_token:
        cur = conn.execute("DELETE FROM sessions WHERE token<>?", (keep_token,))
    else:
        cur = conn.execute("DELETE FROM sessions")
    n = cur.rowcount or 0
    conn.commit()
    conn.close()
    return n


def valid_session(token: str) -> bool:
    if not token:
        return False
    conn = get_conn()
    r = conn.execute(
        "SELECT expires_at FROM sessions WHERE token=?", (token,)
    ).fetchone()
    conn.close()
    if not r:
        return False
    try:
        return datetime.fromisoformat(r["expires_at"]) > datetime.now()
    except Exception:
        return False


def destroy_session(token: str) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    conn.commit()
    conn.close()


def cookie_name() -> str:
    return _COOKIE


def session_max_age() -> int:
    return SESSION_DAYS * 24 * 3600
