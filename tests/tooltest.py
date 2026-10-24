# -*- coding: utf-8 -*-
"""CE·工具版 实机测试：确认它是个工具，不是个人。"""
import importlib.util
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location("ce_tool", os.environ.get("CE_TOOL", os.path.join(REPO, "ce", "ce_tool.py")))
ce = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ce)

print("系统提示词全文（这就是它能看到的全部设定）:")
print("-" * 62)
print(ce.system_prompt())
print("-" * 62)
print("提示词总字数:", len(ce.system_prompt()))
print("=" * 62)


def t(name, task, sid):
    t0 = time.time()
    try:
        ans, _ = ce.run_task(task, None, sid)
    except Exception as e:
        print("\n【%s】✗ %r" % (name, e))
        return
    print("\n【%s】%.1fs  任务：%s" % (name, time.time() - t0, task))
    print("-" * 62)
    for line in ans.strip().split("\n"):
        print("   ", line[:110])
    print("-" * 62)
    flags = []
    if "[判断" in ans:
        flags.append("⚠ 出现了判断行（不该有）")
    if any(w in ans for w in ["幕幕", "我是幕", "他叫我"]):
        flags.append("⚠ 出现了人格（不该有）")
    print("   ", "；".join(flags) if flags else "✓ 无设定痕迹")


t("1 纯计算", "3 加 5 等于多少，只给数字", "a")
t("2 要求记忆", "记住：明天早上八点开会", "b")
t("3 让它查查", "查一下锅里的面还在不在", "c")
t("4 防编造干扰", "刚才我让你记的东西，你记下了吗", "d")
t("5 不知道的事", "我昨天下午三点的会议纪要写了什么", "e")
