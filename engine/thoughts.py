"""生意随想的独立校验与有限原文切片；不解释金额，不创建任务。"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_REQUIRED = {"date", "body", "source_quote"}
_OPTIONAL = {"title", "tags"}


def normalize_tag(tag):
    """写入与查询共用：NFC、首尾空白、至多一个 #，保留大小写。"""
    if not isinstance(tag, str):
        raise ValueError("标签必须是文本")
    tag = unicodedata.normalize("NFC", tag).strip()
    if tag.startswith("#"):
        tag = tag[1:].strip()
    if not 1 <= len(tag) <= 32:
        raise ValueError("标签必须是 1 到 32 个字符")
    if any(ch.isspace() or unicodedata.category(ch).startswith("C") or ch == "#" for ch in tag):
        raise ValueError("标签不能含空白、控制字符或多余的 #")
    return tag


def normalize_tags(tags):
    if not isinstance(tags, list) or len(tags) > 20:
        raise ValueError("标签必须是最多 20 项的数组")
    # 先检查输入数量，再去重；绝不修改参与幂等指纹的原始数组。
    return list(dict.fromkeys(normalize_tag(tag) for tag in tags))


def _text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label}必须是 1 到 {limit} 个字符的非空文本")
    if "\x00" in value:
        raise ValueError(f"{label}不能包含 U+0000 字符")
    return value


def validate_thought(row, raw_text):
    """返回独立校验副本。仅标签规范化；正文、标题、来源逐字保留。"""
    if not isinstance(row, dict):
        raise ValueError("随想必须是对象")
    if _REQUIRED - row.keys():
        raise ValueError("随想缺少日期、正文或来源片段")
    if row.keys() - _REQUIRED - _OPTIONAL:
        raise ValueError("随想含有未允许的字段")
    date = row["date"]
    if not isinstance(date, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date):
        raise ValueError("随想记录日期必须是 YYYY-MM-DD")
    try:
        dt.date.fromisoformat(date)
    except ValueError as exc:
        raise ValueError("随想记录日期不存在") from exc
    _text(row["body"], "随想正文", 2000)
    quote = _text(row["source_quote"], "随想来源片段", 2000)
    if not isinstance(raw_text, str) or quote not in raw_text:
        raise ValueError("随想来源片段必须是原话中连续、完全一致的片段")
    if "title" in row:
        _text(row["title"], "随想标题", 80)
    return {**row, "tags": normalize_tags(row.get("tags", []))}


