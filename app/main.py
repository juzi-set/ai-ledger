"""FastAPI 主程序：Web 界面 + REST API。"""
import json
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException, Response, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from .db import init_db, get_conn
from . import db
from . import importer
from . import stats
from . import auth
from .stats import range_bounds

STATIC_DIR = Path(__file__).parent / "static"
app = FastAPI(title="本地 AI 记账", version="1.5.3")

# 服务端版本 / 构建标识。
# 用途：本机改了代码、NAS 容器却还是旧镜像时，界面表现完全一样，无法区分。
# 现在 /api/health 会返回这两项，导入页底部也会显示，一眼就能确认容器跑的是哪版。
APP_VERSION = "1.5.3"
BUILD_REV = (importer.IMPORT_REV + " + dash-2026-09-11 + account-2026-09-12"
             + " + tx-detail-qa-2026-09-12 + qa-scope-2026-09-13 + tx-edit-resize-2026-09-13"
             + " + tx-subcol-qa-2026-09-13")

# 导入预览的临时缓存（单进程，足够 MVP 使用）
_IMPORT_CACHE = {}

# 无需登录即可访问的接口。
# 为什么把两个 /api/auth/* 也放进来：
#   它们是「改账户名 / 改密码」，本身就必须提供正确**当前密码**才能成功，
#   凭据校验已经内建。若强制要求先登录，登录页上的「Change password?」
#   就会因为拿不到会话而报 401，等于这个入口废掉。
_AUTH_FREE = {
    "/api/login", "/api/logout", "/api/me", "/api/health",
    "/api/auth/set_password", "/api/auth/set_username",
}


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """兜底异常处理：任何未捕获的异常都返回 JSON。

    否则 Starlette 会返回纯文本 "Internal Server Error"，
    前端 r.json() 解析失败，只能抛出 "Unexpected token 'I' ... is not valid JSON"，
    真正的错误信息就丢失了。
    """
    return JSONResponse(
        status_code=500,
        content={"detail": f"服务器内部错误：{type(exc).__name__}: {exc}"},
    )


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in _AUTH_FREE:
        if auth.is_auth_enabled():
            token = request.cookies.get(auth.cookie_name())
            if not auth.valid_session(token):
                return JSONResponse(status_code=401, content={"detail": "未登录或会话已过期"})
    return await call_next(request)


@app.on_event("startup")
def _startup():
    init_db()
    auth.seed_password_from_env()
    auth.seed_username_from_env()
    # 启动时把「我到底在跑哪个库、哪个账户」打到日志里。
    # 这个项目已经两次被「跑的其实不是我改的那份」坑到（旧镜像、旧数据库），
    # 一行日志能省掉大量猜测。
    print(
        f"[启动] 数据库={db.db_path()} 账户={auth.get_username()} "
        f"鉴权={'开' if auth.is_auth_enabled() else '关'} 版本={APP_VERSION}",
        flush=True,
    )


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------- 概览 / 统计 ----------------
@app.get("/api/health")
def health():
    import openpyxl
    return {
        "ok": True,
        "version": APP_VERSION,
        "build_rev": BUILD_REV,
        "openpyxl": openpyxl.__version__,
        # 实际使用的数据库文件：排查「数据没进来」时，先确认服务端到底在读哪个库
        "db": db.db_path(),
        "username": auth.get_username(),
    }


# ---------------- 登录 / 会话 ----------------
@app.get("/api/me")
def api_me(request: Request):
    if not auth.is_auth_enabled():
        return {"authenticated": True, "mode": "open", "username": auth.get_username()}
    token = request.cookies.get(auth.cookie_name())
    if auth.valid_session(token):
        return {
            "authenticated": True,
            "mode": "protected",
            "username": auth.session_username(token) or auth.get_username(),
        }
    return JSONResponse(status_code=401, content={"detail": "未登录"})


