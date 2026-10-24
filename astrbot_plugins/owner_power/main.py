"""主人的最高权限：幕只能听主人的，别人一律拒绝。

跟 pc_tools 的区别：
  pc_tools      受限沙盒，写只能写<工作目录>（给别人用的安全版）
  owner_power   整机权限，只认主人一个 QQ 号

设计要点：
  - 身份判定用 event.get_sender_id()，这是 OneBot 从平台给的，消息里伪造不了
  - 非主人调用：直接拒 + 记账（谁试过、什么时候、想干什么）
  - 主人调用：整机读写、跑命令，全部记账
  - 只拦真正自毁的动作（格式化、删盘、改引导），其余全放行
    想连这些也放开：CATASTROPHIC_GUARD = False
"""
import asyncio
import os
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# ==================== 只认这个人 ====================
# 只有这些 QQ 号能让幕动电脑。别人说什么都没用。
OWNER_UINS = {u.strip() for u in os.environ.get("OWNER_UINS", "").split(",") if u.strip()}

# 审计日志：谁、什么时候、要干什么、结果
# ---- 可移植路径：优先 ASTRBOT_DATA 环境变量，否则 ~/astrbot/data ----
_DATA = os.environ.get("ASTRBOT_DATA") or os.path.join(os.path.expanduser("~"), "astrbot", "data")
PERSONA = os.environ.get("PERSONA", "她")


def _p(*parts):
    return os.path.join(_DATA, *parts)


AUDIT = _p("plugin_data", "owner_power", "audit.log")

# 主人一次读写最多拿多少（防把整个文件塞进上下文烧钱）
MAX_READ = 20000
RECENT_RUNS = {}   # 同一条命令的重试记录，用于熔断
CMD_TIMEOUT = 60

# 只拦“会把自己搞坏”的动作。删某个文件夹不在内——那是主人的自由。
CATASTROPHIC_GUARD = True
CATASTROPHIC = [
    r"\bformat\b", r"diskpart", r"bcdedit", r"vssadmin\s+delete",
    r"(?:remove-item|del|rd|rmdir)\b.*\b[a-z]:\s*\\{0,2}\s*$",   # 删整盘根
    r"\brm\s+-[a-z]*r[a-z]*f[a-z]*\s+/(?:\s|$)",
    r"cipher\s+/w", r"clear-disk", r"initialize-disk",
]


