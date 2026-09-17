"""Excel / CSV 导入与通用列映射。

流程：预览(读取表头+样例+自动建议映射) -> 用户在前端调整映射 -> 应用(按映射归一化并入库)。
支持 xlsx/xlsm 与 csv/txt；旧版 .xls 需先另存为 xlsx。
"""
import csv
import io
import json
import re
from datetime import datetime, date, time, timedelta

from openpyxl import load_workbook

from .rules import categorize

STANDARD_FIELDS = ["date", "amount", "direction", "category", "subcategory",
                   "account", "counterparty", "note", "counted"]

FIELD_LABELS = {
    "date": "日期",
    "amount": "金额",
    "direction": "收支方向（收入/支出）",
    "category": "分类（一级）",
    "subcategory": "子分类（二级）",
    "account": "账户",
    "counterparty": "交易对方/商户",
    "note": "备注/说明",
    "counted": "计入收支（是/否）",
}

# 列名别名：用于「自动识别」时把来源表头对应到标准字段。
# 说明：同一字段内按先后顺序匹配，越靠前优先级越高。
FIELD_ALIASES = {
    "date": ["日期", "记账日期", "交易日期", "记账时间", "交易日", "date", "时间",
             "交易时间", "支付时间", "下单时间", "创建时间", "发生时间", "入账时间",
             "入账日期", "账单日期", "交易创建时间", "交易日期时间", "交易时间戳"],
    "amount": ["金额", "数额", "交易金额", "金额(元)", "金额（元）", "money", "amount",
               "数目", "交易额", "实付", "收支金额", "本币金额", "发生额", "交易金额(元)",
               "金额(￥)", "收支金额(元)"],
    "direction": ["类型", "收支", "方向", "收/支", "收入支出", "inout", "收支类型",
                  "资金流向", "收/支类型", "收支方向", "交易方向", "收付"],
    "category": ["一级分类", "分类", "类别", "科目", "category", "消费类型", "记账分类",
                 "大类", "交易分类"],
    "subcategory": ["二级分类", "子分类", "小类", "子类别", "二级类别", "subcategory"],
    "account": ["账户", "账户名", "卡号", "account", "支付方式", "账户类型", "付款方式",
                "资金账户", "记账账户", "收/付款方式", "收付款方式", "支付账户", "银行卡"],
    "counterparty": ["交易对方", "对方", "商户", "商家", "counterparty", "payee", "对方账户",
                     "交易对象", "店名", "交易商户", "对方户名", "商户名称", "交易对手"],
    "note": ["备注", "说明", "摘要", "note", "memo", "描述", "用途", "说明/备注", "交易备注",
             "商品", "商品说明", "交易内容", "交易说明", "附言"],
    "counted": ["计入收支", "是否计入收支", "计入", "计入统计"],
}

# 全部已知列名别名（小写集合），用于判断「哪一行是表头」（命中别名越多越可能是表头）
_ALL_ALIASES = {a.strip().lower() for _aliases in FIELD_ALIASES.values() for a in _aliases}

# 视为「占位符」的单元格值：这些值不算有效内容（微信账单备注列整列都是「/」）
_TRIVIAL_CELLS = {"", "/", "\\", "-", "—", "－", ".", "。", "·", "无", "null", "none", "n/a", "na"}

INCOME_MARKERS = {"收入", "收", "+", "进", "入账", "贷", "credit", "in", "正数", "收人",
                  "收入(+)", "转入", "收款"}
EXPENSE_MARKERS = {"支出", "支", "-", "出", "花钱", "借", "debit", "out", "开支", "负", "付出",
                   "支出(-)", "转出", "付款"}
# 「计入收支」= 这些值时跳过该行（转账/还款/报销等，避免污染收支统计）
NOT_COUNTED_MARKERS = {"否", "no", "false", "n", "0", "不计入", "exclude"}
# 收支方向列若为这些值，表示该笔不计入收支（微信的「/」代表转账/还款/提现等）
NEUTRAL_MARKERS = {"/", "\\", "－", "—", "不计入", "不计入收支", "中性", "neutral", "不统计"}

