"""统计聚合与自然语言式查询（本地规则，无需联网）。"""
import re
from datetime import datetime, date, timedelta

from .db import get_conn, now_iso
from .rules import load_categories


# ---------------- 交易 CRUD ----------------
def add_transaction(rec: dict):
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO transactions
           (date, amount, direction, category, subcategory, account, counterparty, note, source, raw, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (rec.get("date"), rec.get("amount"), rec.get("direction"), rec.get("category"),
         rec.get("subcategory", ""), rec.get("account"), rec.get("counterparty"),
         rec.get("note"), rec.get("source", "手动"), rec.get("raw", ""),
         now_iso()),
    )
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def bulk_insert(records: list):
    conn = get_conn()
    for rec in records:
        conn.execute(
            """INSERT INTO transactions
               (date, amount, direction, category, subcategory, account, counterparty, note, source, raw, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (rec.get("date"), rec.get("amount"), rec.get("direction"), rec.get("category"),
             rec.get("subcategory", ""), rec.get("account"), rec.get("counterparty"),
             rec.get("note"), rec.get("source", "导入"), rec.get("raw", ""),
             now_iso()),
        )
    conn.commit()
    n = len(records)
    conn.close()
    return n


def delete_transaction(tid: int):
    conn = get_conn()
    conn.execute("DELETE FROM transactions WHERE id=?", (tid,))
    conn.commit()
    conn.close()


def update_transaction(tid: int, rec: dict):
    """按 id 更新一笔交易。rec 可包含 date/amount/direction/category/subcategory/
    account/counterparty/note。返回 tid。业务校验失败抛 ValueError（由接口层转 400）。"""
    conn = get_conn()
    if conn.execute("SELECT id FROM transactions WHERE id=?", (tid,)).fetchone() is None:
        conn.close()
        raise ValueError("交易不存在或已被删除")

    direction = rec.get("direction")
    if direction not in ("income", "expense"):
        conn.close()
        raise ValueError("方向 direction 必须是 income 或 expense")
    try:
        amount = float(rec.get("amount"))
    except (TypeError, ValueError):
        conn.close()
        raise ValueError("金额 amount 必须是数字")

    conn.execute(
        """UPDATE transactions SET
              date=?, amount=?, direction=?, category=?, subcategory=?,
              account=?, counterparty=?, note=?
           WHERE id=?""",
        (rec.get("date") or "", amount, direction, rec.get("category", "") or "",
         rec.get("subcategory", "") or "", rec.get("account", "") or "",
         rec.get("counterparty", "") or "", rec.get("note", "") or "", tid),
    )
    conn.commit()
    conn.close()
    return tid


def get_transactions(start="", end="", category="", direction="", q="", page=1, page_size=50, subcategory=""):
    conn = get_conn()
    where = []
    args = []
    if start:
        where.append("date >= ?")
        args.append(start)
    if end:
        where.append("date <= ?")
        args.append(end)
    if category:
        where.append("category = ?")
        args.append(category)
    if subcategory:
        where.append("subcategory = ?")
        args.append(subcategory)
    if direction:
        where.append("direction = ?")
        args.append(direction)
    if q:
        where.append("(note LIKE ? OR counterparty LIKE ? OR category LIKE ?)")
        args.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) AS c FROM transactions{sql_where}", args).fetchone()["c"]
    rows = conn.execute(
        f"SELECT * FROM transactions{sql_where} ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
        args + [page_size, (page - 1) * page_size],
    ).fetchall()
    conn.close()
    return {"total": total, "page": page, "page_size": page_size,
            "items": [dict(r) for r in rows]}


# ---------------- 聚合 ----------------
def _build_where(start="", end="", category="", direction="", q="", subcategory=""):
    """构造 (WHERE 子句, 参数)。

    ⚠️ 原实现把 " AND direction='income'" 直接接在 WHERE 之后：当筛选条件为空
    （例如仪表盘选「全部」→ start/end 都是空串）时 sql_where 是空串，
    就拼出非法的 `FROM transactions AND direction='income'` → SQL 语法错误 → 接口 500。
    统一用本函数构造，任何条件为空都不会产生非法 SQL。
    """
    frags, args = [], []
    if start:
        frags.append("date >= ?")
        args.append(start)
    if end:
        frags.append("date <= ?")
        args.append(end)
    if category:
        frags.append("category = ?")
        args.append(category)
    if subcategory:
        frags.append("subcategory = ?")
        args.append(subcategory)
    if direction:
        frags.append("direction = ?")
        args.append(direction)
    if q:
        frags.append("(note LIKE ? OR counterparty LIKE ? OR category LIKE ?)")
        args += [f"%{q}%", f"%{q}%", f"%{q}%"]
    sql = (" WHERE " + " AND ".join(frags)) if frags else ""
    return sql, args


def _aggregate(start="", end="", category="", subcategory=""):
    conn = get_conn()
    sql_inc, args_inc = _build_where(start, end, category, "income", "", subcategory)
    sql_exp, args_exp = _build_where(start, end, category, "expense", "", subcategory)
    inc = conn.execute(
        f"SELECT COALESCE(SUM(amount),0) AS s, COUNT(*) AS c FROM transactions{sql_inc}",
        args_inc,
    ).fetchone()
    exp = conn.execute(
        f"SELECT COALESCE(SUM(amount),0) AS s, COUNT(*) AS c FROM transactions{sql_exp}",
        args_exp,
    ).fetchone()
    conn.close()
    income = round(inc["s"], 2)
    expense = round(-exp["s"], 2)
    return {
        "income": income, "expense": expense,
        "balance": round(income - expense, 2),
        "income_count": inc["c"], "expense_count": exp["c"],
        "count": inc["c"] + exp["c"],
    }


def _month_end(y: int, m: int) -> date:
    """某年某月的最后一天。"""
    if m == 12:
        return date(y, 12, 31)
    return date(y, m + 1, 1) - timedelta(days=1)


def _shift_month(y: int, m: int, delta: int):
    """把 (年, 月) 平移 delta 个月（delta 可正可负）。"""
    idx = (y * 12 + (m - 1)) + delta
    return idx // 12, idx % 12 + 1


def range_bounds(key: str):
    """把 range key 转成 (start, end, label)。start/end 为 ISO 'YYYY-MM-DD' 或空串。

    新增时间段只需在这里加一个分支，并在 index.html 的下拉里加一个 <option> 即可。
    注意：必须始终返回三元组 (start, end, label)，否则 /api/summary 会取不到 label。
    """
    today = date.today()
    y, m = today.year, today.month

    # —— 最近 N 天 / 本周 / 今天 ——
    if key == "today":
        return today.isoformat(), today.isoformat(), "今天"
    if key == "this_week":
        s = today - timedelta(days=today.weekday())  # 周一
        return s.isoformat(), today.isoformat(), "本周"
    if key == "last_7d":
        return (today - timedelta(days=6)).isoformat(), today.isoformat(), "近 7 天"
    if key == "last_30d":
        return (today - timedelta(days=29)).isoformat(), today.isoformat(), "近 30 天"
    if key == "last_90d":
        return (today - timedelta(days=89)).isoformat(), today.isoformat(), "近 90 天"

    # —— 月 ——
    if key == "this_month":
        return date(y, m, 1).isoformat(), _month_end(y, m).isoformat(), f"{y}年{m}月"
    if key == "last_month":
        ly, lm = _shift_month(y, m, -1)
        return date(ly, lm, 1).isoformat(), _month_end(ly, lm).isoformat(), f"{ly}年{lm}月"

    # —— 季度 ——
    if key in ("this_quarter", "last_quarter"):
        q = (m - 1) // 3 + 1          # 1..4
        qy = y
        if key == "last_quarter":
            q -= 1
            if q == 0:
                q, qy = 4, y - 1
        sm = 3 * (q - 1) + 1
        em = sm + 2
        return date(qy, sm, 1).isoformat(), _month_end(qy, em).isoformat(), f"{qy}年 Q{q}"

    # —— 近 N 个月 ——
    if key in ("last_6m", "last_12m"):
        n = 6 if key == "last_6m" else 12
        sy, sm = _shift_month(y, m, -(n - 1))
        return date(sy, sm, 1).isoformat(), today.isoformat(), f"近 {n} 个月"

    # —— 年 ——
    if key == "this_year":
        return f"{y}-01-01", f"{y}-12-31", f"{y}年"
    if key == "last_year":
        return f"{y-1}-01-01", f"{y-1}-12-31", f"{y-1}年"

    # 默认：全部
    return "", "", "全部"


def summary_range(start="", end="", label="自定义区间"):
    """按显式起止日期汇总（用于自定义区间）。"""
    agg = _aggregate(start, end)
    agg["period"] = label or "自定义区间"
    return agg


def summary(key="this_month"):
    start, end, label = range_bounds(key)
    agg = _aggregate(start, end)
    agg["period"] = label
    return agg


def by_category(start="", end="", direction="expense", q=""):
    conn = get_conn()
    sql_where, args = _build_where(start, end, "", direction)
    if q:
        sql_where += (" AND " if sql_where else " WHERE ") + "(note LIKE ? OR counterparty LIKE ? OR category LIKE ?)"
        args += [f"%{q}%", f"%{q}%", f"%{q}%"]
    rows = conn.execute(
        f"SELECT category, COALESCE(SUM(ABS(amount)),0) AS total, COUNT(*) AS c "
        f"FROM transactions{sql_where} GROUP BY category ORDER BY total DESC",
        args,
    ).fetchall()
    conn.close()
    return [{"category": r["category"], "total": round(r["total"], 2), "count": r["c"]}
            for r in rows]


def get_subcat_pairs():
    """返回库内所有 (一级分类, 二级分类) 去重对（二级分类非空）。

    用于智能问答识别「二级分类」：库里没有结构化的父子表，
    二级分类是交易记录里自由填写的文本，所以直接从 transactions 聚合出来。
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT category, subcategory FROM transactions "
        "WHERE subcategory IS NOT NULL AND subcategory <> ''"
    ).fetchall()
    conn.close()
    return [(r["category"], r["subcategory"]) for r in rows]