@app.post("/api/login")
def api_login(body: dict, response: Response):
    username = body.get("username", "")
    pw = body.get("password", "")

    if not auth.is_auth_enabled():
        # 开放模式：还没设过密码，账户名也用默认值，直接签发会话
        token = auth.create_session(username or auth.get_username())
        response.set_cookie(
            key=auth.cookie_name(), value=token,
            max_age=auth.session_max_age(), httponly=True, samesite="lax", path="/",
        )
        return {"ok": True, "username": auth.get_username(), "mode": "open"}

    # 账户名、密码都必须是「未通过」才回同一个提示：
    # 分开提示等于告诉试探者「用户名对了、继续猜密码」，没必要送这个信息。
    if not auth.verify_username(username) or not auth.verify_password(pw, auth.get_stored_hash()):
        raise HTTPException(status_code=401, detail="账户名或密码不正确")
    token = auth.create_session(auth.get_username())
    response.set_cookie(
        key=auth.cookie_name(), value=token,
        max_age=auth.session_max_age(), httponly=True, samesite="lax", path="/",
    )
    return {"ok": True, "username": auth.get_username(), "mode": "protected"}


@app.post("/api/logout")
def api_logout(request: Request, response: Response):
    token = request.cookies.get(auth.cookie_name())
    if token:
        auth.destroy_session(token)
    response.delete_cookie(auth.cookie_name(), path="/")
    return {"ok": True}


@app.post("/api/auth/set_password")
def api_set_password(body: dict, request: Request):
    new = body.get("new", "")
    if not new or len(new) < 4:
        raise HTTPException(status_code=400, detail="新密码至少 4 位")
    if auth.is_auth_enabled():
        cur = body.get("current", "")
        # 故意用 400 而不是 401：401 会被前端统一当作「会话过期」并弹回登录页，
        # 那样用户只会看到自己莫名其妙被踢出去，看不到「当前密码不正确」这句真正有用的提示。
        if not auth.verify_password(cur, auth.get_stored_hash()):
            raise HTTPException(status_code=400, detail="当前密码不正确")
    auth.set_password(new)
    # 改完密码，其它设备上的会话立即失效，只有当前这台继续有效
    auth.destroy_other_sessions(request.cookies.get(auth.cookie_name()) or "")
    return {"ok": True}


@app.post("/api/auth/set_username")
def api_set_username(body: dict):
    new = body.get("new", "")
    if auth.is_auth_enabled():
        cur = body.get("current", "")
        if not auth.verify_password(cur, auth.get_stored_hash()):
            raise HTTPException(status_code=400, detail="当前密码不正确")
    try:
        name = auth.set_username(new)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "username": name}
@app.get("/api/summary")
def api_summary(range: str = "this_month", start: str = "", end: str = ""):
    # 自定义区间：只要带了 start/end 就按显式日期算，忽略 range
    if start or end:
        label = f"{start or '最早'} ~ {end or '今天'}"
        return stats.summary_range(start, end, label)
    return stats.summary(range)


@app.get("/api/by_category")
def api_by_category(range: str = "this_month", direction: str = "expense",
                    start: str = "", end: str = "", q: str = ""):
    if not (start or end):
        start, end, _ = range_bounds(range)
    return stats.by_category(start, end, direction, q)


@app.get("/api/tx_summary")
def api_tx_summary(range: str = "", start: str = "", end: str = "",
                   category: str = "", direction: str = "", q: str = "", top: int = 10):
    """流水页图表：当前筛选结果的分类构成 + 每日收支趋势 + 总览。"""
    if range and not start and not end:
        start, end, _ = range_bounds(range)
    return stats.tx_summary(start, end, category, direction, q, top)


@app.get("/api/trend")
def api_trend(months: int = 12):
    return stats.trend(months)


@app.get("/api/category_trend")
def api_category_trend(months: int = 6, direction: str = "expense", top: int = 6):
    return stats.category_trend(months, direction, top)


# ---------------- 交易 ----------------
@app.get("/api/transactions")
def api_transactions(range: str = "", start: str = "", end: str = "",
                     category: str = "", direction: str = "", subcategory: str = "",
                     q: str = "", page: int = 1, page_size: int = 50):
    if range and not start and not end:
        start, end, _ = range_bounds(range)
    return stats.get_transactions(start, end, category, direction, q, page, page_size, subcategory)


@app.post("/api/transactions")
def api_add_transaction(rec: dict):
    tid = stats.add_transaction(rec)
    return {"id": tid}


@app.delete("/api/transactions/{tid}")
def api_delete_transaction(tid: int):
    stats.delete_transaction(tid)
    return {"ok": True}


@app.put("/api/transactions/{tid}")
def api_update_transaction(tid: int, rec: dict):
    try:
        stats.update_transaction(tid, rec)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "id": tid}


@app.post("/api/transactions/batch_delete")
def api_batch_delete(body: dict):
    ids = body.get("ids", [])
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="请提供要删除的 id 列表")
    if len(ids) > 500:
        raise HTTPException(status_code=400, detail="单次最多删除 500 笔")
    n = stats.batch_delete(ids)
    return {"ok": True, "deleted": n}


