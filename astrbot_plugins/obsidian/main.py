"""obsidian —— 连笔记库（Obsidian vault）的手

给谁用：用这套系统的人，笔记大多在 Obsidian 里。这个插件让她能看、能搜、能往里写。

  外部同步      只读参考：把别处的记录批量镜像进库（本项目不含这份代码）
  本插件        按需读写：她要查某篇、要往日记里记一句，直接动笔记库

硬约束（不指望模型自觉，全部由代码保证）：
  ① 路径必须落在白名单库内：拒 ..、拒盘符、拒绝对路径、拒跳出库
  ② 只追加不覆盖：append 永远写在末尾；new 遇到同名直接拒绝
  ③ 单次写入 ≤ 2000 字（防它一口气灌一大段把笔记搞乱）
  ④ 原子写：先写 .tmp 再 replace，断电不会留下半截文件
  ⑤ 搜索跳过 .obsidian/ 与超大文件；结果带可点的 [[wikilink]]
  ⑥ 出口全是可读中文，不把异常抛给上层
"""
import io
import json
import os
import time

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register

# ==================== 配置 ====================
# name → 路径。她用哪一个由名字指定，模型不该看到真实路径以外的东西。
# 库从哪来：先读 Obsidian 自己的登记表，再读本插件 config.json。
# 不写死任何人的路径 —— 别人的库叫什么都有可能。
VAULTS = {}                        # 运行时填充：库名 → 路径
DEFAULT_VAULT = ""                 # 空 = 取第一个可用的库
CONFIG_VAULTS = {}                 # config.json 里手填的（优先级高于自动发现）
DAILY_DIR = "日记"                 # 日记目录（相对库根）
MAX_WRITE_CHARS = 2000             # 单次写入上限
MAX_READ_CHARS = 1500              # 单次读取页大小
MAX_SEARCH_BYTES = 512 * 1024      # 搜索时跳过超过这个大小的文件
SEARCH_LIMIT = 8
SKIP_DIRS = {".obsidian", ".trash", ".git"}


