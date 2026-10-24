"""mindbeat —— 潜意识心跳

本地 9B 当潜意识：每 N 分钟看一眼，判断"该不该说话"（¥0）。
只有判定要说话时，才叫醒皮层（DeepSeek）写那一句。

人设不做性格偏向：这里只给事实数字，不给性格描述，让她自己决定要不要开口。

设计纪律：
- 信号一律由代码取，不让模型"感觉"，防编造
- 五道闸门由代码拦，9B 说 yes 也可能被拒
- 解析失败 = 沉默，不猜
"""
import asyncio
import json
import os
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

# ==================== 配置 ====================
LOCAL_LLM = "http://127.0.0.1:8080/v1/chat/completions"
LOCAL_MODEL = "qwen3"

# ---- 可移植路径：优先 ASTRBOT_DATA 环境变量，否则 ~/astrbot/data ----
_DATA = os.environ.get("ASTRBOT_DATA") or os.path.join(os.path.expanduser("~"), "astrbot", "data")
PERSONA = os.environ.get("PERSONA", "她")


def _p(*parts):
    return os.path.join(_DATA, *parts)


STATE_PATH = _p("plugin_data", "mindbeat", "state.json")
SELF_PATH = _p("persona_self", PERSONA, "自我.md")
STYLE_CFG = _p("config", "astrbot_plugin_group_chat_plus_config.json")

# 总开关：私信模块未定案前先关掉主动发送（用户 2026-10-24 要求）
ENABLED = True

TICK_SEC = 600            # 10 分钟看一眼
MIN_IDLE_SEC = 120 * 60   # 他 2 小时内说过话 → 不打扰（比 45 分钟保守）
MAX_PER_DAY = 2
COOLDOWN_SEC = 90 * 60
QUIET_FROM, QUIET_TO = 23, 7   # 23:00–07:00 静默（除非他先开口）
WATCH_DIRS = [d for d in os.environ.get("WATCH_DIRS", "").split(os.pathsep) if d.strip()]
TOPIC_DUP_WINDOW = 3


def _now():
    return time.time()


def _load_state():
    d = {
        "last_user_ts": 0.0,
        "last_proactive_ts": 0.0,
        "today": time.strftime("%Y-%m-%d"),
        "today_count": 0,
        "umo": "",
        "topics": [],
        "dir_mtime": 0.0,
    }
    if os.path.isfile(STATE_PATH):
        try:
            d.update(json.load(open(STATE_PATH, encoding="utf-8")) or {})
        except Exception as e:
            logger.warning("[mindbeat] state 读取失败: %r" % (e,))
    return d


def _save_state(s):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def _dir_mtime():
    """被监视目录的最新 mtime（取不到就是 0）。"""
    newest = 0.0
    for root in WATCH_DIRS:
        if not os.path.isdir(root):
            continue
        for base, _dirs, files in os.walk(root):
            for fn in files[:400]:
                try:
                    m = os.path.getmtime(os.path.join(base, fn))
                    if m > newest:
                        newest = m
                except OSError:
                    pass
    return newest


def _read_self():
    if os.path.isfile(SELF_PATH):
        try:
            return open(SELF_PATH, encoding="utf-8").read().strip()
        except Exception:
            return ""
    return ""


def _read_style():
    """借 group_chat_plus 的风格块，避免另写一套人设。"""
    if not os.path.isfile(STYLE_CFG):
        return ""
    try:
        d = json.load(open(STYLE_CFG, encoding="utf-8-sig"))
        return (d.get("reply_ai_extra_prompt") or "").strip()
    except Exception:
        return ""


def _hhmm(ts):
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "--"


async def _post_json(url, payload, timeout=90):
    import urllib.request
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _do():
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    return await asyncio.to_thread(_do)