# 常用记账 App 导出预设（按各 App 真实导出表头校准）
PRESETS = {
    # 咔皮记账导出列：日期/时间/类型/金额/一级分类/二级分类/标签/账户/计入收支/计入预算/所属账本/备注/分摊明细
    "咔皮记账": {
        "date": "日期",
        "amount": "金额",
        "direction": "类型",
        "category": "一级分类",
        "subcategory": "二级分类",
        "account": "账户",
        "note": "备注",
        "counted": "计入收支",
    },
    # 微信支付账单：交易时间/交易类型/交易对方/商品/收-支/金额(元)/支付方式/当前状态/交易单号/商户单号/备注
    "微信支付账单": {
        "date": "交易时间",
        "direction": "收/支",
        "amount": "金额(元)",
        "counterparty": "交易对方",
        "note": "商品",
        "account": "支付方式",
    },
    # 支付宝账单：交易时间/交易分类/交易对方/对方账号/商品说明/收-支/金额/收-付款方式/交易状态/...
    "支付宝账单": {
        "date": "交易时间",
        "direction": "收/支",
        "amount": "金额",
        "counterparty": "交易对方",
        "note": "商品说明",
        "account": "收/付款方式",
    },
    # 银行卡流水（通用）：交易日期/交易时间/摘要/交易金额/借/贷/余额/交易对手
    "银行流水": {
        "date": "交易日期",
        "amount": "交易金额",
        "counterparty": "交易对手",
        "note": "摘要",
    },
}


# ---------------- 单元格取值安全化 ----------------
def _coerce(v):
    """把单元格值转成 JSON / SQLite 都安全的标量。

    这是关键修复点：openpyxl 读 xlsx 的日期列会返回 datetime 对象，
    既无法 json.dumps（导致接口 500），也无法作为 SQLite 参数绑定。
    统一在这里转成字符串。
    """
    if v is None:
        return None
    if isinstance(v, bool) or isinstance(v, int) or isinstance(v, str):
        return v
    if isinstance(v, float):
        # 过滤 NaN / Inf，避免后续计算出错
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return v
    if isinstance(v, datetime):
        if (v.hour, v.minute, v.second, v.microsecond) == (0, 0, 0, 0):
            return v.strftime("%Y-%m-%d")
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, time):
        return v.strftime("%H:%M:%S")
    if isinstance(v, (bytes, bytearray)):
        try:
            return bytes(v).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(v).decode("gbk", errors="ignore")
    try:
        from decimal import Decimal
        if isinstance(v, Decimal):
            return float(v)
    except Exception:
        pass
    return str(v)


# 导入模块的代码修订号。会随 /api/health 一起返回，用来确认「容器里到底跑的哪一版代码」。
# 之所以需要它：本机修好、NAS 上仍是旧镜像时，症状完全一样，光看界面无法区分。
IMPORT_REV = "2026-09-11c-dimension-fix"


# ---------------- 读取 ----------------
def _scan_workbook(wb, forgive_dimensions: bool):
    """扫描工作簿所有 sheet，返回 (行数最多的那张表的行, (行数, 最大列数))。

    forgive_dimensions=True 时先 reset_dimensions()，忽略 xlsx 里错误的
    `<dimension ref="A1">` 声明（实测：咔皮记账导出的文件就是这种）。
    评分用 (行数, 列数) 双重比较，避免「只有表头 1 行的辅助表」压过真正的数据表。
    """
    best, best_key = [], (0, 0)
    for ws in wb.worksheets:
        if forgive_dimensions:
            try:
                ws.reset_dimensions()
            except Exception:
                pass
        try:
            rows = [[_coerce(c) for c in r] for r in ws.iter_rows(values_only=True)]
        except Exception:
            continue
        rows = [r for r in rows if any(c not in (None, "") for c in r)]
        key = (len(rows), max((len(r) for r in rows), default=0))
        if key > best_key:
            best, best_key = rows, key
    return best, best_key