def _cfg_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def find_registry():
    """Obsidian 把用过的库登记在这里：%APPDATA%\\obsidian\\obsidian.json"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "obsidian", "obsidian.json")


def detect_vaults():
    """从登记表里读出所有存在的库，库名用目录名；重名自动加序号。

    这是「通用」的关键：用户装了 Obsidian 并在里面开过库，就不用再配路径。
    """
    out = {}
    reg = find_registry()
    try:
        with io.open(reg, encoding="utf-8") as f:
            d = json.load(f) or {}
    except FileNotFoundError:
        return out
    except Exception as e:
        logger.warning("[obsidian] 登记表读不了: %r" % (e,))
        return out
    for _vid, info in (d.get("vaults") or {}).items():
        path = ((info or {}).get("path") or "").strip()
        if not path or not os.path.isdir(path):
            continue
        name = os.path.basename(os.path.normpath(path)) or "库"
        base, i = name, 2
        while name in out:                     # 重名加序号，别互相覆盖
            name = "%s-%d" % (base, i)
            i += 1
        out[name] = path
    return out


def refresh_vaults():
    """合并：自动发现 → 手填覆盖。手填的永远赢。"""
    global VAULTS, DEFAULT_VAULT
    found = detect_vaults()
    found.update(CONFIG_VAULTS or {})
    VAULTS = {k: v for k, v in found.items() if os.path.isdir(v)}
    if DEFAULT_VAULT not in VAULTS:
        # 优先用登记表里标了 open 的那个库
        opened = ""
        try:
            with io.open(find_registry(), encoding="utf-8") as f:
                d = json.load(f) or {}
            for _vid, info in (d.get("vaults") or {}).items():
                if (info or {}).get("open") and (info or {}).get("path"):
                    opened = os.path.basename(os.path.normpath(info["path"]))
        except Exception:
            pass
        DEFAULT_VAULT = opened if opened in VAULTS else (sorted(VAULTS)[0] if VAULTS else "")
    return VAULTS


def _load_cfg():
    """插件目录下的 config.json 可覆盖：库列表、默认库、写入上限等。"""
    g = globals()
    try:
        with io.open(_cfg_path(), encoding="utf-8") as f:
            d = json.load(f) or {}
    except FileNotFoundError:
        return
    except Exception as e:
        logger.warning("[obsidian] config.json 读不了，用默认值: %r" % (e,))
        return
    if isinstance(d.get("vaults"), dict):
        g["CONFIG_VAULTS"] = {str(k): str(v) for k, v in d["vaults"].items()}
    if d.get("default_vault"):
        g["DEFAULT_VAULT"] = str(d["default_vault"])
    for k, key in (("daily_dir", "DAILY_DIR"), ("max_write_chars", "MAX_WRITE_CHARS"),
                   ("max_read_chars", "MAX_READ_CHARS"), ("search_limit", "SEARCH_LIMIT")):
        if d.get(k):
            g[key] = d[k]


_load_cfg()
refresh_vaults()


# ==================== 路径安全（纯函数，可单测） ====================
def _vault_root(name):
    """取出库根的真实路径。名字不认识就返回 (None, 说明)。"""
    name = (name or "").strip()
    if not name:
        name = DEFAULT_VAULT
    if not VAULTS:
        refresh_vaults()
    if not VAULTS:
        return None, ("没发现任何 Obsidian 笔记库。请先在 Obsidian 里打开一个库"
                      "（或在本插件 config.json 的 vaults 里手填 库名→路径）。")
    if name not in VAULTS:
        return None, "没有叫「%s」的笔记库。现有：%s。" % (name, "、".join(VAULTS))
    root = VAULTS[name]
    if not os.path.isdir(root):
        return None, "笔记库「%s」的目录不存在：%s" % (name, root)
    return os.path.abspath(root), ""


def safe_rel(rel, root):
    """把用户/模型给的相对路径变成库内绝对路径；越界一律拒绝。

    拒的东西：绝对路径（含盘符）、.. 跳出、空路径、非 .md 之外的可疑后缀。
    """
    raw0 = (rel or "").strip().replace("\\", "/")
    # 以 / 开头说明用户想给绝对路径：直接拒，免得他以为读到了别处的文件
    if raw0.startswith("/"):
        return None, "不要给绝对路径，只要库内的相对路径（例如 日记/2026-10-24.md）。"
    raw = raw0.lstrip("/")
    if not raw:
        return None, "路径是空的。"
    if len(raw) > 200:
        return None, "路径太长（>200 字）。"
    if ":" in raw.split("/")[0]:
        return None, "不要给盘符，只要库内的相对路径（例如 日记/2026-10-24.md）。"
    parts = []
    for seg in raw.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            return None, "路径里不能有 ..（不许跳出笔记库）。"
        parts.append(seg)
    if not parts:
        return None, "路径不合法。"
    rel_norm = "/".join(parts)
    if not rel_norm.lower().endswith((".md", ".txt")):
        rel_norm += ".md"                     # 默认当 markdown
    full = os.path.abspath(os.path.join(root, rel_norm))
    if not (full == root or full.startswith(root + os.sep)):
        return None, "路径跑到笔记库外面了，拒绝。"
    return full, ""


def _atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _read_text(path):
    with io.open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _rel(root, full):
    return os.path.relpath(full, root).replace("\\", "/")


def _link(root, full):
    """Obsidian 里可点的链接：[[相对路径去掉 .md]]"""
    return "[[%s]]" % _rel(root, full)[:-3] if _rel(root, full).lower().endswith(".md") else _rel(root, full)


def _iter_notes(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if not fn.lower().endswith((".md", ".txt")):
                continue
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(full) > MAX_SEARCH_BYTES:
                    continue
            except OSError:
                continue
            yield full


def search_notes(root, query, limit=None):
    """按标题权重 + 正文命中搜索。返回 [(相对路径, 片段)]。

    标题命中排前面；正文只取第一处命中附近的一小段，避免把整篇塞回上下文。
    """
    q = (query or "").strip()
    if len(q) < 1:
        return []
    limit = limit or SEARCH_LIMIT
    ql = q.lower()
    title_hits, body_hits = [], []
    for full in _iter_notes(root):
        rel = _rel(root, full)
        name = os.path.basename(rel).lower()
        try:
            text = _read_text(full)
        except Exception:
            continue
        if ql in name:
            title_hits.append((rel, "（标题命中）" + text.strip().splitlines()[0][:60] if text.strip() else "（空笔记）"))
            continue
        idx = text.lower().find(ql)
        if idx >= 0:
            seg = text[max(0, idx - 26): idx + 74].replace("\n", " ")
            body_hits.append((rel, "…" + seg + "…"))
        if len(title_hits) + len(body_hits) >= limit * 3:
            break
    return (title_hits + body_hits)[:limit]


@register("obsidian", "mumu", "连 Obsidian 笔记库：搜、读、追加、写日记、新建（只追加不覆盖）", "1.0.0")
class Obsidian(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    # ---------- 工具 1：有哪些库 ----------
    @filter.llm_tool(name="obsidian_vaults")
    async def obsidian_vaults(self, event=None):
        """列出可用的 Obsidian 笔记库名字，以及各自有多少篇笔记。

        Returns:
            str: 库清单（中文）。
        """
        if not VAULTS:
            refresh_vaults()
        lines = []
        for name, path in VAULTS.items():
            ok = os.path.isdir(path)
            n = len(list(_iter_notes(path))) if ok else 0
            lines.append("「%s」%s（%d 篇）" % (name, "可用" if ok else "目录不存在", n))
        if not lines:
            return ("没发现任何 Obsidian 笔记库。装过 Obsidian 的话，"
                    "先在它里面打开一个库；否则在插件 config.json 的 vaults 里手填。")
        return "笔记库：" + "；".join(lines) + "。默认用「%s」。" % DEFAULT_VAULT

    # ---------- 工具 2：搜 ----------
    @filter.llm_tool(name="obsidian_search")
    async def obsidian_search(self, event=None, query: str = "", vault: str = "", limit: float = 0):
        """在 Obsidian 笔记库里搜内容。返回可点开的 [[链接]] 与一小段上下文。

        Args:
            query (str): 要搜的词或短句。
            vault (str): 库名（幕 / 灵枢），不填用默认。
            limit (number): 最多几条，默认 8。

        Returns:
            str: 命中清单；没命中会说明。
        """
        root, why = _vault_root(vault)
        if not root:
            return why
        q = (query or "").strip()
        if not q:
            return "没给要搜的词。"
        try:
            hits = search_notes(root, q, int(limit) if limit else None)
        except Exception as e:
            logger.warning("[obsidian] 搜索失败: %r" % (e,))
            return "搜索出错：%s" % (e,)
        if not hits:
            return "「%s」在「%s」里没搜到。可以换个更短的词。" % (q, vault or DEFAULT_VAULT)
        out = ["在「%s」里搜「%s」，命中 %d 篇：" % (vault or DEFAULT_VAULT, q, len(hits))]
        for rel, seg in hits:
            out.append("- [[%s]] — %s" % (rel[:-3] if rel.lower().endswith(".md") else rel, seg))
        return "\n".join(out)

    # ---------- 工具 3：读 ----------
    @filter.llm_tool(name="obsidian_read")
    async def obsidian_read(self, event=None, path: str = "", vault: str = "", start: float = 0):
        """读一篇笔记。长文会分页，用 start 传上次结尾的字数继续读。

        Args:
            path (str): 库内相对路径，例如 日记/2026-10-24.md。
            vault (str): 库名，不填用默认。
            start (number): 从第几个字开始读，默认 0。

        Returns:
            str: 正文片段；不存在会说明。
        """
        root, why = _vault_root(vault)
        if not root:
            return why
        full, why = safe_rel(path, root)
        if not full:
            return why
        if not os.path.isfile(full):
            near = search_notes(root, os.path.basename(path).replace(".md", "")[:20], 3)
            tips = "；相近的有：" + "、".join("[[%s]]" % r[:-3] for r, _ in near) if near else ""
            return "笔记不存在：%s%s" % (_rel(root, full), tips)
        try:
            text = _read_text(full)
        except Exception as e:
            return "读不了：%s" % (e,)
        s = max(0, int(start or 0))
        seg = text[s:s + MAX_READ_CHARS]
        head = "《%s》共 %d 字，以下是从第 %d 字起：\n" % (_rel(root, full), len(text), s)
        tail = "" if s + MAX_READ_CHARS >= len(text) else \
            "\n（还没读完，用 start=%d 继续）" % (s + MAX_READ_CHARS)
        return head + seg + tail

    # ---------- 工具 4：追加 ----------
    @filter.llm_tool(name="obsidian_append")
    async def obsidian_append(self, event=None, path: str = "", text: str = "", vault: str = ""):
        """往一篇笔记末尾追加内容（不存在就新建）。绝不覆盖已有内容。

        Args:
            path (str): 库内相对路径，例如 日记/2026-10-24.md。
            text (str): 要追加的文字（≤2000 字）。
            vault (str): 库名，不填用默认。

        Returns:
            str: 结果（中文），含追加后的字数。
        """
        root, why = _vault_root(vault)
        if not root:
            return why
        full, why = safe_rel(path, root)
        if not full:
            return why
        body = (text or "").strip()
        if not body:
            return "没给要写的内容。"
        if len(body) > MAX_WRITE_CHARS:
            return "一次写太多了（%d 字 > %d）。分几次写。" % (len(body), MAX_WRITE_CHARS)
        try:
            old = _read_text(full) if os.path.isfile(full) else ""
            stamp = time.strftime("%Y-%m-%d %H:%M")
            block = ("\n\n---\n\n## %s\n\n%s\n" % (stamp, body)) if old else ("# %s\n\n%s\n" %
                    (os.path.splitext(os.path.basename(full))[0], body))
            _atomic_write(full, old + block)
            return "已追加到 %s（现在 %d 字）。" % (_link(root, full), len(old + block))
        except Exception as e:
            logger.warning("[obsidian] 追加失败: %r" % (e,))
            return "写入失败：%s" % (e,)

    # ---------- 工具 5：写日记 ----------
    @filter.llm_tool(name="obsidian_daily")
    async def obsidian_daily(self, event=None, text: str = "", vault: str = "", date: str = ""):
        """往今天的日记里追加一段。日记路径是 日记/YYYY-MM-DD.md，没有就新建。

        Args:
            text (str): 要记的内容（≤2000 字）。
            vault (str): 库名，不填用默认。
            date (str): 可指定日期 YYYY-MM-DD，不填就是今天。

        Returns:
            str: 结果（中文）。
        """
        day = (date or "").strip() or time.strftime("%Y-%m-%d")
        if not (len(day) == 10 and day[4] == "-" and day[7] == "-"):
            return "日期格式要 YYYY-MM-DD，收到的是「%s」。" % (day,)
        return await self.obsidian_append(event=event, path="%s/%s.md" % (DAILY_DIR, day),
                                          text=text, vault=vault)

    # ---------- 工具 6：新建 ----------
    @filter.llm_tool(name="obsidian_new")
    async def obsidian_new(self, event=None, title: str = "", body: str = "", vault: str = ""):
        """新建一篇笔记。同名已存在就拒绝（要补内容请用 obsidian_append）。

        Args:
            title (str): 标题，会变成文件名。
            body (str): 正文（≤2000 字）。
            vault (str): 库名，不填用默认。

        Returns:
            str: 结果（中文）。
        """
        root, why = _vault_root(vault)
        if not root:
            return why
        t = (title or "").strip()
        if not t:
            return "没给标题。"
        # 标题里的非法字符换成下划线，避免造出怪文件名
        bad = '\\/:*?"<>|'
        t = "".join("_" if c in bad else c for c in t)[:80]
        full, why = safe_rel(t + ".md", root)
        if not full:
            return why
        if os.path.isfile(full):
            return "已经有《%s》了。要补内容用 obsidian_append，别覆盖别人的笔记。" % t
        b = (body or "").strip()
        if len(b) > MAX_WRITE_CHARS:
            return "正文太长（%d 字 > %d）。" % (len(b), MAX_WRITE_CHARS)
        try:
            _atomic_write(full, "# %s\n\n%s\n" % (t, b))
            return "已新建 %s。" % _link(root, full)
        except Exception as e:
            logger.warning("[obsidian] 新建失败: %r" % (e,))
            return "新建失败：%s" % (e,)
