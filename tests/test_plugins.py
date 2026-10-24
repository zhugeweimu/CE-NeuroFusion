# -*- coding: utf-8 -*-
"""测两个新插件的核心逻辑（不起 AstrBot，直接调函数）

用法（必须用 AstrBot 的 python，因为插件 import astrbot.api）：
    C:\\Users\\WEI\\AppData\\Roaming\\uv\\tools\\astrbot\\Scripts\\python.exe tests\\test_plugins.py
"""
import asyncio
import importlib.util
import io
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

PLUG = os.environ.get("ASTRBOT_PLUGINS", "")
OK = []
BAD = []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print("  %s %-38s %s" % ("✓" if cond else "✗", name, str(detail)[:70]))
    return cond


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def obj_of(cls):
    """不跑 __init__，拿一个能调方法的空壳。"""
    return cls.__new__(cls)


async def main():
    print("=" * 64)
    print("mindbeat / self_edit 逻辑测试")
    print("=" * 64)

    # ---------- mindbeat ----------
    print("\n[1] mindbeat")
    mb = load("mb", os.path.join(PLUG, "astrbot_plugin_mindbeat", "main.py"))
    inst = obj_of(mb.MindBeat)
    inst.state = mb._load_state()

    # 闸门：深夜
    sig = {"hour": 3, "idle_min": 300, "today_count": 0,
           "since_last_proactive_min": -1, "recent_topics": [], "dir_changed": False}
    ok, why = inst._gates(sig)
    check("闸门-深夜静默会拦", ok is False and "静默" in why, why)

    # 闸门：他在（刚说过话）
    sig = {"hour": 14, "idle_min": 5, "today_count": 0,
           "since_last_proactive_min": -1, "recent_topics": [], "dir_changed": False}
    ok, why = inst._gates(sig)
    check("闸门-他刚说话会拦", ok is False, why)

    # 闸门：每日上限
    sig = {"hour": 14, "idle_min": 300, "today_count": mb.MAX_PER_DAY,
           "since_last_proactive_min": 999, "recent_topics": [], "dir_changed": False}
    ok, why = inst._gates(sig)
    check("闸门-每日上限会拦", ok is False, why)

    # 闸门：冷却
    sig = {"hour": 14, "idle_min": 300, "today_count": 1,
           "since_last_proactive_min": 10, "recent_topics": [], "dir_changed": False}
    ok, why = inst._gates(sig)
    check("闸门-冷却中会拦", ok is False, why)

    # 闸门：全过
    sig = {"hour": 14, "idle_min": 300, "today_count": 1,
           "since_last_proactive_min": 200, "recent_topics": [], "dir_changed": True}
    ok, why = inst._gates(sig)
    check("闸门-条件齐全会放行", ok is True, why or "放行")

    # 本地判断（真调 :8080）
    sig = inst._signals()
    sig["hour"] = 14          # 强制白天，避免深夜静默干扰观察
    speak, why, topic = await inst._decide(sig)
    check("本地判断返回三元组", isinstance(speak, bool) and isinstance(why, str),
          "speak=%s why=%s" % (speak, (why or "")[:30]))

    # 非法输入 → 沉默（把地址指到不存在的端口）
    old = mb.LOCAL_LLM
    mb.LOCAL_LLM = "http://127.0.0.1:9/v1/chat/completions"
    s2, _w, _t = await inst._decide(sig)
    check("本地不可用→沉默不抛异常", s2 is False, "speak=%s" % s2)
    mb.LOCAL_LLM = old

    # ---------- self_edit ----------
    print("\n[2] self_edit")
    se = load("se", os.path.join(PLUG, "astrbot_plugin_self_edit", "main.py"))

    # 夹具自己造：不依赖外部库里"碰巧存在"的那条记录
    # （原来写成引用真实记录，记录一轮对话后就被挤掉了，测试会假失败）
    _fake_mem = ["今天主人熬了一整夜，太阳出来才准备睡", "她在凌晨把外部库的镜像重建了一遍"]
    se._fetch_recent_memories = lambda n=50: _fake_mem[:n]

    ok, hit = se._has_basis(_fake_mem[0][:12])   # 取夹具原句片段，保证连续重合 >= MIN_OVERLAP
    check("出处校验-真事能过", ok is True, "命中 %d 字" % hit)

    ok, hit = se._has_basis("我觉得自己是个很温柔的人")
    check("出处校验-无据会拒", ok is False, "命中 %d 字" % hit)

    check("字数常量", se.MAX_TOTAL == 800 and se.MAX_LINE == 120,
          "总%d/条%d" % (se.MAX_TOTAL, se.MAX_LINE))

    # 编号显示
    demo = "- 一条\n- 二条\n"
    txt = se._numbered(demo)
    check("条目编号", "1. 一条" in txt and "2. 二条" in txt, txt.replace("\n", " / "))

    # 写入 → 快照 → 划掉（在沙盒目录里真跑一遍，不碰真身）
    import tempfile
    tmp = tempfile.mkdtemp(prefix="selftest_")
    se.SELF_DIR = tmp
    se.SELF_PATH = os.path.join(tmp, "自我.md")
    se.HIST_DIR = os.path.join(tmp, "历史")
    se.FORGET_DIR = os.path.join(tmp, "遗忘")
    se.STATE_PATH = os.path.join(tmp, "state.json")

    se._write_self("- 测试行一\n")
    check("写入", os.path.isfile(se.SELF_PATH), se.SELF_PATH)
    snap = se._snapshot()
    check("留档", bool(snap) and os.path.isfile(snap), os.path.basename(snap or ""))

    inst2 = obj_of(se.SelfEdit)
    inst2._writes_disabled = True

    print("\n" + "=" * 64)
    print("通过 %d / 失败 %d" % (len(OK), len(BAD)))
    if BAD:
        print("失败项: " + ", ".join(BAD))
    print("=" * 64)
    return 0 if not BAD else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