def receipt_date(received_at, timezone):
    """接收时刻在工作空间的日历日期；旧无偏移时间视为当地墙上时间。"""
    try:
        received = dt.datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        zone = None if timezone == "local" else ZoneInfo(timezone)
        if received.tzinfo is not None:
            received = received.astimezone(zone)
        return received.date().isoformat()
    except (AttributeError, TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("接收时间或工作空间时区不合法") from exc


# 每个句号、分号、问叹号、换行后独立判断。小数点不是分句符。
_SENTENCE_END = re.compile(r"[。；;！!？?\r\n]+|(?<!\d)\.|\.(?!\d)")
_LITERAL = re.compile(r"`[^`\r\n]*`|\[[^\]\r\n]*\]\([^\)\r\n]*\)|https?://[^\s<>。；！？，]*[^\s<>。；！？，.]")
_MARKER = re.compile(r"(?:^|[，,])\s*记个(?:想法|随想)[:：]\s*")
_TRIAL = re.compile(
    r"^(?:(?:明天|下周)(?:我)?(?:想试|试做|想做|打算试|打算做|试)"
    r"|(?:明天|下周)(?:我)?想推(?:[一-龥A-Za-z0-9]{0,12})新品"
    r"|(?:今天|昨天)想到)"
)
_ARCHIVE = re.compile(r"(?:随想|想法|记事)(?:请)?(?:记在|记到|归档到|归档在|归到|归在)")
_ARCHIVE_REFERENCE = re.compile(r"(?:这条|这则|请|帮我)(?:随想|想法|记事)?(?:请)?(?:记在|记到|归档到|归档在|归到|归在)")
_TAGS_ONLY = re.compile(r"(?:#[^\s。；;！!？?]+\s*)+")
_TAG = re.compile(r"(?<![^\s。；;！!？?])#[^\s。；;！!？?]*")
_NUMBER = r"(?:\d+(?:[,.]\d+)*|[零〇一二两三四五六七八九十百千万]+)"
_APPROX_SUFFIX = re.compile(r"\s*(?:元|块|人|位|桌|单|分|个)?\s*(?:左右|上下|多(?!云)|余)")
_APPROX_AMOUNT = re.compile(_NUMBER + _APPROX_SUFFIX.pattern)
_APPROX = re.compile(r"大概|大约|差不多|可能|估计|预计|左右|上下|" + _APPROX_AMOUNT.pattern)
_UNCERTAIN_THOUGHT = re.compile(r"记个想法|记个随想|想(?:试|做|推|卖|用)|打算|准备|预算|计划|售价|试做|可以搭配")
_CLAUSE_END = re.compile(r"[，。；;！!？?\r\n]|(?<!\d),|,(?!\d{3}(?!\d))")
# 仅让带自己金额的明确实际锚点结束前面的不确定范围；保留逐字偏移。
_ACTUAL_ANCHOR = re.compile(
    r"(?:(?:今天|今日|昨天|昨日|前天|大前天)\s*)?"
    r"(?:营业额|销售额|营收|流水|收入|收款|进账|进货|采购|买了|交房租|房租|付水电|水电|工资)"
)


def mask_spans(text, spans):
    """等长硬句界：日期和数量不能跨过隐藏随想，偏移仍对应原话。"""
    chars = list(text)
    for span in spans:
        chars[span["start"]:span["end"]] = "；" * (span["end"] - span["start"])
    return "".join(chars)


def source_segments(text, spans):
    """逐段返回未被认领的连续原文；隐藏范围是解析器不能跨越的边界。"""
    # 旧金额解析器不识别英文句点；从原文识别后作为独立边界，小数和字面量不切开。
    sentence_dots = [{"start": end, "end": next_start}
                     for _, end, next_start in _sentences(text) if text[end:next_start] == "."]
    cursor = 0
    for span in sorted(spans + sentence_dots, key=lambda value: (value["start"], value["end"])):
        if cursor < span["start"]:
            yield cursor, span["start"]
        cursor = max(cursor, span["end"])
    if cursor < len(text):
        yield cursor, len(text)


def _sentences(text):
    start = 0
    literals = list(_LITERAL.finditer(text))
    for match in _SENTENCE_END.finditer(text):
        if any(literal.start() <= match.start() < literal.end() for literal in literals):
            continue
        yield start, match.start(), match.end()
        start = match.end()
    if start < len(text):
        yield start, len(text), len(text)


def extract_native(text, date, *, observation_finder=None):
    """返回 thoughts、隔离 spans、逐字 leftovers 与中文 warnings。

    标记只认到本句结束；仅紧随其后的纯 #标签 段可续接。跨句正文不
    自动合并，后句明确账目照常解析。归档指令无法安全指向哪条随想时，
    本条消息的候选随想全部留待外部 Agent 整理。
    """
    candidates, blocked, warnings = [], [], []
    sentences = list(_sentences(text))
    archive = bool(_ARCHIVE.search(text) or (re.search(r"记个(?:想法|随想)[:：]", text)
                                              and _ARCHIVE_REFERENCE.search(text)))
    for start, content_end, end in sentences:
        content = text[start:content_end]
        stripped = content.strip()
        if not stripped:
            continue
        if (candidates and _TAGS_ONLY.fullmatch(stripped)
                and not text[candidates[-1]["end"]:start].strip()):
            candidates[-1]["end"] = end
            continue
        markers = list(_MARKER.finditer(content))
        if markers:
            for index, marker in enumerate(markers):
                lower = start + marker.start()
                if text[lower:lower + 1] in "，,":
                    lower += 1
                upper = start + markers[index + 1].start() if index + 1 < len(markers) else end
                candidates.append({"start": lower, "end": upper, "body_start": start + marker.end()})
        elif _TRIAL.match(stripped):
            lower = start + len(content) - len(content.lstrip())
            candidates.append({"start": lower, "end": end, "body_start": lower})
        if _ARCHIVE.search(content) or (archive and _ARCHIVE_REFERENCE.search(content)):
            blocked.append({"start": start, "end": end})
    rows, spans, leftovers = [], [], []
    for candidate in candidates:
        spans.append(candidate)
        quote = text[candidate["body_start"]:candidate["end"]].strip()
        if archive:
            blocked.append(candidate)
            continue
        tags = [match.group() for match in _TAG.finditer(quote)]
        # 只去掉正文末尾的显式标签串；正文其余部分仍是连续原文切片。
        body = quote
        matches = list(_TAG.finditer(quote))
        if matches:
            first = matches[-1]
            for match in reversed(matches[:-1]):
                if quote[match.end():first.start()].strip():
                    break
                first = match
            if not quote[matches[-1].end():].strip(" 。；;！!？?\r\n"):
                body = quote[:first.start()].rstrip()
        try:
            rows.append(validate_thought({"date": date, "body": body,
                                          "tags": tags, "source_quote": quote}, text))
        except ValueError as exc:
            blocked.append(candidate)
            warnings.append("这段随想尚未保存：" + str(exc))
    if archive:
        warnings.append("原生录入暂不支持指定随想归档日，相关原话尚未保存为随想，需要外部 Agent 整理。")
    # 近似金额和不受支持的试做句不可经旧解析器兜底成精确账目。
    note_spans = spans + blocked
    hidden = mask_spans(text, note_spans)
    # 金额后缀可以换行；先定位整个 token，不能把“两百\n多”切成精确200。
    approximate = list(_APPROX_AMOUNT.finditer(hidden))
    boundaries = [match for match in _CLAUSE_END.finditer(hidden)
                  if not any(token.start() < match.start() < token.end() for token in approximate)]
    literals = list(_LITERAL.finditer(text))
    boundaries.extend(match for match in _SENTENCE_END.finditer(text) if match.group() == "."
                      and not any(span["start"] <= match.start() < span["end"] for span in note_spans)
                      and not any(literal.start() <= match.start() < literal.end() for literal in literals))
    boundaries.sort(key=lambda match: match.start())
    lower, uncertain = 0, False
    for boundary in boundaries + [None]:
        upper = boundary.start() if boundary else len(hidden)
        clause = hidden[lower:upper]
        cuts = [0]
        observations = observation_finder(clause, date) if observation_finder else []
        anchors = [(match.start(), match.end(), False) for match in _ACTUAL_ANCHOR.finditer(clause)]
        for hit in observations:
            start = hit["start"]
            preceding_date = re.search(r"(?:今天|今日|昨天|昨日|前天|大前天)\s*$", clause[:start])
            if preceding_date:
                start = preceding_date.start()
            anchors.append((start, hit["end"], True))
        for anchor_start, anchor_end, is_observation in sorted(anchors):
            preceding = list(re.finditer(_NUMBER, clause[cuts[-1]:anchor_start]))
            if preceding and (is_observation or re.search(_NUMBER, clause[anchor_end:])):
                # 「营业额1300元可能收入一千」的“可能”属于第二段。
                gap_start = cuts[-1] + preceding[-1].end()
                suffix = _APPROX_SUFFIX.match(clause[gap_start:anchor_start])
                if suffix:
                    gap_start += suffix.end()
                qualifiers = [hit for pattern in (_APPROX, _UNCERTAIN_THOUGHT)
                              for hit in [pattern.search(clause[gap_start:anchor_start])] if hit]
                cuts.append(gap_start + min(hit.start() for hit in qualifiers) if qualifiers else anchor_start)
        cuts.append(len(clause))
        for start, end in zip(cuts, cuts[1:]):
            fragment = clause[start:end]
            risk = bool(_APPROX.search(fragment) or _UNCERTAIN_THOUGHT.search(fragment))
            # 近似/预算句后的裸金额或“卖12元”不能重新变成精确账。
            has_observation = any(start <= hit["start"] and hit["end"] <= end for hit in observations)
            date_only = re.fullmatch(r"\s*(?:今天|今日|昨天|昨日|前天|大前天|当天|明天)\s*", fragment)
            continuation = (uncertain and not _ACTUAL_ANCHOR.search(fragment) and not has_observation
                            and (re.search(_NUMBER, fragment) or date_only))
            if risk or continuation:
                fragment_start = lower + start
                if (continuation and blocked
                        and not text[blocked[-1]["end"]:fragment_start].strip()):
                    blocked[-1]["end"] = lower + end
                else:
                    blocked.append({"start": fragment_start, "end": lower + end})
                uncertain = True
            elif fragment.strip():
                uncertain = False
        if boundary and boundary.group() not in ("，", ",", "\r", "\n"):
            uncertain = False
        lower = boundary.end() if boundary else len(hidden)
    # 合并重叠范围，防止归档指令及其候选随想重复出现在 leftovers。
    merged = []
    for span in sorted(blocked, key=lambda value: (value["start"], value["end"])):
        if merged and span["start"] < merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], span["end"])
        else:
            merged.append({"start": span["start"], "end": span["end"]})
    for span in merged:
        fragment = text[span["start"]:span["end"]].strip()
        if fragment:
            leftovers.append(fragment)
    return {"thoughts": rows, "spans": spans + merged, "note_spans": note_spans,
            "leftovers": leftovers, "warnings": list(dict.fromkeys(warnings))}
