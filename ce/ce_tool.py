"""
CE · 工具版（给本地模型用）

**没有设定。没有自我。没有判断通道。没有日记。**
它只是一颗子弹：给任务，干活，回来。

**不碰记忆。** 记忆是独立系统的职责，本地模型只负责干活。

保留的只有三件与「干活」直接相关的事（都是从【未】的 ce.py 里挑出来的）：
  1. 内联工具标记 [查:][读:] —— 模型说要查，代码真的去查，结果回灌
  2. claims_action 监控 —— 声称做过却没调工具，驳回重答（防编造）
  3. 思维链拆解 —— 多步任务先拆步骤，跨轮推进

刻意砍掉的：
  - 任何记忆写入/读取（记忆归独立外部系统，CE 不掺和）
  - 我.md / 启动.md 注入
  - [判断] 通道 + 算子归一化 + 二阶元认知
  - 日记、信箱、拒绝.md
  - 客服腔裁剪（那是语气，不是任务）

调用方：任何需要"可靠本地执行"的程序（HTTP 调用，语言不限）。
"""
try:
    from http.server import ThreadingHTTPServer as _Server, BaseHTTPRequestHandler
except ImportError:  # 老 Python 兜底
    from http.server import HTTPServer as _Server, BaseHTTPRequestHandler
import io
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.parse

# ==================== 配置 ====================
_CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _load_cfg():
    if os.path.isfile(_CFG_PATH):
        try:
            with io.open(_CFG_PATH, encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception as e:
            print("[ce] config.json 读不了，用默认值:", e)
    return {}


CFG = _load_cfg()

LLAMA = CFG.get("llama_url", "http://127.0.0.1:8080/v1/chat/completions")
MODEL = CFG.get("model", "qwen3")
PORT = int(CFG.get("port", 8092))
BIND = CFG.get("bind", "127.0.0.1")
TOKEN = CFG.get("token", "")
CORS_ORIGIN = CFG.get("cors_origin", "*")   # 空字符串=不加跨域头
# 允许把结果写成文件的目录（空列表 = 禁止写文件）。日报/周报靠这个落地。
WRITE_ROOTS = CFG.get("write_roots", [])

# 工具只能读这些根目录下的东西（白名单，防止它到处翻）
READ_ROOTS = [
    # 例：os.path.expanduser("~/notes")、"/data/papers"
    # 留空 = 没有可查资料，[查:] 会如实说没有，不会编
]
# 白名单可被 config.json 覆盖（开源时用这个，别改代码）
READ_ROOTS = CFG.get("read_roots", READ_ROOTS)


def _auto_roots():
    """没人配置 read_roots 时，自己去找最常见的几个地方。

    为什么：让用户"把自己文件放哪"写进配置，是最容易被放弃的一步。
    找不到就跳过，绝不报错。
    """
    home = os.path.expanduser("~")
    cands = [
        os.path.join(home, "Desktop"),
        os.path.join(home, "Documents"),
        os.path.join(home, "Downloads"),
        os.path.join(home, "OneDrive", "Documents"),
        os.path.join(home, "Documents", "Obsidian"),
        os.getcwd(),
    ]
    out = []
    for c in cands:
        try:
            if os.path.isdir(c) and c not in out:
                out.append(c)
        except Exception:
            pass
    return out


if CFG.get("auto_roots", True) and not (CFG.get("read_roots") or []):
    _auto = _auto_roots()
    if _auto:
        READ_ROOTS = _auto

# 只写不读：写入接口给你，检索/查找不归你管（那是外部系统的事）
MEM_WRITE = CFG.get("mem_write", "")   # 留空 = 不外接记忆系统；[记:] 会如实说没接
MEM_FOLDER = "幕"

MAX_TOOL_ROUNDS = int(CFG.get("max_tool_rounds", 3))
MAX_OUTPUT_CHARS = int(CFG.get("max_output_chars", 1500))
# 最终答案上限：日报/周报这类成篇输出需要空间（400 会直接截断）
MAX_ANSWER_TOKENS = int(CFG.get("max_answer_tokens", 1200))
# 跨服务兼容：llama.cpp/qwen3 需要关思考；别的服务不认这个字段时 call_model 会自动去掉重试
DISABLE_THINKING = bool(CFG.get("disable_thinking", True))
# repeat_penalty 是 llama.cpp 扩展；设 0 表示不发（对别的服务更友好）
REPEAT_PENALTY = float(CFG.get("repeat_penalty", 1.1) or 0)
# 失败重试次数。云端按 token 计费时，失败请求也可能计费 —— 想省钱就设 0
RETRY_TIMES = int(CFG.get("retry_times", 1))
# 单条工具结果进上下文的上限（字）。太大 = 白烧上下文；太小 = 模型看不清。
TOOL_CTX_CHARS = int(CFG.get("tool_ctx_chars", 1200))
# 索引上限：文档目录可能几万文件，全扫会让人以为程序卡死
INDEX_MAX_FILES = int(CFG.get("index_max_files", 3000))
INDEX_SKIP_DIRS = set(CFG.get("index_skip_dirs", [
    "node_modules", ".git", "venv", "__pycache__", "AppData", ".venv",
    "site-packages", "dist", "build", ".cache"]))
# 分步模式：拆解后一步一提交，步与步之间只传摘要，不把历史全塞进上下文
STEP_MODE = bool(CFG.get("step_mode", True))
# 分步的"形状"：
#   chain  串行咀嚼：A 的摘要喂给 B（信息逐级衰减，只在无法并行时才用）
#   map    围绕同一份原文，多路分别取料，最后合并（默认）
PLAN = CFG.get("plan", "map")
MAX_ASKS = int(CFG.get("max_asks", 6))
PART_CHARS = int(CFG.get("part_chars", 4000))
def _load_capacity():
    """读机器能力文件。路径在这里自己算，不依赖模块级变量的定义顺序。"""
    d = {}
    cap_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capacity.json")
    if os.path.isfile(cap_path):
        try:
            with io.open(cap_path, encoding="utf-8") as f:
                d = json.load(f) or {}
        except Exception as e:
            log("capacity.json 读不了，用预设值: %r" % (e,))
    # 用户可以在 config.json 里直接改数值，不必重跑自检
    if CFG.get("safe_source_chars"):
        d["safe_source_chars"] = int(CFG["safe_source_chars"])
    if CFG.get("max_parallel"):
        d["max_parallel"] = int(CFG["max_parallel"])
    return d


_CAP = _load_capacity()
# 单请求能塞的原文：优先用自检测出的值，没测过就用保守预设 3000 字
SOURCE_CHARS = int(_CAP.get("safe_source_chars") or CFG.get("source_chars", 3000))
# 能并行几路：1 = 一块一块来（自检说不行就一定是 1）
MAX_PARALLEL = max(1, int(_CAP.get("max_parallel") or 1))
# 原文总上限：整份读进来（分段在"喂"的时候做，不是"读"的时候砍掉）。
# 之前读文件就按 SOURCE_CHARS 截断，导致长文件永远只处理前半截，分段代码根本没机会触发。
SOURCE_TOTAL = int(CFG.get("source_total_chars", 60000))

# ---- Obsidian 笔记库（通用：先读 Obsidian 自己的登记表，再看 config 手填） ----
OBS_DAILY_DIR = CFG.get("obsidian_daily_dir") or "日记"
OBS_MAX_WRITE = int(CFG.get("obsidian_max_write", 2000))
OBS_CFG_VAULTS = CFG.get("obsidian_vaults") or {}
OBS_DEFAULT = CFG.get("obsidian_default") or ""
_OBS = {"vaults": {}, "at": 0}


def obs_registry():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "obsidian", "obsidian.json")


def obs_vaults(force=False):
    """库名 → 路径。60 秒缓存：Obsidian 新开库不用重启 CE 也能认到。"""
    if not force and _OBS["vaults"] and time.time() - _OBS["at"] < 60:
        return _OBS["vaults"]
    found = {}
    try:
        with io.open(obs_registry(), encoding="utf-8") as f:
            d = json.load(f) or {}
        for _vid, info in (d.get("vaults") or {}).items():
            path = ((info or {}).get("path") or "").strip()
            if path and os.path.isdir(path):
                name = os.path.basename(os.path.normpath(path)) or "库"
                base, i = name, 2
                while name in found:
                    name = "%s-%d" % (base, i)
                    i += 1
                found[name] = path
                try:
                    if (info or {}).get("open"):
                        _OBS.setdefault("open", {})[name] = True
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    except Exception as e:
        log("Obsidian 登记表读不了: %r" % (e,))
    for k, v in (OBS_CFG_VAULTS or {}).items():      # 手填的覆盖自动发现
        if os.path.isdir(v):
            found[str(k)] = v
    _OBS["vaults"] = found
    _OBS["at"] = time.time()
    return found


def obs_root(name=""):
    """取库根。优先级：指定名字 > config 默认 > 正在打开的库 > 字母序第一个。"""
    vs = obs_vaults()
    if not vs:
        return None, "没发现 Obsidian 笔记库（可先在 Obsidian 里打开一个库，或在 config 里配 obsidian_vaults）"
    n = (name or "").strip() or OBS_DEFAULT
    if n and n in vs:
        return vs[n], ""
    if not n:
        opened = [k for k in sorted(vs) if (_OBS.get("open") or {}).get(k)]
        if opened:
            return vs[opened[0]], ""
        return vs[sorted(vs)[0]], ""
    return None, "没有叫「%s」的库。现有：%s" % (n, "、".join(sorted(vs)))


def obs_safe(rel, root):
    """库内相对路径 → 绝对路径。越界一律拒（和写入入口同一套规矩）。"""
    raw0 = (rel or "").strip().replace("\\", "/")
    if not raw0:
        return None, "路径是空的。"
    if raw0.startswith("/") or (":" in raw0.split("/")[0]):
        return None, "只要库内相对路径（如 日记/2026-10-24.md），别给绝对路径或盘符。"
    parts = []
    for seg in raw0.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            return None, "路径里不能有 ..（不许跳出笔记库）。"
        parts.append(seg)
    if not parts:
        return None, "路径不合法。"
    rel_norm = "/".join(parts)
    if not rel_norm.lower().endswith((".md", ".txt")):
        rel_norm += ".md"
    full = os.path.abspath(os.path.join(root, rel_norm))
    if not (full == root or full.startswith(root + os.sep)):
        return None, "路径跑到笔记库外面了，拒绝。"
    return full, ""


def obs_find(root, q, limit=6):
    """库里搜：标题命中优先，正文命中带一小段上下文。"""
    hits = []
    ql = (q or "").lower()
    if len(ql) < 1:
        return hits
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if not fn.lower().endswith((".md", ".txt")):
                continue
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(full) > 512 * 1024:
                    continue
                with io.open(full, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace("\\", "/")
            if ql in fn.lower():
                hits.append((0, rel, text.strip().splitlines()[0][:60] if text.strip() else "（空）"))
            else:
                i = text.lower().find(ql)
                if i >= 0:
                    hits.append((1, rel, "…" + text[max(0, i - 20):i + 60].replace(chr(10), " ") + "…"))
            if len(hits) >= limit * 3:
                break
    hits.sort(key=lambda x: x[0])
    return [(r, s) for _o, r, s in hits[:limit]]
STEP_MAX = int(CFG.get("step_max", 6))
STEP_NOTE_CHARS = int(CFG.get("step_note_chars", 300))
# 预算：一次请求最多花多少秒 / 调多少次模型。分步模式会多次调用，
# 没有上限的"反复生成"就是反复烧钱（用户明确要求可控）。
BUDGET_SEC = float(CFG.get("budget_sec", 180))
BUDGET_CALLS = int(CFG.get("budget_calls", 30))
CHAIN_KEEP = int(CFG.get("chain_keep", 20))
CHAIN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ce_chain.json")

# 机器能力（由 tools/probe.py 测出来写入；config.json 里的同名项可覆盖）
#   safe_source_chars  单次请求能塞多少字原文（超了就分段喂）
#   max_parallel       能同时跑几路（1 = 一块一块来）
CAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capacity.json")


# 检查点：每一步的产出都落盘，用户可查、可回滚、可带补充要求重跑
CKPT_DIR = CFG.get("ckpt_dir") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".ce_runs")

# 人能直接打开看的运行日志。
# 默认写系统临时目录（用完即弃）；log_keep=true 才落到 ckpt_dir/logs 长期保留。
# 留不留由用户决定：临时目录里的东西系统自己会清，长期目录里的要人删。
LOG_KEEP = bool(CFG.get("log_keep", False))
LOG_DIR = CFG.get("log_dir") or (
    os.path.join(CKPT_DIR, "logs") if LOG_KEEP
    else os.path.join(tempfile.gettempdir(), "ce_logs"))
LOG_DAYS = int(CFG.get("log_days", 3))

# 语义检索（可选）：有嵌入服务就用，没有就退回关键词
EMBED_URL = CFG.get("embed_url", "http://127.0.0.1:8081/v1/embeddings")
EMBED_MODEL = CFG.get("embed_model", "bge")
SEMANTIC = bool(CFG.get("semantic", True))
SEM_MAX_CHUNKS = int(CFG.get("semantic_max_chunks", 600))
SEM_MIN_SCORE = float(CFG.get("semantic_min_score", 0.35))

# 检索档位：auto / none / keyword / semantic / memory
#   none      不检索（没有资料可查）
#   keyword   只用文件夹 + 关键词（零依赖，谁都能跑）
#   semantic  文件夹 + 本地嵌入（有嵌入服务时更好）
#   memory    额外挂一个外部库的检索口（HTTP GET ?q=，返回 {"items":[{"text":...}]}）
RETRIEVAL = CFG.get("retrieval", "auto").lower()
MEM_SEARCH = CFG.get("memory_search_url", "")
# 从记忆库一次取多少候选（取少了就只能看到最新的那几条）
MEM_LIMIT = int(CFG.get("mem_limit", 200))
IDX_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ce_index.json")

try:
    sys.stdout.reconfigure(encoding="utf-8")   # Python 3.7+
except Exception:
    pass                                       # 老版本没有这个 API，忽略即可



_USAGE = threading.local()
_USAGE_TOTAL = {"calls": 0, "prompt": 0, "completion": 0, "failed": 0}


def _usage_add(usage):
    """记账：每次调用把 usage 累加。云端对账时这组数字能直接跟账单比。"""
    if not isinstance(usage, dict):
        return
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)
    if not hasattr(_USAGE, "calls"):
        _USAGE.calls = _USAGE.prompt = _USAGE.completion = _USAGE.failed = 0
    _USAGE.calls += 1
    _USAGE.prompt += pt
    _USAGE.completion += ct
    _USAGE_TOTAL["calls"] += 1
    _USAGE_TOTAL["prompt"] += pt
    _USAGE_TOTAL["completion"] += ct
    log("用量 +%d/%d tokens（累计 %d 次 / in %d / out %d）"
        % (pt, ct, _USAGE.calls, _USAGE.prompt, _USAGE.completion))


def usage_now():
    """本次请求的用量（线程内）。"""
    return {"calls": getattr(_USAGE, "calls", 0),
            "prompt_tokens": getattr(_USAGE, "prompt", 0),
            "completion_tokens": getattr(_USAGE, "completion", 0),
            "failed": getattr(_USAGE, "failed", 0)}


def usage_total():
    return dict(_USAGE_TOTAL)


