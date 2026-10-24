"""self_edit —— 她自己的笔记

我造笔，她写字。

机制由代码保证（校验出处、限额、留档、可回滚），
内容由她自己生成——不在这里替她拟任何一句。

三层人设的注入顺序：
    AstrBot 人设卡  →  group_chat_plus 额外提示词  →  本插件注入的 自我.md

硬约束：
  ① because 必须能在外部记录源或最近对话里找到依据（防凭空造性格）
  ② 只追加，不能删改已有行
  ③ 总长 ≤ 800 字；写满必须先忘掉一条（额度制=遗忘）
  ④ 每天 ≤ 2 次，单条 ≤ 120 字
"""
import datetime
import json
import os
import re
import time
import urllib.parse
import urllib.request

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

# ==================== 配置 ====================
# ---- 可移植路径：优先 ASTRBOT_DATA 环境变量，否则 ~/astrbot/data ----
_DATA = os.environ.get("ASTRBOT_DATA") or os.path.join(os.path.expanduser("~"), "astrbot", "data")
PERSONA = os.environ.get("PERSONA", "她")


def _p(*parts):
    return os.path.join(_DATA, *parts)


SELF_DIR = _p("persona_self", PERSONA)
SELF_PATH = os.path.join(SELF_DIR, "自我.md")
HIST_DIR = os.path.join(SELF_DIR, "自我_历史")
FORGET_DIR = os.path.join(SELF_DIR, "遗忘")
STATE_PATH = os.path.join(SELF_DIR, "state.json")

MEM_LIST = "http://127.0.0.1:8001/外部检索口/api/memories"
FOLDER = os.environ.get("MEMORY_FOLDER", "记忆")

MAX_TOTAL = 800
MAX_LINE = 120
MAX_PER_DAY = 2
MIN_OVERLAP = 8          # because 与某条记忆至少连续重合 8 字
OWNER_UINS = {u.strip() for u in os.environ.get("OWNER_UINS", "").split(",") if u.strip()}


def _sender(event):
    try:
        return str(event.get_sender_id())
    except Exception:
        return "?"


def _is_owner(event):
    return _sender(event) in OWNER_UINS


def _load_state():
    d = {"day": "", "count": 0}
    if os.path.isfile(STATE_PATH):
        try:
            d.update(json.load(open(STATE_PATH, encoding="utf-8")) or {})
        except Exception:
            pass
    return d


def _save_state(s):
    os.makedirs(SELF_DIR, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def _read_self():
    if os.path.isfile(SELF_PATH):
        try:
            return open(SELF_PATH, encoding="utf-8").read()
        except Exception:
            return ""
    return ""


def _write_self(text):
    os.makedirs(SELF_DIR, exist_ok=True)
    tmp = SELF_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, SELF_PATH)


def _snapshot():
    """写前留档。"""
    os.makedirs(HIST_DIR, exist_ok=True)
    cur = _read_self()
    if not cur.strip():
        return ""
    name = time.strftime("%m%d-%H%M%S") + ".md"
    p = os.path.join(HIST_DIR, name)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(cur)
    return p


def _norm(s):
    """去掉标点空白，便于字符重合判断。"""
    return re.sub(r"[\s，。、！？；：\"'（）()\[\]【】…—·,.!?;:]+", "", s or "")


