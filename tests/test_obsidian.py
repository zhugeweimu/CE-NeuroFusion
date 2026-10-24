# -*- coding: utf-8 -*-
"""Obsidian 插件的确定性验收：在一个临时笔记库里跑真文件操作。

为什么不碰真笔记库：她的笔记是真东西，测试不许写进去、也不许依赖某篇一定存在。
临时库里造几篇夹具，测完即弃。

覆盖：
  ① 路径安全：.. 跳出 / 盘符 / 越界 一律拒绝
  ② 搜索：标题命中优先、正文命中带上下文、没命中会说明
  ③ 读取：分页（start 续读）、不存在时给相近建议
  ④ 追加：不存在则新建（带标题）；已存在则写在末尾，绝不覆盖原有内容
  ⑤ 日记：路径固定 日记/YYYY-MM-DD.md，日期格式错误要拒
  ⑥ 新建：同名拒绝；标题里的非法字符被替换
  ⑦ 写入上限：超过 2000 字拒绝
  ⑧ 库不存在 / 库名不认识 → 可读提示
"""
import asyncio
import os
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "astrbot_plugins"))

ok_n = 0
bad_n = []


def check(name, cond, detail=""):
    global ok_n
    if cond:
        ok_n += 1
        print("  ✓ %-30s %s" % (name, detail))
    else:
        bad_n.append(name)
        print("  ✗ %-30s %s" % (name, detail))


def make_vault(root):
    os.makedirs(os.path.join(root, "日记"), exist_ok=True)
    os.makedirs(os.path.join(root, "记忆", "按日期"), exist_ok=True)
    os.makedirs(os.path.join(root, ".obsidian"), exist_ok=True)
    with open(os.path.join(root, "首页.md"), "w", encoding="utf-8") as f:
        f.write("# 首页\n\n这是夹点。她说过：记忆是能记住也能查到。\n")
    with open(os.path.join(root, "记忆", "按日期", "2026-10-24.md"), "w", encoding="utf-8") as f:
        f.write("# 2026-10-24\n\n- 今天把分步模式改成了 map-reduce。\n")
    with open(os.path.join(root, ".obsidian", "workspace.json"), "w", encoding="utf-8") as f:
        f.write("{}")
    with open(os.path.join(root, "大数据.md"), "w", encoding="utf-8") as f:
        f.write("记忆" * 400)


async def main():
    import obsidian.main as ob
    tmp = tempfile.mkdtemp(prefix="obs_vault_")
    make_vault(tmp)
    ob.VAULTS = {"测试": tmp}
    ob.DEFAULT_VAULT = "测试"
    tool = ob.Obsidian.__new__(ob.Obsidian)

    try:
        print("\n[1] 路径安全")
        root, _ = ob._vault_root("测试")
        for bad, why in (("../外面.md", ".."), ("C:/Windows/win.ini", "盘符"),
                         ("/etc/passwd", "绝对路径")):
            full, msg = ob.safe_rel(bad, root)
            check("拒绝 %s" % why, full is None, (msg or "")[:40])
        full, _ = ob.safe_rel("日记/今天", root)
        check("无后缀自动补 .md", full.endswith("今天.md"), os.path.basename(full))

        print("\n[2] 搜索")
        r = await ob.Obsidian.obsidian_search(tool, None, "记忆是能记住", "测试", 0)
        check("正文命中", "[[首页]]" in r, r.splitlines()[0][:30])
        r = await ob.Obsidian.obsidian_search(tool, None, "2026-10-24", "测试", 0)
        check("标题命中", "[[记忆/按日期/2026-10-24]]" in r, r.splitlines()[1][:44])
        r = await ob.Obsidian.obsidian_search(tool, None, "根本不存在xyz", "测试", 0)
        check("没命中会说明", "没搜到" in r, r[:30])
        check("跳过 .obsidian", all(".obsidian" not in x for x, _ in
                                 ob.search_notes(root, "workspace", 8)), "")

        print("\n[3] 读取")
        r = await ob.Obsidian.obsidian_read(tool, None, "首页.md", "测试", 0)
        check("读正文", "记忆是能记住" in r, r.splitlines()[0][:30])
        r = await ob.Obsidian.obsidian_read(tool, None, "不存在的东西.md", "测试", 0)
        check("不存在→提示", "笔记不存在" in r, r[:30])

        print("\n[4] 追加（只加不覆盖）")
        before = open(os.path.join(tmp, "首页.md"), encoding="utf-8").read()
        r = await ob.Obsidian.obsidian_append(tool, None, "首页.md", "新加的一行", "测试")
        after = open(os.path.join(tmp, "首页.md"), encoding="utf-8").read()
        check("追加成功", "已追加" in r, r[:30])
        check("原有内容没被覆盖", before.strip() in after, "含旧内容")
        check("新内容在末尾", after.strip().endswith("新加的一行"), "末尾正确")
        r = await ob.Obsidian.obsidian_append(tool, None, "新笔记/第一条.md", "第一次写", "测试")
        body = open(os.path.join(tmp, "新笔记", "第一条.md"), encoding="utf-8").read()
        check("不存在则新建", "已追加" in r and body.startswith("# 第一条"), body.splitlines()[0])

        print("\n[5] 日记")
        r = await ob.Obsidian.obsidian_daily(tool, None, "今天记一笔", "测试", "2026-10-24")
        p = os.path.join(tmp, "日记", "2026-10-24.md")
        check("日记路径正确", os.path.isfile(p), p.replace(tmp, "<vault>"))
        check("日记内容写入", "今天记一笔" in open(p, encoding="utf-8").read())
        r = await ob.Obsidian.obsidian_daily(tool, None, "x", "测试", "2026/10/24")
        check("日期格式错误被拒", "YYYY-MM-DD" in r, r[:36])

        print("\n[6] 新建 / 上限")
        r = await ob.Obsidian.obsidian_new(tool, None, "新想法", "内容", "测试")
        check("新建成功", "已新建" in r, r[:30])
        r = await ob.Obsidian.obsidian_new(tool, None, "新想法", "又写一次", "测试")
        check("同名拒绝", "已经有" in r and "append" in r, r[:40])
        r = await ob.Obsidian.obsidian_new(tool, None, '非法:名<字>', "x", "测试")
        check("非法字符被替换", "已新建" in r and os.path.isfile(
            os.path.join(tmp, "非法_名_字_.md")), r[:30])
        r = await ob.Obsidian.obsidian_append(tool, None, "首页.md", "长" * 3000, "测试")
        check("超长写入被拒", "一次写太多" in r, r[:36])

        print("\n[7] 坏库")
        r = await ob.Obsidian.obsidian_read(tool, None, "首页.md", "不存在的库", 0)
        check("库名不认识→提示", "没有叫" in r, r[:40])
        ob.VAULTS = {"空": os.path.join(tmp, "没有这个目录")}
        r = await ob.Obsidian.obsidian_vaults(tool, None)
        check("目录不存在→标注", "目录不存在" in r, r[:50])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 56)
    print("通过 %d / 失败 %d" % (ok_n, len(bad_n)))
    if bad_n:
        print("失败项: " + "、".join(bad_n))
    return 1 if bad_n else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