def friendly_err(e, where=""):
    """把底层异常翻译成"下一步该干什么"。这比 traceback 对新用户有用得多。"""
    s = repr(e)
    # 已经是人话（下层翻译过）就直接用，别再套一层“处理请求失败”
    _nice = str(e)
    if any(k in _nice for k in ("连不上模型服务", "模型响应超时", "模型接口返回 404",
                                "连不上模型服务", "端口被占了", "没有权限读写")):
        return _nice
    url = ""
    try:
        url = LLAMA
    except Exception:
        pass
    if "ConnectionRefused" in s or "积极拒绝" in s or "10061" in s:
        return ("连不上模型服务（%s）。检查三件事：\n"
                "  1) 模型起了吗？llama.cpp 一般是 llama-server -m 模型.gguf -c 8192 --port 8080\n"
                "  2) config.json 里的 llama_url 对不对？（当前 %s）\n"
                "  3) 端口被占了吗？换个 --port 再改配置。" % (where or "模型调用", url or "未配置"))
    if "timed out" in s or "TimeoutError" in s:
        return ("模型响应超时（%s）。模型太小/显存不够会极慢；"
                "先把 max_tokens 调小，或确认模型真的在算（看 llama-server 控制台）。" % (where or "模型调用"))
    if "404" in s:
        return ("模型接口返回 404。llama_url 通常要写成 …/v1/chat/completions，"
                "不是 …/completions 也不是裸地址。当前 %s" % (url or "未配置"))
    if "PermissionError" in s:
        return ("没有权限读写这个位置（%s）。换一个自己能写的目录，或改配置里的路径。" % (where or "文件操作"))
    if "Address already in use" in s or "10048" in s:
        return "端口被占了。改 config.json 里的 port，或者关掉占用它的进程。"
    return "%s失败：%s" % (where or "操作", s[:200])


def log(*a):
    """带时间戳的日志，直接刷到 stdout（容器/控制台可直读）。"""
    print("[%s]" % time.strftime("%H:%M:%S"), *a, flush=True)


# ==================== 系统提示：只讲规矩，不讲人 ====================
SYSTEM = """你是任务执行器。只做被要求的事。

规矩：
1. 你说你做过什么，就必须真的做过。没做就说没做。
2. 需要查看东西时，写标记，系统会替你去查，然后把真实结果给你：
   [查:关键词]   搜索
   [读:路径]     读文件
   [记:内容]     把内容写进外部库（只是写进去，怎么检索不归你管）
[搜笔:词]     在自己的 Obsidian 笔记库里搜（返回文件和片段，只准依据这些答）
[读笔:路径]   读笔记库里某一篇（长文用 [读笔:路径@1] 继续）
[日:内容]     往今天的日记里追加一条
   写完标记，等结果回来再说话。
3. 你没有查到，就说没查到。不要用推测填补。
4. 不确定就说不确定，然后停下。
5. 回答简短。不要寒暄，不要总结，不要"希望对你有帮助"。
6. 说到"哪个文件里有"时，只能用检索结果里给出的文件名。检索结果没给，就不要说文件名。
7. 任务要求写成文件、报告、清单这类成篇的东西时，就把该写的都写出来；
   不要压缩成一两句话。字数不够是失败，不是简洁。
8. 不要在正文里自我更正、加括号注释、或写"修正："这种话。
   写错了就重写那一行，不要留痕。
9. 你不负责"记忆"这件事——不负责检索、不负责管理、不负责回忆。
   但你可以"写入"：要存东西就用 [记:内容]，存完就结束，不用管以后怎么找。

当前时间："""


def system_prompt(lean=False):
    """系统提示 + 当前时间。每次调用都重算，避免跨天时间错。

    lean=True：只给角色与输出纪律，不给工具清单。
    分步模式的每一块只负责把这一段写出来，不调工具，
    把 615 字的工具手册重复发 12 遍纯属浪费上下文。
    """
    if lean:
        return ("你是任务执行器。只做被要求的那一块，直接写成品内容。\n"
                "不解释自己在做什么，不写「以下是」，不输出工具标记。\n"
                "当前时间：" + time.strftime("%Y-%m-%d"))
    # 只到"日"：带分钟会让每次请求前缀都不同，本地模型的 prompt 缓存会全部失效
    return SYSTEM + time.strftime("%Y-%m-%d")