def get_subcategories(category: str):
    """返回某一级分类下出现过的二级分类列表（去重、按名称排序）。"""
    if not category:
        return []
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT subcategory FROM transactions "
        "WHERE category=? AND subcategory IS NOT NULL AND subcategory <> '' "
        "ORDER BY subcategory",
        (category,),
    ).fetchall()
    conn.close()
    return [r["subcategory"] for r in rows]


def trend(n_months=12):
    now = datetime.now()
    y, m = now.year, now.month
    labels, income, expense = [], [], []
    for i in range(n_months - 1, -1, -1):
        mm = m - i
        yy = y
        while mm <= 0:
            mm += 12
            yy -= 1
        start = f"{yy}-{mm:02d}-01"
        end = f"{yy}-{mm:02d}-31"
        agg = _aggregate(start, end)
        labels.append(f"{yy}-{mm:02d}")
        income.append(agg["income"])
        expense.append(agg["expense"])
    return {"labels": labels, "income": income, "expense": expense}


def category_trend(n_months=6, direction="expense", top=6):
    """近 N 个月、每个分类逐月的金额，用于「月度分类堆叠图」。

    只保留整段期间金额最大的 top 个分类，其余合并为「其他」，避免图例过多。
    返回 {labels:[..], series:[{category, values:[每月金额]}..]}。
    """
    now = datetime.now()
    y, m = now.year, now.month
    labels, bounds = [], []
    for i in range(n_months - 1, -1, -1):
        yy, mm = _shift_month(y, m, -i)
        labels.append(f"{yy}-{mm:02d}")
        bounds.append((date(yy, mm, 1).isoformat(), _month_end(yy, mm).isoformat()))

    conn = get_conn()
    # 先按整段期间总额定出 top 分类
    start, end = bounds[0][0], bounds[-1][1]
    sql_where, args = _build_where(start, end, "", direction)
    rows = conn.execute(
        f"SELECT category, COALESCE(SUM(ABS(amount)),0) AS total "
        f"FROM transactions{sql_where} GROUP BY category ORDER BY total DESC",
        args,
    ).fetchall()
    top_cats = [r["category"] for r in rows[:top]]

    series = {c: [0.0] * n_months for c in top_cats}
    other = [0.0] * n_months
    for idx, (s, e) in enumerate(bounds):
        sw, a = _build_where(s, e, "", direction)
        mrows = conn.execute(
            f"SELECT category, COALESCE(SUM(ABS(amount)),0) AS total "
            f"FROM transactions{sw} GROUP BY category",
            a,
        ).fetchall()
        for r in mrows:
            if r["category"] in series:
                series[r["category"]][idx] = round(r["total"], 2)
            else:
                other[idx] += r["total"]
    conn.close()

    out = [{"category": c, "values": series[c]} for c in top_cats]
    if any(v > 0 for v in other):
        out.append({"category": "其他", "values": [round(v, 2) for v in other]})
    return {"labels": labels, "series": out}


