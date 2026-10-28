"""随口记 · 表格读取（只用标准库）。

不装 openpyxl/pandas：xlsx 本身就是 zip + XML，读销售表这类规整数据足够。
CSV 额外兼容 GBK —— 国内系统导出的表经常是 GBK，按 UTF-8 读会整片乱码。
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import os
import pyexpat
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib

# Excel 内置的日期数字格式
_BUILTIN_DATE_FMT = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))

_COL_RE = re.compile(r"^([A-Z]{1,3})(\d{1,7})$")
MAX_XLSX_MEMBERS = 1000
MAX_XLSX_UNCOMPRESSED = 50 * 1024 * 1024
MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_COLUMN = 16383
MAX_ROW = 1048576
MAX_SHARED_STRINGS = 200000
_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0"
_SAVE_AS = "旧版 .xls 是二进制格式，标准库读不了。请在 Excel/WPS 里另存为 .xlsx 或 .csv 再发。"


class XlsxLimitError(ValueError):
    """压缩包成员或解压后体积超过上传合同。"""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def col_to_index(ref: str) -> int:
    """'B3' -> 1（0 基列号）。列只到 XFD，行只到 1048576。"""
    match = _COL_RE.match(ref or "")
    if not match:
        raise ValueError("单元格位置不正确")
    letters = match.group(1)
    number = 0
    for char in letters:
        number = number * 26 + (ord(char) - ord("A") + 1)
    index = number - 1
    if index > MAX_COLUMN or int(match.group(2)) > MAX_ROW:
        raise ValueError("单元格超出 XFD1048576")
    return index


class _SafeTreeBuilder(ET.TreeBuilder):
    """pyexpat 2.2.8 不提供拒绝 DTD 的开关，实体声明直接失败。"""

    def __init__(self):
        super().__init__()
        self._entities = {}


def _xml(raw: bytes, budget: list) -> ET.Element:
    if len(raw) > budget[0]:
        raise XlsxLimitError("xlsx 解压内容超过 50MiB")
    budget[0] -= len(raw)
    builder = _SafeTreeBuilder()
    parser = pyexpat.ParserCreate()
    parser.UseForeignDTD(False)
    parser.SetParamEntityParsing(pyexpat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartElementHandler = builder.start
    parser.EndElementHandler = builder.end
    parser.CharacterDataHandler = builder.data

    def reject(*_args):
        raise ValueError("xlsx 不允许文档类型或实体声明")

    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    try:
        parser.Parse(raw, True)
    except pyexpat.ExpatError as exc:
        raise ValueError("xlsx 的 XML 无法读取") from exc
    return builder.close()


def _member_path(name: str) -> str:
    if (not name or name.startswith("/") or name.startswith("\\") or "\\" in name
            or ".." in name.split("/")):
        raise ValueError("xlsx 成员路径不合法")
    return name


def _inflate(zf: zipfile.ZipFile, member: zipfile.ZipInfo, budget: list) -> bytes:
    """按实际输出计量。ZipExtFile 在 Python 3.9 上不能当作解压上限。"""
    if member.flag_bits & 0x1:
        raise ValueError("xlsx 不能包含加密成员")
    if member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise ValueError("xlsx 只接受存储或 deflate 成员")
    if member.compress_size < 0 or member.file_size < 0 or member.file_size > budget[0]:
        raise XlsxLimitError("xlsx 解压内容超过 50MiB")
    try:
        source = zf.open(member)
    except RuntimeError as exc:
        raise ValueError("xlsx 不能包含加密成员") from exc
    with source:
        if member.compress_type == zipfile.ZIP_STORED:
            output = bytearray()
            while True:
                chunk = source.read(65536)
                if not chunk:
                    break
                if len(output) + len(chunk) > budget[0]:
                    raise XlsxLimitError("xlsx 解压内容超过 50MiB")
                output.extend(chunk)
            return bytes(output)
        decoder = zlib.decompressobj(-15)
        output = bytearray()
        while True:
            chunk = source.read(65536)
            if not chunk and not decoder.unconsumed_tail:
                break
            piece = decoder.decompress(chunk, budget[0] - len(output) + 1)
            output.extend(piece)
            if len(output) > budget[0]:
                raise XlsxLimitError("xlsx 解压内容超过 50MiB")
            if not chunk:
                break
        tail = decoder.flush()
        output.extend(tail)
        if len(output) > budget[0]:
            raise XlsxLimitError("xlsx 解压内容超过 50MiB")
        return bytes(output)


def _open_package(path: str) -> tuple:
    """先限制原始文件，再按实测解压量打开。"""
    size = os.path.getsize(path)
    if size > 10 * 1024 * 1024:
        raise XlsxLimitError("XLSX 不能超过 10MiB")
    zf = zipfile.ZipFile(path)
    try:
        members = zf.infolist()
        if len(members) > MAX_XLSX_MEMBERS:
            raise XlsxLimitError("xlsx 成员超过 1000 个")
        compressed = 0
        for member in members:
            _member_path(member.filename)
            compressed += member.compress_size
            if compressed > size:
                raise XlsxLimitError("xlsx 压缩成员超过原始文件")
        return zf, members, [MAX_XLSX_UNCOMPRESSED]
    except BaseException:
        zf.close()
        raise


def _read_member(zf, members, name, budget):
    matches = [member for member in members if member.filename == name]
    if len(matches) != 1:
        raise ValueError(f"xlsx 缺少 {name}")
    return _inflate(zf, matches[0], budget)


def _target(root: ET.Element, rels: ET.Element, relationship: str) -> str:
    relation = next((node for node in rels.iter() if node.get("Id") == relationship), None)
    target = relation.get("Target") if relation is not None else None
    if not target:
        raise ValueError("xlsx 的工作簿关系不完整")
    if target.startswith("/"):
        target = target[1:]
    else:
        target = "xl/" + target
    return _member_path(target)


def _sheets(zf, members, budget) -> list:
    workbook = _xml(_read_member(zf, members, "xl/workbook.xml", budget), budget)
    rels = _xml(_read_member(zf, members, "xl/_rels/workbook.xml.rels", budget), budget)
    sheets = []
    for node in workbook.iter():
        if _local(node.tag) != "sheet":
            continue
        if node.get("state", "visible") != "visible":
            continue
        relationship = node.get("r:id") or node.get(
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        sheets.append((node.get("sheetId") or "", _target(workbook, rels, relationship)))
    if not sheets:
        raise ValueError("xlsx 里没有可见工作表")
    return sheets


def _plain_text(node: ET.Element) -> str:
    """只取真实文本，不把拼音 rPh 拼进单元格。"""
    parts = []

    def walk(current, phonetic):
        if _local(current.tag) == "rPh":
            phonetic = True
        if _local(current.tag) == "t" and not phonetic:
            parts.append(current.text or "")
        for child in list(current):
            walk(child, phonetic)

    walk(node, False)
    return "".join(parts)


def _shared_strings(zf, members, budget) -> list:
    rels = _xml(_read_member(zf, members, "xl/_rels/workbook.xml.rels", budget), budget)
    shared_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings"
    matches = [node for node in rels if node.get("Type") == shared_type]
    if not matches:
        return []
    if len(matches) != 1:
        raise ValueError("xlsx 的共享字符串关系不唯一")
    raw = _read_member(zf, members, _target(rels, rels, matches[0].get("Id")), budget)
    root = _xml(raw, budget)
    values = []
    for node in list(root):
        if _local(node.tag) != "si":
            continue
        values.append(_plain_text(node))
        if len(values) > MAX_SHARED_STRINGS:
            raise XlsxLimitError("xlsx 共享字符串超过上限")
    return values


def _date_styles(zf, members, budget) -> set:
    try:
        raw = _read_member(zf, members, "xl/styles.xml", budget)
    except ValueError:
        return set()
    root = _xml(raw, budget)
    custom = {}
    for node in root.iter():
        if _local(node.tag) != "numFmt":
            continue
        code = re.sub(r'"[^"]*"', "", node.get("formatCode") or "").lower()
        if any(char in code for char in ("y", "d")) or "m" in code:
            custom[int(node.get("numFmtId"))] = True
    cell_xfs = next((node for node in root.iter() if _local(node.tag) == "cellXfs"), None)
    if cell_xfs is None:
        return set()
    result = set()
    for index, node in enumerate(list(cell_xfs)):
        value = node.get("numFmtId")
        if value is not None and (int(value) in _BUILTIN_DATE_FMT or custom.get(int(value))):
            result.add(index)
    return result


def _excel_serial_to_date(value: float) -> str:
    # Excel 的 1900 历法有个著名的闰年 bug，序列号 60 = 1900-02-29（不存在）
    return (_dt.date(1899, 12, 30) + _dt.timedelta(days=int(value))).isoformat()


def _reject_active_package(members) -> None:
    names = {member.filename for member in members}
    forbidden = ("xl/vbaProject.bin", "encryptioninfo", "encryptedpackage")
    for name in names:
        lower = name.lower()
        if (lower in forbidden or lower.startswith("xl/externallinks/")
                or lower.startswith("xl/embeddings/") or lower.endswith(".bin")):
            raise ValueError("xlsx 不能包含宏、外链、嵌入对象或加密包")


def read_xlsx(path: str, sheet_index: int = 0) -> tuple:
    """读指定可见工作表，返回 (rows, sheet_id, flags)。"""
    zf, members, budget = _open_package(path)
    with zf:
        _reject_active_package(members)
        _read_member(zf, members, "[Content_Types].xml", budget)
        sheets = _sheets(zf, members, budget)
        if isinstance(sheet_index, bool) or not isinstance(sheet_index, int) or not 0 <= sheet_index < len(sheets):
            raise ValueError("工作表序号不存在")
        sheet_id, target = sheets[sheet_index]
        shared = _shared_strings(zf, members, budget)
        date_styles = _date_styles(zf, members, budget)
        root = _xml(_read_member(zf, members, target, budget), budget)

    rows, flags = [], {}
    for row_el in root.iter():
        if _local(row_el.tag) != "row":
            continue
        row_number = row_el.get("r")
        if row_number is not None and (not row_number.isdigit() or int(row_number) > MAX_ROW):
            raise ValueError("单元格超出 XFD1048576")
        cells = {}
        for cell in list(row_el):
            if _local(cell.tag) != "c":
                continue
            index = col_to_index(cell.get("r") or "")
            formula = next((item for item in list(cell) if _local(item.tag) == "f"), None)
            if formula is not None and formula.text and "[" in formula.text:
                raise ValueError("xlsx 不能包含外部公式")
            value = next((item for item in list(cell) if _local(item.tag) == "v"), None)
            cached = value.text if value is not None and value.text is not None else ""
            kind = cell.get("t") or "n"
            if formula is not None and not cached:
                text, flags[(len(rows), index)] = "待核对", "unverified"
            elif formula is not None:
                text, flags[(len(rows), index)] = cached, "unverified"
            elif kind == "inlineStr":
                inline = next((item for item in list(cell) if _local(item.tag) == "is"), None)
                text = _plain_text(inline) if inline is not None else ""
            elif kind == "s":
                try:
                    text = shared[int(cached)]
                except (ValueError, IndexError):
                    text = ""
            elif kind == "b":
                text = "TRUE" if cached == "1" else "FALSE"
            elif kind in ("str", "e"):
                text = cached
            else:
                text = cached
                style = cell.get("s")
                if cached and style is not None and int(style) in date_styles:
                    try:
                        text = _excel_serial_to_date(float(cached))
                    except (ValueError, OverflowError):
                        pass
            cells[index] = text.strip()
        if not cells:
            rows.append([])
            continue
        width = max(cells) + 1
        rows.append([cells.get(index, "") for index in range(width)])
    while rows and not any(rows[-1]):
        rows.pop()
    return rows, sheet_id, flags


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("CSV 不是 UTF-8 或 GB18030")


def read_csv(path: str) -> list:
    size = os.path.getsize(path)
    if size > MAX_CSV_BYTES:
        raise ValueError("CSV 内容不能超过 2 MB")
    with open(path, "rb") as handle:
        text = _decode(handle.read(MAX_CSV_BYTES + 1))
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    rows = [[(cell or "").strip() for cell in row] for row in csv.reader(io.StringIO(text), dialect)]
    while rows and not any(rows[-1]):
        rows.pop()
    return rows


def read_table(path: str, sheet_index: int = 0):
    """按扩展名读表。XLSX 返回 (rows, sheet_id, flags)，其他格式只返回 rows。"""
    lower = path.lower()
    with open(path, "rb") as handle:
        signature = handle.read(4)
    if lower.endswith(".xlsm"):
        raise ValueError("不接受带宏的 .xlsm，请另存为不含宏的 .xlsx")
    if signature == _OLE_SIGNATURE:
        raise ValueError(_SAVE_AS)
    if lower.endswith(".xlsx"):
        return read_xlsx(path, sheet_index=sheet_index)
    if lower.endswith((".csv", ".tsv", ".txt")):
        return read_csv(path)
    if lower.endswith(".xls"):
        raise ValueError(_SAVE_AS)
    raise ValueError(f"不认识的表格格式：{path}")


def to_records(rows: list, header_row: int = 0) -> tuple:
    """按列位置保留表头。重复或空表头不会被字典键合并。"""
    if isinstance(header_row, bool) or not isinstance(header_row, int) or not rows or not 0 <= header_row < len(rows):
        raise ValueError("表头行不正确")
    source = [str(value).strip() for value in rows[header_row]]
    counts = {}
    headers = []
    for index, value in enumerate(source):
        counts[value] = counts.get(value, 0) + 1
        if not value:
            headers.append(f"列{index + 1}")
        elif counts[value] == 1 and source.count(value) == 1:
            headers.append(value)
        else:
            headers.append(f"{value}（第{index + 1}列）")
    records = []
    for row in rows[header_row + 1:]:
        if not any(str(cell).strip() for cell in row):
            continue
        records.append({header: (str(row[index]).strip() if index < len(row) else "")
                        for index, header in enumerate(headers)})
    return records, headers
