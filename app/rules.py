"""本地规则引擎：基于关键词自动分类，并推断收支方向。"""
import json
from .db import get_conn
from .categories import CATEGORY_ALIASES


def load_categories():
    """返回 [{name, type, keywords}] 列表。"""
    conn = get_conn()
    rows = conn.execute("SELECT name, type, keywords FROM categories").fetchall()
    conn.close()
    cats = []
    for r in rows:
        try:
            kws = json.loads(r["keywords"]) if r["keywords"] else []
        except Exception:
            kws = []
        cats.append({"name": r["name"], "type": r["type"], "keywords": kws})
    return cats


def categorize(note="", counterparty="", account="", category_hint=""):
    """返回 (category, direction)。

    - category_hint 为已给出的分类名时直接采用（先按 CATEGORY_ALIASES 归一化；
      仍无法对应默认分类则保留原始名称），并据此推断方向。
    - 否则按关键词在 note/counterparty/account 中匹配，命中首个分类即采用。
    - 均未命中则归入「其他」。
    - direction 由分类 type 决定（income/expense）；both/未知时由调用方按金额符号判断。
    """
    note = str(note or "")
    counterparty = str(counterparty or "")
    account = str(account or "")
    category_hint = str(category_hint or "").strip()

    cats = load_categories()
    names = {c["name"] for c in cats}

    best = None
    if category_hint:
        if category_hint in names:
            best = category_hint
        elif category_hint in CATEGORY_ALIASES and CATEGORY_ALIASES[category_hint] in names:
            best = CATEGORY_ALIASES[category_hint]
        else:
            # 无法对应默认分类：保留来源分类名，避免丢失原始信息
            best = category_hint

    if not best:
        text = " ".join([note, counterparty, account]).lower()
        for c in cats:
            for kw in c["keywords"]:
                if kw and kw.lower() in text:
                    best = c["name"]
                    break
            if best:
                break

    if not best:
        best = "其他"

    # 方向推断
    ctype = next((c["type"] for c in cats if c["name"] == best), "both")
    if ctype == "income":
        direction = "income"
    elif ctype == "expense":
        direction = "expense"
    else:
        direction = None  # 由金额符号决定
    return best, direction
