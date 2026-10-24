# -*- coding: utf-8 -*-
"""package.py —— 打 CE 单包（AGPL-3.0）。

按白名单收文件、扫私有痕迹、算 sha256，输出到 dist/。
"""
import base64 as _b64
import hashlib
import os
import sys
import zipfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(REPO, "dist")
# 版本规则（写死的，别凭手感改）：
#   X.Y.Z
#   Z（最后一位）+1  = 只修 bug、只补文档、不动行为
#   Y（第二位）+1    = 有行为变化 / 新增功能 / 配置项变更（Z 归零）
#   X                 = 不兼容的大改（目前不动）
# 发版前必须：bug 已修并验证过，不许边改边发。
VER = "1.4.9"
ROOT = "ce-%s" % VER

# 私有痕迹检测词：base64 编码后存，避免这份检测器自己成为明文清单
_ENC = [
    "RDpccXFhaWJvdA==",
    "RDpc5bmVLeiusOW/huW6kw==",
    "RDpc54G15p6iLeiusOW/huW6kw==",
    "QzpcVXNlcnNcV0VJ",
    "MzY2NTYxMzE0Mg==",
    "MTY4NDE4OTA2Ng==",
]
FORBIDDEN = [_b64.b64decode(x).decode("utf-8") for x in _ENC]

# 检测器自己必然包含这些词（它就是靠它们判定的），不参与自检
EXEMPT = {"tools/package.py"}

CE_ONLY = [
    "README-CE.md",
    "ce/ce_tool.py",
    "ce/config.example.json",
    "ce/CE代码导读.md",
    "ce/LICENSE",
    "tools/probe.py",
    "docs/00-它解决了什么.md",
    "docs/03-踩坑记录.md",
    "docs/09-使用说明与设备自检.md",
    "docs/11-读Obsidian笔记库.md",
    "docs/12-怎么下指令.md",
    "docs/13-给AI助手的配置指南.md",
    "docs/14-一次任务怎么走.md",
    "docs/15-自动迭代.md",
    "docs/16-插件接口.md",
]


def scan(files, label):
    bad = []
    for f in files:
        if f in EXEMPT:
            continue
        p = os.path.join(REPO, f)
        if not os.path.isfile(p):
            continue
        try:
            c = open(p, "r", encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        for k in FORBIDDEN:
            if k in c:
                bad.append((f, k))
    if bad:
        print("  ✗ %s 含私有痕迹：" % label)
        for f, k in bad:
            print("      %s ← %s" % (f, k))
        return False
    return True


def build(files, out_name, label):
    files = [f for f in files if os.path.isfile(os.path.join(REPO, f))]
    if not files:
        print("  ✗ %s 没有文件" % label)
        return None
    if not scan(files, label):
        return None
    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, out_name)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(os.path.join(REPO, f), ROOT + "/" + f.replace("\\", "/"))
    h = hashlib.sha256(open(out, "rb").read()).hexdigest()
    print("  ✓ [%s] %s" % (label, out_name))
    print("      文件 %d，%.1f KB" % (len(files), os.path.getsize(out) / 1024))
    print("      sha256 %s" % h)
    open(out + ".sha256", "w", encoding="utf-8").write("%s  %s\n" % (h, out_name))
    return out


def main():
    print("出包：")
    build(CE_ONLY, "ce-%s.zip" % VER, "CE 单包")
    print("\n说明：")
    print("  ce/ + probe.py + 文档（AGPL-3.0）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
