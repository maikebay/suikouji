"""产品固定使用说明，以及每个工作空间自己维护的补充。

官方段落随产品代码发布，不写入数据库，Agent 不能改、不能删。
本店补充按工作空间保存；换一个 Agent 读到的仍是同一份。
产品升级只替换官方段落，不改补充，也不改账目、分类或名称规则。
"""
from __future__ import annotations

from .model import now_iso


GUIDE_VERSION = "usage-guide-1"
OFFICIAL_LABEL = "产品固定说明"
SUPPLEMENTS_LABEL = "本店补充"
MAX_NOTES = 4000

# 安装交付时必须让 Agent 看到的原句。官方正文不能靠聊天记忆传递。
INSTALL_DELIVERY = (
    "安装或连接完成后先读取这份固定使用说明。"
    "官方部分由产品提供、不能改。"
    "本店补充可由 Agent 在后续使用中继续维护。"
    "换 Agent 也读同一份。"
    "升级产品只更新官方部分。"
)

PRECEDENCE = "产品固定说明优先。本店补充只追加这一家店的说明，不能改掉、删掉或抵消官方规则。"

# 写入这些字段表示在替换官方正文，而不是在维护本店补充。
OFFICIAL_WRITE_FIELDS = frozenset({
    "official", "official_sections", "official_text", "sections",
    "guide", "product_guide", "usage_guide", "guide_version",
    "official_label", "official_readonly",
})
NOTES_WRITE_FIELDS = frozenset({"workspace_id", "request_id", "notes"})

_SECTIONS = (
    {
        "id": "after_connect",
        "title": "安装或刚连上之后先做什么",
        "body": (
            "安装或连接完成后，先读这份使用说明，再读 GET /v1/capabilities，并确认当前是哪本账。"
            "不要靠某一次聊天记忆决定怎么记。"
            "标成「产品固定说明」的部分由产品随版本提供，Agent 不能改、不能删，也不要抄进数据库。"
            "「本店补充」只写这一家店的补充，例如已经确认的叫法和口径；以后可以继续改这段补充，不要重写官方规则，补充也不能抵消官方规则。"
            "同一工作空间换一个 Agent，读到的仍是同一份：官方正文加上这家店的补充。"
            "产品升级只更新官方部分，本店补充留在这家店。"
            "字段长度、重试、导入、表结构和微信收取的细节，以能力契约和 OpenAPI 为准，这里不重复。"
        ),
    },
    {
        "id": "capture",
        "title": "记一笔",
        "body": (
            "先整理口语：把已经发生的收入、进货、费用、销量，和想法、没说清的部分分开。"
            "提交时带上用户的原话，以及整理后的结果。原话不要改写。"
            "金额、日期、记到哪本账、是流水总额还是结算实收，有一样不确定，就先问，不要猜。"
            "「大概」「左右」「两百多」不能写成精确金额。打算、预算、报价还没发生，不要记成已经入账。"
            "没整理进去的原话留在未入账片段里。先告诉用户哪些已经记下，哪些还没入账。"
        ),
    },
    {
        "id": "lookup",
        "title": "查某一项",
        "body": (
            "账在数据库里。要找某一笔或某个名称，用关键词、时间、类型、分类筛选，调用 GET /v1/entries。"
            "一次拿回匹配明细和合计，合计里包含分年金额。先把这些明细和合计告诉用户。"
            "不要把账全部拉出来，再在模型里逐条筛。也不要把全部分类、相同金额和随想交叉核对当作默认做法。"
            "关键词只匹配账目名称。这次合计是匹配账目的金额之和，不是整月收支差额，也不是利润。"
            "不要用这次查询猜测某个名称该归哪一类。已作废的账默认不在明细和合计里。"
        ),
    },
    {
        "id": "month",
        "title": "看某个月",
        "body": (
            "看整月或整段收支，用 GET /v1/summary。不要自己把账拉出来加总。"
            "营业额、进货付款、费用付款和收支差额分开说。"
            "收支差额只是已记下的收入减去进货和费用，不要说成利润。进货付款也不是已经用掉的原料成本。"
            "没有成本或库存依据时，利润说「暂无法计算」，并说明缺什么。还没记过的日子不要当成 0。"
        ),
    },
    {
        "id": "correct",
        "title": "记错了怎么改",
        "body": (
            "先向用户确认是哪一笔、要改什么。得到确认后再动。"
            "整笔记错、不该继续计入时，做软作废：原记录还在，默认的汇总和列表不再计入。不要物理删除。"
            "金额或分类写错、这笔仍然要留，用修正，并写上依据。只改这一笔时，不要顺手改掉以后的记法。"
            "软作废不能代替修正。别的店的账看不到，也不要改。具体字段和重试见能力契约。"
        ),
    },
    {
        "id": "name_later",
        "title": "这个名称以后怎么记",
        "body": (
            "某个名称以后怎么归类，以这本账里的名称规则为准。先读 GET /v1/name-rules。"
            "不要靠聊天记忆，也不要翻历史账来猜分类。历史出现过几次都不能代替规则。"
            "用户明确说以后都这样记，并确认了类型、分类和账单上的分类，再保存已确认规则。已经入账的记录不改写。"
            "听不清是只改一次还是以后都这样，就先问。还没确认的候选不要拿来入账。"
            "这一条没写分类，又没有已确认规则，就问用户。不要猜。"
        ),
    },
    {
        "id": "thoughts",
        "title": "记想法、回头找想法",
        "body": (
            "想法、新品打算和经营感受跟账分开记。随想不计入收入、进货或费用，也不变成任务或提醒。"
            "可以和账写在同一次原话里，也可以只记想法。没有标签也可以记，不要为了标签追问。"
            "正文里的「明天」「下周」留在想法里，不表示已经发生，也不改归档日期，除非用户明确指定记在哪一天。"
            "回头找想法时按记录日期、标签或关键词查随想，不要到账目里翻。"
        ),
    },
    {
        "id": "speaking",
        "title": "跟用户怎么说",
        "body": (
            "用这家店的名字和明白的中文。先说正在看哪本账、哪段时间、哪些事。"
            "不报接口名、字段名、路径，也不把服务器上的文件路径当成可以下载的地址。"
            "查到的明细和合计先说出来。整月报表里，收支差额不要说成利润。还没算出来的写成「暂无法计算」。"
            "只说已经做完的事。范围没定，就先问范围。"
        ),
    },
    {
        "id": "never",
        "title": "绝对不要做的事",
        "body": (
            "不要猜分类。"
            "不要把「大概」写成精确金额。"
            "不要把账全部拉出来再筛选。"
            "不要把打算或预算说成已经发生。"
            "不要把收支差额说成利润。"
            "不要把还没记录的当成 0。"
        ),
    },
)


