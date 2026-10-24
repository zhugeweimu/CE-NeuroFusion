"""obs_ctl —— 连 OBS 的手

为什么单独做一个插件：OBS 不是人人都有。没有的人装了这个插件也不该受影响
——所以它在「探测不到 OBS」时静默休眠，只回一句可读的话，不报错、不刷日志、不重试。

协议：obs-websocket v5（OBS 28+ 自带，默认端口 4455，可选密码）
  连接 → 服务端发 Hello(challenge, salt) → 客户端算 auth 发 Identify
       → 之后每个请求是 op=6，响应 op=7

硬约束（不指望模型自觉，全部由代码保证）：
  ① 动作白名单：只认 start/stop/toggle，别的一律拒绝
  ② 直播开播默认禁止（allow_stream_start=false）——AI 不能自己开播
  ③ 熔断：同一动作 120 秒内失败 3 次 → 停手并说明原因（防它反复试）
  ④ 每次请求 8 秒超时；连不上先做 TCP 探测，不做盲目重连
  ⑤ 所有出口都是可读中文，不抛异常给上层
"""
import asyncio
import base64
import hashlib
import json
import os
import socket
import time

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register

# ==================== 配置 ====================
# 想改就改这里，或者在 AstrBot 插件配置里覆盖同名键
OBS_HOST = "127.0.0.1"
OBS_PORT = 4455
OBS_PASSWORD = ""              # OBS → 工具 → WebSocket 服务器设置 里的密码
ALLOW_STREAM_START = False     # 默认禁止 AI 开播（安全）
REQ_TIMEOUT = 8.0              # 单请求超时（秒）
PROBE_TIMEOUT = 0.8            # TCP 探测超时（秒）
FAIL_WINDOW = 120              # 熔断统计窗口（秒）
FAIL_LIMIT = 3                 # 窗口内失败几次就停手

_ACTIONS = ("start", "stop", "toggle")
_fail_log = {}                 # 动作 → [失败时间戳]


def _now():
    return time.time()


