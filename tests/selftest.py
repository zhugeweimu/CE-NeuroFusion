# -*- coding: utf-8 -*-
"""CE 自测：不碰真实数据、不调真实模型，用桩验证核心机制。

覆盖：
  1. 提示词两档：lean 不含工具手册
  2. 提示里的时间只到「日」——带分钟会让 prompt 缓存每次失效
  3. 历史按字数裁剪
  4. 条目数抽取与计数
  5. 未接记忆系统时 [记:] 给可操作提示
  6. 空目录检索如实回答
"""
import importlib.util
import os
import re
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CE = os.environ.get("CE_TOOL") or os.path.join(REPO, "ce", "ce_tool.py")

spec = importlib.util.spec_from_file_location("ce_tool", CE)
ce = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ce)

tmp = tempfile.mkdtemp(prefix="ce_selftest_")
ce.CHAIN_PATH = os.path.join(tmp, "chain.json")
ce.CKPT_DIR = os.path.join(tmp, "ck")
ce._runlog = lambda *a, **k: None
ce.READ_ROOTS = []

ok = 0
fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ✓ %s" % name)
    else:
        fail += 1
        print("  ✗ %s  %s" % (name, detail))


print("CE 自测（%s）" % CE)

# 1. 提示词两档
full, lean = ce.system_prompt(), ce.system_prompt(lean=True)
check("完整提示含工具标记", "[查:" in full)
check("lean 提示不含工具手册", "[查:" not in lean, lean[:60])
check("lean 明显更短", len(lean) < len(full) / 3, "%d vs %d" % (len(lean), len(full)))

# 2. 时间精度
check("提示里的时间不带分钟", not re.search(r"\d{2}:\d{2}", full), full[-24:])

# 3. 历史裁剪
hist = [{"role": "system", "content": "sys"}]
for _ in range(30):
    hist.append({"role": "user", "content": "x" * 1500})
    hist.append({"role": "assistant", "content": "y" * 1500})
before = sum(len(m["content"]) for m in hist)
ce.SOURCE_CHARS = 3000
trimmed = ce._trim_history(hist)
after = sum(len(m["content"]) for m in trimmed)
check("历史被裁到预算内", after <= ce.SOURCE_CHARS + 1600, "%d → %d" % (before, after))
check("裁剪保留 system", trimmed[0]["role"] == "system")
check("裁剪保留最新一条", trimmed[-1] is hist[-1])

# 4. 条目数
check("抽「5 条」", ce.expect_count("列出 5 条建议") == 5)
check("抽「三种」", ce.expect_count("给我三种方案") == 3)
check("抽「前 10」", ce.expect_count("取前 10 个") == 10)
check("无数量词不抽", ce.expect_count("整理一下这些文件") is None)
check("数列表条目", ce.count_items("- 甲\n- 乙\n- 丙") == 3)
check("数编号条目", ce.count_items("1. 甲\n2. 乙") == 2)
check("单句算 1 条", ce.count_items("就一句话") == 1)

# 5. 没接记忆系统
ce.MEM_WRITE = ""
msg = ce.do_tool("记", "测试")
check("[记:] 未接记忆库时给提示", "没接记忆系统" in msg, msg[:60])

# 6. 空目录检索
ce.rebuild()
r = ce.do_tool("查", "随便什么")
check("空目录检索如实回答", ("没资料" in r) or ("没查到" in r), r[:60])

print("\n结果：%d 通过 / %d 失败" % (ok, fail))
sys.exit(0 if fail == 0 else 1)
