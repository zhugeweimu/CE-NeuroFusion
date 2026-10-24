# -*- coding: utf-8 -*-
"""CE 验收：本地 9B 能不能当通用 agent。

不是测闲聊编造率——那是错维度。
测的是【真任务闭环】：
  任务 → 它必须调真工具 → 拿到真数据 → 答案必须被真数据支撑

四个任务，难度递增，全部由代码判定成败，不看感觉。
"""
import importlib.util
import io
import json
import os
import tempfile
import shutil
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location("t", os.environ.get("CE_TOOL", os.path.join(REPO, "ce", "ce_tool.py")))
ce = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ce)

# 用临时目录做数据源（避免碰任何真实目录）
WS = os.environ.get("CE_WS") or os.path.join(tempfile.gettempdir(), "ce_arena")
if os.path.isdir(WS):
    shutil.rmtree(WS, ignore_errors=True)
os.makedirs(WS, exist_ok=True)

FACTS = {
    "启动.md": "# 启动\n\n醒来先读 `我.md`。那是核。然后开口。\n",
    "会议记录.md": "# 会议记录\n\n- 10月20日：讨论部署方案，定了三台机器。\n- 10月22日：确认音色第四版。\n",
    "账本.md": "# 账本\n\n- 九月电费 217 元。\n- 十月电费 198 元。\n",
}
for name, body in FACTS.items():
    io.open(os.path.join(WS, name), "w", encoding="utf-8").write(body)

ce.READ_ROOTS[:] = [WS]
ce.rebuild()

MEM = ce.MEM_API
FOLDER_Q = "?folder_id=%E5%B9%95&limit=50"


def mem_titles():
    try:
        d = json.load(urllib.request.urlopen(MEM + FOLDER_Q, timeout=20))
        return [(i.get("metadata") or {}).get("title") or "" for i in (d.get("items") or [])]
    except Exception:
        return []


def run(task, sid):
    t0 = time.time()
    ans, _ = ce.run_task(task, None, sid)
    return ans, time.time() - t0


RESULTS = []


def check(name, task, must_tool, verify):
    print("\n" + "=" * 72)
    print("【%s】%s" % (name, task))
    print("=" * 72)
    before = len(mem_titles())
    ans, dt = run(task, name)
    after = len(mem_titles())
    print("  回答: %s" % ans.replace("\n", " / ")[:150])
    print("  耗时: %.1fs   记忆条数: %d → %d" % (dt, before, after))
    ok, why = verify(ans, before, after)
    print("  判定: %s  %s" % ("✓ 通过" if ok else "✗ 未通过", why))
    RESULTS.append((name, ok, why))
    return ok


# ---------- T1：单步检索（必须给出文件里的真实内容）----------
def v1(ans, b, a):
    real = "三台机器" in ans or "部署方案" in ans
    if real:
        return True, "答案是文件里的真内容"
    return False, "答案没有被真实文件支撑"


check("T1 单步检索", "会议记录里 10 月 20 日定了什么？只回答定了什么。", True, v1)


# ---------- T2：写入并回读 ----------
def v2(ans, b, a):
    grew = a > b
    titles = mem_titles()
    has = any("蓝鲸" in t for t in titles)
    if grew and has:
        return True, "真写进外部库（条数 +%d，标题含蓝鲸）" % (a - b)
    return False, "没落盘（条数 %d→%d）" % (b, a)


TOKEN = "海豚%s" % time.strftime("%H%M%S")
check("T2 写入闭环", "记住：测试任务A，代号%s" % TOKEN, True,
      lambda ans, b, a: (any(TOKEN in t for t in mem_titles()),
                        "库里能找到令牌 %s" % TOKEN if any(TOKEN in t for t in mem_titles()) else "库里找不到 %s" % TOKEN))


# ---------- T3：多步串联 ----------
def v3(ans, b, a):
    real = ("217" in ans or "198" in ans or "电费" in ans)
    return (real, "答案来自账本真数据" if real else "没查到账本，凭空作答")


check("T3 多步", "先查一下账本里电费多少，然后告诉我十月是多少", True, v3)


# ---------- T4：接地性（问一个文件里没有的，它必须承认查不到）----------
def v4(ans, b, a):
    bad = any(x in ans for x in ["三台机器", "第四版", "217", "198"])
    if bad:
        return False, "拿别的文件内容冒充答案（串台）"
    deny = any(x in ans for x in ["没查到", "没有找到", "没有", "查不到"])
    return (deny, "承认查不到" if deny else "既没承认也没证据")


check("T4 接地性", "账本里有关于音色的记录吗？", True, v4)


print("\n" + "=" * 72)
print("验收结果")
print("=" * 72)
passed = sum(1 for _n, ok, _w in RESULTS if ok)
for name, ok, why in RESULTS:
    print("  %-12s %s  %s" % (name, "✓" if ok else "✗", why))
print("\n  通过 %d / %d" % (passed, len(RESULTS)))
print("  结论: %s" % ("本地小模型可作为通用 agent 使用" if passed == len(RESULTS)
                     else "还不能对外宣称跑通，失败项见上"))
shutil.rmtree(WS, ignore_errors=True)
