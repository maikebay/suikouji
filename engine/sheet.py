"""随口记 · 表格读取（只用标准库）。

不装 openpyxl/pandas：xlsx 本身就是 zip + XML，读销售表这类规整数据足够。
CSV 额外兼容 GBK —— 国内系统导出的表经常是 GBK，按 UTF-8 读会整片乱码。
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import re
import xml.etree.ElementTree as ET
import zipfile

# Excel 内置的日期数字格式
_BUILTIN_DATE_FMT = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))

_COL_RE = re.compile(r"^([A-Z]+)(\d+)$")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def col_to_index(ref: str) -> int:
    """'B3' -> 1（0 基列号）"""
    m = _COL_RE.match(ref or "")
    if not m:
        return 0
    letters = m.group(1)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


# ---------------------------------------------------------------- xlsx


def _read_shared_strings(zf: zipfile.ZipFile) -> list:
    try:
        raw = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    out = []
    for si in root:
        if _local(si.tag) != "si":
            continue
        out.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
    return out


def _date_style_indexes(zf: zipfile.ZipFile) -> set:
    """找出 cellXfs 里哪些样式是日期，用来判断数字单元格要不要转日期。"""
    try:
        root = ET.fromstring(zf.read("xl/styles.xml"))
    except KeyError:
        return set()

    custom = {}
    for node in root.iter():
        if _local(node.tag) == "numFmt":
            fid = node.get("numFmtId")
            code = node.get("formatCode") or ""
            # 去掉引号内的字面量再判断，避免 "元" 里的字符误判
            stripped = re.sub(r'"[^"]*"', "", code).lower()
            if any(ch in stripped for ch in ("y", "d")) or "m" in stripped:
                custom[int(fid)] = True

    out = set()
    cell_xfs = None
    for node in root.iter():
        if _local(node.tag) == "cellXfs":
            cell_xfs = node
            break
    if cell_xfs is None:
        return out
    for idx, xf in enumerate(cell_xfs):
        fid = xf.get("numFmtId")
        if fid is None:
            continue
        fid = int(fid)
        if fid in _BUILTIN_DATE_FMT or custom.get(fid):
            out.add(idx)
    return out


def _excel_serial_to_date(value: float) -> str:
    # Excel 的 1900 历法有个著名的闰年 bug，序列号 60 = 1900-02-29（不存在）
    base = _dt.date(1899, 12, 30)
    return (base + _dt.timedelta(days=int(value))).isoformat()


def read_xlsx(path: str, sheet_index: int = 0) -> list:
    """读第一个（或指定）工作表，返回 list[list[str]]。"""
    with zipfile.ZipFile(path) as zf:
        shared = _read_shared_strings(zf)
        date_styles = _date_style_indexes(zf)

        names = sorted(n for n in zf.namelist()
                       if n.startswith("xl/worksheets/") and n.endswith(".xml"))
        if not names:
            raise ValueError("xlsx 里没有工作表")
        target = names[min(sheet_index, len(names) - 1)]
        root = ET.fromstring(zf.read(target))

    rows = []
    for row_el in root.iter():
        if _local(row_el.tag) != "row":
            continue
        cells = {}
        for c in row_el:
            if _local(c.tag) != "c":
                continue
            idx = col_to_index(c.get("r") or "")
            ctype = c.get("t") or "n"
            style = c.get("s")

            if ctype == "inlineStr":
                is_el = next((x for x in c if _local(x.tag) == "is"), None)
                text = "".join(t.text or "" for t in is_el.iter()
                               if _local(t.tag) == "t") if is_el is not None else ""
            else:
                v_el = next((x for x in c if _local(x.tag) == "v"), None)
                raw = v_el.text if v_el is not None and v_el.text is not None else ""
                if ctype == "s":
                    try:
                        text = shared[int(raw)]
                    except (ValueError, IndexError):
                        text = ""
                elif ctype == "b":
                    text = "TRUE" if raw == "1" else "FALSE"
                elif ctype in ("str", "e"):
                    text = raw
                else:
                    text = raw
                    if raw and style is not None and int(style) in date_styles:
                        try:
                            text = _excel_serial_to_date(float(raw))
                        except (ValueError, OverflowError):
                            pass
            cells[idx] = text.strip()

        if not cells:
            rows.append([])
            continue
        width = max(cells) + 1
        rows.append([cells.get(i, "") for i in range(width)])

    while rows and not any(rows[-1]):
        rows.pop()
    return rows


# ---------------------------------------------------------------- csv


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_csv(path: str) -> list:
    with open(path, "rb") as fh:
        text = _decode(fh.read())
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [[(c or "").strip() for c in row] for row in reader]
    while rows and not any(rows[-1]):
        rows.pop()
    return rows


# ---------------------------------------------------------------- 入口


def read_table(path: str) -> list:
    """按扩展名读表，返回 list[list[str]]。"""
    lower = path.lower()
    if lower.endswith(".xlsx") or lower.endswith(".xlsm"):
        return read_xlsx(path)
    if lower.endswith((".csv", ".tsv", ".txt")):
        return read_csv(path)
    if lower.endswith(".xls"):
        raise ValueError(
            "旧版 .xls 是二进制格式，标准库读不了。请在 Excel/WPS 里另存为 .xlsx 或 .csv 再发。"
        )
    raise ValueError(f"不认识的表格格式：{path}")


def to_records(rows: list, header_row: int = 0) -> tuple:
    """首行当表头，转成 list[dict]。返回 (records, headers)。"""
    if not rows:
        return [], []
    headers = [str(h).strip() for h in rows[header_row]]
    # 表头有空列时补名字，否则后面的键会互相覆盖
    headers = [h or f"列{i + 1}" for i, h in enumerate(headers)]
    records = []
    for row in rows[header_row + 1:]:
        if not any(str(c).strip() for c in row):
            continue
        rec = {}
        for i, h in enumerate(headers):
            rec[h] = str(row[i]).strip() if i < len(row) else ""
        records.append(rec)
    return records, headers
