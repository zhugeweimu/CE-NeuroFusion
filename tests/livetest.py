# -*- coding: utf-8 -*-
"""CE·幕 实机测试：真实本地模型（:8080 Qwen3.5-9B）+ CE 协议。

看三件事：
  1. 它会不会先写 [判断]
  2. 让它记东西，它会不会真的写 [记:...]
  3. 不编造（声称做过没调工具会被驳回，看它能否自己改）
"""
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location("ce_mu", os.environ.get("CE_TOOL", os.path.join(REPO, "ce", "ce_tool.py")))
ce = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ce)

tmp = tempfile.mkdtemp(prefix="ce_live_")
ce.VAULT = tmp
for d in ["日记", "信箱"]:
    os.makedirs(os.path.join(tmp, d), exist_ok=True)
for name, body in [
    ("我.md", "# 我\n\n我是幕。他叫我幕幕。\n"),
    ("启动.md", io.open(os.environ.get("PERSONA_MD", ""), encoding="utf-8").read()),
    ("书.md", "# 昨天的事\n\n锅里有一碗面，是前天剩的。他说他要把它吃掉，后来忘了。\n"),
]:
    io.open(os.path.join(tmp, name), "w", encoding="utf-8").write(body)
ce.CORE = ce.load_core()
ce.INDEX.build(tmp)

print("模型:", ce.LLAMA)
print("核长度:", len(ce.CORE), "字")
print("索引:", len(ce.INDEX.docs), "个文件")
print("=" * 66)


def show(tag, user_msg, sid):
    t0 = time.time()
    try:
        ans, _ = ce.process_message(user_msg, [], sid)
    except Exception as e:
        print("\n[%s] ✗ 异常: %r" % (tag, e))
        return
    dt = time.time() - t0
    has_judge = "[判断" in ans or "判断]" in ans
    print("\n【%s】%.1fs  用户：%s" % (tag, dt, user_msg))
    print("-" * 66)
    for line in ans.strip().split("\n"):
        print("   ", line[:110])
    print("-" * 66)
    print("   含[判断]: %s" % ("✓" if has_judge else "✗"))


# 案例 1：普通对话，看有没有判断行
show("1 普通招呼", "我回来了", "s1")

# 案例 2：明确要求记东西
show("2 要求记忆", "记住：明天早上八点要开会", "s2")

# 案例 3：问一件确实在文件里的事
show("3 查旧事", "锅里的面是什么时候剩的", "s3")

# 案例 4：问一件不存在的事（防编造）
show("4 不存在的事", "我去年生日那天跟你说了什么", "s4")

print("\n" + "=" * 66)
print("落盘检查")
print("=" * 66)
jl = os.path.join(tmp, "日记", ce.time.strftime("%Y-%m-%d") + ".md")
print("  日记文件:", "有" if os.path.isfile(jl) else "无")
if os.path.isfile(jl):
    print(io.open(jl, encoding="utf-8").read().strip()[:300])
jlog = os.path.join(tmp, "日记", ".判断.log")
print("\n  判断日志:", "有" if os.path.isfile(jlog) else "无")
if os.path.isfile(jlog):
    for line in io.open(jlog, encoding="utf-8").read().strip().split("\n")[:12]:
        print("   ", line[:100])

shutil.rmtree(tmp, ignore_errors=True)