def official_sections() -> list:
    """返回官方段落的副本。调用方改返回值不会改产品正文。"""
    return [
        {"label": OFFICIAL_LABEL, "id": item["id"], "title": item["title"], "body": item["body"]}
        for item in _SECTIONS
    ]


def usage_guide_policy() -> dict:
    """放进 GET /v1/capabilities，让只读能力接口的 Agent 也能拿到固定用法。"""
    return {
        "id": "agent.usage_guide",
        "guide_version": GUIDE_VERSION,
        "official_readonly": True,
        "official_label": OFFICIAL_LABEL,
        "supplements_label": SUPPLEMENTS_LABEL,
        "precedence": PRECEDENCE,
        "install_delivery": INSTALL_DELIVERY,
        "read": {"method": "GET", "path": "/v1/usage-guide", "workspace_required": True},
        "write": {
            "method": "POST", "path": "/v1/usage-guide",
            "writes": "workspace_notes_only",
            "official_readonly": True,
            "idempotency": "request_id scoped to principal, workspace, and operation",
        },
        "notes_max_length": MAX_NOTES,
        "empty_notes": "新工作空间返回完整官方说明，本店补充为空，不必把官方正文写入数据库。",
        "sharing": "同一工作空间的每个已授权 Agent 读到同一份官方说明和本店补充。",
        "upgrade": "升级产品只更新官方部分；本店补充留在该工作空间，不改历史账目、分类或名称规则。",
        "field_rules": "字段级限制、重试、导入、schema 与 iLink 细节见本契约其余部分和 OpenAPI，不要从使用说明里猜。",
        "official_sections": official_sections(),
    }


def guide_document(workspace_id: str, notes: str, updated_at, updated_by) -> dict:
    present = notes != ""
    return {
        "workspace_id": workspace_id,
        "guide_version": GUIDE_VERSION,
        "official_readonly": True,
        "precedence": PRECEDENCE,
        "official": {
            "label": OFFICIAL_LABEL,
            "sections": official_sections(),
        },
        "supplements": {
            "label": SUPPLEMENTS_LABEL,
            "notes": notes,
            "updated_at": updated_at if present else None,
            "updated_by": updated_by if present else None,
        },
        "contract": {
            "capabilities": "GET /v1/capabilities",
            "detail": "docs/agent-capabilities.md",
            "openapi": "docs/openapi.yaml",
        },
    }


class UsageGuideStore:
    """只读写本店补充。官方正文不进这张表。"""

    def __init__(self, store):
        self.store = store

    def read(self, workspace_id: str) -> dict:
        row = self.store.conn.execute(
            "SELECT notes, updated_at, updated_by FROM usage_guide_notes WHERE shop_id=?",
            (workspace_id,),
        ).fetchone()
        if not row:
            return {"notes": "", "updated_at": None, "updated_by": None}
        return {"notes": row["notes"], "updated_at": row["updated_at"], "updated_by": row["updated_by"]}

    def write(self, workspace_id: str, notes: str, actor: str) -> dict:
        if notes == "":
            self.store.conn.execute(
                "DELETE FROM usage_guide_notes WHERE shop_id=?", (workspace_id,))
            return {"notes": "", "updated_at": None, "updated_by": None}
        updated_at = now_iso()
        self.store.conn.execute(
            """INSERT INTO usage_guide_notes(shop_id, notes, updated_at, updated_by)
               VALUES(?,?,?,?)
               ON CONFLICT(shop_id) DO UPDATE SET
                 notes=excluded.notes,
                 updated_at=excluded.updated_at,
                 updated_by=excluded.updated_by""",
            (workspace_id, notes, updated_at, actor),
        )
        return {"notes": notes, "updated_at": updated_at, "updated_by": actor}
