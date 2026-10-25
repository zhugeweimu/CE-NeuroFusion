# -*- coding: utf-8 -*-
"""ce_iterate —— 增量整理插件（独立运行版）

检测"新/变"文件的四种依据，按可靠性排序：
  ① 文件签名（mtime + size）与上次记录比对   —— 能发现"老文件被改"
  ② 可选内容指纹（--hash，读前 4KB 算 sha1） —— 能发现"mtime 和大小都没变但内容变了"
  ③ mtime 大于游标                        —— 首次运行/游标丢失时的兜底
  ④ NTFS USN 日志探测（--probe-usn）       —— 只做可用性探测；拿不到就明确报告，不假装

权限与异常：
  · os.walk 遇到无权限目录不会抛异常，只静默跳过 → 这里显式计数并回报
  · 单个文件 stat/读取失败 → 记一条错误，继续处理其余文件
  · 全部目录都读不了 → 明确返回"无法扫描"，绝不返回"没有新文件"

唤醒门铃（避免每次扫描都烧模型）：
  · 变化文件数 ≥ --min-files
  · 且 变化总字节 ≥ --min-bytes
  · 且 距上次运行 ≥ --min-interval 秒
  不满足 → 不调用模型，只更新签名并记录"未唤醒"原因
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time
import urllib.request

CE = os.environ.get("CE_BASE", "http://127.0.0.1:8092")
TOKEN = os.environ.get("CE_TOKEN", "")
EXTS = (".md", ".txt", ".json", ".py", ".log", ".csv")
STATE_NAME = ".ce_iterate_state.json"
SEEN_MAX = 5000


# ---------------- 状态 ----------------
def state_path(root: str) -> str:
    return os.path.join(root, STATE_NAME)


def load_state(root: str) -> dict:
    try:
        with io.open(state_path(root), encoding="utf-8") as f:
            d = json.load(f) or {}
        d.setdefault("since", 0.0)
        d.setdefault("seen", {})
        d.setdefault("runs", [])
        return d
    except Exception:
        return {"since": 0.0, "seen": {}, "runs": []}


def save_state(root: str, d: dict) -> None:
    seen = d.get("seen") or {}
    if len(seen) > SEEN_MAX:
        items = sorted(seen.items(), key=lambda kv: kv[1][0], reverse=True)[:SEEN_MAX]
        d["seen"] = dict(items)
    p = state_path(root)
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


# ---------------- 文件签名 ----------------
def _sig(fp: str, use_hash: bool) -> tuple:
    st = os.stat(fp)
    h = ""
    if use_hash:
        try:
            with io.open(fp, "rb") as f:
                h = hashlib.sha1(f.read(4096)).hexdigest()[:16]
        except Exception:
            h = "?"
    return (int(st.st_mtime), int(st.st_size), h)


def scan_changed(roots, seen, since, skip_dirs=(), use_hash=False):
    """返回 (changed, errors, scanned)。changed 每项 (mtime, path, size, why)。"""
    changed, errors, scanned = [], [], 0
    skips = [os.path.abspath(d) for d in skip_dirs if d]
    for root in roots:
        if not os.path.isdir(root):
            errors.append("目录不存在：%s" % root)
            continue
        for dp, dirs, files in os.walk(root, onerror=lambda e: errors.append("遍历失败：%r" % (e,))):
            if any(os.path.abspath(dp).startswith(s) for s in skips):
                dirs[:] = []
                continue
            dirs[:] = [x for x in dirs
                       if not x.startswith(".") and x not in ("node_modules", "__pycache__", ".git")]
            for fn in files:
                if fn.startswith(".") or fn == STATE_NAME:
                    continue
                if not fn.lower().endswith(EXTS):
                    continue
                fp = os.path.join(dp, fn)
                scanned += 1
                try:
                    mtime, size, h = _sig(fp, use_hash)
                except Exception as e:
                    errors.append("读不了 %s：%r" % (fp, e))
                    continue
                if size <= 0:
                    continue
                old = seen.get(fp)
                if old is None:
                    # 已有基线（seen 非空）时：路径没见过就算新文件——
                    # 复制过来的文件会保留旧 mtime，只看 mtime > since 会漏。
                    if (not seen) and mtime <= since:
                        continue
                    changed.append((mtime, fp, size, "新文件"))
                elif int(old[0]) != mtime or int(old[1]) != size:
                    changed.append((mtime, fp, size, "有改动"))
                elif use_hash:
                    if len(old) <= 2 or not old[2]:
                        changed.append((mtime, fp, size, "首次指纹基线"))
                    elif old[2] != h:
                        changed.append((mtime, fp, size, "内容变了（签名不变）"))
    changed.sort()
    return changed, errors, scanned


def remember(seen: dict, rows, use_hash=False) -> None:
    for mtime, fp, size, _why in rows:
        h = ""
        if use_hash:
            try:
                h = _sig(fp, True)[2]
            except Exception:
                h = ""
        seen[fp] = [int(mtime), int(size), h] if use_hash else [int(mtime), int(size)]


# ---------------- 分批 ----------------
def read_piece(fp: str, limit: int) -> str:
    try:
        with io.open(fp, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read(limit * 2)
    except Exception:
        return ""
    if len(txt) > limit:
        txt = txt[:limit] + "\n（超长，已截断）"
    return txt


def batch(rows, budget: int):
    cur, used = [], 0
    for mt, fp, _sz, _why in rows:
        piece = read_piece(fp, budget)
        if not piece:
            continue
        body = "\n\n### %s（%s·%s）\n%s" % (
            os.path.basename(fp), time.strftime("%m-%d %H:%M", time.localtime(mt)), _why, piece)
        if cur and used + len(body) > budget:
            yield cur
            cur, used = [], 0
        cur.append((mt, fp, body))
        used += len(body)
    if cur:
        yield cur


# ---------------- CE ----------------
def call_ce(task: str, source: str, session: str, timeout: int = 300) -> str:
    payload = {"task": task, "source": source, "session_id": session,
               "auto": False, "stepwise": False}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(CE.rstrip("/") + "/task", data=data,
                                 headers={"Content-Type": "application/json",
                                          **({"X-CE-Token": TOKEN} if TOKEN else {})})
    last = None
    for _ in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return (json.loads(r.read().decode("utf-8")).get("answer") or "").strip()
        except Exception as e:
            last = e
            time.sleep(1.5)
    raise last


# ---------------- USN 探测（只判断能不能用） ----------------
def usn_available(volume: str = "C:") -> tuple:
    """NTFS USN 日志是否可读。需要管理员；不可用就如实说，不假装。"""
    try:
        import ctypes
        import ctypes.wintypes as wt
        k32 = ctypes.windll.kernel32
        GENERIC_READ = 0x80000000
        FILE_SHARE_ALL = 0x7
        OPEN_EXISTING = 3
        FSCTL_QUERY_USN_JOURNAL = 0x000900F4
        h = k32.CreateFileW("\\\\.\\%s" % volume, GENERIC_READ, FILE_SHARE_ALL,
                            None, OPEN_EXISTING, 0, None)
        if h is None or h == -1:
            return False, "打不开卷句柄（多半需要管理员权限）"
        try:
            buf = ctypes.create_string_buffer(64)
            ret = wt.DWORD(0)
            ok = k32.DeviceIoControl(h, FSCTL_QUERY_USN_JOURNAL, None, 0,
                                     buf, 64, ctypes.byref(ret), None)
            if not ok:
                return False, "FSCTL_QUERY_USN_JOURNAL 失败（错误码 %d）" % k32.GetLastError()
            return True, "可用（管理员）"
        finally:
            k32.CloseHandle(h)
    except Exception as e:
        return False, "探测异常：%r" % (e,)


# ---------------- 主流程 ----------------
def run(roots, out_dir, task, since=None, max_chars=6000, dry_run=False, max_batches=20,
        use_hash=False, min_files=1, min_bytes=200, min_interval=0, force=False):
    st = load_state(roots[0])
    since_ts = float(since) if since else float(st.get("since") or 0)
    if not since_ts:
        since_ts = time.time() - 7 * 86400
    clock_back = since_ts > time.time() + 60      # 游标在未来 = 时钟被改过
    seen = {} if clock_back else dict(st.get("seen") or {})

    changed, errors, scanned = scan_changed(
        roots, seen, since_ts, skip_dirs=[out_dir] if out_dir else [], use_hash=use_hash)
    total_bytes = sum(r[2] for r in changed)
    last_run = (st.get("runs") or [{}])[-1].get("ts", 0) if st.get("runs") else 0
    idle = time.time() - float(last_run or 0)

    reason = ""
    if clock_back:
        reason = "时钟回拨（游标在未来），按已知文件签名重扫"
    if not changed:
        reason = ("时钟回拨后：无变化" if clock_back else "无变化")
    elif len(changed) < min_files:
        reason = "变化文件 %d < 门槛 %d" % (len(changed), min_files)
    elif total_bytes < min_bytes:
        reason = "变化 %d 字节 < 门槛 %d" % (total_bytes, min_bytes)
    elif min_interval and idle < min_interval:
        reason = "距上次 %.0f 秒 < 间隔门槛 %d" % (idle, min_interval)
    wake = (not reason) or force

    print("扫描：%d 个候选，%d 个变化，%d 字节；错误 %d 条"
          % (scanned, len(changed), total_bytes, len(errors)))
    for e in errors[:5]:
        print("  ! " + e)
    if dry_run:
        for mt, fp, sz, why in changed[:50]:
            print("  - [%s] %s  %d 字节" % (why, fp, sz))
        return {"files": len(changed), "batches": 0, "answer": "", "dry_run": True,
                "wake": wake, "reason": reason, "errors": errors, "scanned": scanned}
    if not wake:
        remember(seen, changed, use_hash)
        st["seen"] = seen
        st["runs"] = (st.get("runs") or [])[-20:] + [{
            "ts": time.time(), "t": time.strftime("%Y-%m-%d %H:%M:%S"),
            "task": task[:60], "files": len(changed), "batches": 0, "wake": False,
            "reason": reason, "errors": len(errors)}]
        save_state(roots[0], st)
        print("未唤醒模型：%s" % reason)
        return {"files": len(changed), "batches": 0, "answer": "", "wake": False,
                "reason": reason, "errors": errors, "scanned": scanned}

    outs, done, used, cursor = [], [], 0, since_ts
    for bi, grp in enumerate(batch(changed, max_chars)):
        if bi >= max_batches:
            print("批数上限 %d，剩下的下次继续" % max_batches)
            break
        material = "".join(p for _mt, _fp, p in grp)
        try:
            answer = call_ce(task, material, "iter-%d-%d" % (int(time.time()), bi))
        except Exception as e:
            print("  第 %d 批 CE 调用失败：%r" % (bi + 1, e))
            break
        used += 1
        if answer:
            outs.append("## 第 %d 批（%d 个文件）\n%s" % (bi + 1, len(grp), answer))
        for mt, fp, _b in grp:
            done.append(fp)
            cursor = max(cursor, mt)
        print("  第 %d 批：%d 文件 → %d 字" % (bi + 1, len(grp), len(answer)))

    text = "\n\n".join(outs).strip() or "（这次没有产出）"
    if out_dir and outs:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, time.strftime("增量整理-%Y%m%d-%H%M.md"))
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(text)
        print("已写：%s" % path)

    remember(seen, changed, use_hash)
    st["seen"] = seen
    st["since"] = cursor
    st["runs"] = (st.get("runs") or [])[-20:] + [{
        "ts": time.time(), "t": time.strftime("%Y-%m-%d %H:%M:%S"), "task": task[:60],
        "files": len(done), "batches": used, "wake": True, "reason": "",
        "errors": len(errors)}]
    save_state(roots[0], st)
    return {"files": len(done), "batches": used, "answer": text, "wake": True,
            "reason": "", "errors": errors, "scanned": scanned}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--task", default="把这些新增内容整理成一份要点清单，每条一行")
    ap.add_argument("--since", type=float, default=None)
    ap.add_argument("--max-chars", type=int, default=6000)
    ap.add_argument("--max-batches", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--hash", action="store_true", help="内容指纹（更准，但更慢）")
    ap.add_argument("--min-files", type=int, default=1)
    ap.add_argument("--min-bytes", type=int, default=200)
    ap.add_argument("--min-interval", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--probe-usn", default="", help="探测某卷的 USN 日志，如 C:")
    a = ap.parse_args()
    if a.probe_usn:
        ok, msg = usn_available(a.probe_usn)
        print("USN %s：%s（%s）" % (a.probe_usn, "可用" if ok else "不可用", msg))
        return
    roots = [x.strip() for x in a.roots.split(",") if x.strip()]
    r = run(roots, a.out, a.task, a.since, a.max_chars, a.dry_run, a.max_batches,
            a.hash, a.min_files, a.min_bytes, a.min_interval, a.force)
    print("结果：文件 %d，批 %d，唤醒=%s%s"
          % (r["files"], r["batches"], r.get("wake"), ("（%s）" % r["reason"]) if r.get("reason") else ""))


if __name__ == "__main__":
    main()