def _audit(who: str, kind: str, detail: str, ok: bool, note: str = ""):
    line = "[%s] %-14s %-7s %-4s %s%s\n" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), who, kind,
        "OK" if ok else "DENY", detail[:400],
        ("  => " + note[:200]) if note else "")
    try:
        os.makedirs(os.path.dirname(AUDIT), exist_ok=True)
        with open(AUDIT, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _sender(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_sender_id())
    except Exception:
        return "unknown"


def _is_owner(event: AstrMessageEvent) -> bool:
    return _sender(event) in OWNER_UINS


def _deny(event: AstrMessageEvent, kind: str, detail: str) -> str:
    who = _sender(event)
    _audit(who, kind, detail, False, "不是主人")
    logger.warning(f"[owner_power] 拒绝 {who} 的 {kind}: {detail[:80]}")
    return "这个只有主人能让我做。"


def _check_catastrophic(cmd: str) -> str:
    if not CATASTROPHIC_GUARD:
        return ""
    low = cmd.lower()
    for pat in CATASTROPHIC:
        if re.search(pat, low):
            return "这条会把这个系统搞坏，我拦下了（%s）。真要执行，把 CATASTROPHIC_GUARD 改成 False。" % pat
    return ""


@register("owner_power", "mumu", "主人的最高权限：整机读写、跑命令，只认主人", "1.0.0")
class OwnerPower(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    # ---------------- 跑命令 ----------------
    @filter.llm_tool(name="owner_run")
    async def owner_run(self, event: AstrMessageEvent, command: str):
        """在电脑上执行一条命令（PowerShell）。只有主人能用。

        Args:
            command(string): 要执行的命令
        """
        if not _is_owner(event):
            return _deny(event, "RUN", command)

        cmd = (command or "").strip()
        if not cmd:
            return "命令是空的。"

        bad = _check_catastrophic(cmd)
        if bad:
            _audit(_sender(event), "RUN", cmd, False, bad)
            return bad

        # 熔断：同一条命令 2 分钟内试过 2 次还不行，就不许再试
        now = time.time()
        hist = RECENT_RUNS.setdefault(cmd, [])
        hist[:] = [x for x in hist if now - x < 120]
        if len(hist) >= 2:
            msg = ("同一条命令两分钟内已经试过两次都没成，不许再试。"
                   "换个办法，或者直接说做不到。")
            _audit(_sender(event), "RUN", cmd, False, msg)
            return msg
        hist.append(now)

        try:
            proc = await asyncio.create_subprocess_shell(
                'powershell -NoProfile -Command "%s"' % cmd.replace('"', '`"'),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=CMD_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                _audit(_sender(event), "RUN", cmd, False, "超时 %ds" % CMD_TIMEOUT)
                return "跑了 %d 秒还没完，掐掉了。" % CMD_TIMEOUT

            text = (out or b"").decode("utf-8", "replace").strip()
            errt = (err or b"").decode("utf-8", "replace").strip()
            rc = proc.returncode
            _audit(_sender(event), "RUN", cmd, rc == 0, "退出码 %s" % rc)

            body = "退出码 %s" % rc
            if rc != 0:
                body = ("**这条失败了**（退出码 %s）。别当成功，也别重试同一条命令。"
                        "要么换个办法，要么直接说做不到。\n" % rc) + body
            if text:
                body += "\n" + text[:3000]
            if errt:
                body += "\n[stderr]\n" + errt[:1200]
            return body
        except Exception as e:
            _audit(_sender(event), "RUN", cmd, False, repr(e))
            return "执行出错：%s" % e

    # ---------------- 读文件（整机）----------------
    @filter.llm_tool(name="owner_read")
    async def owner_read(self, event: AstrMessageEvent, path: str):
        """读电脑上任意一个文件。只有主人能用。

        Args:
            path(string): 文件的完整路径
        """
        if not _is_owner(event):
            return _deny(event, "READ", path)

        p = os.path.abspath((path or "").strip().strip('"'))
        if not os.path.isfile(p):
            _audit(_sender(event), "READ", p, False, "不存在")
            return "没有这个文件：%s" % p
        try:
            raw = open(p, "rb").read(MAX_READ * 6)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("gbk", "replace")
            _audit(_sender(event), "READ", p, True, "%d 字节" % len(text))
            return text[:MAX_READ] + ("\n…（后面还有，被截断了）" if len(text) > MAX_READ else "")
        except Exception as e:
            _audit(_sender(event), "READ", p, False, repr(e))
            return "读不了：%s" % e

    # ---------------- 写文件（整机）----------------
    @filter.llm_tool(name="owner_write")
    async def owner_write(self, event: AstrMessageEvent, path: str, content: str):
        """往电脑上任意位置写文件。只有主人能用。

        Args:
            path(string): 文件的完整路径
            content(string): 写进去的内容
        """
        if not _is_owner(event):
            return _deny(event, "WRITE", path)

        p = os.path.abspath((path or "").strip().strip('"'))
        try:
            parent = os.path.dirname(p)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(content or "")
            _audit(_sender(event), "WRITE", p, True, "%d 字" % len(content or ""))
            return "写好了：%s（%d 字）" % (p, len(content or ""))
        except Exception as e:
            _audit(_sender(event), "WRITE", p, False, repr(e))
            return "写不进去：%s" % e

    # ---------------- 列目录（整机）----------------
    @filter.llm_tool(name="owner_list")
    async def owner_list(self, event: AstrMessageEvent, path: str):
        """列电脑上任意一个目录。只有主人能用。

        Args:
            path(string): 目录路径，比如 C:\\Users\\WEI\\Desktop
        """
        if not _is_owner(event):
            return _deny(event, "LIST", path)

        p = os.path.abspath((path or "").strip().strip('"'))
        if not os.path.isdir(p):
            _audit(_sender(event), "LIST", p, False, "不是目录")
            return "没有这个目录：%s" % p
        try:
            rows = []
            with os.scandir(p) as it:
                for e in sorted(it, key=lambda x: x.name)[:80]:
                    try:
                        if e.is_dir():
                            rows.append("[目录] " + e.name)
                        else:
                            rows.append("%-44s %10.1f KB" % (e.name, e.stat().st_size / 1024))
                    except OSError:
                        rows.append(e.name)
            _audit(_sender(event), "LIST", p, True, "%d 项" % len(rows))
            return "\n".join(rows) if rows else "（空的）"
        except Exception as e:
            _audit(_sender(event), "LIST", p, False, repr(e))
            return "列不出来：%s" % e