# ==================== HTTP ====================
def _post(url, payload, timeout=120):
    """POST JSON 并解析返回；超时/网络错误抛给调用方处理。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(url, timeout=20):
    """GET JSON。"""
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


_BUDGET = threading.local()


def _budget_new():
    _BUDGET.t0 = time.time()
    _BUDGET.calls = 0
    _USAGE.calls = _USAGE.prompt = _USAGE.completion = _USAGE.failed = 0


def _budget_check(what="模型"):
    """超时就抛异常，由上层收尾并如实说明没做完。"""
    t0 = getattr(_BUDGET, "t0", None)
    if t0 is None:
        return
    used = time.time() - t0
    if used > BUDGET_SEC:
        raise TimeoutError("超预算：已用 %.0f 秒（上限 %.0f），剩余步骤没做完" % (used, BUDGET_SEC))
    if getattr(_BUDGET, "calls", 0) >= BUDGET_CALLS:
        raise TimeoutError("超预算：模型调用已达 %d 次上限，剩余步骤没做完" % BUDGET_CALLS)
    _BUDGET.calls = getattr(_BUDGET, "calls", 0) + 1


def call_model(messages, max_tokens=400):
    """调本地模型（OpenAI 兼容接口）。

    跨服务兼容做法：
      · enable_thinking 只在开了 DISABLE_THINKING 时才发（llama.cpp/qwen3 需要它，
        别的服务不认这个字段会 400）
      · repeat_penalty 同理（llama.cpp 的扩展字段）
      · 万一仍然 400，就去掉这两个可选字段再试一次 —— 宁可少个参数，也别起不来
    """
    body = {
        "model": MODEL,
        "temperature": 0.3,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    optional = {}
    if DISABLE_THINKING:
        optional["chat_template_kwargs"] = {"enable_thinking": False}
    if REPEAT_PENALTY:
        optional["repeat_penalty"] = REPEAT_PENALTY
    body.update(optional)

    _budget_check()
    last = None
    for attempt in range(RETRY_TIMES + 1):
        try:
            resp = _post(LLAMA, body, timeout=300)
            _usage_add(resp.get("usage") or {})
            return resp["choices"][0]["message"]["content"] or ""
        except Exception as e:
            last = e
            _USAGE_TOTAL["failed"] = _USAGE_TOTAL.get("failed", 0) + 1
            msg = str(e)
            # 服务端不认扩展字段（400/422/未知参数）→ 去掉可选字段重试
            if optional and ("400" in msg or "422" in msg or "unknown" in msg.lower()
                             or "unrecognized" in msg.lower() or "invalid" in msg.lower()):
                dropped = "、".join(optional)
                log("服务端不认扩展字段（%s），去掉后重试：%s" % (dropped, msg[:80]))
                body = {k: v for k, v in body.items() if k not in optional}
                optional = {}
                continue
            if attempt == 0:
                time.sleep(1.5)
    raise RuntimeError(friendly_err(last, "模型调用")) from last


class Index:
    """文件索引：关键词（倒排）+ 可选语义（嵌入）两层，检索失败自动降级。"""
    def __init__(self):
        self.inv, self.docs = {}, []
        self.chunks = []      # [{"p":路径,"t":文本,"v":向量}]
        self.lock = threading.Lock()

    @staticmethod
    def tok(text):
        text = (text or "").lower()
        out = []
        for seg in re.findall(r"[\u4e00-\u9fa5]+|[a-z0-9]+", text):
            if re.match(r"[a-z0-9]+", seg):
                out.append(seg)
            else:
                out.extend(seg[i:i + 2] for i in range(max(1, len(seg) - 1)))
                if len(seg) == 1:
                    out.append(seg)
        return out

    def build(self, roots):
        docs, inv = [], {}
        for root in roots:
            if not os.path.isdir(root):
                continue
            if len(docs) >= INDEX_MAX_FILES:
                log("索引已达上限 %d 个文件，其余跳过（config: index_max_files）" % INDEX_MAX_FILES)
                break
            for dirpath, dirs, files in os.walk(root):
                # 跳过垃圾目录（node_modules/.git/venv 等）——之前定义了却没生效
                dirs[:] = [d for d in dirs
                           if not d.startswith(".") and d not in INDEX_SKIP_DIRS]
                for fn in files:
                    if len(docs) >= INDEX_MAX_FILES:
                        break
                    if not fn.endswith((".md", ".txt", ".json", ".py", ".log")):
                        continue
                    p = os.path.join(dirpath, fn)
                    try:
                        if os.path.getsize(p) > 512 * 1024:
                            continue
                        with io.open(p, "r", encoding="utf-8", errors="replace") as f:
                            body = f.read()
                    except Exception:
                        continue
                    i = len(docs)
                    docs.append({"path": p, "body": body})
                    for t in set(self.tok(fn + " " + body)):
                        inv.setdefault(t, []).append(i)
        with self.lock:
            self.docs, self.inv = docs, inv
        if SEMANTIC and TIER in ("semantic", "auto"):
            try:
                self._build_semantic()
            except Exception as e:
                log("语义索引失败，退回关键词: %r" % (e,))
        return len(docs)

    # ---------- 语义层（可选，零外部依赖，HTTP 调本地嵌入）----------
    @staticmethod
    def _embed(texts):
        body = json.dumps({"model": EMBED_MODEL, "input": texts},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            EMBED_URL, data=body,
            headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read().decode("utf-8"))
        return [x["embedding"] for x in d.get("data") or []]

    @staticmethod
    def _split(body):
        """按段落切块，约 400 字一块；太碎不要。"""
        parts, buf = [], ""
        for para in re.split(r"\n\s*\n", body or ""):
            para = para.strip()
            if not para:
                continue
            if len(buf) + len(para) <= 400:
                buf = (buf + "\n" + para).strip()
            else:
                if buf:
                    parts.append(buf)
                buf = para[:400]
        if buf:
            parts.append(buf)
        return parts

    def _sig(self):
        h = hashlib.sha256()
        for d in self.docs:
            h.update(d["path"].encode("utf-8", "ignore"))
            try:
                h.update(str(int(os.path.getmtime(d["path"]))).encode())
            except OSError:
                pass
        h.update((EMBED_MODEL or "").encode())
        return h.hexdigest()[:16]

    def _build_semantic(self):
        sig = self._sig()
        if os.path.isfile(IDX_CACHE):
            try:
                cd = json.load(io.open(IDX_CACHE, encoding="utf-8"))
                if cd.get("sig") == sig and cd.get("chunks"):
                    self.chunks = cd["chunks"]
                    log("语义索引命中缓存：%d 块" % len(self.chunks))
                    return
            except Exception:
                pass

        chunks = []
        for d in self.docs:
            for seg in self._split(d["body"])[:12]:
                chunks.append({"p": d["path"], "t": seg})
                if len(chunks) >= SEM_MAX_CHUNKS:
                    break
            if len(chunks) >= SEM_MAX_CHUNKS:
                break
        if not chunks:
            return

        vecs = []
        for i in range(0, len(chunks), 16):
            vecs.extend(self._embed([c["t"] for c in chunks[i:i + 16]]))
        for c, v in zip(chunks, vecs):
            c["v"] = [round(x, 4) for x in v]
        self.chunks = chunks
        try:
            io.open(IDX_CACHE, "w", encoding="utf-8").write(
                json.dumps({"sig": sig, "chunks": chunks}, ensure_ascii=False))
        except Exception as e:
            log("索引缓存写不进去: %r" % (e,))
        log("语义索引就绪：%d 块" % len(chunks))

    def search(self, q, top_k=5):
        if self.chunks:
            try:
                qv = self._embed([q])[0]
                scored = []
                for c in self.chunks:
                    v = c.get("v") or []
                    if len(v) != len(qv):
                        continue
                    s = sum(a * b for a, b in zip(qv, v))
                    na = math.sqrt(sum(a * a for a in qv))
                    nb = math.sqrt(sum(b * b for b in v))
                    if na and nb:
                        s /= (na * nb)
                    if s >= SEM_MIN_SCORE:
                        # 文件名命中的加成：问「日报」时「日报.md」必须排前面，
                        # 否则会被"正文提了一嘴"的大文件挤下去（语义分不看文件名）。
                        _flat = os.path.basename(c["p"]).lower().replace(" ", "")
                        _qs = [x for x in re.split(r"\s+", q or "") if x]
                        if any(x in _flat for x in _qs):
                            s = s + 0.28
                        scored.append((s, c))
                scored.sort(key=lambda x: -x[0])
                if scored:
                    return [(os.path.basename(c["p"]), c["t"][:280].replace("\n", " "))
                            for _s, c in scored[:top_k]]
            except Exception as e:
                log("语义检索失败，用关键词: %r" % (e,))

        toks = self.tok(q)
        if not toks:
            return []
        # 这里不再 dict(self.inv) 拷贝：
        # build() 每次都是整体赋值 self.docs/self.inv（写时替换，不原地改），
        # 读侧在锁内拿到引用即可，旧引用在重建期间仍然可用且不会被改坏。
        # 文件很多时，每次搜索复制整本倒排表会把内存和 GC 吃掉。
        with self.lock:
            docs, inv = self.docs, self.inv
        score = {}
        for t in toks:
            ids = inv.get(t) or []
            idf = math.log(1 + len(docs) / (1 + len(ids)))
            for i in ids:
                score[i] = score.get(i, 0.0) + idf
        # 文件名/路径命中的加权：用户问"日报"时，叫「日报.md」的文件必须排前面，
        # 否则会被正文里提了一嘴"日报"的大文件挤下去。
        for i in list(score.keys()):
            try:
                name = docs[i].get("p") or docs[i].get("name") or ""
            except Exception:
                continue
            flat = name.lower().replace(" ", "").replace("_", "").replace("-", "")
            for tk in toks:
                if tk and tk in flat:
                    score[i] = score[i] * 2.5
                    break
        out = []
        for i, _s in sorted(score.items(), key=lambda x: -x[1])[:top_k]:
            d = docs[i]
            body = d["body"]
            hit = body.find(toks[0]) if toks else -1
            snip = body[max(0, hit - 50): hit + 250] if hit >= 0 else body[:200]
            out.append((os.path.basename(d["path"]), snip.replace("\n", " ")))
        return out


def detect_tier():
    """决定当前用哪一档检索。auto 时按可用性从高到低降级。"""
    if RETRIEVAL != "auto":
        return RETRIEVAL
    has_files = any(os.path.isdir(r) for r in (READ_ROOTS or []))
    if not has_files:
        return "memory" if MEM_SEARCH else "none"
    if SEMANTIC:
        try:
            Index._embed(["ping"])
            return "semantic"
        except Exception as e:
            log("嵌入不可用（%r），降级为关键词" % (e,))
    return "keyword"


TIER = "none"


def memory_search(q, top_k=5):
    """外部库检索（可选）。它没有这个口就返回空。

    很多记忆服务的列表接口会忽略 q、只按时间给最新 N 条。
    所以这里多要候选（MEM_LIMIT），再在本地按词重合排序 ——
    "只记得今天"就是这么来的：取 5 条，永远是最新的 5 条。
    """
    if not MEM_SEARCH:
        return []
    try:
        import urllib.parse
        url = MEM_SEARCH + ("&" if "?" in MEM_SEARCH else "?") + urllib.parse.urlencode(
            {"q": q, "limit": MEM_LIMIT})
        d = _get(url, timeout=20)
        cand = d.get("items") or []
        qtok = [x for x in re.split(r"[\s，。；、,.;:?!（）()]+", q or "") if len(x) >= 2]

        def _score(it):
            if not isinstance(it, dict):
                return 0
            s = (it.get("text") or it.get("content") or it.get("title") or "")
            return sum(1 for x in qtok if x in s)

        try:
            cand = sorted(cand, key=_score, reverse=True)
        except Exception:
            pass
        out = []
        for it in cand[:top_k]:
            if isinstance(it, dict):
                txt = it.get("text") or it.get("content") or it.get("title") or ""
                src = it.get("source") or it.get("id") or "外部库"
            else:
                txt, src = str(it), "外部库"
            if txt:
                out.append((str(src)[:40], str(txt)[:280]))
        return out
    except Exception as e:
        log("外部库检索失败: %r" % (e,))
        return []

# 全局索引实例（/health 与 [查:] 都要用；之前误删过，这里补上）
INDEX = Index()


def rebuild():
    """重建索引：先探测检索档位，再按档位建关键词/语义索引。"""
    global TIER
    TIER = detect_tier()
    if TIER == "none":
        log("检索档位: none —— 没有可查的资料，[查:] 会直接说没资料")
        return 0
    if TIER == "memory":
        log("检索档位: memory —— 只挂外部库")
        return 0
    n = INDEX.build(READ_ROOTS)
    log("索引: %d 个文件" % n)
    return n


# ==================== 工具 ====================
TOOL_RE = re.compile(r"\[(查|读|记|归|搜笔|读笔|日|地图)[:：]?([^\]]*)\]")
_PLACEHOLDER = {"内容", "xxx", "…", "...", "略", "无", "空", "占位"}

# 本轮真正命中的文件名（接地守卫用）
# 线程本地状态：服务器是 ThreadingHTTPServer，同一进程会并发处理多个请求。
# 若把这些做成模块级全局，两个请求会互相覆盖命中集与进度 —— 接地守卫就会误判。
_TLS = threading.local()


def hits_get():
    """本线程本轮的工具命中集（接地守卫的依据）。"""
    if not hasattr(_TLS, "hits"):
        _TLS.hits = []
    return _TLS.hits


def set_step_info(v):
    """本线程这次请求的分步进度（会放进 /task 响应）。"""
    _TLS.step_info = v


def step_info():
    return getattr(_TLS, "step_info", None)


def _inside(path):
    """路径是否落在 READ_ROOTS 白名单内（所有读写的前提）。"""
    p = os.path.abspath(path)
    return any(p.startswith(os.path.abspath(r) + os.sep) or p == os.path.abspath(r)
               for r in READ_ROOTS)


_REPEAT = threading.local()


_PUNCT_RE = re.compile(r"[\s\u3000\.,;:!?\"'`~()\[\]{}<>/\\|\-_=+*，。；：！？“”‘’（）【】《》、…]+")


# 归一化：全局一份（原来 _nf/_norm 只是别的函数内部的局部名，新加的判据调不到）
_NORM_STRIP_RE = re.compile(
    "[\\s\\u3000\\-_\u00b7.\u3002\uff0c\u3001\uff1b\uff1a,.;:!?\uff1f\uff01"
    "\"'\u201c\u201d\u2018\u2019()\uff08\uff09\\[\\]\u3010\u3011"
    "\u300c\u300d\u300e\u300f\u3008\u3009\u300a\u300b<>/\\\\|]+")


def _nf(s):
    """归一化名字/文本：去空格、标点、连接符，统一小写。"""
    return _NORM_STRIP_RE.sub("", (s or "").lower())


def _norm(s):
    return _nf(s)


def _arg_key(arg):
    """把参数归一化成"同一个意思"：去空格/标点/引号，全角转半角，统一小写。
    小模型会拿 "读 x.md@0" / "读 x.md @0" / "读「x.md」@0" 当三件不同的事来刷次数，
    不归一化的话熔断就是摆设。"""
    s = (arg or "").strip().lower()
    # 全角 → 半角（数字字母标点常见区段）
    s = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in s)
    s = _PUNCT_RE.sub("", s)
    for ch in "\u300c\u300d\u300e\u300f\u3008\u3009\u300a\u300b\u3010\u3011\u201c\u201d\u2018\u2019":
        s = s.replace(ch, "")
    return s[:160]


def _repeat_guard(tag, arg):
    """同一动作反复做没有进展 → 拦。真实教训：幕连试 20 多次启动不存在的程序。"""
    if not hasattr(_REPEAT, "seen"):
        _REPEAT.seen = {}
    key = (tag.strip().lower(), _arg_key(arg))
    n = _REPEAT.seen.get(key, 0) + 1
    _REPEAT.seen[key] = n
    if n > 3:
        return ("同一个动作已经试了 %d 次，没有进展。换个办法，或者说做不到。" % n)
    return None


def dir_map(limit_dirs=12, per_dir=3):
    """代码生成的目录地图：不经过模型，直接列出"这里有什么"。

    为什么需要它：用户说"整理一下"，模型不知道有什么可整理，只能猜文件名。
    先给它一张地图，它就有据可依，也不用背完整文件名。
    """
    lines = []
    for root in (READ_ROOTS or [])[:limit_dirs]:
        try:
            entries = [e for e in os.listdir(root) if not e.startswith(".")]
        except Exception:
            continue
        files, dirs = [], []
        for e in entries:
            full = os.path.join(root, e)
            (dirs if os.path.isdir(full) else files).append(e)
        if not files and not dirs:
            continue
        lines.append("%s/  （%d 个文件，%d 个子目录）" % (root, len(files), len(dirs)))
        for d in sorted(dirs)[:per_dir]:
            try:
                n = len(os.listdir(os.path.join(root, d)))
            except Exception:
                n = 0
            lines.append("    [目录] %s/  （%d 项）" % (d, n))
        newest = []
        for f in files:
            try:
                newest.append((os.path.getmtime(os.path.join(root, f)), f))
            except Exception:
                pass
        newest.sort(reverse=True)
        for _m, f in newest[:per_dir]:
            lines.append("    %s" % f)
        if len(files) > per_dir:
            lines.append("    …还有 %d 个文件" % (len(files) - per_dir))
    return "\n".join(lines) if lines else "（没有可列出的目录）"


_WIDE_MAP = str.maketrans({
    "：": ":", "＼": "\\", "／": "/", "　": " ", "，": ",", "。": ".",
    "；": ";", "（": "(", "）": ")", "＂": '"', "‘": "'", "’": "'",
    "＜": "<", "＞": ">", "｜": "|", "＊": "*", "？": "?", "！": "!",
})


def _wide_fix(s, tag=""):
    """全角符号 → 半角。模型经常打出全角冒号，路径就废了。

    只对路径类参数（读/归/记/读笔/搜笔）做，避免污染检索关键词。
    """
    s = s or ""
    if tag not in ("读", "归", "记", "读笔", "搜笔"):
        return s
    fixed = s.translate(_WIDE_MAP)
    if fixed != s:
        log("全角已纠正：%s → %s" % (s[:40], fixed[:40]))
    return fixed


def do_tool(tag, arg):
    arg = _wide_fix(arg, tag)
    """执行一个内联工具标记：读/查/记/归。返回给模型看的文本。"""
    arg = (arg or "").strip()
    if arg in _PLACEHOLDER or len(arg) < 2:
        return "参数是占位符或太短，没执行。要真内容再标一次。"
    blocked = _repeat_guard(tag, arg)
    if blocked:
        log("熔断 %s:%s" % (tag, arg[:30]))
        return blocked

    if tag in ("读", "读段"):
        # 分段读：path@N 表示从第 N 段开始（每段 MAX_OUTPUT_CHARS 字）
        # 长文件/代码复审必须分段，否则模型只看到开头就下结论
        part, chunk = 0, 0
        if "@" in arg:
            _p, _c = arg.rsplit("@", 1)
            if _c.strip().isdigit():
                arg, chunk = _p.strip(), int(_c.strip())
        p = arg if os.path.isabs(arg) else os.path.join(READ_ROOTS[0], arg)
        if not _inside(p):
            return "不在允许读取的目录里，拒绝。"
        if os.path.isfile(p):
            hits_get()[:] = list(dict.fromkeys(hits_get() + [os.path.basename(p)]))
            with io.open(p, "r", encoding="utf-8", errors="replace") as f:
                total = f.read()
            start = chunk * MAX_OUTPUT_CHARS
            seg = total[start:start + MAX_OUTPUT_CHARS]
            if not seg:
                return "已经读完了（共 %d 字，你要的第 %d 段是空的）。" % (len(total), chunk)
            head = "[%s 第 %d 段，共 %d 字；全文 %d 字%s]" % (
                os.path.basename(p), chunk, len(seg), len(total),
                "，还有下一段： [读:%s@%d]" % (arg, chunk + 1)
                if start + len(seg) < len(total) else "，已到末尾")
            return head + "\n" + seg
        if os.path.isdir(p):
            return "目录:\n" + "\n".join("  - %s" % x for x in os.listdir(p)[:30])
        return "没有这个路径：%s" % arg

    if tag == "地图":
        m = dir_map()
        log("目录地图：%d 字" % len(m))
        return m

    if tag == "查":
        hits = []
        if TIER == "memory":
            hits = memory_search(arg, 5)
        elif TIER in ("semantic", "keyword"):
            hits = INDEX.search(arg, top_k=5)
        if not hits and MEM_SEARCH and TIER != "memory":
            hits = memory_search(arg, 5)
        if not hits and TIER == "none" and not MEM_SEARCH:
            hits_get().clear()
            return "没资料可查（这台机器没配可读目录，也没挂外部库）。别猜，直接说没有。"
        if not hits:
            hits_get().clear()
            return "没查到「%s」。不要推测，直接说没查到。" % arg
        _h = hits_get()
        _h[:] = list(dict.fromkeys(_h + [n for n, _s in hits]))
        lines = ["查到 %d 条（这些是唯一可以引用的文件）：" % len(hits)]
        for name, snip in hits:
            lines.append("- %s: %s" % (name, snip[:200]))
        return "\n".join(lines)


    if tag == "搜笔":
        # [搜笔:关键词] 或 [搜笔:库名|关键词]
        vault, q = "", arg
        if "|" in arg:
            vault, q = [x.strip() for x in arg.split("|", 1)]
        root, why = obs_root(vault)
        if not root:
            return why
        hits = obs_find(root, q, 6)
        if not hits:
            return "在笔记库里没搜到「%s」。" % q
        hits_get()[:] = list(dict.fromkeys(hits_get() + [h[0] for h in hits]))
        return "笔记库命中 %d 篇（只准依据这些）：\n" % len(hits) + "\n".join(
            "- %s — %s" % (h[0], h[1]) for h in hits)

    if tag == "读笔":
        # [读笔:路径@N]  和 [读:] 一样分段，只是限定在笔记库内
        part = 0
        if "@" in arg:
            _p, _c = arg.rsplit("@", 1)
            if _c.strip().isdigit():
                arg, part = _p.strip(), int(_c.strip())
        vault, relpath = "", arg
        if "|" in arg:
            vault, relpath = [x.strip() for x in arg.split("|", 1)]
        root, why = obs_root(vault)
        if not root:
            return why
        full, why = obs_safe(relpath, root)
        if not full:
            return why
        if not os.path.isfile(full):
            near = obs_find(root, os.path.basename(relpath).replace(".md", "")[:16], 3)
            return "笔记不存在：%s%s" % (relpath, ("；相近：" + "、".join(r for r, _ in near)) if near else "")
        with io.open(full, encoding="utf-8", errors="replace") as f:
            total = f.read()
        start = part * MAX_OUTPUT_CHARS
        seg = total[start:start + MAX_OUTPUT_CHARS]
        hits_get()[:] = list(dict.fromkeys(hits_get() + [os.path.basename(full)]))
        tail = "" if start + MAX_OUTPUT_CHARS >= len(total) else \
            "\n（未完，继续用 [读笔:%s@%d]）" % (relpath, part + 1)
        return "[%s 第 %d 段，共 %d 字]\n%s%s" % (relpath, part, len(total), seg, tail)

    if tag == "日":
        # [日:内容]  追加到今天日记（也可以用 [日:日期|内容]）
        day = time.strftime("%Y-%m-%d")
        body = arg
        if "|" in arg:
            a, b = [x.strip() for x in arg.split("|", 1)]
            if len(a) == 10 and a[4] == "-" and a[7] == "-":
                day, body = a, b
        root, why = obs_root("")
        if not root:
            return why
        full, why = obs_safe("%s/%s.md" % (OBS_DAILY_DIR, day), root)
        if not full:
            return why
        if len(body) > OBS_MAX_WRITE:
            return "一次写太多（%d 字 > %d）。分几次。" % (len(body), OBS_MAX_WRITE)
        old = ""
        if os.path.isfile(full):
            with io.open(full, encoding="utf-8", errors="replace") as f:
                old = f.read()
        stamp = time.strftime("%H:%M")
        block = ("\n\n- %s %s\n" % (stamp, body)) if old else ("# %s\n\n- %s %s\n" % (day, stamp, body))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + ".tmp"
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(old + block)
        os.replace(tmp, full)
        return "已记进今天日记 %s（现在 %d 字）。" % (day, len(old + block))

    if tag == "归":
        # [归:目录@天数]  归拢最近 N 天变动过的文件 —— 纯代码取事实，零编造
        arg2, days = arg, 1
        if "@" in arg:
            _p, _d = arg.rsplit("@", 1)
            if _d.strip().isdigit():
                arg2, days = _p.strip(), int(_d.strip())
        # 用户可能写成"归拢 D:\某目录"（没有 @天数）——把尾巴上的说明去掉
        arg2 = arg2.strip().strip("，,。")
        if os.path.isdir(arg2):
            pass
        else:
            _hit = [x for x in arg2.split() if os.path.isdir(x)]
            if _hit:
                arg2 = _hit[0]
        root = arg2 if os.path.isabs(arg2) else os.path.join(READ_ROOTS[0] if READ_ROOTS else ".", arg2)
        if not _inside(root):
            return "不在允许读取的目录里，拒绝。"
        if not os.path.isdir(root):
            return "没有这个目录：%s" % arg2
        cutoff = time.time() - days * 86400
        rows, n = [], 0
        for dp, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                if not fn.endswith((".md", ".txt", ".json", ".py", ".log", ".csv", ".docx", ".xlsx")):
                    continue
                fp = os.path.join(dp, fn)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                if st.st_mtime < cutoff:
                    continue
                n += 1
                rel = os.path.relpath(fp, root)
                rows.append((st.st_mtime, rel, st.st_size))
                if n >= 200:
                    break
            if n >= 200:
                break
        if not rows:
            # 最近 N 天没变动 ≠ 目录是空的：退回全量清单（只列文件名，不读内容）
            allrows = []
            for dp, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for fn in files:
                    fp = os.path.join(dp, fn)
                    try:
                        st = os.stat(fp)
                    except OSError:
                        continue
                    allrows.append((st.st_mtime, os.path.relpath(fp, root), st.st_size))
            if not allrows:
                return "这个目录里确实什么都没有：%s" % arg2
            allrows.sort(reverse=True)
            lines = ["最近 %d 天没有文件变动；以下是为全部 %d 个文件（按修改时间倒序，最多 60 条）："
                     % (days, len(allrows))]
            for mt, rel, sz in allrows[:60]:
                lines.append("- %s  %s  %d 字节" % (
                    time.strftime("%m-%d %H:%M", time.localtime(mt)), rel, sz))
            hits = []
            for _mt, rel, _sz in allrows[:60]:
                hits.append(rel)
                hits.append(os.path.basename(rel))
            _h = hits_get()
            _h[:] = list(dict.fromkeys(_h + hits))
            return "\n".join(lines)
        rows.sort(reverse=True)
        lines = ["最近 %d 天变动过的文件 %d 个（这些是唯一可以用作依据的事实）：" % (days, len(rows))]
        for mt, rel, sz in rows[:120]:
            lines.append("- %s  %s  %d 字节" % (
                time.strftime("%m-%d %H:%M", time.localtime(mt)), rel, sz))
        hits = []
        for _mt, rel, _sz in rows[:120]:
            hits.append(rel)
            hits.append(os.path.basename(rel))
        _h = hits_get()
        _h[:] = list(dict.fromkeys(_h + hits))
        return "\n".join(lines)

    if tag == "记" and "|" in arg and arg.split("|", 1)[0].strip().lower().endswith((".md", ".txt")):
        # [记:日记/某某.md|内容] —— 明确给了 .md/.txt 目标就写笔记库（只追加不覆盖）
        relpath, body = [x.strip() for x in arg.split("|", 1)]
        root, why = obs_root("")
        if not root:
            return why
        full, why = obs_safe(relpath, root)
        if not full:
            return why
        if len(body) > OBS_MAX_WRITE:
            return "一次写太多（%d 字 > %d）。分几次。" % (len(body), OBS_MAX_WRITE)
        old = ""
        if os.path.isfile(full):
            with io.open(full, encoding="utf-8", errors="replace") as f:
                old = f.read()
        block = ("\n\n---\n\n## %s\n\n%s\n" % (time.strftime("%Y-%m-%d %H:%M"), body)) if old \
            else ("# %s\n\n%s\n" % (os.path.splitext(os.path.basename(full))[0], body))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + ".tmp"
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(old + block)
        os.replace(tmp, full)
        return "已追加到笔记 %s（现在 %d 字，旧内容没动）。" % (relpath, len(old + block))

    if tag == "记" and not MEM_WRITE:
        return ("没接记忆系统：config.json 里的 mem_write 是空的。"
                "填上你的写入地址（形如 http://127.0.0.1:PORT/xxx/api/memories）后重试。")

    if tag == "记":
        # 只写，不读、不查、不改。写失败如实说。
        payload = {
            "title": arg[:60] or "记录",
            "content": arg,
            "folder_id": MEM_FOLDER,
            "bucket": "memory",
            "memory_type": "knowledge",
            "status": "active",
            "core_tags": ["写入"],
            "diffusion_tags": ["ce"],
            "opposing_tags": ["待整理"],
        }
        try:
            _post(MEM_WRITE, payload, timeout=60)
            return "已写入外部库。"
        except Exception as e:
            return "写入失败：%r" % e

    return "未知标记"


# ==================== 防编造：声称做过却没调工具 ====================
# 「已记住」也要抓——实机测出来 9B 最爱用这种最短的假话，而它连「经」都没有。
CLAIM_RE = re.compile(
    r"(?:我|咱)?(?:已|已经|刚刚|刚才|这就)(?:被|经)?"
    r"(?:记|录|写|存|看|读|翻|查|打开|找|问)"
    r"(?:下|入|进|过|到|了|完|录|好|住)")
CLAIM_RE2 = re.compile(
    r"(?:我|咱)(?:把|将)?[^。！？\n]{0,12}"
    r"(?:记|录|写|存|查|读)(?:下|入|进|了|完|好|住)")


def claims_action(text):
    """从回答里抓「声称做过某事」的句子（防编造的第一道）。"""
    t = (text or "").strip()
    # 太短又只有动词的（「已记住。」）优先按整句判
    for rx in (CLAIM_RE, CLAIM_RE2):
        m = rx.search(t)
        if m:
            return m.group(0)
    return ""


# ==================== 意图 → 必需工具 ====================
# 9B 太懒：光在提示词里写「用标记」它不用。所以按意图在代码里逼一次。
# 坑：「会议记录」里的「记录」不是要记东西。所以只认动词形态。
INTENT_RULES = [
    ("查", ["查一下", "查查", "搜一下", "搜索一下", "找一下", "找找", "翻一下"]),
    ("读", ["读一下", "打开这个文件", "读这个文件"]),
]


_DIR_WORDS = ("归拢", "清点", "目录", "文件夹", "桌面", "路径", "\\\\", ":\\")
_LIST_WORDS = ("列一下", "列出", "有哪些", "有什么", "里有什么", "多少个", "统计")


ITER_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ce_iterate.json")
ITER_EXTS = (".md", ".txt", ".json", ".py", ".log", ".csv")


def _iter_state():
    try:
        with io.open(ITER_STATE, encoding="utf-8") as f:
            d = json.load(f) or {}
        d.setdefault("since", 0.0)
        d.setdefault("runs", [])
        return d
    except Exception:
        return {"since": 0.0, "runs": []}


def _iter_save(d):
    try:
        tmp = ITER_STATE + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ITER_STATE)
        return True
    except Exception as e:
        log("迭代状态写不了: %r" % (e,))
        return False


def iter_scan_new(since, limit=400):
    """扫 read_roots 下比 since 新的文件（按 mtime 升序）。"""
    rows = []
    for root in (READ_ROOTS or []):
        if not os.path.isdir(root):
            continue
        for dp, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in INDEX_SKIP_DIRS]
            for fn in files:
                if fn.startswith("."):            # 隐藏文件不扫（含 .ce_iterate/.ce_index）
                    continue
                if not fn.lower().endswith(ITER_EXTS):
                    continue
                fp = os.path.join(dp, fn)
                if os.path.abspath(fp) == os.path.abspath(ITER_STATE):
                    continue
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                if st.st_mtime <= since or st.st_size <= 0:
                    continue
                rows.append((st.st_mtime, fp, st.st_size))
    rows.sort()
    return rows[:limit]


def iter_batches(rows, budget):
    """把新文件按预算切批；单个文件超预算就截断（会注明）。"""
    batches, cur, used = [], [], 0
    for mt, fp, sz in rows:
        try:
            with io.open(fp, "r", encoding="utf-8", errors="replace") as f:
                txt = f.read()
        except Exception:
            continue
        rel = os.path.relpath(fp, READ_ROOTS[0]) if READ_ROOTS else os.path.basename(fp)
        piece = "\n\n### %s（%s）\n%s" % (rel, time.strftime("%m-%d %H:%M", time.localtime(mt)), txt)
        if len(piece) > budget:
            piece = piece[:budget] + "\n（超预算，已截断）"
        if cur and used + len(piece) > budget:
            batches.append(cur)
            cur, used = [], 0
        cur.append((fp, piece))
        used += len(piece)
    if cur:
        batches.append(cur)
    return batches


def run_iterate(task, session_id="default", since=None, max_rounds=3, save_to=""):
    """增量迭代：扫新文件 → 分批整理 → 边整理边再扫新到的 → 汇总。

    返回 dict：{answer, files, batches, since, rounds}
    """
    _budget_new()
    hits_get().clear()
    st = _iter_state()
    if since:
        since_ts = float(since)
    else:
        since_ts = float(st.get("since") or 0) or (time.time() - 7 * 86400)   # 首次：近 7 天
    log("迭代开始：since=%s，任务=%s" % (
        time.strftime("%m-%d %H:%M", time.localtime(since_ts)), task[:40]))

    outs = []
    done_files, rounds = [], 0
    used_batches = 0
    while rounds < max(1, int(max_rounds)):
        rounds += 1
        rows = iter_scan_new(since_ts)
        if not rows:
            log("迭代：没有新文件了（第 %d 轮）" % rounds)
            break
        batches = iter_batches(rows, max(1500, SOURCE_CHARS - 800))
        log("迭代第 %d 轮：新文件 %d 个 → %d 批" % (rounds, len(rows), len(batches)))
        for bi, batch in enumerate(batches):
            used_batches += 1
            material = "".join(p for _fp, p in batch)
            msgs = [
                {"role": "system", "content": system_prompt()},
                {"role": "user", "content": (
                    "要做的整理：%s\n\n下面是新到的材料（第 %d 批，共 %d 个文件）：\n%s"
                    % (task, bi + 1, len(batch), material))},
            ]
            got = (call_model(msgs, max_tokens=MAX_ANSWER_TOKENS) or "").strip()
            if got:
                outs.append("## 第 %d 轮 / 第 %d 批（%d 个文件）\n%s"
                            % (rounds, bi + 1, len(batch), got))
            for mt, fp, _sz in [(r[0], r[1], r[2]) for r in rows if r[1] in [b[0] for b in batch]]:
                done_files.append(fp)
                since_ts = max(since_ts, mt)
            _save_ckpt(session_id, used_batches, {"iterate": True, "batch": bi + 1,
                                                  "files": [b[0] for b in batch], "out": got})
            _runlog(session_id, "迭代 第%d轮 第%d批：%d 文件 → %d 字"
                    % (rounds, bi + 1, len(batch), len(got)))
        # 这一轮做完再扫，看有没有整理期间新掉进来的文件（继续下一轮）
        nxt = iter_scan_new(since_ts)
        if not nxt:
            break
        if rounds >= max_rounds:
            log("迭代到达轮数上限 %d，剩下的下次继续" % max_rounds)
            break

    answer = "\n\n".join(outs).strip() or "（这段时间没有新文件）"
    answer = _clean_final(answer, answer)
    _save_last(session_id, answer)
    st["since"] = since_ts
    st["runs"] = (st.get("runs") or [])[-20:] + [{
        "t": time.strftime("%Y-%m-%d %H:%M:%S"), "task": task[:80],
        "files": len(done_files), "batches": used_batches, "rounds": rounds}]
    _iter_save(st)
    if save_to:
        try:
            ap = os.path.abspath(save_to)
            if any(ap.startswith(os.path.abspath(r) + os.sep) for r in (WRITE_ROOTS or [])):
                with io.open(ap, "w", encoding="utf-8") as f:
                    f.write(answer)
                log("迭代结果已写 %s" % ap)
            else:
                log("save_to 不在 write_roots，拒绝写入: %s" % save_to)
        except Exception as e:
            log("迭代结果写入失败: %r" % (e,))
    return {"answer": answer, "files": done_files, "batches": used_batches,
            "rounds": rounds, "since": since_ts}


def _meaningful(ans):
    """结果去掉符号后还有没有实际内容。防"三个空条目"当成品交出去。"""
    s = ans or ""
    s = re.sub(r"[\s\-*#>`_~·•…]+", "", s)
    return len(s) >= 8


def need_tool(task):
    t = task or ""
    # 「归」单独判：必须同时提到目录/路径 + 清点动作，否则"列出三条建议"会被误吞
    if any(w in t for w in ("归拢", "清点")) or (
            any(d in t for d in _DIR_WORDS) and any(l in t for l in _LIST_WORDS)):
        return "归"
    for tag, words in INTENT_RULES:
        if any(w in t for w in words):
            return tag
    return ""


def extract_arg(tag, task):
    """从任务里直接抽出参数——不让模型再抄一遍（它会抄成占位词）。

    严格要求：关键词后面必须紧跟冒号或逗号，且抽到的东西不能是问句。
    """
    t = (task or "").strip()
    pats = {
        "归": r"^(?:请)?(?:归拢|列一下|列出|清点)\s*[：:，,]?\s*(.+)$",
        "查": r"^(?:请)?(?:查一下|查查|搜一下|搜索一下|找一下|找找|翻一下)\s*[：:，,]\s*(.+)$",
        "读": r"^(?:请)?(?:读一下|打开这个文件|读这个文件)\s*[：:，,]\s*(.+)$",
    }
    m = re.match(pats.get(tag, r"$.^"), t)
    if not m:
        if tag == "归":
            # 口语说法也能抽："帮我看看桌面有什么文件" → "桌面"
            arg = re.sub(r"^(?:请|帮我|帮忙|给我|看看|看一下|瞄一眼|列一下|列出|清点|归拢)\s*", "", t)
        else:
            return ""
    else:
        arg = m.group(1).strip().strip("。！？ ")
    if tag == "归":
        # 剥掉尾巴上的描述，只留目录
        for tail in ("里有什么文件", "里有什么", "下有什么", "有什么文件", "有哪些文件",
                     "里有哪些文件", "的文件", "多少个", "有多少"):
            arg = arg.replace(tail, "")
        # 语气词前置："清点一下记忆库目录" → "记忆库目录"
        for head in ("一下", "一眼", "看看", "看一看", "帮我", "帮忙", "请", "给我"):
            arg = arg.replace(head, "")
        arg = arg.strip()
        arg = arg.strip().strip("。！？ ，,：:")
        # 必须像目录：带盘符/反斜杠，或含目录词
        if not (os.path.isabs(arg) or "\\" in arg or "/" in arg or
                any(w in arg for w in ("目录", "文件夹", "桌面", "记忆库"))):
            return ""
        return arg
    # 问句不是要存的内容
    if any(x in arg for x in ["？", "?", "吗", "什么", "为什么", "怎么"]):
        return ""
    return arg


def force_tool(tag, task, history):
    """参数由代码抽；抽不出来才问模型。抽出来就直接执行，不问模型。"""
    arg = extract_arg(tag, task)
    if arg and len(arg) >= 2 and arg not in _PLACEHOLDER:
        return "[%s:%s] → %s" % (tag, arg, do_tool(tag, arg)), ""

    hist = list(history) + [{"role": "user", "content":
                             "只输出一行：[%s:具体内容]。不要别的话。" % tag}]
    try:
        raw = call_model(hist, max_tokens=120)
    except Exception as e:
        return "", "生成失败 %r" % e
    found = TOOL_RE.findall(raw or "")
    if not found:
        return "", "它仍然没写标记：%s" % (raw or "")[:60]
    results = []
    for tg, a in found[:2]:
        results.append("[%s:%s] → %s" % (tg, a, do_tool(tg, a)))
    return "\n".join(results), ""


# ==================== 拆解多步任务 ====================
def should_decompose(msg):
    return any(re.search(m, msg or "") for m in
               ["一步一步", "逐步", "为什么", "先.*再.*最后", "步骤"])


def decompose(question):
    """让模型把任务拆成不超过 5 步，返回步骤名列表。"""
    txt = call_model([{"role": "system", "content": system_prompt()},
                      {"role": "user", "content":
                       "把这个问题拆成不超过5步，每步一行，用「步骤N：」开头，只写步骤名。\n\n" + question}],
                     max_tokens=300)
    steps = re.findall(r"步骤\d+[：:]\s*(.+?)(?:\n|$)", txt)
    if not steps:
        steps = [l.strip() for l in txt.split("\n") if len(l.strip()) > 4][:5]
    return steps or None


CHAIN = {}


# ==================== 接地守卫：来源不许乱认 ====================
# 实测失败：问「账本里有音色吗」，命中是《会议记录.md》，
# 它却答「账本里有记录，在会议记录.md 中提到」——把 A 的内容算到 B 头上。
# 内容是真的，来源是假的。对 agent 来说来源错了等于证据链断了。
_ASSERT = r"[^。！？\n]{0,6}(?:有|记载|提到|记录|写着|包含)"
_FILE_RE = re.compile(r"([\w\u4e00-\u9fa5]{1,24}\.(?:md|txt|json|py|log))")
_OBJ_RE = re.compile(r"([\w\u4e00-\u9fa5]{2,6})(?:里|中|里面|当中)")


def grounding_violations(answer, task):
    """返回 (乱认的文件, 乱认的对象)。空列表 = 干净。"""
    a = answer or ""
    hits_now = list(hits_get())
    # 模型会在中英文之间插空格（分词习惯），文件名比对必须先去空白和连接符，
    # 否则真实文件名「08-CE缺口与优化清单.md」会被判成乱认的「缺口与优化清单.md」。
    def _nf(s):
        return re.sub(r"[\s\-_—–·]+", "", s or "")

    hitn = [_nf(h) for h in hits_now]
    bad_files = []
    for f in set(_FILE_RE.findall(a)):
        fn = _nf(f)
        if not fn:
            continue
        if any(fn == h or fn in h or h in fn for h in hitn if h):
            continue
        bad_files.append(f)
    bad_objs = []
    for o in set(_OBJ_RE.findall(task or "")):
        # 回答里有没有「账本有/账本中提到」
        if not re.search(re.escape(o) + _ASSERT, a):
            continue
        # 这个对象对应的文件一次都没命中 → 它是在借用别的文件的证据
        if not any(o in h for h in hits_now):
            bad_objs.append(o)
    return bad_files, bad_objs


def grounding_fix(answer, task, history):
    """先让模型自己改一次；还错就由代码给一句真话。"""
    bad_files, bad_objs = grounding_violations(answer, task)
    if not bad_files and not bad_objs:
        return answer, None
    allowed = "、".join(hits_get()) if hits_get() else "（无）"
    tip = []
    if bad_objs:
        tip.append("你说了「%s」里有，但检索结果里没有它。" % "、".join(bad_objs))
    if bad_files:
        tip.append("你提到了不在结果里的文件：%s。" % "、".join(bad_files))
    instr = ("%s\n检索真正命中的文件只有：%s\n"
             "重说一遍：明确说哪个文件里有、哪个没有。不许把别的文件的内容算到这里。"
             "简短，不要解释。" % ("\n".join(tip), allowed))
    try:
        a2 = (call_model(list(history) + [{"role": "user", "content": instr}]) or "").strip()
    except Exception:
        a2 = ""
    if a2:
        b2 = grounding_violations(a2, task)
        if not b2[0] and not b2[1]:
            return a2, "模型自改成功"
    # 代码兜底：说事实
    parts = []
    for o in bad_objs:
        parts.append("%s里没有。" % o)
    if hits_get():
        parts.append("检索只命中：%s。" % "、".join(hits_get()))
    else:
        parts.append("没查到任何文件。")
    return "".join(parts), "代码兜底"


# ==================== 主循环 ====================
def _log_file(session_id):
    d = os.path.join(LOG_DIR, _safe_name(session_id))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, time.strftime("%Y-%m-%d") + ".log")


def _runlog(session_id, line):
    """人可直读的单行日志：时间 | 内容。同时刷到 stdout，边跑边能看。"""
    try:
        with io.open(_log_file(session_id), "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), line))
    except Exception as e:
        log("日志写不进去: %r" % (e,))
    log("[%s] %s" % (session_id, line))


def purge_old_logs():
    """按 LOG_DAYS 清理旧日志（只清临时目录，长期目录不动手）。"""
    if LOG_KEEP or not os.path.isdir(LOG_DIR):
        return 0
    cutoff = time.time() - LOG_DAYS * 86400
    n = 0
    for dp, _dirs, files in os.walk(LOG_DIR):
        for fn in files:
            fp = os.path.join(dp, fn)
            try:
                if os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
                    n += 1
            except OSError:
                pass
    return n


def _safe_name(s):
    return re.sub(r"[^\w\-]+", "_", (s or "run"))[:60]


def _ckpt_path(session_id, idx):
    return os.path.join(CKPT_DIR, _safe_name(session_id), "step-%02d.json" % idx)


def _last_path(session_id):
    d = os.path.join(CKPT_DIR, _safe_name(session_id))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "last-answer.txt")


def _save_last(session_id, text):
    """把"到目前为止的答案"落盘：客户端超时/断线也不至于啥都拿不回来。"""
    try:
        p = _last_path(session_id)
        tmp = p + ".tmp"
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text or "")
        os.replace(tmp, p)
    except Exception as e:
        log("last-answer 存不下: %r" % (e,))


def _read_last(session_id):
    try:
        with io.open(_last_path(session_id), encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""
    except Exception:
        return ""


def _save_ckpt(session_id, idx, data):
    """每一步都落一份检查点：谁也不用相信内存，出问题回滚靠它。"""
    try:
        d = os.path.dirname(_ckpt_path(session_id, idx))
        os.makedirs(d, exist_ok=True)
        with io.open(_ckpt_path(session_id, idx), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("检查点写不进去: %r" % (e,))


def _load_ckpts(session_id):
    d = os.path.join(CKPT_DIR, _safe_name(session_id))
    out = []
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.startswith("step-") and fn.endswith(".json"):
                try:
                    with io.open(os.path.join(d, fn), encoding="utf-8") as f:
                        out.append(json.load(f))
                except Exception:
                    pass
    return out


def rollback(session_id, to_step, extra="", keep_facts=False):
    """回滚到第 to_step 步之后（0 = 从头），并可选追加补充要求。
    后续检查点删除，链的 notes 截断，done 复位 —— 再调 /task 就从这儿往下走。"""
    with _CHAIN_LOCK:
        chains = _load_chain()
        ch = chains.get(session_id)
    if not ch:
        return {"ok": False, "error": "没有这个 session 的链"}
    n = len(ch.get("steps") or [])
    to_step = max(0, min(int(to_step), n))
    ch["i"] = to_step
    ch["notes"] = (ch.get("notes") or [])[:to_step]
    # map 模式把每块成品存在 parts 里（不是 notes）——
    # 只截 notes 会让回滚后的 parts 还留着旧块，最后合并又混进过期内容。
    if "parts" in ch:
        ch["parts"] = (ch.get("parts") or [])[:to_step]
    ch["done"] = False
    ch.pop("final", None)
    # facts 是第一步预取到的"事实清单"，回滚后可能已经不再适用
    # （用户常会补"只算代码"这类新要求），必须清空让它重新取，
    # 否则旧资料会污染后续步骤 —— 这是回滚最容易漏的一处。
    if not keep_facts:
        ch["facts"] = ""
        # source 有两种来源：用户给的正文（要保留，那是他的输入）
        # 和代码自己 [归:] 预取的素材（必须清，否则新要求会吃到旧素材）。
        if ch.get("source_from") == "prefetch":
            # 不直接清空：重取可能更差（实测 252 字 → 125 字）。
            # 标记过期，下次预取时取并集，信息只增不减。
            ch["source_stale"] = True
    if extra.strip():
        ch["extra"] = ((ch.get("extra") or "") + chr(10) + extra.strip()).strip()
        # map 模式：用户补的要求常常是"改范围"（只算 .md / 不要 .py），
        # 光把它拼进每块的提示里，块本身（比如"Python 文件明细"）照样会跑。
        # 所以这里用「原任务 + 补充要求」把剩余块重新拆一遍。
        if ch.get("mode") == "map" and to_step < len(ch.get("steps") or []):
            try:
                merged = ch.get("task") or ""
                asks = decompose_asks(merged + chr(10) + "补充要求（必须遵守）：" + extra.strip(),
                                      max_n=max(1, len(ch["steps"]) - to_step))
                heads = [a[0] for a in asks]
                details = [a[1] for a in asks]
                ch["steps"] = (ch["steps"] or [])[:to_step] + heads
                ch["details"] = (ch.get("details") or [])[:to_step] + details
                _runlog(session_id, "补充要求后重新拆剩余 %d 块：%s" % (
                    len(heads), " / ".join(h[:12] for h in heads)))
            except Exception as e:
                log("重新拆解失败（保留原块）: %r" % (e,))
    _save_one(session_id, ch)
    # 删掉回滚点之后的检查点
    d = os.path.join(CKPT_DIR, _safe_name(session_id))
    if os.path.isdir(d):
        for fn in os.listdir(d):
            m = re.match(r"step-(\d+)\.json$", fn)
            if m and int(m.group(1)) >= to_step:
                try:
                    os.remove(os.path.join(d, fn))
                except Exception:
                    pass
    log("回滚 %s → 第 %d 步（共 %d），补充要求 %d 字" % (
        session_id, to_step, n, len(extra or "")))
    _runlog(session_id, "回滚到第 %d/%d 步%s" % (
        to_step, n, ("，补充要求：" + extra.strip()[:60]) if extra.strip() else ""))
    return {"ok": True, "to_step": to_step, "of": n,
            "facts_cleared": not keep_facts,
            "next": None if to_step >= n else ch["steps"][to_step],
            "extra": ch.get("extra") or ""}


# 链文件的并发保护。
# 服务器是多线程的：A 线程给自己会话写链时，若把整个字典覆盖回去，
# 会把 B 线程刚写的会话冲掉（读-改-写竞态）。所以：
#   1) 所有写都先进这把锁
#   2) 锁内重新读一次文件，只替换自己这条，再整体落盘
# 这样不同会话互不影响；同一会话本身是顺序调用的，不需要更细的锁。
_CHAIN_LOCK = threading.Lock()


def _save_one(session_id, ch):
    """把某个会话的链合并写回文件（并发安全）。"""
    with _CHAIN_LOCK:
        d = _load_chain()
        d[session_id] = ch
        _save_chain(d)
        return d


def _load_chain():
    """读分步链状态（跨 HTTP 请求、跨重启都要接得上）。"""
    if os.path.isfile(CHAIN_PATH):
        try:
            with io.open(CHAIN_PATH, encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}
    return {}


def _save_chain(d):
    """原子写链状态：先写 .tmp 再 replace，防写一半断电。
    只保留最近 CHAIN_KEEP 条，避免文件无限增长。"""
    try:
        if len(d) > CHAIN_KEEP:
            # 已完成的优先丢，保留未完成的
            done_keys = [k for k, v in d.items() if v.get("done")]
            for k in done_keys[:max(0, len(d) - CHAIN_KEEP)]:
                d.pop(k, None)
        tmp = CHAIN_PATH + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, CHAIN_PATH)
    except Exception as e:
        log("链状态存不下: %r" % (e,))


def _trim_history(history, budget=None):
    """按**字数**裁对话历史，而不是按条数。

    为什么不能只按条数：一条工具结果可能 1500 字，30 条就是 4.5 万字，
    远超单请求能容纳的量 —— 那还是会爆上下文，只是爆得更晚。
    做法：保留 system，再从最近往前装，装不下就停（旧的整条丢弃）。
    """
    budget = int(budget or max(2000, SOURCE_CHARS))
    if not history:
        return history
    head = []
    rest = list(history)
    if rest and rest[0].get("role") == "system":
        head = [rest.pop(0)]
    used, keep = 0, []
    for m in reversed(rest):
        c = len(m.get("content") or "")
        if keep and used + c > budget:
            break
        used += c
        keep.append(m)
    keep.reverse()
    out = head + keep
    if len(out) < len(history):
        log("历史裁剪：%d 条 → %d 条（预算 %d 字，实际 %d 字）"
            % (len(history), len(out), budget, used))
    return out


def _lean_step(task, step, notes, tool_ctx="", extra=""):
    """单步上下文：任务一句话 + 之前各步的摘要（截断） + 本步 + 工具结果。
    绝不把完整历史塞进来 —— 小模型的上下文就是这么撑爆的。"""
    prev = ""
    if notes:
        prev = "前面几步的结论（只信这些，不要重做）：\n" + "\n".join(
            "%d) %s" % (i + 1, n[:STEP_NOTE_CHARS]) for i, n in enumerate(notes)) + "\n\n"
    add = ("补充要求（用户后加的，必须遵守）：\n" + extra.strip() + "\n\n") if (extra or "").strip() else ""
    msg = "总任务：%s\n\n%s%s当前步骤：%s" % (task, prev, add, step)
    if tool_ctx:
        msg += "\n\n本步工具结果：\n" + tool_ctx[:TOOL_CTX_CHARS]
    return [{"role": "system", "content": system_prompt()},
            {"role": "user", "content": msg}]


_NAME_CACHE = {"t": 0, "names": []}


def _roots_names():
    """把 READ_ROOTS 下一层的目录/文件名收集起来（60 秒缓存）。
    目的：用户随口说"docs""<工作目录>"，也能对上真目录。"""
    now = time.time()
    if now - _NAME_CACHE["t"] < 60 and _NAME_CACHE["names"]:
        return _NAME_CACHE["names"]
    names = []
    for root in (READ_ROOTS or []):
        try:
            for child in os.listdir(root):
                names.append((child, os.path.join(root, child)))
        except Exception:
            pass
    _NAME_CACHE["t"] = now
    _NAME_CACHE["names"] = names
    return names


def _resolve_hint(name):
    """把一个"裸名字"解析成真实路径：先直接试，再相对每个白名单根试，再按名字匹配一层子项。
    只认绝对路径是不够的——实际用的时候人是随手敲个文件夹名的。"""
    raw = (name or "").strip().strip("，。；：、,.\"'“”‘’()（）《》【】")
    if len(raw) < 2 or len(raw) > 80:
        return None
    for cand in (raw,):
        if os.path.isdir(cand) or os.path.isfile(cand):
            return os.path.abspath(cand)
    for root in (READ_ROOTS or []):
        cand = os.path.join(root, raw)
        if os.path.isdir(cand) or os.path.isfile(cand):
            return cand
    # 白名单根本身的名字也要认（"<工作目录>"就是根，不是根的子项）
    for root in (READ_ROOTS or []):
        if _nf(os.path.basename(os.path.normpath(root))) == _nf(raw):
            return root
    want = _nf(raw)
    for child, full in _roots_names():
        if _nf(child) == want or want in _nf(child):
            return full
    return None


def _prefetch(step):
    """步骤级工具预取：代码先替模型把它这一步要用的原料取好。

    为什么需要：小模型经常只"叙述"不调用工具（实测第1步报 5 个文件，真实 32 个）。
    能由代码保证的，不指望模型自觉。
    规则（每条都可预期、可测）：
      1) 步骤里出现存在的目录 → 代跑 [归:目录@1]
      2) 步骤里出现存在的文件 → 代跑 [读:文件@0]
      3) 步骤里有「查/检索/搜/找」且带关键词 → 代跑 [查:关键词]
    最多取两样，总长截断，避免把上下文撑爆。
    """
    outs, used = [], 0

    # 0) 任务本身在问"有什么/哪些/整理/归类"时，直接把目录地图交给它。
    #    不指望模型主动写 [地图:] —— 它连"自己不知道有哪些文件"都意识不到。
    if re.search(r"有哪些|哪些文件|有什么|整理|归拢|归类|分类|清理|目录", step or ""):
        try:
            m = dir_map()
            if m and not m.startswith("（没有"):
                outs.append("[目录地图]\n" + m)
                used += 1
        except Exception:
            pass

    for m in re.finditer(r"([A-Za-z]:[\\\\/][^\s，。；;\"'（）()]+)", step or ""):
        raw = m.group(1).rstrip(".，,；;：:")
        base = raw.split("@")[0]
        try:
            if os.path.isdir(base):
                r = do_tool("归", base + "@1")
            elif os.path.isfile(base):
                r = do_tool("读", base + "@0")
            else:
                continue
        except Exception as e:
            continue
        if r and not r.startswith(("不在允许", "没有这个")):
            outs.append("[预取] " + r)
            used += 1
            if used >= 2:
                break

    # 绝对路径没命中时，退一步把话里的"裸名字"逐个拿去解析（docs / <工作目录> / 周报…）
    if used == 0:
        for m in re.finditer(r"[\u4e00-\u9fa5A-Za-z0-9_\-.]{2,30}", step or ""):
            tok = m.group(0)
            if tok in ("最近一天", "最近", "一天", "文件", "目录", "文件夹", "所有", "全部"):
                continue
            hit = _resolve_hint(tok)
            if not hit:
                continue
            try:
                r = do_tool("归", hit + "@1") if os.path.isdir(hit) else do_tool("读", hit + "@0")
            except Exception:
                continue
            if r and not r.startswith(("不在允许", "没有这个")):
                outs.append("[预取] " + r)
                used += 1
                log("预取解析相对名: %s → %s" % (tok, hit))
                if used >= 2:
                    break

    if used < 2:
        m = re.search(r"(?:查|检索|搜索|找一下|查一下)\s*[：: ]?\s*([^\s，。；;]{2,24})", step or "")
        if m:
            kw = m.group(1)
            r = do_tool("查", kw)
            if r and not r.startswith(("没查到", "没资料")):
                outs.append("[预取] " + r)
                used += 1

    return "\n\n".join(outs)[:3000]


# 数值按「量名」比，不按单位比：
#   "代码文件共 17 个" 与 "共 32 个文件" 单位都是「个」，但它们是不同的量。
#   早先按单位比，把 32/17/15 全判成冲突 —— 那是误报。
_NUM_LABEL_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z]{2,8})\s*(?:共|有|为|计|是|=|：|:)?\s*(\d{1,6})\s*(个|条|项|份|处|次|行|张|页)")


def _pairs(text):
    """抽出 (量名, 数值) 对。量名取数字前面那串汉字/字母。"""
    out = []
    for label, num, unit in _NUM_LABEL_RE.findall(text or ""):
        key = (label + unit).replace("的", "").strip()
        if len(key) < 3:
            continue
        out.append((key, int(num)))
    return out


def number_conflicts(prev_notes, new_note):
    """跨步数字一致性：同一个「量名」在不同步骤给出不同数值 → 冲突。

    实测动机：同一批文件，不同步骤说代码 17 / 15 / 14 个，单看每步都合理。
    """
    seen = {}
    for n in list(prev_notes) + [new_note]:
        for key, val in _pairs(n):
            seen.setdefault(key, set()).add(val)
    bad = []
    for key, vals in seen.items():
        if len(vals) >= 2:
            bad.append("%s=%s" % (key, "/".join(str(v) for v in sorted(vals))))
    return bad


def numbers_unsourced(answer, haystack):
    """来源检查：回答里的两三位数字，是否能在给定文本里找到。
    只看 >=10 的数，避免把序号、0/1 这类误判。只提示，不改写。"""
    nums = set(re.findall(r"\b(\d{2,6})\b", answer or ""))
    missing = []
    for n in nums:
        # 年份/日期不是"数据"，不该被要求有出处（实测 2026 被误报）
        if re.fullmatch(r"(19|20)\d{2}", n):
            continue
        if n not in (haystack or ""):
            missing.append(n)
    return missing[:8]


def _clean_final(text, joined):
    """最终答案清洗：去掉残留的工具标记；若清完太空，就用各步结论兜底。"""
    s = TOOL_RE.sub("", text or "").strip()
    s = re.sub(r"搜索\s*$", "", s).strip()
    if len(s) < 8:
        log("合成结果太空，回退为各步结论")
        return "（以下按步骤汇总）\n" + joined
    return s


# 拆解废话特征：这种"要求"不是要求，是原地踏步
_META_ONLY = re.compile(
    r"^(执行|处理|完成|进行|继续|开始|开展|着手|按照?|根据|相关|以上|下面|接下来|步骤|"
    r"要求|任务|工作|内容|部分|环节|流程|操作|分析|整理|输出|生成|给出|提供|确保|实现|"
    r"第[一二三四五六七八九十\d]+步?|步骤\d*|同?上|如?前述?)[\s:：，。、]*(步骤?\d*|要求\d*|任务\d*)?$")
# 元词字集：这些字凑在一起不构成"要交付的内容"
_META_CHARS = set("执行处理完成进行继续开始开展着手按照根据相关以上下面接下来步骤要求任务工作内容"
                  "部分环节流程操作分析整理输出生成给出提供确保实现第步同上前述如该把将对并再还就都"
                  "一下这个那个些其对")

_TAUTOLOGY_WORDS = ("执行步骤", "完成上述", "按步骤", "继续执行", "重复上述", "同上", "见上")


_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def expect_count(task):
    """从任务里抽"要几条"。抽不到返回 None。

    只认明确的数量词，宁可不抽也不乱抽 —— 抽错会逼模型凑数。
    """
    s = task or ""
    pats = [
        r"(\d{1,3})\s*(?:条|个|点|项|类|种|步|名|行)",
        r"前\s*(\d{1,3})",
        r"([一二两三四五六七八九十]{1,2})\s*(?:条|个|点|项|类|种|步)",
    ]
    for pat in pats:
        m = re.search(pat, s)
        if not m:
            continue
        g = m.group(1)
        if g.isdigit():
            n = int(g)
        else:
            n = _CN_NUM.get(g[-1], 0) if g else 0
        if 2 <= n <= 30:      # 1 条没意义，>30 太容易误判
            return n
    return None


def count_items(text):
    """数产出里有几条：优先数列表行/编号行，没有就按首层顿号/分号分句。"""
    s = (text or "").strip()
    if not s:
        return 0
    lines = [l.strip() for l in s.splitlines() if l.strip()]
    bullet = [l for l in lines if re.match(r"^(?:[-*·•]\s*|\d{1,2}[.、)]\s*|[（(]\d{1,2}[)）]\s*|##+\s*)", l)]
    if bullet:
        return len(bullet)
    # 没有列表符号：看是否有"1. 2. 3."嵌在行内
    inline = re.findall(r"(?:^|[。；;\s])(\d{1,2})[.、)]", s)
    if len(inline) >= 2:
        return len(set(inline))
    # 再退一步：单段落里的顿号分句（至少 3 个才算条目）
    if s.count("、") >= 2 and "\n" not in s:
        return s.count("、") + 1
    return len(lines) if len(lines) >= 2 else 1


def _wish_hint(task):
    """判断任务是否像"许愿"，返回一句提示（不像就返回空串）。

    为什么要有它：许愿式指令不会报错，只会安静地变差——
    拆不开、每块空产出、成品一堆废话。用户看不出是自己没给清楚。
    """
    s = (task or "").strip()
    if not s:
        return ""
    # 有具体对象（路径/扩展名/数字/明确动作）就不再唠叨
    concrete = bool(re.search(r"[A-Za-z]:[\\/]|/|\\.(md|txt|py|json|docx?)", s)) \
        or bool(re.search(r"\\d", s)) \
        or any(v in s for v in ("列出", "统计", "分组", "分类", "提取", "改写",
                                "比较", "归拢", "整理", "生成", "写一份", "写成"))
    wishy = any(w in s for w in ("帮我", "随便", "看看", "优化一下", "总结一下",
                                 "搞一下", "弄一下", "处理一下"))
    if wishy and not concrete:
        return ("这个任务比较像\"许愿\"：缺具体对象或交付物，可能拆不成块。"
                "建议补上：素材（文件/目录/原文）+ 交付物（要几样、什么形式）。"
                "见 docs/12-怎么下指令.md")
    if len(s) < 10 and not concrete:
        return "任务描述太短，建议写清素材与交付物（见 docs/12-怎么下指令.md）"
    return ""


def _is_junk_ask(text, task):
    """废话判据：空 / 只剩元词 / 就是任务原文 / 自我指涉（喊口号）。"""
    s = (text or "").strip()
    if len(s) < 4:
        return True
    if _META_ONLY.match(s):
        return True
    if _norm(s) and _norm(s) == _norm(task or ""):
        return True
    for w in _TAUTOLOGY_WORDS:
        if w in s:
            return True
    # 把"元词字符"逐个扣掉，剩下不到 3 个实义字 → 废话
    # （锚定正则只能匹配整句，像"按照要求处理相关内容"这种扣不干净，所以按字符集算）
    _PUNCT_SET = set(" \u3000，。、：:；！？\"'“”‘’()（）【】、…")
    core = [c for c in s
            if c not in _META_CHARS and not c.isdigit() and c not in _PUNCT_SET]
    if len(core) < 3:
        return True
    return False


def decompose_asks(task, max_n=None):
    """只拆「用户要求」，不拆动作序列。

    为什么：拆成"先扫描、再分类、再统计"这种动作链，天然就是串行咀嚼；
    拆成"要哪些内容"这种彼此独立的子要求，才能各自回原文取料。
    返回 [(标题, 要求), ...]；模型没拆好就退化为单个要求。
    """
    max_n = max_n or MAX_ASKS
    txt = call_model([{"role": "system", "content": system_prompt()},
                      {"role": "user", "content":
                       "把下面这个任务拆成不超过 %d 个彼此独立的小要求（不是动作步骤，"
                       "是最终要交付的几块内容）。每行一条，格式：\n"
                       "要求N：<标题> | <具体要求>\n"
                       "例：要求1：代码清单 | 列出涉及的代码文件及一句话说明\n\n"
                       "任务：%s" % (max_n, task)}], max_tokens=400)
    asks = []
    for line in (txt or "").splitlines():
        m = re.match(r"\s*(?:要求|任务)\s*\d+\s*[：:]\s*(.+)", line.strip())
        if not m:
            continue
        body = m.group(1).strip()
        if "|" in body:
            title, detail = body.split("|", 1)
        else:
            title, detail = body[:12], body
        title, detail = title.strip() or "部分%d" % (len(asks) + 1), detail.strip()
        if len(detail) >= 4:
            asks.append((title, detail))
        if len(asks) >= max_n:
            break
    # 去废话 + 去重（小模型必出"步骤1：执行步骤1"这种东西）
    clean, seen = [], set()
    for title, detail in asks:
        if _is_junk_ask(detail, task) and _is_junk_ask(title, task):
            log("丢弃废话要求: %s" % (detail or title)[:40])
            continue
        k = _norm(title + detail)[:60]
        if k in seen:
            continue
        seen.add(k)
        clean.append((title, detail))
    if not clean:
        log("拆解全是废话，退回整篇当一个要求")
        clean = [("整体", task)]
    if len(clean) != len(asks):
        log("拆解去废话：%d → %d 条" % (len(asks), len(clean)))
    return clean


def plan_work(source_chars, ask_chars=800):
    """算总账：这份正文 + 这个要求，要几轮、能不能并行。

    原则（用户定调）：
      · 先看设备上限（capacity.json），不猜；
      · 不要因为能跑 18000 就塞两份 8000 并行 —— 总占用要留余量；
      · 不能并行就一个个来，本地模型不要钱，只求一轮里做完；
      · 全程可调：config.json 里改数字即可，不必重跑自检。
    """
    per = max(800, int(SOURCE_CHARS))
    segs = max(1, (int(max(0, source_chars)) + per - 1) // per)
    n_ctx = int(_CAP.get("n_ctx") or 0)
    slots = int(_CAP.get("slots") or 1)
    cpt = float(_CAP.get("chars_per_token") or 1.2)
    per_req_tokens = int((per + ask_chars) / max(0.5, cpt))
    parallel = 1
    why = "自检判定不能并行（%s）" % ("；".join(_CAP.get("reasons") or []) or "单槽或显存不足")
    if MAX_PARALLEL > 1 and n_ctx > 0:
        total_ctx = n_ctx * max(1, slots)
        # 留 10% 余量：每路请求占用 × 并行数 ≤ 总上下文 × 0.9
        can = int((total_ctx * 0.9) // max(1, per_req_tokens))
        parallel = max(1, min(MAX_PARALLEL, can))
        why = ("总上下文 %d，每路约占 %d tokens，余量内最多 %d 路" %
               (total_ctx, per_req_tokens, parallel))
    return {
        "source_chars": int(source_chars),
        "per_request_chars": per,
        "segments": segs,            # 这份正文要分几段喂
        "parallel": parallel,        # 建议同时跑几路（1 = 一个个来）
        "note": why,
    }


def _segments(text, limit):
    """把长文按段落切成 <= limit 字的若干段。大文件必须分段喂，否则会被静默截断。"""
    text = text or ""
    if len(text) <= limit:
        return [text]
    out, buf = [], ""
    for para in text.split("\n\n"):
        if len(para) > limit:                      # 单段就超长：硬切
            if buf:
                out.append(buf)
                buf = ""
            for i in range(0, len(para), limit):
                out.append(para[i:i + limit])
            continue
        if len(buf) + len(para) + 2 <= limit:
            buf = (buf + "\n\n" + para).strip() if buf else para
        else:
            out.append(buf)
            buf = para
    if buf:
        out.append(buf)
    return [x for x in out if x.strip()]


def _part_prompt(task, ask_title, ask_detail, base):
    """单个子要求的上下文：共享底座（原文/素材）+ 只这一个要求。

    system 用 lean：这一块只写成品，不带工具手册。
    """
    return [{"role": "system", "content": system_prompt(lean=True)},
            {"role": "user", "content":
             "总任务：%s\n\n【共享素材，只准依据这里的内容】\n%s\n\n"
             "现在只做这一块：%s\n具体要求：%s\n\n"
             "直接写出这一块的成品内容（不要写成「一句话结论」）。\n"
             "只输出这一块，不要写其它块，不要加「以下是」这种过渡语。"
             % (task, base[:SOURCE_CHARS], ask_title, ask_detail)}]


def run_mapreduce(task, session_id, source_text=""):
    """map-reduce：每路独立取材，最后合并。返回 (答案, 是否完成, 链)。"""
    global TIER
    chains = _load_chain()
    ch = chains.get(session_id)
    fresh = (not ch) or ch.get("task") != task or ch.get("mode") != "map"
    if fresh:
        asks = decompose_asks(task)
        ch = {"mode": "map", "task": task, "steps": [a[0] for a in asks],
              "details": [a[1] for a in asks], "i": 0, "parts": [],
              "done": False, "source": (source_text or "")[:SOURCE_TOTAL],   # 存全文，分段在喂的时候做
              "source_from": ("user" if (source_text or "").strip() else ""),
              "extra": (ch or {}).get("extra") if ch else ""}
        _save_one(session_id, ch)
        _runlog(session_id, "拆要求 %d 块：%s" % (
            len(asks), " / ".join(a[0] for a in asks)))
        log("map：拆成 %d 个独立要求" % len(asks))

    if ch.get("done"):
        return ch.get("final") or "", True, ch

    i = ch["i"]
    t_part = time.time()

    # 共享底座：优先用调用方给的原文；否则用 [归:] 预取的事实清单
    base = ch.get("source") or ""
    if not base:
        pre = _prefetch(task + chr(10) + (ch["details"][i] if i < len(ch["details"]) else ""))
        if pre:
            old = ch.get("source") or ""
            if ch.get("source_stale") and old:
                # 合并：旧清单里的行 + 新取到的行，去重后保留更全的那份
                seen, merged = set(), []
                for line in (old + chr(10) + pre).splitlines():
                    k = line.strip()
                    if k and k not in seen:
                        seen.add(k)
                        merged.append(k)
                ch["source"] = chr(10).join(merged)[:SOURCE_TOTAL]
            else:
                ch["source"] = pre[:SOURCE_TOTAL]
            ch["source_from"] = "prefetch"
            ch.pop("source_stale", None)
            base = ch["source"]
            _save_one(session_id, ch)
            _runlog(session_id, "共享素材已固化 %d 字" % len(base))

    title = ch["steps"][i]
    detail = ch["details"][i] if i < len(ch["details"]) else title
    # 开跑就先记一笔：万一这一块超时/断线，日志里能看出停在哪
    _runlog(session_id, "开始块%d/%d %s（若超时，已完成的前 %d 块已落盘）"
            % (i + 1, len(ch["steps"]), title[:16], i))
    extra = ch.get("extra") or ""
    if extra.strip():
        detail = detail + chr(10) + "补充要求：" + extra

    plan = plan_work(len(base), len(detail) + len(title))
    if not ch.get("plan"):
        ch["plan"] = plan
        _save_one(session_id, ch)
        _runlog(session_id, "总账：正文 %d 字 → 分 %d 段；并行 %d 路（%s）" % (
            plan["source_chars"], plan["segments"], plan["parallel"], plan["note"]))
    # 段预算必须扣掉固定开销：system 提示 + 总任务 + 本块要求 + 余量。
    # 否则"上限 1800"时实际发出去 2500 字，预算形同虚设。
    _fixed = len(system_prompt(lean=True)) + len(task) + len(title) + len(detail) + 400
    _seg_budget = max(600, SOURCE_CHARS - _fixed)
    segs = _segments(base, _seg_budget)
    if len(segs) > 1:
        _runlog(session_id, "块「%s」原文 %d 字 → 分 %d 段喂（段预算 %d = 上限 %d - 固定 %d）"
                % (title[:14], len(base), len(segs), _seg_budget, SOURCE_CHARS, _fixed))
    seg_out = []
    for si, seg in enumerate(segs):
        tail = ("【这是第 %d/%d 段，只提炼本段与该要求相关的内容】" % (si + 1, len(segs))) \
            if len(segs) > 1 else ""
        _msgs = _part_prompt(task, title, detail, seg + chr(10) + tail)
        _runlog(session_id, "块%d/%d 第%d/%d段：上下文 %d 字 / %d 条消息（预算 %d）"
                % (i + 1, len(ch["steps"]), si + 1, len(segs),
                   sum(len(m["content"]) for m in _msgs), len(_msgs), SOURCE_CHARS))
        got = (call_model(_msgs,
                          max_tokens=MAX_ANSWER_TOKENS) or "").strip()
        if got:
            seg_out.append(got)
    part = "\n\n".join(seg_out).strip()

    # 分步块数条数：只在"没拆成多块"时逐块守。
    # 否则"列 3 条"被拆成 3 块，每块都被要求凑 3 条 → 最后可能拼成 9 条。
    _need_n = expect_count(task + chr(10) + detail) if len(ch["steps"]) <= 1 else None
    if _need_n and count_items(part) < _need_n:
        _got = count_items(part)
        log("块「%s」条目不足：%d/%d → 要求重写" % (title[:12], _got, _need_n))
        try:
            _again = call_model(
                _part_prompt(task, title,
                             detail + chr(10) + "必须给出正好 %d 条，每条单独一行以 - 开头。" % _need_n,
                             base[:SOURCE_CHARS]),
                max_tokens=MAX_ANSWER_TOKENS) or ""
            if count_items(_again) >= _got:
                part = _again.strip()
        except Exception as e:
            log("条目重写失败: %r" % (e,))
        if count_items(part) < _need_n:
            part = part + "\n\n（注：要求 %d 条，实际 %d 条）" % (_need_n, count_items(part))

    ch["parts"].append(part)
    ch["i"] = i + 1
    _save_last(session_id, _clean_final(
        "\n\n".join("## %s\n\n%s" % (h, b) for h, b in
                     zip(ch["steps"][:len(ch["parts"])], [x for x in ch["parts"] if x])),
        "\n".join(ch["parts"])))

    flags = []
    if len(part) < 20:
        flags.append("空产出")
    _save_ckpt(session_id, i, {
        "step_no": i + 1, "total": len(ch["steps"]), "step": "%s | %s" % (title, detail[:60]),
        "note": part[:300], "flags": flags, "tool_output": base[:1500],
        "extra": extra, "mode": "map",
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed": round(time.time() - t_part, 1),
    })
    _runlog(session_id, "块%d/%d %s | 产出 %d 字%s" % (
        i + 1, len(ch["steps"]), title[:16], len(part),
        (" flags=" + "、".join(flags)) if flags else ""))

    if ch["i"] >= len(ch["steps"]):
        # 合并：只有一块就直接交付；多块优先代码拼接（零损耗），
        # 仅当需要统一行文时才叫模型合并，并明确禁止新增内容。
        parts = [x for x in ch["parts"] if x]
        if len(parts) <= 1:
            final = parts[0] if parts else ""
        else:
            heads = ch["steps"][:len(parts)]
            joined = "\n\n".join("## %s\n\n%s" % (h, b) for h, b in zip(heads, parts))
            final = joined
            if len(joined) < 80:
                final = call_model([{"role": "system", "content": system_prompt(lean=True)},
                                    {"role": "user", "content":
                                     "把下面这些块合并成一份连贯的成品。只做合并与排版，"
                                     "不许新增任何内容，不许删掉具体条目。\n\n" + joined}],
                                   max_tokens=MAX_ANSWER_TOKENS)
        ch["done"] = True
        ch["final"] = _clean_final(final, "\n".join(ch["parts"]))
        # 成品也要过接地守卫：编造的文件名/对象一律拦下重写。
        # （之前只在单轮路径和分步每步做，map 的最终成品漏了——实测出现过不存在的 notesA.md）
        try:
            bad_files, bad_objs = grounding_violations(ch["final"], task)
            if bad_files or bad_objs:
                log("成品接地不合格：文件 %s 对象 %s" % (bad_files[:3], bad_objs[:3]))
                _runlog(session_id, "成品被守卫拦下：%s" % "、".join((bad_files + bad_objs)[:4]))
                # 不在成品上做「重写」：重写只会把内容换成一句空话（实测 2 字）。
                # 改成保留原文 + 末尾加一行警告，让人自己判断。
                warn = "\n\n> 注意：以上提到的 %s 没能在本轮取料里核对到出处，可能不准。" % "、".join(
                    (bad_files + bad_objs)[:4])
                ch["final"] = ch["final"] + warn
        except Exception as e:
            log("成品接地检查出错（不拦）：%r" % (e,))
        _save_one(session_id, ch)
        log("map 完成，共 %d 块，成品 %d 字" % (len(ch["steps"]), len(ch["final"])))
        _runlog(session_id, "完成 %d 块，成品 %d 字" % (len(ch["steps"]), len(ch["final"])))
        return ch["final"], True, ch

    _save_one(session_id, ch)
    return "", False, ch


def run_stepwise(task, session_id):
    """一步一提交。返回 (答案, 是否完成, 进度信息)。"""
    chains = _load_chain()
    ch = chains.get(session_id)
    if not ch or ch.get("task") != task:
        steps = decompose(task)[:STEP_MAX] or [task]
        ch = {"task": task, "steps": steps, "i": 0, "notes": [], "done": False, "facts": ""}
        _save_one(session_id, ch)
        log("分步：拆成 %d 步" % len(steps))
        _runlog(session_id, "拆解 %d 步：%s" % (
            len(steps), " / ".join(s[:24] for s in steps)))

    if ch.get("done"):
        return ch.get("final") or "", True, ch

    i = ch["i"]
    t_step = time.time()          # 本步耗时从这里算
    step = ch["steps"][i]
    # 最后一步和前面几步的产出物不同：
    #   前面几步产出「结论摘要」（压榨上下文，给后面用）
    #   最后一步产出「成品」（直接交付给用的人）
    # 之前把最后一步也压成摘要，再让一次合成把摘要拼起来 —— 交付物变成
    # 「摘要的摘要」，真正的内容已经丢了。这是设计错误，这里改掉。
    is_last = (i + 1 >= len(ch["steps"]))
    log("分步 %d/%d：%s" % (i + 1, len(ch["steps"]), step[:40]))

    # 本步：允许它要工具（最多 2 轮），工具结果只在本步内使用
    # 预取的扫描范围要包含任务原文：拆出来的步骤常常丢掉完整路径，
    # 只看步骤文本会导致这一步什么都取不到（实测第1步只报 5 个文件）。
    pre = _prefetch(step + chr(10) + task)
    if pre:
        log("步骤预取 %d 字" % len(pre))
        # 第一步取到的事实清单要在链里存住：后面几步不会再取一遍，
        # 否则小模型会拿后一次的少量检索结果覆盖掉前面 32 条的清单（实测漂移）。
        if not ch.get("facts"):
            ch["facts"] = pre[:4000]
            log("事实清单已固化 %d 字" % len(ch["facts"]))
    carried = ch.get("facts") or ""
    ctx = (carried + (("\n\n" + pre) if pre and pre != carried else ""))[:4000]
    hist = _lean_step(task, step, ch["notes"], ctx)
    used_ctx = ctx
    for _round in range(2):
        raw = call_model(hist, max_tokens=600)
        tools = TOOL_RE.findall(raw)
        if not tools:
            break
        outs = []
        for tag, arg in tools:
            r = do_tool(tag, arg)
            outs.append("[%s:%s] → %s" % (tag, arg, r))
        used_ctx += "\n".join(outs) + "\n"
        hist = _lean_step(task, step, ch["notes"], used_ctx)

    product = ""
    if is_last:
        # 成品要求：按任务要求的结构直接写出来，不许写成"一句话结论"
        product = (call_model(_lean_step(
            task,
            step + "｜这一步直接输出最终成品。按任务要求的结构写（Markdown、分节、列清单都可以）；"
                   "不要只给一句结论，不要写「以下是汇总」「按步骤汇总」这类过渡语，"
                   "也不要说数据保存在哪里。",
            ch["notes"], used_ctx, ch.get("extra")), max_tokens=MAX_ANSWER_TOKENS) or "").strip()
        note = "（成品 %d 字）" % len(product)
        ch["product"] = product
    else:
        note = (call_model(_lean_step(
            task, step + "｜用一句话给出这一步的结论（不超过 %d 字）" % STEP_NOTE_CHARS,
            ch["notes"], used_ctx, ch.get("extra")), max_tokens=200) or "").strip()

    # 标红信号：给用户看「这一步值得怀疑」，不是自动判定错误
    flags = []
    if not note:
        flags.append("空结论")
    if "熔断" in used_ctx or "没有进展" in used_ctx:
        flags.append("重复熔断")
    if "不在允许" in used_ctx or "没查到" in used_ctx:
        flags.append("检索空手")
    conf = number_conflicts(ch["notes"], note)
    if conf:
        flags.append("数字不一致(%s)" % "; ".join(conf)[:40])
    _save_ckpt(session_id, i, {
        "step_no": i + 1, "total": len(ch["steps"]),
        "step": step, "note": note,
        "flags": flags,
        "tool_output": used_ctx[:2000],
        "extra": ch.get("extra") or "",
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed": round(time.time() - t_step, 1),
    })
    _runlog(session_id, "步%d/%d flags=%s | %s | 工具产出 %d 字" % (
        i + 1, len(ch["steps"]), ("、".join(flags) or "-"),
        (note or "（空）")[:80], len(used_ctx)))
    if flags:
        log("检查点 %d 标红: %s" % (i + 1, "、".join(flags)))
    ch["notes"].append(note or "（这一步没有结论）")
    ch["i"] = i + 1

    if ch["i"] >= len(ch["steps"]):
        joined = "\n".join("%d) %s" % (k + 1, n) for k, n in enumerate(ch["notes"]))
        product = (ch.pop("product", "") or "").strip()
        if len(product) >= 40:
            # 正常路径：最后一步的成品就是交付物，不再做二次合成
            final = product
        else:
            # 兜底：成品太空（模型没照做），才退回按各步结论合成
            log("成品过短（%d 字），退回按结论合成" % len(product))
            _runlog(session_id, "成品过短（%d 字），退回按各步结论合成" % len(product))
            final = call_model([{"role": "system", "content": system_prompt()},
                                {"role": "user", "content":
                                 "总任务：%s\n\n%s各步结论：\n%s\n\n据此给最终成品。"
                                 "直接写成品内容，不要只给摘要。" % (
                                     task,
                                     ("补充要求（用户后加的，必须遵守）：\n" + ch["extra"] + "\n\n")
                                     if (ch.get("extra") or "").strip() else "",
                                     joined)}],
                               max_tokens=MAX_ANSWER_TOKENS)
        ch["done"] = True
        ch["final"] = _clean_final(final, joined)
        src = (ch.get("facts") or "") + joined + task
        miss = numbers_unsourced(ch["final"], src)
        if miss:
            _runlog(session_id, "最终答案里有数字找不到出处（只提示）：%s" % "、".join(miss))
        _save_one(session_id, ch)
        log("分步完成，共 %d 步" % len(ch["steps"]))
        _runlog(session_id, "完成 %d 步，最终答案 %d 字" % (
            len(ch["steps"]), len(ch["final"])))
        return ch["final"], True, ch

    _save_one(session_id, ch)
    return "", False, ch


def run_task(task, history=None, session_id="default", force_step=False,
             source_text="", auto=False):
    """一个任务的入口：分步优先，否则单轮工具循环 + 三重守卫。"""
    _budget_new()
    hits_get().clear()
    set_step_info(None)
    if STEP_MODE and (force_step or should_decompose(task)):
        try:
            _rounds = 0
            while True:
                if PLAN == "map":
                    ans, done, ch = run_mapreduce(task, session_id, source_text)
                else:
                    ans, done, ch = run_stepwise(task, session_id)
                _rounds += 1
                # auto：一次请求里把所有块跑完（受 budget_sec / budget_calls 约束）。
                # 每跑完一块都已落盘，所以中途被预算掐断也不会丢。
                if not auto or done:
                    break
                if _rounds > 64:            # 兜底：绝不无限循环
                    log("auto 安全上限 64 轮，停手")
                    break
        except TimeoutError as e:
            log("预算耗尽: %r" % (e,))
            partial = _read_last(session_id)
            _runlog(session_id, "预算耗尽，已保存部分产出 %d 字" % len(partial))
            return ("（没做完：%s。已完成的 %d 步结果已落盘，可用 GET /last?session=%s 取回；"
                    "下次同一 session_id 调用会接着走）" % (e, 0, session_id)), None
        set_step_info({"step": ch["i"], "of": len(ch["steps"]), "done": done,
                       "next": None if done else ch["steps"][ch["i"]]})
        if not done:
            # 没做完：返回进度，让调用方决定什么时候再叫下一步
            return "（第 %d/%d 步完成，还没做完。继续调用同一个 session_id 会走下一步）" % (
                ch["i"], len(ch["steps"])), None
        return ans, None
    _map_done = False
    if history is None:
        history = [{"role": "system", "content": system_prompt()}]
        # 在问"有什么/整理"且没给原文时，先把目录地图放进对话。
        # 不指望模型主动想起 [地图:] —— 它连"自己不知道"都意识不到。
        if not (source_text or "").strip():
            try:
                if re.search(r"有哪些|哪些文件|有什么|整理|归拢|归类|分类|清理|目录", task or ""):
                    _m = dir_map()
                    if _m and not _m.startswith("（没有"):
                        history.append({"role": "user", "content":
                            "可用目录清单（代码扫出来的真实结果，只准依据它回答，不要编文件名）：\n" + _m})
                        log("目录地图已入上下文：%d 字" % len(_m))
                        _map_done = True
                        # 地图里的文件名要登记为"命中"，否则接地守卫会把它们
                        # 当成模型编造的文件名，把正确回答改掉（实测踩到）。
                        _h = hits_get()
                        for _line in _m.splitlines():
                            _fn = _line.strip().strip("-").strip()
                            if _fn and not _fn.startswith(("…", "[目录]", "可")):
                                if _fn.endswith("/"):
                                    _fn = _fn.rstrip("/").split(os.sep)[-1]
                                if _fn and _fn not in _h:
                                    _h.append(_fn)
            except Exception as e:
                log("目录地图注入失败: %r" % (e,))

    chain = CHAIN.get(session_id)
    if chain and chain["i"] < len(chain["steps"]):
        step = chain["steps"][chain["i"]]
        ans = call_model([{"role": "system", "content": system_prompt()},
                          {"role": "user", "content":
                           "总任务：%s\n当前步骤：%s\n只做这一步。" % (chain["q"], step)}],
                         max_tokens=400)
        chain["res"].append("步骤%d（%s）：%s" % (chain["i"] + 1, step, ans.strip()))
        chain["i"] += 1
        if chain["i"] >= len(chain["steps"]):
            history.append({"role": "user", "content":
                            "总任务：%s\n\n各步结果：\n%s\n\n给最终答案。"
                            % (chain["q"], "\n\n".join(chain["res"]))})
            del CHAIN[session_id]
        else:
            history.append({"role": "user", "content": "继续：%s" % chain["steps"][chain["i"]]})
    elif should_decompose(task):
        steps = decompose(task)
        if steps:
            CHAIN[session_id] = {"q": task, "steps": steps, "i": 0, "res": []}
            ans = call_model([{"role": "system", "content": system_prompt()},
                              {"role": "user", "content":
                               "总任务：%s\n当前步骤：%s\n只做这一步。" % (task, steps[0])}],
                             max_tokens=400)
            CHAIN[session_id]["res"].append("步骤1（%s）：%s" % (steps[0], ans.strip()))
            CHAIN[session_id]["i"] = 1
            if CHAIN[session_id]["i"] >= len(steps):
                history.append({"role": "user", "content":
                                "总任务：%s\n\n结果：\n%s\n\n给最终答案。"
                                % (task, "\n\n".join(CHAIN[session_id]["res"]))})
                del CHAIN[session_id]
            else:
                history.append({"role": "user", "content": "继续：%s" % steps[1]})
        else:
            history.append({"role": "user", "content": task})
    else:
        history.append({"role": "user", "content": task})

    history = _trim_history(history)

    answer = call_model(history, max_tokens=MAX_ANSWER_TOKENS)
    # 条目数不足：要求"列 N 条"却只给几条 → 明确要求重写一次；还不行就标注。
    _want_n = expect_count(task)
    if _want_n and count_items(answer) < _want_n:
        _got = count_items(answer)
        log("条目不足：要求 %d 条，产出 %d 条 → 要求重写" % (_want_n, _got))
        history.append({"role": "assistant", "content": answer})
        history.append({"role": "user", "content":
                        "你只给了 %d 条，要求是 %d 条。重写，必须正好 %d 条，"
                        "每条单独一行、以 - 开头，不要解释。"
                        % (_got, _want_n, _want_n)})
        answer = call_model(_trim_history(history), max_tokens=MAX_ANSWER_TOKENS)
        if count_items(answer) < _want_n:
            answer = answer.rstrip() + "\n\n（注：要求 %d 条，实际只给出 %d 条）" % (
                _want_n, count_items(answer))
    history.append({"role": "assistant", "content": answer})

    # 工具循环
    tools = TOOL_RE.findall(answer)
    used = False
    done_args = set()          # 真正执行成功的参数
    for _ in range(MAX_TOOL_ROUNDS):
        if not tools:
            break
        results = []
        for tag, arg in tools:
            r = do_tool(tag, arg)
            ok = not r.startswith("参数是占位符")
            if ok:
                used = True
                done_args.add(arg.strip())
            results.append("[%s:%s] → %s" % (tag, arg, r[:TOOL_CTX_CHARS]))
        history.append({"role": "user", "content":
                        "工具结果：\n" + "\n".join(results) + "\n\n基于真实结果回答。"})
        # 任务在问"有什么/整理/归类"且没有给原文时，先把目录地图塞进去。
        # 不指望模型自己想到去列目录 —— 它连"不知道有哪些"都意识不到。
        if re.search(r"有哪些|哪些文件|有什么|整理|归拢|归类|分类|清理|目录", task or "") and not source_text:
            try:
                _m = dir_map()
                if _m and not _m.startswith("（没有"):
                    history.append({"role": "user", "content":
                        "可用目录（代码列出的真实清单，只准依据它回答，不要编文件名）：\n" + _m})
            except Exception:
                pass
        history = _trim_history(history)
        _want_n = expect_count(task)
        answer = call_model(history, max_tokens=MAX_ANSWER_TOKENS)
        # 条目数不足：明确要求它补齐，重写一次；还不行就标注（不冒充完整）
        if _want_n and count_items(answer) < _want_n:
            log("条目不足：要求 %d 条，产出 %d 条 → 要求重写" % (_want_n, count_items(answer)))
            history.append({"role": "assistant", "content": answer})
            history.append({"role": "user", "content":
                            "你只给了 %d 条，要求是 %d 条。重写，必须正好 %d 条，"
                            "每条单独一行、以 - 开头，不要解释。"
                            % (count_items(answer), _want_n, _want_n)})
            answer = call_model(_trim_history(history), max_tokens=MAX_ANSWER_TOKENS)
            if count_items(answer) < _want_n:
                answer = answer.rstrip() + "\n\n（注：要求 %d 条，实际只给出 %d 条）" % (
                    _want_n, count_items(answer))
            history.append({"role": "assistant", "content": answer})
        else:
            history.append({"role": "assistant", "content": answer})
        tools = TOOL_RE.findall(answer)

    # 意图强制：任务要求「记/查/读」，代码自己抽参数执行。
    # 关键教训：模型会写 [查:内容] 这种占位词，看着像调了工具，其实什么也没做。
    # 所以这里不看它写没写标记，只看【这个意图的参数有没有被真正执行】。
    want = need_tool(task)
    if want and _map_done and want == "查":
        log("已有目录地图，跳过强制检索")
        want = None
    if want:
        arg = extract_arg(want, task)
        if arg and len(arg) >= 2 and arg not in _PLACEHOLDER and arg.strip() not in done_args:
            r = do_tool(want, arg)
            log("代码代做 %s:%s → %s" % (want, arg[:20], r[:30]))
            if not r.startswith(("参数是占位符", "写入失败", "没查到", "没有这个路径", "不在允许")):
                used = True
                history.append({"role": "user", "content":
                                "工具结果：\n[%s:%s] → %s\n\n基于真实结果回答。"
                                % (want, arg, r[:TOOL_CTX_CHARS])})
                answer = call_model(history, max_tokens=MAX_ANSWER_TOKENS).strip()
                history.append({"role": "assistant", "content": answer})
                done_args.add(arg.strip())

    # 防编造：驳回一次。驳完还敢说做过 → 代码接管，不让它编。
    if not used:
        claim = claims_action(answer)
        if claim:
            history.append({"role": "user", "content":
                            "你说了「%s」，但你这一轮没有调用任何工具，所以那件事没有发生。\n"
                            "要么真的做（写 [查:...] / [读:...]），要么说你没做。\n"
                            "重说这一句。不要解释，不要道歉。" % claim})
            answer2 = (call_model(history, max_tokens=MAX_ANSWER_TOKENS) or "").strip()
            if answer2:
                answer = answer2
                history.append({"role": "assistant", "content": answer})
                tags = TOOL_RE.findall(answer)
                if tags:
                    for tag, arg in tags[:3]:
                        do_tool(tag, arg)
                    used = True
            # 驳完还说做过却没调工具：硬接管，输出与事实一致的话
            if not used and claims_action(answer):
                want2 = need_tool(task)
                answer = {"记": "没记住。我没能写进外部库。",
                          "查": "没查成。我没能调用检索。",
                          "读": "没读到。"}.get(want2, "没做过。我不能说做过。")

    # 接地守卫：来源乱认就纠正（模型自改一次，不行就代码兜底）
    answer, how = grounding_fix(answer, task, history)
    if how:
        log("接地守卫: %s" % how)

    # 普通路径同样可能残留工具标记（模型把 [查:...] 写进正文），统一清洗
    final_text = re.sub(r"\[(查|读|记|归|搜笔|读笔|日)[:：][^\]]*\]", "", answer).strip()
    _save_last(session_id, final_text)     # 落盘：客户端超时也能用 /last 取回
    return final_text, history


# ==================== HTTP ====================
class Handler(BaseHTTPRequestHandler):
    """HTTP 门面：/health 与 /task，带 CORS 与可选 token 校验。"""
    def _cors(self):
        if CORS_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Max-Age", "600")

    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self._cors()
        self.end_headers()
        self.wfile.write(b)

    def do_OPTIONS(self):
        """浏览器预检。没有这个，网页前端调本服务必报跨域。"""
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _authed(self):
        """config.json 里设了 token 才校验（默认空 = 不校验，仅本机用）。"""
        if not TOKEN:
            return True
        got = (self.headers.get("Authorization") or "").replace("Bearer ", "").strip()
        return got == TOKEN

    def do_GET(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if self.path.startswith("/logs"):
            import urllib.parse as _up
            qs = _up.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            sid = (qs.get("session") or [""])[0]
            tail = int((qs.get("tail") or ["200"])[0])
            if not sid:
                runs = sorted(os.listdir(LOG_DIR)) if os.path.isdir(LOG_DIR) else []
                return self._send(200, {"ok": True, "log_dir": LOG_DIR, "sessions": runs})
            try:
                with io.open(_log_file(sid), encoding="utf-8") as f:
                    lines = f.read().splitlines()[-tail:]
                return self._send(200, {"ok": True, "session": sid,
                                        "file": _log_file(sid), "lines": lines})
            except FileNotFoundError:
                return self._send(404, {"ok": False, "error": "这个 session 还没有日志"})
        if self.path.startswith("/usage"):
            return self._send(200, {"ok": True, "total": usage_total(),
                                    "note": "进程启动以来的累计；重启归零。云端对账时可与账单比对"})
        if self.path.startswith("/last"):
            import urllib.parse as _up
            qs = _up.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            sid = (qs.get("session") or ["default"])[0]
            txt = _read_last(sid)
            if not txt:
                return self._send(404, {"ok": False,
                                        "error": "这个 session 还没有任何落盘的答案"})
            return self._send(200, {"ok": True, "session": sid,
                                    "chars": len(txt), "answer": txt})
        if self.path.startswith("/runs"):
            sid = ""
            if "?" in self.path:
                import urllib.parse as _up
                qs = _up.parse_qs(self.path.split("?", 1)[1])
                sid = (qs.get("session") or [""])[0]
            if sid:
                return self._send(200, {"ok": True, "session": sid,
                                        "checkpoints": _load_ckpts(sid)})
            root = CKPT_DIR
            runs = sorted(os.listdir(root)) if os.path.isdir(root) else []
            return self._send(200, {"ok": True, "runs": runs})
        self._send(200, {"ok": True, "docs": len(INDEX.docs),
                         "port": PORT, "mode": "tool",
                         "retrieval": TIER,
                         "chunks": len(INDEX.chunks),
                         "capacity": {k: _CAP.get(k) for k in
                                      ("n_ctx", "slots", "chars_per_token",
                                       "safe_source_chars", "max_parallel")},
                         "limits": {"source_chars": SOURCE_CHARS,
                                    "max_parallel": MAX_PARALLEL,
                                    "source_total": SOURCE_TOTAL},
                         "log_dir": LOG_DIR,
                         "log_keep": LOG_KEEP,
                         "ckpt_dir": CKPT_DIR})

    def do_POST(self):
        try:
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            if self.path.startswith("/rollback"):
                sid = d.get("session_id") or "default"
                r = rollback(sid, d.get("to_step", 0), d.get("extra") or "",
                             bool(d.get("keep_facts")))
                return self._send(200 if r.get("ok") else 400, r)
            if self.path.startswith("/rebuild"):
                return self._send(200, {"ok": True, "docs": rebuild()})
            task = (d.get("task") or d.get("message") or "").strip()
            # 把落盘目标写进任务：否则模型会自己编一个路径然后声称"已写入"
            _st = (d.get("save_to") or "").strip()
            if _st and task:
                task = task + "\n（说明：结果会由系统自动保存到 %s，你不用自己写文件，也不要说已写入。）" % _st
            if not task:
                return self._send(400, {"error": "task required"})
            if d.get("iterate"):
                r = run_iterate(task, d.get("session_id") or "default",
                                since=d.get("since"), max_rounds=int(d.get("max_rounds") or 3),
                                save_to=(d.get("save_to") or "").strip())
                return self._send(200, {"ok": True, "answer": r["answer"],
                                        "files": len(r["files"]), "batches": r["batches"],
                                        "rounds": r["rounds"], "since": r["since"]})
            t0 = time.time()
            auto_used = bool(d.get("auto"))
            wish_hint = _wish_hint(d.get("task") or "")
            src_text = d.get("source") or ""
            sf = (d.get("source_file") or "").strip()
            if not src_text and sf:
                ap = os.path.abspath(sf)
                if any(ap.startswith(os.path.abspath(r) + os.sep) for r in (READ_ROOTS or [])) \
                        and os.path.isfile(ap):
                    try:
                        with io.open(ap, encoding="utf-8", errors="replace") as f:
                            src_text = f.read()[:SOURCE_TOTAL]
                        log("已读入 source_file: %s（%d 字）" % (sf, len(src_text)))
                    except Exception as e:
                        log("source_file 读取失败: %r" % (e,))
                else:
                    log("source_file 不在白名单或不存在: %s" % sf)

            ans, _ = run_task(task, d.get("history"), d.get("session_id") or "default",
                              force_step=bool(d.get("stepwise")), source_text=src_text,
                              auto=bool(d.get("auto")))

            # 空产出拦截：去掉符号后没有实际内容，就不当成成品交出去
            try:
                _p0 = step_info()
                _is_prog = _p0 is not None and not _p0.get("done")
                if ans and not _is_prog and not _meaningful(ans):
                    log("空产出拦截：原结果 %d 字，无实际内容" % len(ans))
                    ans = ("（没有可交付的内容）模型只给出了符号或空条目，没有实际内容。"
                           "可能原因：目标文件不存在、检索无命中、或文件为空。"
                           "原始输出 %d 字已丢弃，可看 /logs 复核。" % len(ans))
            except Exception:
                pass

            saved = ""
            want = (d.get("save_to") or "").strip()
            # 分步没做完时 answer 只是一句进度提示，绝不能写进目标文件
            # —— 否则会把上一次写好的成品覆盖成"第 1/5 步完成"。
            _prog = step_info()
            if want and _prog is not None and not _prog.get("done"):
                saved = "（未完成，本次不写文件；进度 %s/%s）" % (
                    _prog.get("step"), _prog.get("of"))
                want = ""
            if want:
                ap = os.path.abspath(want)
                okdir = any(ap.startswith(os.path.abspath(r) + os.sep)
                            for r in (WRITE_ROOTS or []))
                if not okdir:
                    saved = "拒绝写入（不在 write_roots 里）: %s" % want
                else:
                    try:
                        os.makedirs(os.path.dirname(ap), exist_ok=True)
                        with io.open(ap, "w", encoding="utf-8", newline="\n") as f:
                            f.write(ans)
                        saved = ap
                    except Exception as e:
                        saved = "写入失败: %r" % (e,)

            self._send(200, {"ok": True, "answer": ans, "saved": saved,
                             "progress": step_info(),
                             "usage": usage_now(),
                             "auto": bool(auto_used),
                             "hint": wish_hint,
                             "elapsed": round(time.time() - t0, 2)})
        except Exception as e:
            # 不把 traceback 甩给用户：给可操作的中文，细节进日志
            msg = friendly_err(e, "处理请求")
            log("请求失败: " + msg.splitlines()[0])
            self._send(500, {"ok": False, "error": msg})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    log("CE·工具版 启动（无设定）")
    log("  模型 %s" % LLAMA)
    log("  可读 %s" % READ_ROOTS)
    rebuild()
    log("  监听 http://127.0.0.1:%d" % PORT)
    try:
        _n = purge_old_logs()
        if _n:
            log("清理旧日志 %d 个" % _n)
    except Exception:
        pass
    log("机器能力（tools/probe.py 测）: n_ctx=%s slots=%s → 单请求可喂 %d 字；并行 %d 路" % (
        _CAP.get("n_ctx"), _CAP.get("slots"), SOURCE_CHARS, MAX_PARALLEL))
    if not _CAP:
        log("  未找到 capacity.json —— 按保守预设跑（单请求 %d 字、串行）。"
            "建议先跑 tools/probe.py 自检。" % SOURCE_CHARS)
    log("运行日志（人可直读）: %s" % LOG_DIR)
    log("检查点（回滚用）    : %s" % CKPT_DIR)
    _Server((BIND, PORT), Handler).serve_forever()