def read_raw(file_bytes: bytes, filename: str):
    """返回二维列表（含表头行与数据行），所有单元格已安全化。

    ⚠️ 关键坑：不要直接依赖 `wb.active` 或工作表自带的尺寸声明。
    部分记账 App（实测：咔皮记账）导出的 xlsx 里，工作表写着
    `<dimension ref="A1">`，即「声明自己只有一个单元格」。openpyxl 在
    read_only=True 时会相信这个声明，于是整张表只读到 'A1' 一个格子
    （表现就是「字段映射 共 0 行」、且只有「日期」能识别）。
    这里对每张表先 reset_dimensions() 强制忽略该声明、按实际 XML 重新扫描，
    再在所有工作表中选数据行最多的那张（自动跳过空的「内部转账」等辅助表）。

    兜底：万一 reset_dimensions 在该文件/该 openpyxl 版本上仍无效，
    结果会退化成「1 行或 1 列」。此时**必须**改走普通模式（read_only=False）
    重读一次——普通模式按 XML 实际内容装载，完全不看尺寸声明。
    绝不能因为 best 非空就提前 return，否则又会静默返回一份残缺数据。
    """
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        try:
            best, key = _scan_workbook(wb, forgive_dimensions=True)
        finally:
            wb.close()

        # 退化判定：只读到 1 行、或所有行都只有 1 列 → 尺寸声明仍被信任了
        if key[0] <= 1 or key[1] <= 1:
            try:
                wb2 = load_workbook(io.BytesIO(file_bytes), read_only=False, data_only=True)
                try:
                    best2, key2 = _scan_workbook(wb2, forgive_dimensions=False)
                finally:
                    wb2.close()
                if key2 > key:
                    best, key = best2, key2
            except Exception:
                pass
        return best
    # csv / txt
    text = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "big5"):
        try:
            text = file_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = file_bytes.decode("utf-8", errors="ignore")
    # 统一换行，便于 StringIO 逐行读取
    if name.endswith(".tsv"):
        reader = csv.reader(io.StringIO(text), delimiter="\t")
    else:
        reader = csv.reader(io.StringIO(text))
    return [[_coerce(c) for c in r] for r in reader]


def parse_records(file_bytes: bytes, filename: str):
    """返回 (headers, rows)。headers 为去重后的列名列表，rows 为 dict 列表。"""
    raw = read_raw(file_bytes, filename)
    raw = [r for r in raw if any(c not in (None, "") for c in r)]
    if not raw:
        return [], []

    # 选表头行：优先「命中已知列名别名最多」的行；一个都对不上时退回「填充单元格最多」的行。
    # 只在开头 50 行内寻找，避免把正文数据行误当表头（微信账单表头在第 18 行，
    # 前面全是「微信昵称：…」「起始时间：…」之类的说明文字）。
    window = min(len(raw), 50)

    def _alias_hits(i):
        return sum(1 for x in raw[i] if isinstance(x, str) and x.strip().lower() in _ALL_ALIASES)

    def _filled(i):
        return sum(1 for x in raw[i] if x not in (None, ""))

    header_idx = max(range(window), key=lambda i: (_alias_hits(i), _filled(i)))
    if _alias_hits(header_idx) == 0:
        header_idx = max(range(window), key=_filled)
    headers_raw = raw[header_idx]
    headers = []
    seen = {}
    for j, h in enumerate(headers_raw):
        h = str(h).strip() if h is not None else ""
        if h == "":
            h = f"列{j+1}"
        if h in seen:
            h = f"{h}_{j+1}"
        seen[h] = True
        headers.append(h)

    rows = []
    for r in raw[header_idx + 1:]:
        if all(x in (None, "") for x in r):
            continue
        d = {headers[j]: (r[j] if j < len(r) else "") for j in range(len(headers))}
        rows.append(d)
    return headers, rows


