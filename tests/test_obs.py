# -*- coding: utf-8 -*-
"""OBS 插件的确定性验收：用一个假的 obs-websocket v5 服务端跑真实协议。

为什么要假服务端：本机没装 OBS。不测真实协议就等于没测——
Hello/Challenge/Identify/RequestResponse 这套流程错一个字节就连不上。

覆盖：
  ① 认证握手（带密码的 challenge-response）算得对不对
  ② 状态查询 / 列场景 / 切场景 / 录制开关
  ③ 不存在的场景名 → 可读拒绝，不抛异常
  ④ 开播默认被拦（allow_stream_start=false）
  ⑤ 动作白名单（乱传动作名）
  ⑥ 熔断（同动作连续失败）
  ⑦ OBS 没开时的可读提示（端口不通）
"""
import asyncio
import base64
import hashlib
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "astrbot_plugins"))

PORT = 4456
PASSWORD = "test-pass"
SALT = "salt1234"
CHALLENGE = "challenge5678"

ok_n = 0
bad_n = []


def check(name, cond, detail=""):
    global ok_n
    if cond:
        ok_n += 1
        print("  ✓ %-34s %s" % (name, detail))
    else:
        bad_n.append(name)
        print("  ✗ %-34s %s" % (name, detail))


def expect_auth():
    secret = base64.b64encode(hashlib.sha256((PASSWORD + SALT).encode()).digest())
    return base64.b64encode(hashlib.sha256(secret + CHALLENGE.encode()).digest()).decode()


class FakeObs:
    """只实现测试需要的那几个请求。"""

    def __init__(self):
        self.scene = "主场景"
        self.scenes = ["主场景", "游戏"]
        self.recording = False
        self.streaming = False
        self.authed = False

    async def handler(self, ws):
        await ws.send(json.dumps({"op": 0, "d": {
            "obsWebSocketVersion": "5.5.2", "rpcVersion": 1,
            "authentication": {"challenge": CHALLENGE, "salt": SALT}}}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("op") == 1:
                got = (msg.get("d") or {}).get("authentication")
                if got != expect_auth():
                    await ws.send(json.dumps({"op": 9, "d": {"error": "Authentication failed"}}))
                    return
                self.authed = True
                await ws.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
                continue
            if msg.get("op") != 6:
                continue
            d = msg.get("d") or {}
            rid = d.get("requestId")
            rt = d.get("requestType")
            data = d.get("requestData") or {}
            resp, ok = {}, True
            if rt == "GetVersion":
                resp = {"obsVersion": "30.1.2", "obsWebSocketVersion": "5.5.2"}
            elif rt == "GetSceneList":
                resp = {"scenes": [{"sceneName": n} for n in self.scenes]}
            elif rt == "GetCurrentProgramScene":
                resp = {"currentProgramSceneName": self.scene}
            elif rt == "SetCurrentProgramScene":
                nm = data.get("sceneName")
                if nm in self.scenes:
                    self.scene = nm
                else:
                    ok = False
            elif rt == "GetRecordStatus":
                resp = {"outputActive": self.recording,
                        "outputDuration": 1500 if self.recording else 0}
            elif rt in ("StartRecord", "StopRecord", "ToggleRecord"):
                self.recording = (True if rt == "StartRecord"
                                  else False if rt == "StopRecord"
                                  else not self.recording)
            elif rt == "GetStreamStatus":
                resp = {"outputActive": self.streaming,
                        "outputDuration": 3000 if self.streaming else 0}
            elif rt in ("StartStream", "StopStream", "ToggleStream"):
                self.streaming = (True if rt == "StartStream"
                                  else False if rt == "StopStream"
                                  else not self.streaming)
            else:
                ok = False
            await ws.send(json.dumps({"op": 7, "d": {
                "requestType": rt, "requestId": rid,
                "requestStatus": {"result": ok, "code": 100 if ok else 600},
                "responseData": resp}}))


async def main():
    import websockets
    fake = FakeObs()
    server = await websockets.serve(fake.handler, "127.0.0.1", PORT)
    try:
        import obs_ctl.main as obs
        obs.OBS_HOST, obs.OBS_PORT = "127.0.0.1", PORT
        obs.OBS_PASSWORD = PASSWORD
        obs.REQ_TIMEOUT = 5.0

        print("\n[1] 协议：认证 + 状态")
        st = await obs._state()
        check("握手认证通过", fake.authed is True)
        check("拿到了版本", "30.1.2" in st["version"], st["version"])
        check("拿到了当前场景", st["scene"] == "主场景", st["scene"])
        check("录制/直播初始为否", st["recording"] is False and st["streaming"] is False)
        txt = obs._fmt_state(st)
        check("状态文案可读", "当前场景：主场景" in txt, txt[:40])

        print("\n[2] 工具层：列场景 / 切场景 / 录制")
        tool = obs.ObsCtl.__new__(obs.ObsCtl)
        r = await obs.ObsCtl.obs_scenes(tool, None)
        check("列场景", "主场景" in r and "游戏" in r, r[:50])
        r = await obs.ObsCtl.obs_switch_scene(tool, None, "游戏")
        check("切场景成功", "已切到场景「游戏」" in r, r[:40])
        r = await obs.ObsCtl.obs_switch_scene(tool, None, "不存在的场景")
        check("不存在的场景可读拒绝", "没有叫" in r and "现有" in r, r[:50])
        r = await obs.ObsCtl.obs_record(tool, None, "start")
        check("开始录制", "录制 start 完成" in r, r[:40])
        r = await obs.ObsCtl.obs_record(tool, None, "status")
        check("录制状态=是", "正在录制：是" in r, r[:40])
        r = await obs.ObsCtl.obs_record(tool, None, "stop")
        check("停止录制", "录制 stop 完成" in r, r[:40])

        print("\n[3] 安全闸")
        r = await obs.ObsCtl.obs_stream(tool, None, "start")
        check("开播被拦", "开播被拒绝" in r, r[:40])
        r = await obs.ObsCtl.obs_record(tool, None, "删掉所有录像")
        check("乱传动作被拒", "不认" in r, r[:40])
        obs._fail_log.clear()
        for i in range(obs.FAIL_LIMIT):
            obs._mark_fail("record")
        r = await obs.ObsCtl.obs_record(tool, None, "start")
        check("熔断生效", "先停手" in r, r[:40])
        obs._fail_log.clear()
    finally:
        # Windows 上 ProactorEventLoop 关服务器时会抛一个无害断言，压掉它
        try:
            server.close()
            await server.wait_closed()
        except AssertionError:
            pass

    print("\n[4] OBS 没开时")
    import obs_ctl.main as obs
    obs.OBS_PORT = 45999                      # 没人听的端口
    tool = obs.ObsCtl.__new__(obs.ObsCtl)
    r = await obs.ObsCtl.obs_state(tool, None)
    check("端口不通→可读提示", "连不上 OBS" in r and "可能原因" in r, r[:60])

    print("\n" + "=" * 56)
    print("通过 %d / 失败 %d" % (ok_n, len(bad_n)))
    if bad_n:
        print("失败项: " + "、".join(bad_n))
    return 1 if bad_n else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
