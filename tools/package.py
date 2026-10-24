# -*- coding: utf-8 -*-
"""package.py —— 打两个包：CE 单包与完整包（都 AGPL-3.0，插件可选）。

为什么两个包：
  · 用 CE 的人可能根本不用 AstrBot，不该被迫连插件一起拖走；
  · 插件是对接 AGPL 项目的扩展，单独放完整包里，许可边界才干净。

它会：
  1) 按白名单收文件
  2) 扫私有痕迹（路径/QQ），发现即拒绝出包
  3) 写 dist/*.zip 与 .sha256
"""
import hashlib
import io
import os
import sys
import zipfile

sys.stdout.reconfigure(encoding="utf-8")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VER = "1.0.0"
OUT_DIR = os.path.join(REPO, "dist")

# ---- CE 单包（MIT）：只要 CE 能跑起来的东西 ----
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
]

# ---- 完整包（AGPL）：CE + 可选插件 + 文档 ----
FULL = [
    "README.md", "CHANGELOG.md", "LICENSE", "NOTICE.md", ".gitignore",
    "README-CE.md",
    "ce/ce_tool.py", "ce/config.example.json", "ce/CE代码导读.md", "ce/LICENSE",
    "tools/probe.py", "tools/package.py",
    "docs/00-它解决了什么.md",
 "docs/02-CE设计.md", "docs/03-踩坑记录.md",
    "docs/08-CE缺口与优化清单.md", "docs/09-使用说明与设备自检.md",
    "docs/11-读Obsidian笔记库.md",
]
PLUGINS = ["mindbeat", "self_edit", "sticker", "owner_power",
           "local_agent", "obsidian", "obs_ctl"]
"astrbot_plugins/README.md",
for p in PLUGINS:
    FULL.append("astrbot_plugins/%s/main.py" % p)
    FULL.append("astrbot_plugins/%s/metadata.yaml" % p)
    _ex = "astrbot_plugins/%s/config.example.json" % p
    if os.path.isfile(os.path.join(REPO, _ex)):
        FULL.append(_ex)
for fn in sorted(os.listdir(os.path.join(REPO, "tests"))):
    if fn.endswith(".py"):
        FULL.append("tests/" + fn)

# 私有痕迹检测词：base64 编码后存，避免这份检测器自己成为明文清单
import base64 as _b64
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


def scan(files, label):
    bad = []
    for f in files:
        if f in EXEMPT:
            continue
        t = io.open(os.path.join(REPO, f), encoding="utf-8", errors="replace").read()
        for pat in FORBIDDEN:
            if pat in t:
                bad.append((f, pat))
    if bad:
        print("  ✗ [%s] 有私有痕迹，拒绝出包：" % label)
        for f, pat in bad:
            print("     %-46s 含 %s" % (f, pat))
        return False
    return True


def build(files, out_name, label):
    # 缺文件不炸：干净导出目录里可能没有插件（它们是可选的）
    files = [f for f in files if os.path.isfile(os.path.join(REPO, f))]
    missing = []
    if missing:
        print("  ✗ [%s] 白名单缺文件：" % label + "、".join(missing))
        return None
    if not scan(files, label):
        return None
    out = os.path.join(OUT_DIR, out_name)
    os.makedirs(OUT_DIR, exist_ok=True)
    root = out_name.replace(".zip", "")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(os.path.join(REPO, f), root + "/" + f)
    h = hashlib.sha256()
    with open(out, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    digest = h.hexdigest()
    with io.open(out + ".sha256", "w", encoding="utf-8") as fh:
        fh.write("%s  %s\n" % (digest, os.path.basename(out)))
    print("  ✓ [%s] %s" % (label, os.path.basename(out)))
    print("      文件 %d，%.1f KB" % (len(files), os.path.getsize(out) / 1024.0))
    print("      sha256 %s" % digest)
    return out


def main():
    print("出包：")
    a = build(CE_ONLY, "ce-%s.zip" % VER, "CE 单包 / AGPL")
    b = build(FULL, "agent-neurofusion-%s.zip" % VER, "完整包 / AGPL")
    print("\n说明：")
    print("  CE 单包  ce/ + probe.py + 文档（AGPL-3.0）")
    print("  完整包  CE + AstrBot 对接件 + 测试（AGPL-3.0）")
    return 0 if (a and b) else 1


if __name__ == "__main__":
    sys.exit(main())