# ---------------- 流水页图表汇总 ----------------
def tx_summary(start="", end="", category="", direction="", q="", top=10):
    """流水页「当前筛选结果图表」：返回分类构成 + 每日收支趋势 + 总览。

    - by_category：按分类聚合（绝对值），含 income/expense 分量，用于环形图
    - by_day：按日聚合收入/支出，用于柱状图
    - income/expense/count/balance：当前筛选集合的总览数字
    """
    where, args = _build_where(start, end, category, direction, q)
    w_inc, a_inc = _build_where(start, end, category, "income", q)
    w_exp, a_exp = _build_where(start, end, category, "expense", q)
    conn = get_conn()
    inc = conn.execute(
        f"SELECT COALESCE(SUM(amount),0) AS s, COUNT(*) AS c FROM transactions{w_inc}", a_inc
    ).fetchone()
    exp = conn.execute(
        f"SELECT COALESCE(SUM(-amount),0) AS s, COUNT(*) AS c FROM transactions{w_exp}", a_exp
    ).fetchone()

    cat_rows = conn.execute(
        f"SELECT category, direction, COALESCE(SUM(ABS(amount)),0) AS total, COUNT(*) AS c "
        f"FROM transactions{where} GROUP BY category, direction ORDER BY total DESC",
        args,
    ).fetchall()
    day_rows = conn.execute(
        f"SELECT date, direction, COALESCE(SUM(ABS(amount)),0) AS total "
        f"FROM transactions{where} GROUP BY date, direction",
        args,
    ).fetchall()
    conn.close()

    cat_map = {}
    for r in cat_rows:
        c = cat_map.setdefault(r["category"], {"total": 0.0, "count": 0, "income": 0.0, "expense": 0.0})
        c["total"] += r["total"]
        c["count"] += r["c"]
        if r["direction"] == "income":
            c["income"] += r["total"]
        else:
            c["expense"] += r["total"]
    by_category = sorted(cat_map.items(), key=lambda kv: -kv[1]["total"])
    if top:
        by_category = by_category[:top]
    by_category = [{"category": k, **v} for k, v in by_category]

    day_map = {}
    for r in day_rows:
        d = day_map.setdefault(r["date"], {"income": 0.0, "expense": 0.0})
        if r["direction"] == "income":
            d["income"] += r["total"]
        else:
            d["expense"] += r["total"]
    by_day = [{"date": k, **v} for k, v in sorted(day_map.items())]

    income = round(inc["s"], 2)
    expense = round(exp["s"], 2)
    return {
        "income": income, "expense": expense,
        "count": inc["c"] + exp["c"],
        "balance": round(income - expense, 2),
        "by_category": by_category,
        "by_day": by_day,
    }


