"""本地代理：把"跑腿活"交给本地 9B（:8092 CE），不烧幕幕的上下文。

为什么要有它：
  主模型上下文很贵（人格 + 上下文 + 历史 ≈ 2800 tokens 起步）。
  查文件、算数、整理一段文字——这类活本地 9B 能干，而且有 CE 兜底
  （防编造 / 接地守卫 / 工具真执行）。

用法：幕幕在对话里调 local_task("任务描述")，本地跑完把结果拿回来。
"""
import json
import os
import urllib.request

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

_CE_BASE = os.environ.get("CE_BASE", "http://127.0.0.1:8092")
CE = _CE_BASE.rstrip("/") + "/task"
CE_HEALTH = _CE_BASE.rstrip("/") + "/health"
OWNER_UINS = {u.strip() for u in os.environ.get("OWNER_UINS", "").split(",") if u.strip()}
TIMEOUT = 180


def _post(url, payload, timeout=TIMEOUT):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _alive():
    try:
        with urllib.request.urlopen(CE_HEALTH, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


@register("local_agent", "mumu", "本地代理：把跑腿活交给本地 9B + CE", "1.0.0")
class LocalAgent(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        h = _alive()
        if h:
            logger.info(f"[local] CE 在线，索引 {h.get('docs')} 个文件")
        else:
            logger.warning("[local] CE 没起（:8092），local_task 会失败。"
                           "先把 CE 服务起起来（python ce/ce_tool.py，默认 127.0.0.1:8092）")

    @filter.llm_tool(name="local_task")
    async def local_task(self, event: AstrMessageEvent, task: str):
        """把一件不需要你自己动脑的跑腿活交给本地小模型去做。查文件、算数、整理文字都用它。

        好处：不占你的上下文，本地跑完只把结果给你。任务要写清楚、一次一件事。

        Args:
            task(string): 要让本地模型做的事，一句话说清，比如「查一下账本里十月电费多少」
        """
        if str(event.get_sender_id()) not in OWNER_UINS:
            return "只有主人能派活。"
        t = (task or "").strip()
        if len(t) < 2:
            return "任务太短，说清要做什么。"
        try:
            r = _post(CE, {"task": t, "session_id": "astrbot"})
        except Exception as e:
            logger.error(f"[local] 调用失败: {e!r}")
            return "本地代理没响应（可能没启动）。我直接自己做吧。"
        if not r.get("ok"):
            return "本地代理出错：%s" % (r.get("error") or "未知")
        ans = (r.get("answer") or "").strip()
        used = r.get("elapsed")
        logger.info(f"[local] {t[:30]} → {used}s")
        return ans or "本地代理没给出内容。"