# ---------------- 分类 ----------------
@app.get("/api/categories")
def api_categories():
    conn = get_conn()
    rows = conn.execute("SELECT name, type, keywords, icon, color FROM categories").fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            kws = json.loads(r["keywords"]) if r["keywords"] else []
        except Exception:
            kws = []
        out.append({"name": r["name"], "type": r["type"], "keywords": kws,
                    "icon": r["icon"], "color": r["color"]})
    return out


@app.get("/api/subcategories")
def api_subcategories(category: str = ""):
    """某一级分类下出现过的二级分类列表（供编辑表单下拉）。"""
    return stats.get_subcategories(category)


@app.post("/api/categories")
def api_add_category(cat: dict):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO categories (name, type, keywords, icon, color) VALUES (?,?,?,?,?)",
        (cat["name"], cat.get("type", "expense"),
         json.dumps(cat.get("keywords", []), ensure_ascii=False),
         cat.get("icon", "●"), cat.get("color", "#888888")),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/categories/{name}")
def api_delete_category(name: str):
    conn = get_conn()
    conn.execute("DELETE FROM categories WHERE name=?", (name,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------- 导入 ----------------
@app.post("/api/import/preview")
async def api_import_preview(file: UploadFile = File(...), preset: str = ""):
    data = await file.read()
    filename = file.filename or "data.xlsx"
    try:
        headers, rows = importer.parse_records(data, filename)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"读取文件失败：{e}")
    if not headers:
        raise HTTPException(status_code=400, detail="文件中未找到有效数据行")
    mapping = importer.suggest_mapping(headers, preset or None, rows)

    # 「退化读取」自检：正常账单绝不会只有 0 行或 1 列。
    # 一旦出现，说明读到的是一份残缺内容（典型原因：xlsx 写了错误的
    # <dimension ref="A1">，而当前服务端代码不含修复逻辑 = 容器是旧镜像）。
    # 这种事必须**明说**，不能安静地显示「共 0 行」让用户以为文件没数据。
    warning = ""
    if len(headers) <= 1 or not rows:
        warning = (
            f"异常：只读到 {len(headers)} 列、{len(rows)} 行数据。该 xlsx 很可能声明了错误的"
            "尺寸（如 <dimension ref=\"A1\">），而当前服务端版本过旧、未包含对应修复。"
            f"请更新镜像后重试（当前服务端 build_rev={BUILD_REV}）。"
        )

    token = uuid.uuid4().hex
    _IMPORT_CACHE[token] = (headers, rows)
    return {
        "token": token,
        "filename": filename,
        "total_rows": len(rows),
        "headers": headers,
        "sample": rows[:20],
        "mapping": mapping,
        "fields": importer.STANDARD_FIELDS,
        "field_labels": importer.FIELD_LABELS,
        "warning": warning,
        "build_rev": BUILD_REV,
    }


@app.post("/api/import/apply")
def api_import_apply(body: dict):
    try:
        token = body.get("token")
        mapping = body.get("mapping", {})
        source = body.get("source", "导入")
        cached = _IMPORT_CACHE.get(token)
        if not cached:
            raise HTTPException(status_code=400, detail="导入会话已过期，请重新上传文件")
        headers, rows = cached
        # mapping 字段可能只给了有值的；补齐空字段
        full_mapping = {f: mapping.get(f, "") for f in importer.STANDARD_FIELDS}
        records, skipped, reasons = importer.apply_mapping_ex(rows, full_mapping, source)
        n = stats.bulk_insert(records)
        _IMPORT_CACHE.pop(token, None)
        reason_text = "；".join(f"{k} {v} 行" for k, v in
                               sorted(reasons.items(), key=lambda kv: -kv[1]))
        return {"inserted": n, "skipped": skipped, "total": len(rows),
                "reasons": reasons, "reason_text": reason_text}
    except HTTPException:
        raise
    except Exception as e:
        # 把真实原因回给前端，而不是让浏览器只能看到 "Internal Server Error"
        return JSONResponse(
            status_code=400,
            content={"detail": f"导入失败：{type(e).__name__}: {e}"},
        )


# ---------------- 智能查询 ----------------
@app.post("/api/query")
def api_query(body: dict):
    return stats.natural_query(body.get("text", ""))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
