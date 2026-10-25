# -*- coding: utf-8 -*-
"""ce_iterate —— AstrBot 插件：新增文件自动整理（走 CE）

职责：
  插件：定时扫新文件、维护游标、分批、调 CE、写报告、可选推送到聊天
  CE  ：只做上下文压缩（收 source，吐整理结果）

命令：
  /ce_iter 整理         立刻跑一次
  /ce_iter 状态         看游标与上次运行
  /ce_iter 干跑         只列出会处理哪些文件

配置（环境变量，避免再写一份 config.json）：
  CE_ITER_ROOTS    要扫的目录，逗号分隔（必填）
  CE_ITER_OUT      报告输出目录（默认 根目录/增量整理）
  CE_ITER_TASK     整理要求（默认"整理成要点清单，每条一行"）
  CE_ITER_EVERY    自动运行间隔秒数（0=只手动，默认 0）
  CE_ITER_MAX_CHARS 每批材料上限（默认 6000）
  CE_BASE          CE 地址（默认 http://127.0.0.1:8092）
  CE_TOKEN         CE 令牌（可选）
"""
import asyncio
import os
import sys
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ce_iterate_cli as eng  # 复用 CLI 的扫描/分批/游标逻辑

ROOTS = [x.strip() for x in (os.environ.get("CE_ITER_ROOTS") or "").split(",") if x.strip()]
OUT = os.environ.get("CE_ITER_OUT") or (os.path.join(ROOTS[0], "增量整理") if ROOTS else "")
TASK = os.environ.get("CE_ITER_TASK") or "把这些新增内容整理成一份要点清单，每条一行"
EVERY = int(os.environ.get("CE_ITER_EVERY") or 0)
MAX_CHARS = int(os.environ.get("CE_ITER_MAX_CHARS") or 6000)
MIN_FILES = int(os.environ.get("CE_ITER_MIN_FILES") or 1)
MIN_BYTES = int(os.environ.get("CE_ITER_MIN_BYTES") or 200)
MIN_INTERVAL = int(os.environ.get("CE_ITER_MIN_INTERVAL") or 0)
USE_HASH = (os.environ.get("CE_ITER_HASH") or "0") not in ("0", "false", "False")


@register("ce_iterate", "mumu", "新增文件自动整理（走 CE）", "0.1.0")
class CEIterate(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._task = None
        logger.info("[ce_iter] 启动：roots=%s out=%s every=%ds"
                    % (ROOTS, OUT, EVERY))

    async def initialize(self):
        if EVERY > 0 and ROOTS:
            self._task = asyncio.create_task(self._loop())

    async def terminate(self):
        if self._task and not self._task.done():
            self._task.cancel()

    async def _loop(self):
        while True:
            try:
                await asyncio.to_thread(eng.run, ROOTS, OUT, TASK, None, MAX_CHARS, False, 20,
                             USE_HASH, MIN_FILES, MIN_BYTES, MIN_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[ce_iter] 定时运行失败: %r" % (e,))
            await asyncio.sleep(max(60, EVERY))

    @filter.command("ce_iter")
    async def ce_iter(self, event: AstrMessageEvent):
        """整理新增文件 / 状态 / 干跑。"""
        arg = (event.message_str or "").replace("/ce_iter", "").strip()
        if not ROOTS:
            yield event.plain_result("没配 CE_ITER_ROOTS（要扫的目录），先设环境变量再重载插件")
            return
        if arg in ("状态", "status"):
            st = eng.load_state(ROOTS[0])
            yield event.plain_result(
                "游标：%s\n最近运行：%s"
                % (time.strftime("%Y-%m-%d %H:%M", time.localtime(st.get("since") or 0)),
                   (st.get("runs") or [{}])[-1]))
            return
        if arg in ("干跑", "dry"):
            r = await asyncio.to_thread(eng.run, ROOTS, "", TASK, None, MAX_CHARS, True, 20,
                             USE_HASH, MIN_FILES, MIN_BYTES, MIN_INTERVAL)
            yield event.plain_result("会处理 %d 个文件（未调用模型）" % r["files"])
            return
        r = await asyncio.to_thread(eng.run, ROOTS, OUT, TASK, None, MAX_CHARS, False, 20,
                             USE_HASH, MIN_FILES, MIN_BYTES, MIN_INTERVAL)
        head = (r.get("answer") or "")[:300]
        yield event.plain_result("处理 %d 个文件，%d 批。\n%s"
                                 % (r["files"], r["batches"], head))