def _cfg_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _load_cfg():
    """插件根目录下的 config.json 可以覆盖上面的默认值（有就用，没有就用默认）。"""
    globals_ = globals()
    try:
        with open(_cfg_path(), "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        for k, v in d.items():
            key = k.upper()
            if key in globals_:
                globals_[key] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("[obs_ctl] config.json 读不了，用默认值: %r" % (e,))


_load_cfg()


# ==================== 熔断（纯函数，可单测） ====================
def _blocked(action):
    """同一动作在窗口内失败太多次 → 返回一句可读的拒绝理由，否则 None。"""
    hist = [t for t in _fail_log.get(action, []) if _now() - t < FAIL_WINDOW]
    _fail_log[action] = hist
    if len(hist) >= FAIL_LIMIT:
        return ("「%s」在 %d 秒内已经失败 %d 次，先停手。"
                "请确认 OBS 开着、WebSocket 服务器已启用、端口和密码对得上。"
                % (action, FAIL_WINDOW, len(hist)))
    return None


def _mark_fail(action):
    _fail_log.setdefault(action, []).append(_now())


def _mark_ok(action):
    _fail_log.pop(action, None)


def _check_action(action):
    """动作白名单。模型很爱自创动作名，这里统一收口。"""
    a = (action or "").strip().lower()
    if a in ("", "status", "查询"):
        return "status"
    if a not in _ACTIONS:
        return None
    return a


# ==================== 连通性探测 ====================
def _probe():
    """先看端口通不通。不通就别费劲连接，也不要重试。"""
    try:
        with socket.create_connection((OBS_HOST, int(OBS_PORT)), timeout=PROBE_TIMEOUT):
            return True, ""
    except OSError as e:
        return False, ("连不上 OBS（%s:%s）：%r。可能原因：OBS 没开；"
                       "OBS 里没启用 WebSocket 服务器；端口不是 %s。"
                       % (OBS_HOST, OBS_PORT, e, OBS_PORT))


# ==================== obs-websocket v5 客户端 ====================
class ObsClient:
    """最小实现：Hello → Identify → 顺序请求。够用就行，不引入额外依赖。"""

    def __init__(self):
        self.ws = None
        self.rpc = 1
        self._rid = 0

    async def __aenter__(self):
        import websockets
        self.ws = await asyncio.wait_for(
            websockets.connect("ws://%s:%s" % (OBS_HOST, OBS_PORT),
                               max_size=8 * 1024 * 1024),
            timeout=REQ_TIMEOUT)
        await self._handshake()
        return self

    async def __aexit__(self, *exc):
        try:
            if self.ws is not None:
                await self.ws.close()
        except Exception:
            pass

    async def _recv(self):
        raw = await asyncio.wait_for(self.ws.recv(), timeout=REQ_TIMEOUT)
        return json.loads(raw)

    async def _handshake(self):
        hello = await self._recv()
        if hello.get("op") != 0:
            raise RuntimeError("握手失败：第一条不是 Hello（收到 op=%r）" % hello.get("op"))
        d = hello.get("d") or {}
        self.rpc = d.get("rpcVersion") or 1
        ident = {"rpcVersion": self.rpc, "eventSubscriptions": 0}
        auth = d.get("authentication") or {}
        if auth.get("challenge"):
            # v5 认证：sha256(password+salt) → base64 → 拼 challenge → sha256 → base64
            secret = base64.b64encode(
                hashlib.sha256((OBS_PASSWORD + auth.get("salt", "")).encode("utf-8")).digest())
            ident["authentication"] = base64.b64encode(
                hashlib.sha256(secret + auth["challenge"].encode("utf-8")).digest()).decode("utf-8")
        await self.ws.send(json.dumps({"op": 1, "d": ident}))
        back = await self._recv()
        if back.get("op") == 9:
            raise RuntimeError("认证失败：密码不对（OBS 里 WebSocket 密码要一致）")
        if back.get("op") not in (2, 3):
            raise RuntimeError("认证失败：收到 op=%r" % back.get("op"))

    async def request(self, req_type, data=None):
        """发一个请求并等它的响应。失败抛 RuntimeError（中文原因）。"""
        self._rid += 1
        rid = "r%d" % self._rid
        payload = {"requestType": req_type, "requestId": rid}
        if data:
            payload["requestData"] = data
        await self.ws.send(json.dumps({"op": 6, "d": payload}))
        while True:
            msg = await self._recv()
            if msg.get("op") != 7:
                continue                      # 事件之类的，跳过
            d = msg.get("d") or {}
            if d.get("requestId") != rid:
                continue
            st = d.get("requestStatus") or {}
            if not st.get("result", False):
                raise RuntimeError("%s 失败（code=%s）" % (req_type, st.get("code")))
            return d.get("responseData") or {}


# ==================== 预置查询 ====================
async def _state():
    """一次连接里取齐：版本、场景、是否录制、是否直播。"""
    async with ObsClient() as c:
        ver = await c.request("GetVersion")
        scene = await c.request("GetCurrentProgramScene")
        rec = await c.request("GetRecordStatus")
        live = await c.request("GetStreamStatus")
    return {
        "version": ((ver.get("obsVersion") or "") + " / ws " +
                    (ver.get("obsWebSocketVersion") or "")).strip(" /"),
        "scene": scene.get("currentProgramSceneName") or scene.get("sceneName") or "（未知）",
        "recording": bool(rec.get("outputActive")),
        "streaming": bool(live.get("outputActive")),
    }


def _fmt_state(s):
    parts = ["OBS %s" % (s["version"] or "（版本未知）"),
             "当前场景：%s" % s["scene"],
             "录制：%s" % ("进行中" if s["recording"] else "没在录"),
             "直播：%s" % ("进行中" if s["streaming"] else "没在播")]
    return "；".join(parts)


@register("obs_ctl", "mumu", "连 OBS：查状态、切场景、录屏/直播开关（没有 OBS 就休眠）", "1.0.0")
class ObsCtl(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    # ---------- 工具 1：状态 ----------
    @filter.llm_tool(name="obs_state")
    async def obs_state(self, event=None):
        """查 OBS 当前状态：版本、当前场景、是否在录制、是否在直播。

        Returns:
            str: 一行中文状态；连不上时说明原因。
        """
        ok, why = _probe()
        if not ok:
            return why
        try:
            return _fmt_state(await _state())
        except Exception as e:
            _mark_fail("state")
            logger.warning("[obs_ctl] obs_state 失败: %r" % (e,))
            return "查状态失败：%s" % (e,)

    # ---------- 工具 2：场景列表 ----------
    @filter.llm_tool(name="obs_scenes")
    async def obs_scenes(self, event=None):
        """列出 OBS 里所有场景名，并标出当前用的是哪个。

        Returns:
            str: 场景清单（中文）。
        """
        ok, why = _probe()
        if not ok:
            return why
        try:
            async with ObsClient() as c:
                lst = await c.request("GetSceneList")
                cur = await c.request("GetCurrentProgramScene")
            names = [x.get("sceneName") for x in (lst.get("scenes") or []) if x.get("sceneName")]
            now = cur.get("currentProgramSceneName")
            if not names:
                return "OBS 里还没有场景。"
            return "共 %d 个场景：%s。当前：%s。" % (
                len(names), "、".join("「%s」" % n for n in names), now or "（未知）")
        except Exception as e:
            _mark_fail("scenes")
            return "列场景失败：%s" % (e,)

    # ---------- 工具 3：切场景 ----------
    @filter.llm_tool(name="obs_switch_scene")
    async def obs_switch_scene(self, event=None, name: str = ""):
        """切换到指定名字的 OBS 场景。名字要先从 obs_scenes 里拿到，不要自己编。

        Args:
            name (str): 场景名，必须和 OBS 里完全一致。

        Returns:
            str: 切换结果（中文）。
        """
        target = (name or "").strip()
        if not target:
            return "没给场景名。先用 obs_scenes 看有哪些场景，再按原样传名字。"
        blocked = _blocked("switch")
        if blocked:
            return blocked
        ok, why = _probe()
        if not ok:
            return why
        try:
            async with ObsClient() as c:
                lst = await c.request("GetSceneList")
                exist = [x.get("sceneName") for x in (lst.get("scenes") or [])]
                if target not in exist:
                    return ("没有叫「%s」的场景。现有：%s。" %
                            (target, "、".join("「%s」" % n for n in exist) or "（空）"))
                await c.request("SetCurrentProgramScene", {"sceneName": target})
            _mark_ok("switch")
            return "已切到场景「%s」。" % target
        except Exception as e:
            _mark_fail("switch")
            return "切场景失败：%s" % (e,)

    # ---------- 工具 4：录制 ----------
    @filter.llm_tool(name="obs_record")
    async def obs_record(self, event=None, action: str = "status"):
        """录制控制。action 只能是 start（开始）、stop（停止）、toggle（切换）或 status（只看状态）。

        Args:
            action (str): start / stop / toggle / status。

        Returns:
            str: 操作结果（中文）。
        """
        act = _check_action(action)
        if act is None:
            return "动作「%s」不认。只能用 start / stop / toggle / status。" % (action,)
        ok, why = _probe()
        if not ok:
            return why
        blocked = _blocked("record")
        if blocked:
            return blocked
        try:
            async with ObsClient() as c:
                if act == "status":
                    rec = await c.request("GetRecordStatus")
                    return ("正在录制：%s。已录 %.0f 秒。" %
                            ("是" if rec.get("outputActive") else "否",
                             float(rec.get("outputDuration") or 0) / 1000.0))
                req = {"start": "StartRecord", "stop": "StopRecord", "toggle": "ToggleRecord"}[act]
                await c.request(req)
                rec = await c.request("GetRecordStatus")
            _mark_ok("record")
            return "录制 %s 完成；现在%s在录。" % (act, "" if rec.get("outputActive") else "没")
        except Exception as e:
            _mark_fail("record")
            return "录制操作失败：%s" % (e,)

    # ---------- 工具 5：直播 ----------
    @filter.llm_tool(name="obs_stream")
    async def obs_stream(self, event=None, action: str = "status"):
        """直播控制。action 只能是 start / stop / toggle / status。开播默认被禁用，需主人在配置里放开。

        Args:
            action (str): start / stop / toggle / status。

        Returns:
            str: 操作结果（中文）。
        """
        act = _check_action(action)
        if act is None:
            return "动作「%s」不认。只能用 start / stop / toggle / status。" % (action,)
        if act == "start" and not ALLOW_STREAM_START:
            return ("开播被拒绝：插件配置里 allow_stream_start=false。"
                    "直播不是能随便启的动作，要开得主人自己动手，或者改配置。")
        ok, why = _probe()
        if not ok:
            return why
        blocked = _blocked("stream")
        if blocked:
            return blocked
        try:
            async with ObsClient() as c:
                if act == "status":
                    live = await c.request("GetStreamStatus")
                    return ("正在直播：%s。已播 %.0f 秒。" %
                            ("是" if live.get("outputActive") else "否",
                             float(live.get("outputDuration") or 0) / 1000.0))
                req = {"start": "StartStream", "stop": "StopStream", "toggle": "ToggleStream"}[act]
                await c.request(req)
                live = await c.request("GetStreamStatus")
            _mark_ok("stream")
            return "直播 %s 完成；现在%s在播。" % (act, "" if live.get("outputActive") else "没")
        except Exception as e:
            _mark_fail("stream")
            return "直播操作失败：%s" % (e,)