def _fetch_recent_memories(limit=50):
    """从外部记录源取最近的条目文本。失败返回空列表（宁可拒，不可放）。"""
    try:
        q = MEM_LIST + "?" + urllib.parse.urlencode(
            {"folder_id": FOLDER, "limit": limit})
        with urllib.request.urlopen(urllib.request.Request(q), timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        logger.warning("[self_edit] 读外部库失败: %r" % (e,))
        return []
    out = []
    for it in d.get("items") or []:
        meta = it.get("metadata") or {}
        out.append(((meta.get("title") or "") + " " + (it.get("content_snippet") or "")))
    return out


def _has_basis(because, extra_texts=()):
    """because 是否真有出处。取最长连续重合字符数 ≥ MIN_OVERLAP 即算有据。"""
    b = _norm(because)
    if len(b) < 4:
        return False, 0
    best = 0
    pool = list(_fetch_recent_memories(50)) + [t for t in extra_texts if t]
    for t in pool:
        n = _norm(t)
        if not n:
            continue
        # 用 because 的每个 ≥MIN_OVERLAP 窗口去撞
        for i in range(0, max(0, len(b) - MIN_OVERLAP) + 1):
            seg = b[i:i + MIN_OVERLAP]
            if seg and seg in n:
                return True, len(seg)
        # 粗算：字符集重合（兜底不了 → 保持 best=0）
    return best >= MIN_OVERLAP, best


@register("self_edit", "mumu", "她自己的笔记：可改、需出处、有遗忘额度", "1.0.0")
class SelfEdit(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        os.makedirs(SELF_DIR, exist_ok=True)
        logger.info("[self_edit] 自我笔记: %s（%d 字）" % (SELF_PATH, len(_read_self())))

    # ---------- 注入（在人设卡与额外提示词之后）----------
    @filter.on_llm_request()
    async def _inject(self, event: AstrMessageEvent, req: ProviderRequest):
        if event.get_extra("_self_injected"):
            return
        me = _read_self().strip()
        if not me:
            return
        try:
            base = getattr(req, "system_prompt", "") or ""
            req.system_prompt = base + "\n\n（我自己记下的事：\n" + me + "\n）"
            event.set_extra("_self_injected", True)
        except Exception as e:
            logger.error("[self_edit] 注入失败: %r" % (e,))

    # ---------- 工具：写一条 ----------
    @filter.llm_tool(name="adjust_self")
    async def adjust_self(self, event: AstrMessageEvent, text: str, because: str):
        """记下一条关于我自己的认识。必须说明是因为什么。

        Args:
            text(string): 关于我自己，一句话，不要超过 120 字
            because(string): 因为什么——必须指向一件真实发生过的事（记忆里的内容或刚才的对话）
        """
        if not _is_owner(event):
            return "只有主人能让我改自己的笔记。"

        t = (text or "").strip()
        b = (because or "").strip()
        if len(t) < 4:
            return "太短了，写清楚一点。"
        if len(t) > MAX_LINE:
            return "一条最多 %d 字，你这条 %d 字。拆开写。" % (MAX_LINE, len(t))
        if len(b) < 4:
            return "得说清是因为什么。没出处的自我认识不算认识。"

        # ① 出处校验（外部库 + 本轮用户原话）
        user_text = str(event.message_str or "")
        ok, hit = _has_basis(b, extra_texts=[user_text])
        if not ok:
            return ("找不到你说的那件事。是要记忆里有、或者刚才真的发生过，我才能记。"
                    "（依据：「%s」）" % b[:30])

        cur = _read_self()
        # ④ 每日次数
        st = _load_state()
        today = time.strftime("%Y-%m-%d")
        if st.get("day") != today:
            st = {"day": today, "count": 0}
        if int(st.get("count", 0)) >= MAX_PER_DAY:
            return "今天已经改过 %d 次了。明天再说。" % MAX_PER_DAY

        line = "- " + t
        new = (cur.rstrip() + "\n" + line).strip() + "\n"

        # ③ 总长上限
        if len(new) > MAX_TOTAL:
            return ("记满了（%d/%d 字）。先划掉一条再写：adjust_self_forget(第几行)。"
                    "\n现在的条目：\n%s" % (len(new), MAX_TOTAL, _numbered(cur)))

        # 留档后写
        snap = _snapshot()
        _write_self(new)
        st["count"] = int(st.get("count", 0)) + 1
        _save_state(st)
        logger.info("[self_edit] 追加一条（依据命中 %d 字）: %s" % (hit, t[:40]))
        return "记下了。" + ("（历史：%s）" % os.path.basename(snap) if snap else "")

    # ---------- 工具：划掉一条（遗忘额度）----------
    @filter.llm_tool(name="adjust_self_forget")
    async def adjust_self_forget(self, event: AstrMessageEvent, line: int):
        """划掉我自己笔记里的一条。写不下了，或者不想再这样看自己了。

        Args:
            line(int): 第几行（从 1 开始，只看条目行）
        """
        if not _is_owner(event):
            return "只有主人能让我改自己的笔记。"
        cur = _read_self()
        rows = [x for x in cur.splitlines() if x.strip().startswith("- ")]
        if not rows:
            return "现在没有条目。"
        try:
            n = int(line)
        except Exception:
            return "给我一个行号（1 到 %d）。" % len(rows)
        if n < 1 or n > len(rows):
            return "行号超了，现在是 1 到 %d。" % len(rows)

        victim = rows[n - 1]
        _snapshot()
        os.makedirs(FORGET_DIR, exist_ok=True)
        fp = os.path.join(FORGET_DIR, time.strftime("%m%d-%H%M%S") + ".md")
        with open(fp, "w", encoding="utf-8", newline="\n") as f:
            f.write("# 划掉的一条（%s）\n\n%s\n" % (
                datetime.datetime.now().isoformat(timespec="seconds"), victim))

        kept = []
        dropped = False
        for x in cur.splitlines():
            if not dropped and x.strip() == victim.strip():
                dropped = True
                continue
            kept.append(x)
        _write_self(("\n".join(kept).strip() + "\n") if kept else "")
        logger.info("[self_edit] 划掉一条，移入 遗忘/: %s" % victim[:40])
        return "划掉了。没销毁，放在遗忘里了：%s" % os.path.basename(fp)

    # ---------- 她能看到自己写了什么 ----------
    @filter.llm_tool(name="read_self")
    async def read_self(self, event: AstrMessageEvent):
        """看看我自己都记了些什么。"""
        if not _is_owner(event):
            return "只有主人能看我的笔记。"
        cur = _read_self().strip()
        if not cur:
            return "还没记过什么。"
        return "我记下的（%d/%d 字）：\n%s\n\n%s" % (
            len(cur), MAX_TOTAL, _numbered(cur), _hint(cur))


def _numbered(text):
    rows = [x for x in (text or "").splitlines() if x.strip().startswith("- ")]
    if not rows:
        return "（空）"
    return "\n".join("%d. %s" % (i + 1, r[2:].strip()) for i, r in enumerate(rows))


def _hint(text):
    n = len(text or "")
    if n > MAX_TOTAL * 0.85:
        return "（快满了，该决定忘掉哪条了）"
    return ""
