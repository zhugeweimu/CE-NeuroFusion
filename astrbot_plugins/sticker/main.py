"""幕的表情包 v2：按意思挑，不是随机抽。

问题：263 张图，文件名都是 face_emoji_000.png，图本身她看不见（没有视觉），
      所以 v1 只能 random.choice —— 发的都是随机的，跟话没关系。

v2 的做法：**用「她用这张图时的意思」当标签**
  第一次为了某个意思发图 → 随机取一张，并把这个意思绑到这张上
  以后同样的意思 → 用同一张
  意思相近（嵌入相似度够）→ 用那一张
  找不到合适的 → 不发，并告诉她可以先把图收进来再说清用途

这样"挑"是靠**积累 + 语义匹配**，不靠看图。
"""
import json
import math
import os
import random
import shutil
import time
import urllib.request

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, register

# ---- 可移植路径：优先 ASTRBOT_DATA 环境变量，否则 ~/astrbot/data ----
_DATA = os.environ.get("ASTRBOT_DATA") or os.path.join(os.path.expanduser("~"), "astrbot", "data")
PERSONA = os.environ.get("PERSONA", "她")


def _p(*parts):
    return os.path.join(_DATA, *parts)


STICKER_DIR = os.environ.get("STICKER_DIR") or _p("stickers")
TAGS_PATH = _p("plugin_data", "sticker", "tags.json")
EMBED_API = "http://127.0.0.1:8081/v1/embeddings"
EMBED_MODEL = "bge"

EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
OWNER_UINS = {u.strip() for u in os.environ.get("OWNER_UINS", "").split(",") if u.strip()}
MAX_MB = 8
MATCH_THRESHOLD = 0.72     # 低于这个相似度就当"没有合适的"


def _all_stickers():
    if not os.path.isdir(STICKER_DIR):
        return []
    out = []
    for name in os.listdir(STICKER_DIR):
        p = os.path.join(STICKER_DIR, name)
        if not os.path.isfile(p) or not name.lower().endswith(EXTS):
            continue
        try:
            if os.path.getsize(p) > MAX_MB * 1024 * 1024:
                continue
        except OSError:
            continue
        out.append(p)
    return sorted(out)


def _load_tags():
    if os.path.isfile(TAGS_PATH):
        try:
            return json.load(open(TAGS_PATH, encoding="utf-8")) or {}
        except Exception:
            return {}
    return {}


def _save_tags(d):
    os.makedirs(os.path.dirname(TAGS_PATH), exist_ok=True)
    tmp = TAGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TAGS_PATH)


def _embed(texts):
    body = json.dumps({"model": EMBED_MODEL, "input": texts},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        EMBED_API, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read().decode("utf-8"))
    return [x["embedding"] for x in d.get("data") or []]


def _cos(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@register("sticker", "mumu", "幕的表情包：按意思挑图、收图", "2.0.0")
class Sticker(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        os.makedirs(STICKER_DIR, exist_ok=True)
        os.makedirs(os.path.dirname(TAGS_PATH), exist_ok=True)
        self.tags = _load_tags()
        logger.info("[sticker] v2 表情库 %d 张，已记意思 %d 条" % (
            len(_all_stickers()), len(self.tags)))

    # ---------- 发一张（按意思挑）----------
    @filter.llm_tool(name="send_sticker")
    async def send_sticker(self, event: AstrMessageEvent, mood: str):
        """发一个表情包。要说什么意思，就写什么，别写"一个表情包"。

        Args:
            mood(string): 你想表达的意思或情绪，一句话，比如「没辙了」「行吧随你」「我困了」
        """
        mood = (mood or "").strip()
        if len(mood) < 2:
            return "说说你想表达什么，我再找。只写「表情包」那种不行。"

        files = _all_stickers()
        if not files:
            return "表情库是空的，发不了。"

        # ① 按意思找已经绑好的
        chosen = None
        if self.tags:
            try:
                qv = _embed([mood])[0]
                keys = list(self.tags.keys())
                kvs = _embed(keys)
                scored = sorted(
                    ((_cos(qv, v), k) for k, v in zip(keys, kvs)), reverse=True)
                if scored and scored[0][0] >= MATCH_THRESHOLD:
                    cand = self.tags[scored[0][1]]
                    p = os.path.join(STICKER_DIR, cand)
                    if os.path.isfile(p):
                        chosen = (p, "%s（相近：%s，%.2f）" % (
                            scored[0][1], scored[0][1], scored[0][0]))
                elif scored:
                    logger.info("[sticker] 最相近只有 %.2f，不硬凑" % scored[0][0])
            except Exception as e:
                logger.warning("[sticker] 语义匹配失败，退化为新绑: %r" % (e,))

        # ② 没有就新绑一张：优先没被用过的
        if chosen is None:
            used = set(self.tags.values())
            fresh = [f for f in files if os.path.basename(f) not in used]
            pool = fresh or files
            p = random.choice(pool)
            self.tags[mood] = os.path.basename(p)
            _save_tags(self.tags)
            chosen = (p, "新绑：%s → %s" % (mood, os.path.basename(p)))
            logger.info("[sticker] 新绑 %s → %s" % (mood, os.path.basename(p)))

        path, why = chosen
        try:
            await event.send(MessageChain([Image(file=path)]))
            logger.info("[sticker] 发送 %s（%s）" % (os.path.basename(path), why))
            return "（发了：%s）" % why
        except Exception as e:
            logger.error("[sticker] 发送失败: %r" % (e,))
            return "表情包发不出去：%s" % e

    # ---------- 收一张 ----------
    @filter.llm_tool(name="add_sticker")
    async def add_sticker(self, event: AstrMessageEvent, path: str, mood: str = ""):
        """把一张图片加进表情库。只有主人能用。最好顺手说清这张是干什么的。

        Args:
            path(string): 图片的完整路径
            mood(string): 这张图用在什么心情/场合，比如「无语」「得意」
        """
        try:
            who = str(event.get_sender_id())
        except Exception:
            who = "unknown"
        if who not in OWNER_UINS:
            return "只有主人能往表情库里加图。"

        src = os.path.abspath((path or "").strip().strip('"'))
        if not os.path.isfile(src):
            return "没有这个文件：%s" % src
        if not src.lower().endswith(EXTS):
            return "只收图片（png/jpg/gif/webp）。"

        try:
            os.makedirs(STICKER_DIR, exist_ok=True)
            base = os.path.basename(src)
            dst = os.path.join(STICKER_DIR, "add_%s_%s" % (
                time.strftime("%m%d%H%M"), base))
            shutil.copy2(src, dst)
            mood = (mood or "").strip()
            if mood:
                self.tags[mood] = os.path.basename(dst)
                _save_tags(self.tags)
            logger.info("[sticker] 新增 %s（%s）" % (dst, mood or "没标用途"))
            return "收下了。现在一共 %d 张%s。" % (
                len(_all_stickers()), ("，并记下用途：" + mood) if mood else "")
        except Exception as e:
            return "加不进去：%s" % e

    # ---------- 看看手里有什么 ----------
    @filter.llm_tool(name="sticker_book")
    async def sticker_book(self, event: AstrMessageEvent):
        """看看我已经给哪些意思配了图。"""
        if not self.tags:
            return "还没给任何意思配过图。发的时候说个意思，我会自己绑一张。"
        rows = sorted(self.tags.items(), key=lambda x: x[1])
        head = "我给这些意思配了图（%d 条）：\n" % len(rows)
        return head + "\n".join("- %s → %s" % (k, v) for k, v in rows[:40])