# ---------------- 映射 ----------------
def _column_quality(sample_rows, header):
    """估算某列的信息量（0~1）：非空且非占位符（如微信备注列的「/」）的占比。"""
    if not sample_rows or not header:
        return 0.0
    good = 0
    for r in sample_rows:
        v = r.get(header)
        if v is None:
            continue
        if str(v).strip().lower() in _TRIVIAL_CELLS:
            continue
        good += 1
    return good / len(sample_rows)


def suggest_mapping(headers, preset: str = None, sample_rows=None):
    """根据表头名自动建议 标准字段->原始列名 映射。

    同一字段若命中多个候选列（例：微信账单里「备注/说明」同时命中「备注」和「商品」），
    优先选**实际有内容**的那一列——微信备注列整列都是「/」占位符，
    真正有用的是「商品」列。未传 sample_rows 时保持原有的别名优先级顺序。
    """
    mapping = {f: "" for f in STANDARD_FIELDS}
    lower_headers = {str(h).strip().lower(): h for h in headers}

    def _pick(field):
        best_h, best_q = "", -1.0
        for alias in FIELD_ALIASES.get(field, []):
            h = lower_headers.get(alias.lower())
            if h is None:
                continue
            q = _column_quality(sample_rows, h)
            if q > best_q:
                best_h, best_q = h, q
        return best_h

    if preset and preset in PRESETS:
        for f, expect in PRESETS[preset].items():
            # 优先精确匹配预设期望列名，否则退回别名匹配
            mapping[f] = expect if expect in headers else _pick(f)
        return mapping

    for f in STANDARD_FIELDS:
        mapping[f] = _pick(f)
    return mapping


# ---------------- 解析工具 ----------------
def parse_amount(v):
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s == "":
        return 0.0
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    s = s.replace(",", "").replace("，", "").replace("¥", "").replace("￥", "") \
         .replace("$", "").replace("元", "").replace(" ", "").replace("\u00a0", "")
    if s.startswith("-"):
        neg = True
        s = s[1:]
    elif s.startswith("+"):
        s = s[1:]
    try:
        val = float(s)
    except ValueError:
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        val = float(m.group()) if m else 0.0
    return -val if neg else val