def batch_delete(ids: list):
    """按 id 列表批量删除交易。返回实际删除的行数。"""
    ids = [int(i) for i in ids if str(i).isdigit()]
    if not ids:
        return 0
    conn = get_conn()
    ph = ",".join("?" * len(ids))
    cur = conn.execute(f"DELETE FROM transactions WHERE id IN ({ph})", ids)
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n


# ---------------- 自然语言查询 ----------------
def natural_query(text: str):
    text = str(text or "").strip()
    now = datetime.now()
    y, m = now.year, now.month
    start, end, period_label = "", "", "全部"

    if any(k in text for k in ["本月", "这个月"]):
        start, end, period_label = f"{y}-{m:02d}-01", f"{y}-{m:02d}-31", f"{y}年{m}月"
    elif any(k in text for k in ["上月", "上个月"]):
        lm = m - 1 or 12
        ly = y if m > 1 else y - 1
        start, end, period_label = f"{ly}-{lm:02d}-01", f"{ly}-{lm:02d}-31", f"{ly}年{lm}月"
    elif any(k in text for k in ["今年", "本年"]):
        start, end, period_label = f"{y}-01-01", f"{y}-12-31", f"{y}年"
    elif any(k in text for k in ["去年", "上一年"]):
        start, end, period_label = f"{y-1}-01-01", f"{y-1}-12-31", f"{y-1}年"
    else:
        ym = re.search(r"(\d{4})年(\d{1,2})月", text)
        if ym:
            yy, mm = int(ym.group(1)), int(ym.group(2))
            start, end, period_label = f"{yy}-{mm:02d}-01", f"{yy}-{mm:02d}-31", f"{yy}年{mm}月"
        else:
            ym2 = re.search(r"(\d{4})年", text)
            if ym2:
                yy = int(ym2.group(1))
                start, end, period_label = f"{yy}-01-01", f"{yy}-12-31", f"{yy}年"
            else:
                n = re.search(r"最近\s*(\d+)\s*个?月", text)
                if n:
                    nn = int(n.group(1))
                    sy, sm = y, m
                    for _ in range(nn - 1):
                        sm -= 1
                        if sm == 0:
                            sm = 12
                            sy -= 1
                    start, end, period_label = f"{sy}-{sm:02d}-01", f"{y}-{m:02d}-31", f"近{nn}个月"

    # 分类识别（排除「收入/支出」等度量词，避免与指标词冲突）
    STOP_KW = {"收入", "支出", "花", "花费", "开销", "赚", "进账", "进项",
               "结余", "净", "剩", "多少", "笔", "笔数", "次数"}
    cats = load_categories()
    cat = None
    for c in cats:
        if c["name"] in text:
            cat = c["name"]
            break
    if not cat:
        for c in cats:
            for kw in c["keywords"]:
                if kw and kw in text and kw not in STOP_KW:
                    cat = c["name"]
                    break
            if cat:
                break

    # 二级分类识别：库里没有结构化的父子表，二级分类是交易记录里自由填写的文本。
    # 从 transactions 聚合出 (一级分类 -> 二级分类列表)，再把出现在问句里的二级分类捞出来。
    pairs = get_subcat_pairs()
    sub_to_cat = {}
    for c, s in pairs:
        sub_to_cat.setdefault(s, c)          # 同一个二级分类若挂在多个一级下，取第一个
    sub = None
    matched = [s for s in sub_to_cat if s and s in text]
    if matched:
        # 优先选与已识别一级分类匹配的二级分类
        if cat:
            for s in matched:
                if sub_to_cat[s] == cat:
                    sub = s
                    break
        if not sub:
            sub = max(matched, key=len)       # 否则取最长（最具体）的匹配
        # 若只提到了二级分类、没提一级，用二级反推一级
        if not cat:
            cat = sub_to_cat[sub]

    # 指标识别
    metric = "expense"
    if any(w in text for w in ["收入", "赚", "进账", "进项"]):
        metric = "income"
    elif any(w in text for w in ["结余", "净", "剩", "存下"]):
        metric = "balance"
    elif any(w in text for w in ["笔数", "几笔", "多少笔", "次数"]):
        metric = "count"

    agg = _aggregate(start, end, cat, sub)    # 命中分类（未指定分类时即全量）
    total_agg = _aggregate(start, end)        # 全量，用于算「该分类占总体的比例」

    if metric == "income":
        value, unit, verb = agg["income"], "元", "收入"
    elif metric == "balance":
        value, unit, verb = agg["balance"], "元", "结余"
    elif metric == "count":
        value, unit, verb = agg["count"], "笔", "交易笔数"
    else:
        value, unit, verb = agg["expense"], "元", "支出"

    # 图表/明细看哪个方向：balance 看收支两侧；income/expense 看对应方向；count 默认看支出构成。
    bd_dir = ""
    if metric == "income":
        bd_dir = "income"
    elif metric in ("expense", "count"):
        bd_dir = "expense"

    # 明细：当前区间内、命中分类（及二级分类）下的具体交易（按金额绝对值从大到小，最多 60 笔）
    tx_rows = get_transactions(start, end, cat or "", bd_dir, "", 1, 60, subcategory=sub or "")["items"]
    tx_rows.sort(key=lambda r: abs(r.get("amount") or 0), reverse=True)
    items_total = get_transactions(start, end, cat or "", bd_dir, "", 1, subcategory=sub or "")["total"]

    # 展示用的分类标签：指定了二级分类时写成「一级/二级」
    if sub:
        clabel = f"「{cat}/{sub}」"
    elif cat:
        clabel = f"「{cat}」"
    else:
        clabel = ""

    # 占比：只有「指定了分类」且指标是收入/支出/笔数时才有意义
    denom, share = None, None
    if cat:
        denom = {"income": total_agg["income"],
                 "expense": total_agg["expense"],
                 "count": total_agg["count"]}.get(metric)
        if denom:
            share = round(value / denom * 100, 1)

    if cat and items_total == 0:
        answer = f"{period_label}没有找到{clabel}的记录。"
    elif cat and share is not None and metric in ("expense", "income"):
        word = "支出" if metric == "expense" else "收入"
        answer = (f"{period_label}{clabel}的{word}为 {value} 元，"
                  f"占{period_label}总{word}的 {share}%"
                  f"（总{word} {denom} 元，共 {items_total} 笔）。")
    elif cat and share is not None and metric == "count":
        answer = (f"{period_label}{clabel}共 {value} 笔，"
                  f"占{period_label}总笔数的 {share}%（总 {denom} 笔）。")
    else:
        answer = f"{period_label}{clabel}的{verb}为 {value} {unit}（收入 {agg['income']} 元 / 支出 {agg['expense']} 元）。"
        if metric == "balance" and value < 0:
            answer += " 注意：该期间已超支。"

    # 环形图：指定了分类（或二级分类）时，只画「该分类 vs 其他」，直接体现它占总支出的比例；
    # 未指定分类时，才画全部各分类的构成；命中了分类但查无记录时，则不画环形图。
    scoped = bool(cat) and metric in ("expense", "income") and items_total > 0
    focus = f"{cat}/{sub}" if sub else (cat or "")
    if scoped:
        cnt_key = "expense_count" if metric == "expense" else "income_count"
        cat_cnt, all_cnt = agg[cnt_key], total_agg[cnt_key]
        other_total = round(denom - value, 2)
        breakdown = []
        if value > 0:
            breakdown.append({"category": focus, "total": value, "count": cat_cnt})
        if other_total > 0:
            breakdown.append({"category": "其他", "total": other_total, "count": max(all_cnt - cat_cnt, 0)})
    elif cat and metric in ("income", "expense"):
        breakdown = []           # 命中分类但无记录 → 不画环形图
    else:
        breakdown = by_category(start, end, bd_dir)

    return {
        "answer": answer,
        "period": period_label,
        "category": cat,
        "subcategory": sub or "",
        "metric": metric,
        "value": value,
        "income": agg["income"],
        "expense": agg["expense"],
        "balance": agg["balance"],
        "count": agg["count"],
        # —— 供前端画图与列明细 ——
        "breakdown": breakdown,          # scoped 时 = [该分类, 其他]；否则 = 全部分类构成
        "items": tx_rows,                # 具体交易（已按分类/方向过滤）
        "items_total": items_total,      # 该区间实际命中的交易总数
        # —— 分类占比信息（供前端标题 / 中心标签 / 说明）——
        "scoped": scoped,                # 是否已按分类收敛
        "focus_category": focus,         # 收敛的分类名（含二级时为「一级/二级」）
        "focus_share": share,            # 该分类占总体百分比（未收敛为 None）
        "focus_total": denom,            # 总体金额 / 笔数（未收敛为 None）
    }