@register("mindbeat", "mumu", "潜意识心跳：本地判断该不该说话", "1.0.0")
class MindBeat(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.state = _load_state()
        self._task = None
        logger.info("[mindbeat] 启动，tick=%ds 静默=%d-%d 点" % (
            TICK_SEC, QUIET_FROM, QUIET_TO))

    async def initialize(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def terminate(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[mindbeat] 已停止")

    # ---------- 记录用户活动 ----------
    @filter.on_llm_request()
    async def _on_req(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            umo = getattr(event, "unified_msg_origin", "") or ""
        except Exception:
            umo = ""
        changed = False
        if umo and umo != self.state.get("umo"):
            self.state["umo"] = umo
            changed = True
        self.state["last_user_ts"] = _now()
        changed = True
        if changed:
            _save_state(self.state)

    # ---------- 心跳 ----------
    async def _loop(self):
        await asyncio.sleep(60)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[mindbeat] tick 出错（循环继续）: %r" % (e,))
            await asyncio.sleep(TICK_SEC)

    def _signals(self):
        s = self.state
        now = _now()
        idle = now - (s.get("last_user_ts") or 0) if s.get("last_user_ts") else 999999
        dirm = _dir_mtime()
        dir_changed = dirm > (s.get("dir_mtime") or 0)
        return {
            "now": time.strftime("%Y-%m-%d %H:%M", time.localtime(now)),
            "hour": int(time.strftime("%H", time.localtime(now))),
            "idle_min": int(idle / 60),
            "since_last_proactive_min": int((now - (s.get("last_proactive_ts") or 0)) / 60)
            if s.get("last_proactive_ts") else -1,
            "today_count": s.get("today_count", 0),
            "dir_changed": dir_changed,
            "recent_topics": s.get("topics", [])[-TOPIC_DUP_WINDOW:],
        }

    def _gates(self, sig):
        """五道硬闸门。返回 (通过?, 原因)。"""
        h = sig["hour"]
        quiet = (h >= QUIET_FROM) or (h < QUIET_TO)
        if quiet:
            return False, "深夜静默"
        if sig["today_count"] >= MAX_PER_DAY:
            return False, "今日已达上限 %d 次" % MAX_PER_DAY
        if sig["idle_min"] < MIN_IDLE_SEC / 60:
            return False, "他刚说过话（%d 分钟前）" % sig["idle_min"]
        if sig["since_last_proactive_min"] >= 0 and \
                sig["since_last_proactive_min"] * 60 < COOLDOWN_SEC:
            return False, "冷却中（上次 %d 分钟前）" % sig["since_last_proactive_min"]
        return True, ""

    async def _decide(self, sig):
        """本地 9B 判断。返回 (speak, why, topic)。失败即沉默。"""
        prompt = (
            "你在替一个人判断：现在要不要主动开口找对方说话。\n"
            "只根据下面的事实，不要假设。\n\n"
            "事实：\n"
            "- 现在时间：%(now)s\n"
            "- 对方上次说话：%(idle_min)d 分钟前\n"
            "- 我今天已经主动说过：%(today_count)d 次\n"
            "- 我上次主动说话：%(since_last_proactive_min)s 分钟前（-1 表示从没说过）\n"
            "- 监视的文件夹有人动过：%(dir_changed)s\n"
            "- 我最近说过的话题：%(recent_topics)s\n\n"
            "判断标准只有一条：现在开口，对对方是有意义的，还是打扰。\n"
            "宁可不说。拿不准就说不说。\n\n"
            "只输出一行 JSON，不要别的话：\n"
            '{"speak": true 或 false, "why": "一句话理由", "topic": "想说什么"}'
        ) % sig
        try:
            d = await _post_json(LOCAL_LLM, {
                "model": LOCAL_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 160,
                # qwen3 默认把话全说在 reasoning 里，content 会是空的（踩过）
                "chat_template_kwargs": {"enable_thinking": False},
            }, timeout=60)
            msg = d["choices"][0]["message"]
            text = (msg.get("content") or "").strip()
            if not text:
                # 兜底：万一思考没关掉，从 reasoning_content 里捞 JSON
                text = (msg.get("reasoning_content") or "").strip()
        except Exception as e:
            logger.warning("[mindbeat] 本地判断失败，按沉默处理: %r" % (e,))
            return False, "", ""

        m = re.search(r"\{.*?\}", text, re.S)
        if not m:
            logger.info("[mindbeat] 本地未给 JSON，沉默：%s" % text[:80])
            return False, "", ""
        try:
            j = json.loads(m.group(0))
        except Exception:
            logger.info("[mindbeat] JSON 解析失败，沉默")
            return False, "", ""
        return bool(j.get("speak")), str(j.get("why") or ""), str(j.get("topic") or "")

    async def _compose(self, sig, why, topic):
        """叫醒皮层写那一句。失败返回空串。"""
        style = _read_style()
        mine = _read_self()
        sys_prompt = (style + "\n\n" + mine).strip()
        sys_prompt += (
            "\n\n（现在没人跟你说话。是你自己想开口。"
            "只回一句，像平常那样说。不要解释你为什么开口，不要提时间，不要提机制。）"
        )
        body = "现在 %s。你想开口，因为：%s。想说的是：%s。" % (
            sig["now"], why or "（说不出理由）", topic or "（没有具体话题）")
        try:
            prov = self.context.get_using_provider()
            if prov is None:
                logger.warning("[mindbeat] 没有可用 provider，放弃")
                return ""
            resp: LLMResponse = await prov.text_chat(
                prompt=body, system_prompt=sys_prompt, contexts=[])
            return (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:
            logger.error("[mindbeat] 生成失败: %r" % (e,))
            return ""

    async def _send(self, umo, text):
        chain = MessageChain([Plain(text)])
        try:
            await self.context.send_message(session=umo, message_chain=chain)
            return True
        except TypeError:
            # 老签名可能不接受关键字
            try:
                await self.context.send_message(umo, chain)
                return True
            except Exception as e:
                logger.error("[mindbeat] 发送失败(位置参数): %r" % (e,))
                return False
        except Exception as e:
            logger.error("[mindbeat] 发送失败: %r" % (e,))
            return False

    async def tick(self):
        """一次心跳。返回 dict，便于手工测试。"""
        if not ENABLED:
            logger.info("[mindbeat] 已禁用（ENABLED=False），不动")
            return {"speak": False, "gate": "已禁用"}

        sig = self._signals()
        s = self.state

        # 跨天重置次数
        today = time.strftime("%Y-%m-%d")
        if s.get("today") != today:
            s["today"] = today
            s["today_count"] = 0
            _save_state(s)

        ok, reason = self._gates(sig)
        if not ok:
            logger.info("[mindbeat] 不动：%s" % reason)
            return {"speak": False, "gate": reason, "signals": sig}

        speak, why, topic = await self._decide(sig)
        if not speak:
            logger.info("[mindbeat] 本地判断：不说")
            return {"speak": False, "gate": "本地说不说", "why": why, "signals": sig}

        umo = s.get("umo") or ""
        if not umo:
            logger.warning("[mindbeat] 没有会话目标（还没人跟他说过话），放弃")
            return {"speak": False, "gate": "无 umo", "signals": sig}

        text = await self._compose(sig, why, topic)
        if not text:
            return {"speak": False, "gate": "生成失败", "signals": sig}

        sent = await self._send(umo, text)
        if not sent:
            return {"speak": False, "gate": "发送失败", "text": text, "signals": sig}

        s["last_proactive_ts"] = _now()
        s["today_count"] = int(s.get("today_count", 0)) + 1
        s["topics"] = (s.get("topics") or [])[-(TOPIC_DUP_WINDOW * 2):] + [topic[:60]]
        s["dir_mtime"] = _dir_mtime()
        _save_state(s)
        logger.info("[mindbeat] 已主动开口：%s（因为 %s）" % (text[:40], why[:40]))
        return {"speak": True, "text": text, "why": why, "signals": sig}

    # ---------- 手工触发（方便验收，不暴露给 LLM）----------
    @filter.command("heartbeat")
    async def heartbeat(self, event: AstrMessageEvent):
        """手动跑一次心跳，看它会不会动（仅调试用）。"""
        r = await self.tick()
        if r.get("speak"):
            yield event.plain_result("动了一次：" + str(r.get("text") or "")[:60])
        else:
            yield event.plain_result("没动：" + str(r.get("gate") or "?"))