def parse_date(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (int, float)):
        n = float(v)
        # 8 位整数视为 YYYYMMDD（如 20260729），否则按 Excel 序列日期处理
        if 19000101 <= n <= 21001231 and float(n).is_integer() and len(str(int(n))) == 8:
            s = str(int(n))
            return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
        try:
            return (datetime(1899, 12, 30) + timedelta(days=n)).strftime("%Y-%m-%d")
        except Exception:
            return None
    s = str(v).strip()
    if s == "":
        return None
    # 去掉 ISO 的 T 与时区/毫秒，便于统一按 fmt 解析
    s_clean = s.replace("T", " ").strip()
    if s_clean.endswith("Z"):
        s_clean = s_clean[:-1].strip()
    s_clean = re.sub(r"\.\d+", "", s_clean)
    s_clean = re.sub(r"([+-]\d{2}):?(\d{2})$", "", s_clean).strip()

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日", "%Y-%m-%d %H:%M:%S",
                "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M",
                "%m/%d/%Y", "%d/%m/%Y", "%Y.%m.%d", "%Y%m%d", "%Y-%m",
                "%Y年%m月", "%Y/%m", "%Y.%m"):
        try:
            return datetime.strptime(s_clean, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s_clean).strftime("%Y-%m-%d")
    except Exception:
        pass
    # 兜底：抓取 年-月-日 数字（含中文分隔）
    m = re.search(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            return None
    m2 = re.search(r"(\d{4})\s*[-/.年]\s*(\d{1,2})", s)
    if m2:
        y, mo = int(m2.group(1)), int(m2.group(2))
        try:
            return datetime(y, mo, 1).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def infer_direction(raw, amount=None):
    """返回 income / expense / neutral / None。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "":
        return None
    low = s.lower()
    if s in NEUTRAL_MARKERS or low in NEUTRAL_MARKERS:
        return "neutral"
    if s in INCOME_MARKERS or low in INCOME_MARKERS:
        return "income"
    if s in EXPENSE_MARKERS or low in EXPENSE_MARKERS:
        return "expense"
    if any(k in s for k in ("收入", "进账", "入账", "贷")):
        return "income"
    if any(k in s for k in ("支出", "出账", "借", "开支")):
        return "expense"
    return None


def _normalize_row_ex(row, mapping):
    """按映射把一行归一化为交易 dict。

    返回 (rec | None, 跳过原因 | None)。
    """
    def g(field):
        h = mapping.get(field)
        return row.get(h) if h else None

    raw_date = g("date")
    date_v = parse_date(raw_date)
    if not date_v:
        if raw_date in (None, ""):
            return None, "缺少日期"
        return None, f"日期无法识别（{raw_date}）"

    # 「计入收支」为否时跳过（转账/还款/报销等，避免污染收支统计）
    counted_raw = g("counted")
    if counted_raw is not None and str(counted_raw).strip() != "":
        if str(counted_raw).strip().lower() in NOT_COUNTED_MARKERS:
            return None, "计入收支=否"

    amount = parse_amount(g("amount"))
    direction = infer_direction(g("direction"), amount)
    # 微信/支付宝的「/」等中性标记 = 转账/还款/提现，不计入收支
    if direction == "neutral":
        return None, "不计入收支（转账/还款/提现等）"

    category_hint = g("category")
    subcategory_raw = g("subcategory")
    subcategory = str(subcategory_raw).strip() if subcategory_raw not in (None, "") else ""
    category, cat_dir = categorize(
        note=g("note"), counterparty=g("counterparty"),
        account=g("account"), category_hint=category_hint,
    )

    if direction is None:
        direction = cat_dir or ("expense" if amount < 0 else "income")

    # 让金额符号与方向一致
    if direction == "expense" and amount > 0:
        amount = -amount
    elif direction == "income" and amount < 0:
        amount = -amount

    return {
        "date": date_v,
        "amount": round(amount, 2),
        "direction": direction,
        "category": category,
        "subcategory": subcategory,
        "account": None if g("account") in (None, "") else str(g("account")).strip(),
        "counterparty": None if g("counterparty") in (None, "") else str(g("counterparty")).strip(),
        "note": None if g("note") in (None, "") else str(g("note")).strip(),
        # default=str 兜底：即使显式化了不可序列化对象也不会再让整个导入 500
        "raw": json.dumps(row, ensure_ascii=False, default=str),
    }, None


def normalize_row(row, mapping):
    """按映射将一行归一化为交易 dict；需要跳过的行返回 None。"""
    rec, _reason = _normalize_row_ex(row, mapping)
    return rec


def apply_mapping(rows, mapping, source="导入"):
    """归一化所有行，返回 (records, skipped)。"""
    records, skipped, _ = apply_mapping_ex(rows, mapping, source)
    return records, skipped


def apply_mapping_ex(rows, mapping, source="导入"):
    """归一化所有行，返回 (records, skipped, reasons)。

    reasons: {跳过原因: 行数}，用于给用户可读的反馈（为什么某些行没导入）。
    """
    records = []
    reasons = {}
    for row in rows:
        rec, reason = _normalize_row_ex(row, mapping)
        if rec is None:
            reasons[reason or "未知原因"] = reasons.get(reason or "未知原因", 0) + 1
            continue
        rec["source"] = source
        records.append(rec)
    skipped = sum(reasons.values())
    return records, skipped, reasons
