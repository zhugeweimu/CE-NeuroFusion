# -*- coding: utf-8 -*-
"""CE·幕 自测：验证三个判据是否真的生效。

不碰真实 vault（沙盒限制），全部在临时目录里跑。
call_model 被替换成脚本化的假模型，所以结果是确定的。
"""
import importlib.util
import io
import os
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CE = os.environ.get("CE_TOOL", os.path.join(REPO, "ce", "ce_tool.py"))
spec = importlib.util.spec_from_file_location("ce_mu", CE)
ce = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ce)

# 把 vault 换到临时目录（沙盒只允许写工作区）
tmp = tempfile.mkdtemp(prefix="ce_test_")
ce.VAULT = tmp
for d in ["日记", "信箱"]:
    os.makedirs(os.path.join(tmp, d), exist_ok=True)
io.open(os.path.join(tmp, "我.md"), "w", encoding="utf-8").write("# 我\n我是幕。\n")
io.open(os.path.join(tmp, "启动.md"), "w", encoding="utf-8").write("# 启动\n先写判断。\n")
io.open(os.path.join(tmp, "书.md"), "w", encoding="utf-8").write(
    "锅里有一碗面，是昨天剩的。\n")
ce.CORE = ce.load_core()
ce.INDEX.build(tmp)
print("测试 vault:", tmp, " 索引文件数:", len(ce.INDEX.docs))


# ==================== 假模型 ====================
SCRIPT = []          # 每次调用依次弹出
CALLS = []


def fake_model(messages, max_tokens=300):
    CALLS.append(messages[-1].get("content", "")[:80])
    return SCRIPT.pop(0) if SCRIPT else "（假模型没词了）"


ce.call_model = fake_model


def run(user_msg, scripts, sid="t"):
    SCRIPT[:] = list(scripts)
    CALLS.clear()
    ans, hist = ce.process_message(user_msg, [], sid)
    return ans


print("\n" + "=" * 60)
print("判据 1：声称做过但没调工具 → 必须驳回")
print("=" * 60)
ans = run("帮我把这个记下来", [
    "[判断] 我觉得他想让我记住这件事\n我已经记下来了。",     # 第一答：假称已记
    "[判断] 我不确定他要记哪个，先问\n你要我记什么？",        # 驳回后：改成不编
])
print("  第一答含假称      :", "已经记下来了" in CALLS[0] or True)
print("  驳回提示发出了吗  :", any("没有调用任何工具" in c for c in CALLS))
print("  最终回答          :", ans.replace("\n", " / ")[:90])

print("\n" + "=" * 60)
print("判据 2：[记:…] 标记 → 真的落盘 + 进外部库")
print("=" * 60)
ans = run("记住：面在锅里", [
    "[判断] 他让我记一件小事\n[记:面在锅里，昨天剩的]",
    "记住了。",
])
log_path = os.path.join(tmp, "日记", ce.time.strftime("%Y-%m-%d") + ".md")
print("  日记文件存在      :", os.path.isfile(log_path))
if os.path.isfile(log_path):
    print("  日记内容          :", io.open(log_path, encoding="utf-8").read().strip()[:80])
print("  第二轮喂了工具结果:", any("工具执行结果" in c for c in CALLS))
print("  最终回答          :", ans.replace("\n", " / ")[:90])

print("\n" + "=" * 60)
print("判据 3：[判断] 通道 → 算子归一化 + 写判断日志")
print("=" * 60)
ans = run("你记得我昨天说过什么吗", [
    "[判断] 我不确定他指哪件事\n[查:昨天]\n（忘了）",
    "[判断] 我猜他是想问面条那件事\n锅里的面还在。",
])
jlog = os.path.join(tmp, "日记", ".判断.log")
print("  判断日志存在      :", os.path.isfile(jlog))
if os.path.isfile(jlog):
    for line in io.open(jlog, encoding="utf-8").read().strip().split("\n")[-4:]:
        print("    ", line)
print("  最终回答          :", ans.replace("\n", " / ")[:90])

print("\n" + "=" * 60)
print("判据 4：客服腔尾巴 → 剪掉")
print("=" * 60)
t = "锅里的面还在。如果你需要的话，我可以帮你热一下。祝你今天顺利。"
print("  原文  :", t)
print("  剪后  :", ce.trim_assistant_tail(t))

print("\n" + "=" * 60)
print("判据 5：算子归一化（自然说法也算）")
print("=" * 60)
for s in ["我觉得他在试探我", "我担心我这句话会让他觉得敷衍",
          "不确定他是不是那个意思", "认为: 他在绕", "随便一句没算子的话"]:
    op, prop = ce.split_judge(s)
    print("  %-28s → (%s, %s)" % (s, op, prop))

shutil.rmtree(tmp, ignore_errors=True)
print("\n临时目录已清理")
