# -*- coding: utf-8 -*-
"""
雨课堂自动刷课脚本 v7 (Edge + Playwright，支持并行)
================================================
- 独立 Edge profile 保存登录态：首次运行请在打开的浏览器里扫码登录一次，之后自动复用
- 可见窗口模式：启动后可以把 Edge 窗口最小化，脚本在后台照常运行
- 流程：首页 -> 我听的课 -> 逐门课程：点"未完成" -> 逐个刷【视频+计分+未完成】
        -> 刷完一门自动进下一门 -> 全部课程完成后弹窗提示
- 进度记录：progress.json 自动记录每门课程的 总目标/剩余/是否已刷完
  （--status 可只查看进度，不刷课）
- 视频条目：进入学习空间页，检测 <video> 播放状态，自动播放到结束
- 浏览器内核级静音（--mute-audio），页面 JS 无法取消
- 遇到弹题（选择题/提交按钮等）：打印页面文字并暂停，等你作答后按回车继续
- 并行刷课（--parallel 2/3）：最多 3 门不同课程同时刷，自动复制登录态 profile，
  每个 worker 独立 Edge 窗口 + 独立静音，进度记录带锁合并写入

用法:
    python auto.py                       # 串行刷所有课程的未完成视频
    python auto.py --parallel 3          # 3 门课程同时刷（最多 3 个窗口）
    python auto.py --course 分布式数据库技术   # 只刷指定课程
    python auto.py --status              # 只看进度记录，不刷课
    python auto.py --dwell 8             # 课件每页停留秒数（默认 10，视频用不到）
    python auto.py --max-lessons 3       # 每门课程最多刷几个条目（0=全部）
    python auto.py --headless            # 完全无头模式（不显示窗口）
"""
import argparse
import atexit
import ctypes
import datetime
import hashlib
import json
import multiprocessing
import os
import re
import shutil
import sys
import time

from playwright.sync_api import sync_playwright

# 打包（PyInstaller）后 sys.frozen 为真：数据文件放在 exe 同目录，而不是临时解压目录
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(BASE_DIR, "edge_profile")
LOG_FILE = os.path.join(BASE_DIR, "log.txt")
PROGRESS_FILE = os.path.join(BASE_DIR, "progress.json")
TASKS_FILE = os.path.join(BASE_DIR, "tasks.json")
INDEX_URL = "https://www.yuketang.cn/v2/web/index"


def log(msg):
    line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}"
    # 控制台可能不存在（打包后的 windowed 子进程），print 需容错
    try:
        if sys.stdout is not None:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_progress():
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"updated": "", "courses": {}}


def save_progress(prog):
    prog["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(prog, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"保存进度记录失败: {e}")


def save_progress_merge(updates):
    """并行模式下的进度合并保存：带文件锁，只更新 updates 中的课程，保留其他 worker 写的课程"""
    lock_path = PROGRESS_FILE + ".lock"
    fd = None
    for _ in range(50):  # 最多等 10 秒拿锁
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            time.sleep(0.2)
    if fd is None:
        # 拿不到锁就放弃本次写入：避免无锁裸写导致两个窗口互相覆盖、进度丢失
        log("进度写盘：锁占用超时（10 秒），放弃本次写入（避免覆盖其他窗口的进度）。")
        return
    try:
        prog = load_progress()
        prog["courses"].update(updates)
        prog["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(prog, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"保存进度记录失败: {e}")
    finally:
        if fd is not None:
            os.close(fd)
            try:
                os.remove(lock_path)
            except Exception:
                pass


def print_progress_summary(prog):
    courses = prog.get("courses", {})
    if not courses:
        log("（暂无进度记录，跑一次后自动生成 progress.json）")
        return
    log("========== 历史进度 ==========")
    for name, info in courses.items():
        if info.get("done"):
            log(f"  [已刷完 ✓] {name}（共 {info.get('total', 0)} 条）")
        else:
            log(f"  [刷课中] {name}：剩余 {info.get('remaining', '?')} 条 / 总 {info.get('total', '?')} 条")
    log("==============================")


def page_text(page, max_len=3000):
    try:
        return page.evaluate(
            """(max_len) => (document.body.innerText || '').slice(0, max_len)""", max_len
        )
    except Exception:
        return ""


def is_logged_in(page):
    url = page.url
    if "login" in url.lower():
        return False
    text = page_text(page, 800).lower()
    if ("扫码" in text or "账号登录" in text) and "我的课程" not in text:
        return False
    if "我的课程" in text or "继续学习" in text:
        return True
    if not text.strip():
        return None  # 页面还没渲染出来（Edge 打开慢/白屏）：不判定，继续等待
    return True


def wait_login(page, timeout_min=5):
    log("检查登录状态……")
    start = time.time()
    while True:
        r = is_logged_in(page)
        if r is True:
            log("已登录，继续。")
            return True
        if time.time() - start > timeout_min * 60:
            log("等待登录超时，请重新运行脚本并尽快登录。")
            return False
        if r is False:
            log("浏览器已打开登录页，请在浏览器中扫码/输入账号登录（等待中……）")
        else:
            log("页面加载中，继续等待……")
        page.wait_for_timeout(4000)


def click_text(page, text, exact=False, timeout=5000):
    loc = page.get_by_text(text, exact=exact)
    try:
        loc.first.click(timeout=timeout)
        return True
    except Exception:
        pass
    loc = page.get_by_role("button", name=text)
    try:
        loc.first.click(timeout=timeout)
        return True
    except Exception:
        return False


def click_tab(page, name, timeout=5000):
    for role in ("tab", "menuitem"):
        loc = page.get_by_role(role, name=name)
        try:
            loc.first.click(timeout=timeout)
            return True
        except Exception:
            continue
    return click_text(page, name)


def dismiss_popups(page):
    """关闭常见弹窗/遮罩（公告、引导等）"""
    try:
        for kw in ("我知道了", "关闭", "取消", "跳过"):
            b = page.get_by_role("button", name=kw)
            if b.count() and b.first.is_visible():
                b.first.click(timeout=1500)
                log(f"已关闭弹窗: {kw}")
                page.wait_for_timeout(1200)
                return True
    except Exception:
        pass
    return False


def find_course_entries(page):
    """从“我听的课”列表提取课程卡片（div.lesson-cardS / el-card）"""
    try:
        return page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                for (const el of document.querySelectorAll('div')) {
                    const cls = (el.className || '').toString();
                    if (!/(lesson-card|box-card)/i.test(cls)) continue;
                    const txt = (el.innerText || '').trim().replace(/\\s+/g, ' ');
                    if (!txt || txt.length < 5) continue;
                    // 只按学期识别（秋/春），不要求含"班"——避免不含"班"的课程卡片整门被丢
                    if (!/(秋|春)/.test(txt)) continue;
                    const key = txt.slice(0, 30);
                    if (seen.has(key)) continue;
                    seen.add(key);
                    out.push({txt: txt.slice(0, 120), cls: cls.slice(0, 80)});
                }
                return out;
            }"""
        ) or []
    except Exception as e:
        log(f"find_course_entries 失败: {e}")
        return []


SEMESTER_RE = re.compile(r"(20\d{2})(春|秋)")


def course_name_of(txt):
    """课程卡片文字 → 课程名："Python Web程序设计实训 刘恋 2026秋-2024级…" → "Python Web程序设计实训"
    规则：先切掉学期及之后的内容，再去掉末尾的教师名（2~4 个汉字且不是课程主体）。"""
    txt = (txt or "").strip()
    m = SEMESTER_RE.search(txt)
    head = txt[: m.start()].strip() if m else txt
    parts = head.split()
    if len(parts) > 1 and 2 <= len(parts[-1]) <= 4 and all("\u4e00" <= ch <= "\u9fff" for ch in parts[-1]):
        head = " ".join(parts[:-1]).strip()
    return head or txt


def filter_latest_semester(entries):
    """只保留最新学期的课程。返回 (学期标签, 该学期课程列表)"""
    tagged = []
    for e in entries:
        m = SEMESTER_RE.search(e["txt"])
        if m:
            year = int(m.group(1))
            is_autumn = m.group(2) == "秋"  # 同一年秋 > 春
            tagged.append((year, is_autumn, e))
    if not tagged:
        log("未能识别课程学期，按全部课程处理。")
        return "未知", entries
    best = max(tagged, key=lambda t: (t[0], t[1]))
    semester = f"{best[0]}秋" if best[1] else f"{best[0]}春"
    keep = [e for (y, a, e) in tagged if (y, a) == (best[0], best[1])]
    return semester, keep


def go_to_my_courses(page):
    """回到首页并切到“我听的课”列表（跨课程切换用）"""
    try:
        page.goto(INDEX_URL, timeout=60000, wait_until="domcontentloaded")
    except Exception as e:
        log(f"返回首页失败: {e}")
    page.wait_for_timeout(3000)
    click_tab(page, "我听的课")
    page.wait_for_timeout(3000)


TARGET_KINDS = ("视频", "课件")


def normalize_item_type(t):
    """待办类型归一：雨课堂会出现 视频 / 课件 / 作业 / 考试 / 开放型作业 等写法"""
    t = (t or "").strip()
    up = t.upper()
    if "视频" in t:
        return "视频"
    if "课件" in t or "PPT" in up or "文档" in t or "讲义" in t:
        return "课件"
    if "作业" in t:
        return "作业"
    if "考试" in t or "测验" in t or "测试" in t:
        return "考试"
    return t or "其他"


def is_unfinished_status(status):
    s = (status or "").strip()
    return "未" in s or "进行中" in s


def parse_todo_items(page, kinds=TARGET_KINDS):
    """解析“未完成”待办列表：返回 (全部条目, 目标条目)

    目标 = 待学习条目：视频（计分）+ 课件（全部未完成课件）。
    kinds 里没有的类型不参与刷课，只统计。"""
    try:
        items = page.evaluate(
            """() => {
                const out = [];
                const metaType = /^(视频|课件|作业|考试|开放型作业|讨论|文档|讲义)$/;
                const metaWord = /计分|未开始|进行中|已完成|未完成|已过期|未学会/;
                document.querySelectorAll('li.unfinished-study__item').forEach(li => {
                    const lines = (li.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
                    if (!lines.length) return;
                    const type = lines[0];
                    const scored = lines.some(l => l.includes('计分'));
                    const status = lines.find(l => /未开始|进行中|已完成|未完成|已过期/.test(l)) || '未开始';
                    let name = lines.find(l => !metaType.test(l) && !metaWord.test(l)
                                            && /项目|课|章|节|讲/.test(l) && l.length > 2);
                    if (!name) name = lines.find(l => !metaType.test(l) && !metaWord.test(l) && l.length > 2);
                    if (!name) name = lines[1] || lines.join(' ');
                    out.push({type: type, scored: scored, status: status, name: name});
                });
                return out;
            }"""
        )
    except Exception as e:
        log(f"parse_todo_items 失败: {e}")
        return [], []
    items = items or []
    for it in items:
        it["kind"] = normalize_item_type(it.get("type"))
    targets = []
    for it in items:
        if not is_unfinished_status(it.get("status")):
            continue
        if it["kind"] == "视频" and it.get("scored") and "视频" in kinds:
            targets.append(it)
        elif it["kind"] == "课件" and "课件" in kinds:
            targets.append(it)
    return items, targets


def count_target_kinds(items):
    """统计待办里「视频/课件」各自的未完成条数（与本次只刷哪一类无关）"""
    d = {}
    for it in items or []:
        if not is_unfinished_status(it.get("status")):
            continue
        k = it.get("kind")
        if k == "视频" and it.get("scored"):
            d[k] = d.get(k, 0) + 1
        elif k == "课件":
            d[k] = d.get(k, 0) + 1
    return d


def refresh_todo_list(page, retries=5, wait=3000, kinds=TARGET_KINDS):
    """确认待办列表状态并解析。返回 (all_items, targets, state)
    state: 'ok'=列表正常 | 'empty'=页面显示“暂无未完成” | 'fail'=多次重试仍加载不出来"""
    all_items, targets = parse_todo_items(page, kinds)
    if all_items:
        return all_items, targets, "ok"
    attempt = 0
    while attempt < retries:
        txt = page_text(page, 800).replace(" ", "")
        if "暂无未完成" in txt or ("暂无" in txt and "未完成" in txt):
            log("页面显示“暂无未完成”，该课程无待办，直接跳过。")
            return [], [], "empty"
        attempt += 1
        log(f"待办列表尚未加载出来（第 {attempt}/{retries} 次重试）……")
        page.wait_for_timeout(wait)
        all_items, targets = parse_todo_items(page, kinds)
        if all_items:
            return all_items, targets, "ok"
    return [], [], "fail"


def check_quiz(page):
    """检测弹题：出现可见的 提交/下一题/确定 等按钮视为弹题"""
    try:
        for kw in ("提交", "下一题", "确定"):
            loc = page.get_by_role("button", name=kw)
            if loc.count() and loc.first.is_visible():
                return True
        text = page_text(page, 2500)
        for kw in ("请作答", "单选题", "多选题", "判断题", "填空题"):
            if kw in text:
                return True
    except Exception:
        pass
    return False


def handle_quiz(page):
    """弹题处理：把页面文字打到控制台和日志，暂停等用户作答"""
    text = page_text(page, 3000)
    log("========== 检测到弹题 ==========")
    log("题目页面文字如下，请阅读并作答：")
    for line in text.splitlines():
        line = line.strip()
        if line:
            log("  " + line)
    log("请在浏览器中完成作答，答完后回到本窗口按回车继续……")
    try:
        input()
    except EOFError:
        log("无法等待输入，跳过本题。")
        page.wait_for_timeout(15000)


LEARN_RE = re.compile(r"学习进度[：:]\s*(\d+)\s*/\s*(\d+)")
NUM_PAIR_RE = re.compile(r"(?<!\d)(\d{1,4})\s*/\s*(\d{1,4})(?!\d)")


def _frames_of(page):
    """页面本身 + 所有 iframe：课件可能嵌在 iframe 里，只在主文档里找会漏"""
    out = [page]
    try:
        out += list(page.frames)
    except Exception:
        pass
    return out


def _frame_text(target, max_len=1200):
    try:
        return target.evaluate(
            "(n) => { const b = document.body; return b ? (b.innerText || '').slice(0, n) : ''; }",
            max_len,
        ) or ""
    except Exception:
        return ""


def _read_texts(page, max_len=1200):
    out = []
    for t in _frames_of(page):
        txt = _frame_text(t, max_len)
        if txt.strip():
            out.append(txt)
    return out


def lesson_progress(page):
    """读取学习页“学习进度：x/y”（课件类），主页面与 iframe 都查"""
    for txt in _read_texts(page, 1500):
        m = LEARN_RE.search(txt)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None, None


def _slide_image_digest(page):
    """无进度/无页码时的兜底判据：对页面中部截图取指纹（图片型 PPT 文字不变也能判断翻页）"""
    try:
        vp = page.viewport_size or {"width": 1280, "height": 800}
        clip = {
            "x": int(vp["width"] * 0.18),
            "y": int(vp["height"] * 0.12),
            "width": int(vp["width"] * 0.64),
            "height": int(vp["height"] * 0.58),
        }
        shot = page.screenshot(clip=clip, type="jpeg", quality=40, timeout=8000)
        return hashlib.md5(shot).hexdigest()[:12]
    except Exception as e:
        log(f"截图指纹获取失败（本轮改用文字判据）: {str(e)[:100]}")
        return None


def courseware_state(page):
    """课件页状态：学习进度 + 页码对（+ 无进度时的截图指纹）"""
    cur, total = lesson_progress(page)
    texts = _read_texts(page, 1200)
    pair = None
    for txt in texts:
        pairs = NUM_PAIR_RE.findall(txt)
        if pairs:
            a, b = pairs[-1]
            if int(b) >= int(a) and int(b) > 0:
                pair = (int(a), int(b))
                break
    img = None
    if cur is None and pair is None:
        img = _slide_image_digest(page)
    return {"cur": cur, "total": total, "pair": pair, "img": img}


def _state_advanced(before, after):
    """判断翻页是否真的生效：学习进度 +1 > 页码变化 > 截图指纹变化"""
    if after["cur"] is not None and before["cur"] is not None and after["cur"] > before["cur"]:
        return True
    if after["pair"] and before["pair"] and after["pair"][0] > before["pair"][0]:
        return True
    if after["img"] and before["img"] and after["img"] != before["img"]:
        return True
    return False


def _click_visible(loc, timeout=2500):
    try:
        if loc.count():
            first = loc.first
            if first.is_visible():
                first.click(timeout=timeout)
                return True
    except Exception:
        pass
    return False


NEXT_BUTTON_TEXTS = ("下一页", "下一张", "下页", "下一步", "下一节")
NEXT_CSS = (
    "[aria-label*='下一']",
    "[title*='下一']",
    "[aria-label*='next']",
    "[title*='next']",
    "button[class*='next']",
    "[class*='next-btn']",
    "[class*='nextPage']",
    "[class*='next-page']",
    "[class*='arrow-right']",
    "[class*='ArrowRight']",
)


class CoursewarePager:
    """课件翻页策略集合：按钮 → 图标 → 键盘 → 大纲缩略图 → 滚动。
    每个策略单独试探一次，哪招生效就锁定优先复用；失效再换下一招。"""

    KEYS = ("ArrowRight", "PageDown", "Space", "ArrowDown")

    def __init__(self, page, name=""):
        self.page = page
        self.name = name
        self.used = None
        self.key_idx = 0
        self.thumb_idx = 1

    def labels(self):
        return ("按钮", "图标", "键盘", "缩略图", "滚动")

    def run(self, label, want=None):
        fn = {
            "按钮": self._by_button,
            "图标": self._by_icon,
            "键盘": self._by_key,
            "缩略图": self._by_thumb,
            "滚动": self._by_scroll,
        }[label]
        if label == "缩略图":
            return fn(want)
        return fn()

    def _by_button(self):
        p = self.page
        for txt in NEXT_BUTTON_TEXTS:
            for getter in (p.get_by_role("button", name=re.compile(txt)),
                           p.get_by_role("link", name=re.compile(txt)),
                           p.get_by_text(txt, exact=False)):
                if _click_visible(getter):
                    return True
        return False

    def _by_icon(self):
        p = self.page
        for sel in NEXT_CSS:
            try:
                loc = p.locator(sel)
                cnt = min(loc.count(), 5)
            except Exception:
                continue
            for i in range(cnt):
                try:
                    el = loc.nth(i)
                    if el.is_visible():
                        el.click(timeout=2500)
                        return True
                except Exception:
                    continue
        return False

    def _by_key(self):
        p = self.page
        try:
            vp = p.viewport_size or {"width": 1280, "height": 800}
            # 点画面中部偏上：让幻灯片区域拿到焦点，同时避开左右工具栏
            p.mouse.click(vp["width"] * 0.5, vp["height"] * 0.45)
            p.wait_for_timeout(250)
        except Exception:
            pass
        key = self.KEYS[self.key_idx % len(self.KEYS)]
        self.key_idx += 1
        try:
            p.keyboard.press(key)
            return True
        except Exception:
            return False

    def _by_thumb(self, want=None):
        """点左侧大纲/缩略图里页码等于目标页的那一项（找不到就按序号顺推）"""
        p = self.page
        sel = (".page, [class*='thumb'], [class*='catalog'] li, "
               "[class*='outline'] li, [class*='slide-list'] li, [class*='menu'] li, "
               "ul li, ol li")
        try:
            loc = p.locator(sel)
            cnt = min(loc.count(), 300)
        except Exception:
            return False
        for i in range(cnt):
            try:
                el = loc.nth(i)
                if not el.is_visible():
                    continue
                txt = (el.inner_text() or "").strip()
                # 页码文本可能是 "1" / "第1页" / "01"，容忍少量前后缀
                m = re.match(r"^\D{0,2}(\d{1,4})\D{0,2}$", txt)
                if not m:
                    continue
                no = int(m.group(1))
                if want is not None and no != want:
                    continue
                if want is None and no <= self.thumb_idx:
                    continue
                el.click(timeout=2500)
                self.thumb_idx = no
                return True
            except Exception:
                continue
        return False

    def _by_scroll(self):
        p = self.page
        try:
            vp = p.viewport_size or {"width": 1280, "height": 800}
            p.mouse.move(vp["width"] * 0.5, vp["height"] * 0.6)
            p.mouse.wheel(0, int(vp["height"] * 0.8))
            return True
        except Exception:
            return False


def _page_video_state(page):
    try:
        return page.evaluate(
            """() => {
                const v = document.querySelector('video');
                if (!v) return null;
                return {paused: v.paused, ended: v.ended,
                        t: Math.round(v.currentTime), d: Math.round(v.duration || 0)};
            }"""
        )
    except Exception:
        return None


def _dwell_on_page(page, dwell, name):
    """停留在当前页：支持暂停、弹题作答；页面内嵌视频则等它播完再算停留时间"""
    video_waited = 0
    waited = 0.0
    while waited < dwell:
        if wait_if_paused(page, name):
            waited = 0.0
            continue
        page.wait_for_timeout(1000)
        waited += 1.0
        if check_quiz(page):
            handle_quiz(page)
            waited = 0.0
            continue
        st = _page_video_state(page)
        if st and not st["ended"] and (st["d"] == 0 or st["t"] < st["d"] - 1):
            # 课件里嵌了视频：先把视频放完（最多等 2 倍时长 + 60s），停留时间暂不计
            force_mute(page)
            try:
                page.evaluate(
                    """() => { const v = document.querySelector('video');
                        if (v) { v.muted = true; v.volume = 0; const p = v.play(); if (p) p.catch(()=>{}); } }"""
                )
            except Exception:
                pass
            video_waited += 1.0
            waited = max(0.0, waited - 1.0)
            if st["d"] and video_waited > st["d"] * 2 + 60:
                log(f"[{name}] 页内视频等待超时（{st['t']}/{st['d']}），继续翻页")
                return


def dump_courseware_diag(page, name):
    """翻页卡住时输出页面结构诊断，方便定位该课程课件的真实 DOM"""
    log(f"[{name}] 课件翻页未生效，输出页面诊断（便于后续适配）：")
    try:
        for t in _frames_of(page):
            txt = _frame_text(t, 300).replace("\n", " ")
            log(f"  frame: {str(getattr(t, 'url', ''))[:90]} | text: {txt[:160]}")
    except Exception:
        pass
    try:
        info = page.evaluate(
            """() => {
                const out = {iframe: document.querySelectorAll('iframe').length, els: []};
                const sel = 'button,[role=button],svg,[class*=page],[class*=slide],[class*=next],[class*=thumb]';
                document.querySelectorAll(sel).forEach(e => {
                    if (out.els.length >= 40) return;
                    const r = e.getBoundingClientRect();
                    if (r.width * r.height < 200) return;
                    out.els.push({
                        tag: e.tagName,
                        cls: (e.className || '').toString().slice(0, 36),
                        txt: ((e.innerText || '') + '').trim().slice(0, 16)
                    });
                });
                return out;
            }"""
        )
        log(f"  主文档 iframe 数量: {info.get('iframe')}")
        for b in info.get("els", [])[:25]:
            log(f"    元素 <{b['tag']}> class={b['cls']} text={b['txt']}")
    except Exception as e:
        log(f"  诊断信息获取失败: {str(e)[:120]}")


COURSEWARE_UNREAD_JS = """() => {
    const out = [];
    document.querySelectorAll('span.page').forEach(sp => {
        const slide = sp.closest('.swiper-slide');
        if (!slide) return;
        const flag = slide.querySelector('span.flag.noRead');
        if (flag && (flag.offsetWidth || flag.offsetHeight)) out.push(parseInt(sp.innerText.trim()));
    });
    return out.sort((a, b) => a - b);
}"""

COURSEWARE_ACTIVE_JS = """() => {
    const t = document.querySelector('.thumb-swiper-slide-active span.page')
           || document.querySelector('.thumb-swiper-slide-active .page');
    return t ? parseInt(t.innerText.trim()) : null;
}"""

TOTAL_PAGES_RE = re.compile(r"共\s*(\d+)\s*页")


def courseware_unread_pages(page):
    """大纲里仍带“未读”标记的页码（雨课堂用 span.flag.noRead 标记未读页）"""
    try:
        return page.evaluate(COURSEWARE_UNREAD_JS) or []
    except Exception:
        return []


def courseware_active_page(page):
    """大纲当前高亮的页码"""
    try:
        return page.evaluate(COURSEWARE_ACTIVE_JS)
    except Exception:
        return None


def courseware_total_pages(page):
    """总页数：优先“学习进度 x/y”的 y，其次大纲标题“共 N 页”"""
    cur, total = lesson_progress(page)
    if total:
        return total
    for txt in _read_texts(page, 800):
        m = TOTAL_PAGES_RE.search(txt)
        if m:
            return int(m.group(1))
    return None


def click_courseware_thumb(page, n):
    """点大纲缩略图里的第 n 页（缩略图在 swiper 里，先滚到可见再点）"""
    for scope in ("div.swiper-container.pptThumbsSwiper", "[class*='pptThumbsSwiper']"):
        try:
            thumbs = page.locator(scope)
            if thumbs.count() == 0:
                continue
            pg = thumbs.first.locator("span.page").filter(has_text=re.compile(rf"^{n}$"))
            if pg.count():
                pg.first.scroll_into_view_if_needed(timeout=3000)
                pg.first.click(timeout=4000)
                return True
        except Exception:
            continue
    return False


def wait_page_read(page, n, max_wait, name):
    """等到第 n 页的“未读”标记消失（= 前端确认已读）；返回 (是否消失, 耗时)"""
    start = time.time()
    min_hold = 2.0  # 最短停留：页面每页上报有节流，停留太短服务端会漏记
    while True:
        page.wait_for_timeout(400)
        elapsed = time.time() - start
        if wait_if_paused(page, name):
            start = time.time()
            continue
        if check_quiz(page):
            handle_quiz(page)
            start = time.time()
        gone = n not in courseware_unread_pages(page)
        if gone and elapsed < min_hold:
            page.wait_for_timeout(int((min_hold - elapsed) * 1000))
            gone = n not in courseware_unread_pages(page)
        if gone:
            return True, round(time.time() - start, 1)
        if elapsed >= max_wait:
            return False, round(elapsed, 1)


def courseware_outline_mode(page, dwell, name, total, unread):
    """大纲驱动翻页：按页码点缩略图 → 等“未读”消失 → 立刻下一页。
    dwell 在这里是最长等待（兜底），正常情况下每页只花 1~2 秒。"""
    cur, _ = lesson_progress(page)
    log(f"[{name}] 大纲驱动模式：共 {total} 页，未读 {len(unread)} 页，未读标记消失即翻页（最长等 {dwell}s/页）")

    missed = 0
    for n in unread:
        cur, _ = lesson_progress(page)
        if cur is not None and total and cur >= total:
            break
        if not click_courseware_thumb(page, n):
            log(f"[{name}] 点击大纲第 {n} 页失败，尝试键盘翻页")
            page.keyboard.press("ArrowRight")
        confirmed, cost = wait_page_read(page, n, dwell, name)
        if confirmed:
            cur, _ = lesson_progress(page)
            log(f"[{name}] 第 {n} 页已读确认（{cost}s）｜进度 {cur}/{total}")
        else:
            missed += 1
            log(f"[{name}] 第 {n} 页等 {cost}s 仍未消“未读”，先继续下一页（最后以学习进度为准）")

    # 收尾校验：前端消“未读”比服务端落库快，多等几轮，把掉队的页补上再收工
    stable = 0
    for attempt in range(1, 6):
        page.wait_for_timeout(8000)
        remain = courseware_unread_pages(page)
        cur, _ = lesson_progress(page)
        if not remain and total and cur is not None and cur >= total:
            stable += 1
            log(f"[{name}] 校验 {attempt}：未读 0 页、进度 {cur}/{total}")
            if stable >= 2:
                log(f"[{name}] 连续两次校验通过，课件完成")
                return True
            continue
        stable = 0
        if remain:
            log(f"[{name}] 校验 {attempt}：仍有 {len(remain)} 页未读 {remain[:8]}，补刷……")
            for n in remain:
                if not click_courseware_thumb(page, n):
                    page.keyboard.press("ArrowRight")
                confirmed, cost = wait_page_read(page, n, dwell, name)
                log(f"[{name}] 补刷第 {n} 页{'成功' if confirmed else '未确认'}（{cost}s）")
        else:
            log(f"[{name}] 校验 {attempt}：无未读标记，但进度 {cur}/{total} 未满，等服务端补齐")

    cur, _ = lesson_progress(page)
    remain = courseware_unread_pages(page)
    if total and cur is not None and cur >= total and not remain:
        log(f"[{name}] 学习进度已满 {cur}/{total}，课件完成")
        return True
    log(f"[{name}] 翻页结束：进度 {cur}/{total}，仍有 {len(remain)} 页未读（未确认的 {missed} 页）")
    return False


def _pager_mode(page, dwell, name, max_pages, st):
    """兜底模式：没有大纲/未读标记时，用按钮/图标/键盘/滚动逐步试探翻页"""
    total = st["total"]
    pager = CoursewarePager(page, name)
    labels = list(pager.labels())
    turns = 0
    cap = max_pages if max_pages > 0 else (total or 200)
    dead_rounds = 0
    guard = 0
    limit = (total or 200) * 2 + 60

    while guard < limit:
        guard += 1
        if wait_if_paused(page, name):
            st = courseware_state(page)
            continue
        if check_quiz(page):
            handle_quiz(page)
            st = courseware_state(page)
            continue

        cur, total = st["cur"], st["total"]
        if total and cur is not None and cur >= total:
            log(f"[{name}] 已到最后一页（学习进度 {cur}/{total}）")
            return True
        if turns >= cap:
            log(f"[{name}] 已达本次翻页上限 {cap} 页，停止")
            return total is not None and cur is not None and cur >= total

        before = st
        order = labels if not pager.used else [pager.used] + [l for l in labels if l != pager.used]
        unlocked = False
        for label in order:
            want = None
            if label == "缩略图":
                if before["cur"] is not None:
                    want = before["cur"] + 1
                elif before["pair"]:
                    want = before["pair"][0] + 1
            if not pager.run(label, want):
                continue
            page.wait_for_timeout(1200)
            after = courseware_state(page)
            if _state_advanced(before, after):
                st = after
                turns += 1
                dead_rounds = 0
                pager.used = label
                blocked = ""
                if after["cur"] is not None:
                    blocked = f"{after['cur']}/{after['total']}"
                elif after["pair"]:
                    blocked = f"{after['pair'][0]}/{after['pair'][1]}"
                log(f"[{name}] 翻到第 {turns + 1} 页（{label}）{('｜进度 ' + blocked) if blocked else ''}")
                _dwell_on_page(page, dwell, name)
                st = courseware_state(page)
                unlocked = True
                break
            log(f"[{name}] 策略“{label}”本次未翻动页面，换下一招")

        if unlocked:
            continue
        dead_rounds += 1
        if dead_rounds >= 2:
            if total is None and turns > 0:
                log(f"[{name}] 已翻到最后一页（页数未在学生端标注，未能确认学习进度），按完成处理")
                return True
            log(f"[{name}] 所有翻页策略连续 {dead_rounds} 轮都无效，本条目未确认完成")
            dump_courseware_diag(page, name)
            return False
        page.wait_for_timeout(1500)

    log(f"[{name}] 翻页循环达到上限（{limit} 次），停止")
    return False


def courseware_mode(page, dwell, name, max_pages=0):
    """课件（PPT/文档）学习模式。

    首选「大纲驱动」：点大纲缩略图翻页，当前页的“未读”标记一消失就翻下一页
    （服务端记阅读只要 1 秒左右，比固定停留快 5~8 倍）。
    读不到大纲/未读标记时退回按钮/键盘/滚动试探模式。"""
    log(f"[{name}] 进入课件学习模式（每页最长等 {dwell}s）")
    for txt in ("继续观看", "继续学习", "开始学习"):
        try:
            if _click_visible(page.get_by_role("button", name=re.compile(txt)), 3000):
                log(f"[{name}] 点击「{txt}」进入课件")
                page.wait_for_timeout(2500)
                break
        except Exception:
            pass

    page.wait_for_timeout(1200)
    total = courseware_total_pages(page)
    unread = courseware_unread_pages(page)
    cur, _ = lesson_progress(page)

    if total:
        log(f"[{name}] 课件共 {total} 页，学习进度 {cur}/{total}，未读 {len(unread)} 页")
    elif unread:
        log(f"[{name}] 未读到总页数，未读 {len(unread)} 页")
    else:
        log(f"[{name}] 未读到页码/未读标记，改用按钮/键盘翻页模式")

    if total and not unread and cur is not None and cur >= total:
        log(f"[{name}] 学习进度已满 {cur}/{total}，无需翻页")
        return True
    if unread:
        return courseware_outline_mode(page, dwell, name, total or len(unread), unread)

    return _pager_mode(page, dwell, name, max_pages, courseware_state(page))


def ppt_mode(page, dwell, name, max_pages=0):
    """兼容旧调用名：转调 courseware_mode"""
    return courseware_mode(page, dwell, name, max_pages=max_pages)


def force_mute(page):
    """强制静音页面所有 video（muted + 音量归零），防页面播放器重置静音状态"""
    try:
        page.evaluate(
            """() => {
                document.querySelectorAll('video').forEach(v => {
                    v.muted = true;
                    v.volume = 0;
                });
            }"""
        )
    except Exception:
        pass


PAUSE_FILE = os.path.join(BASE_DIR, "pause.flag")
RUN_LOCK = os.path.join(BASE_DIR, "run.lock")


def write_run_lock(kinds=(), parallel=1):
    """主进程写 run.lock（pid/启动时间/类型），控制台据此识别“外部启动的刷课任务”，
    即使脚本是从命令行启动的，控制台的开始/暂停/结束按钮也能接管它。"""
    if multiprocessing.current_process().name != "MainProcess":
        return
    info = {
        "pid": os.getpid(),
        "start": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "types": list(kinds),
        "parallel": parallel,
    }
    try:
        with open(RUN_LOCK, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
    except Exception:
        pass
    atexit.register(clear_run_lock)


def clear_run_lock():
    try:
        if os.path.exists(RUN_LOCK):
            os.remove(RUN_LOCK)
    except Exception:
        pass


def wait_if_paused(page, name=""):
    """GUI 点「暂停」后写 pause.flag：检测到则暂停视频/停住翻页，等「继续」后恢复"""
    if not os.path.exists(PAUSE_FILE):
        return False
    log(f"[{name}] ⏸ 收到暂停指令，已暂停（等待继续…）")
    paused_video = False
    try:
        if page.locator("video").count() > 0:
            page.evaluate(
                """() => {
                    document.querySelectorAll('video').forEach(v => { if (!v.paused) { v.pause(); } });
                }"""
            )
            paused_video = True
    except Exception:
        pass
    while os.path.exists(PAUSE_FILE):
        page.wait_for_timeout(500)
    log(f"[{name}] ▶ 收到继续指令，恢复")
    if paused_video:
        try:
            page.evaluate(
                """() => {
                    document.querySelectorAll('video').forEach(v => {
                        v.muted = true; v.volume = 0; const p = v.play(); if (p) p.catch(()=>{});
                    });
                }"""
            )
        except Exception:
            pass
    return True


def video_mode(page, name, quick=False):
    """视频播放模式：实时获取播放状态为主判定，加超时兜底（防异常卡死）
    quick=True 时：检测到视频可播放即返回（不完整播放，用于快速遍历验收）"""
    log(f"[{name}] 检测到视频，进入视频播放模式{'（快速模式）' if quick else ''}")
    try:
        cont = page.get_by_role("button", name="继续观看")
        if cont.count() and cont.first.is_visible():
            cont.first.click(timeout=3000)
            page.wait_for_timeout(2000)
    except Exception:
        pass

    def vid_state():
        return page.evaluate(
            """() => {
                const v = document.querySelector('video');
                if (!v) return null;
                return {paused: v.paused, ended: v.ended,
                        t: Math.round(v.currentTime), d: Math.round(v.duration || 0)};
            }"""
        )

    if quick:
        # 快速模式：等 video 出现且能播放，确认后立即切换下一条
        deadline = time.time() + 30
        while time.time() < deadline:
            if wait_if_paused(page, name):
                deadline = time.time() + 30
                continue
            st = vid_state()
            if st is not None:
                log(f"[{name}] 检测到视频（{st['t']}/{st['d']}）")
                if not st["paused"]:
                    log(f"[{name}] 视频可播放，切换到下一条（快速模式，不完整播放）")
                    return True
                if st["ended"]:
                    return True
            page.wait_for_timeout(3000)
        log(f"[{name}] 快速模式：30 秒内未检测到可播放视频，跳过")
        return False

    force_mute(page)
    page.evaluate(
        """() => {
            const v = document.querySelector('video');
            if (v) { v.muted = true; v.volume = 0; const p = v.play(); if (p) p.catch(()=>{}); }
        }"""
    )
    # 超时兜底：实时状态为主判定，超时用于防呆
    start_time = time.time()
    timeout_limit = 1800  # 拿不到时长时的默认兜底：30 分钟
    got_duration = False
    stale = 0
    last_t = -1
    while True:
        page.wait_for_timeout(10000)
        # 每轮轮询先检查 GUI 暂停指令
        if wait_if_paused(page, name):
            start_time = time.time()
            continue
        # 每次轮询强制静音，防播放器把静音状态重置回去
        force_mute(page)
        elapsed = time.time() - start_time
        if elapsed > timeout_limit:
            log(f"[{name}] 超过最长等待 {timeout_limit}s 仍未结束，放弃本条（未确认完成）")
            return False
        st = vid_state()
        if st is None:
            log(f"[{name}] 找不到 video 元素，稍后重试")
            if page.locator("video").count() == 0 and "video" in page.url:
                log(f"[{name}] video 元素消失，页面可能已跳转，视为完成。")
                return True
            continue
        if st["d"] and not got_duration:
            got_duration = True
            timeout_limit = st["d"] * 2 + 300
            log(f"[{name}] 视频时长 {st['d']}s，最长等待 {timeout_limit}s（超时兜底）")
        log(f"[{name}] 播放状态: paused={st['paused']} ended={st['ended']} {st['t']}/{st['d']}")
        if st["ended"] or (st["d"] and st["t"] >= st["d"] - 1):
            log(f"[{name}] 视频播放完成")
            return True
        if st["paused"]:
            log(f"[{name}] 视频暂停，尝试恢复播放")
            page.evaluate(
                """() => {
                    const v = document.querySelector('video');
                    if (v) { v.muted = true; v.volume = 0; const p = v.play(); if (p) p.catch(()=>{}); }
                }"""
            )
        if st["t"] == last_t:
            stale += 1
            if stale >= 3:
                log(f"[{name}] 播放进度长时间不变，尝试点“继续观看”/重新播放")
                try:
                    cont = page.get_by_role("button", name="继续观看")
                    if cont.count() and cont.first.is_visible():
                        cont.first.click(timeout=3000)
                except Exception:
                    pass
                force_mute(page)
                stale = 0
        else:
            stale = 0
            last_t = st["t"]


def has_courseware_markers(page):
    """页面是否有课件特征元素（幻灯片/页码/缩略图），用于区分“课件”和“视频型课件”"""
    try:
        n = page.evaluate(
            """() => {
                let c = 0;
                for (const el of document.querySelectorAll('[class*=slide],[class*=page],[class*=ppt],[class*=thumb]')) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 80 && r.height > 60) c++;
                }
                return c;
            }"""
        )
        return bool(n)
    except Exception:
        return False


def study_one(page, context, item, dwell, unfinished_url, quick=False):
    """刷单个待办条目（视频/课件），完成后返回未完成列表页"""
    name = item["name"]
    kind = item.get("kind") or normalize_item_type(item.get("type"))
    log(f"===== 开始学习: {name} | 类型={kind} | 状态={item['status']} =====")
    before_tabs = len(context.pages)
    try:
        loc = page.locator("li.unfinished-study__item").filter(has_text=name).first
        loc.click(timeout=8000)
    except Exception as e:
        log(f"点击条目失败: {e}")
        return False
    page.wait_for_timeout(5000)

    if len(context.pages) > before_tabs:
        page = context.pages[-1]
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(4000)

    # 学习页判断：视频是懒加载的（进入页面几秒后才挂 <video>），轮询最多 12 秒
    has_video = False
    for _ in range(8):
        try:
            if page.locator("video").count() > 0:
                has_video = True
                break
        except Exception:
            pass
        page.wait_for_timeout(1500)

    if kind == "课件":
        cur, total = lesson_progress(page)
        # 视频型课件（整页就一个视频、没有幻灯片/页码）：直接按视频刷，避免翻页策略空转
        if total is None and has_video and not has_courseware_markers(page):
            log(f"[{name}] 该课件条目实际是视频型，改走视频播放模式")
            ok = video_mode(page, name, quick=quick)
        else:
            ok = courseware_mode(page, dwell, name)
            if not ok and has_video:
                log(f"[{name}] 课件模式未完成但检测到视频，补一次视频播放")
                ok = video_mode(page, name, quick=quick)
    else:
        if has_video:
            ok = video_mode(page, name, quick=quick)
        else:
            ok = courseware_mode(page, dwell, name)
            if not ok:
                try:
                    if page.locator("video").count() > 0:
                        log(f"[{name}] 课件模式失败但检测到视频，改走视频播放模式")
                        ok = video_mode(page, name, quick=quick)
                except Exception:
                    pass

    # 返回未完成列表页（SPA 同页跳转，直接 goto 带 tab=unfinished 的 URL 最稳）
    try:
        page.goto(unfinished_url, timeout=60000, wait_until="domcontentloaded")
    except Exception as e:
        log(f"返回列表页失败: {e}")
    page.wait_for_timeout(3000)
    return ok


def open_course(page, entry):
    """从课程列表页点击进入课程，返回课程名（找不到卡片时关弹窗+刷新重试）"""
    course_name = course_name_of(entry["txt"])
    log(f"进入课程: {entry['txt'][:60]}")
    card = page.locator("div.lesson-cardS").filter(has_text=course_name).first
    for attempt in range(3):
        try:
            card.click(timeout=8000)
            page.wait_for_timeout(4000)
            return course_name
        except Exception as e:
            log(f"点击课程卡片失败（第 {attempt + 1} 次重试）: {str(e)[:120]}")
            try:
                dismiss_popups(page)
            except Exception:
                pass
            try:
                page.goto(INDEX_URL, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
            except Exception:
                pass
            try:
                click_tab(page, "我听的课")
                page.wait_for_timeout(2500)
            except Exception:
                pass
    raise RuntimeError(f"多次重试仍无法进入课程 {course_name}")


def study_course(page, context, entry, dwell, max_lessons, quick=False, kinds=TARGET_KINDS):
    """刷单门课程：进课程 -> 未完成 -> 逐个刷目标（视频/课件）。
    返回 (本次刷完数, 该课剩余目标数, 目标总数, 类型分布, 目标分类计数, 剩余分类计数)"""
    course_name = open_course(page, entry)

    log("点击“未完成”……")
    click_text(page, "未完成")
    page.wait_for_timeout(2500)
    unfinished_url = page.url
    log(f"未完成列表 URL: {unfinished_url}")

    # 解析待办（异步加载，0 条时重试；页面显示“暂无未完成”则直接跳过）
    all_items, targets, state = refresh_todo_list(page, kinds=kinds)
    # 分类计数按“全部目标类型”算，与本次只刷视频/只刷课件无关，
    # 否则只跑一类时会把另一类的剩余清零（导致误标「无任务」）
    kinds_all = count_target_kinds(all_items)
    targets_in_run = {}  # 本次要刷的（受 kinds 过滤）
    for it in targets:
        k = it.get("kind") or "其他"
        targets_in_run[k] = targets_in_run.get(k, 0) + 1
    kinds_in_targets = kinds_all
    kind_rem = dict(kinds_all)
    kind_txt = "、".join(f"{k}{v}条" for k, v in kinds_all.items()) or "无"
    run_txt = "、".join(f"{k}{v}条" for k, v in targets_in_run.items()) or "无"
    log(f"[{course_name}] 待办共 {len(all_items)} 条，视频/课件未完成 {sum(kinds_all.values())} 条"
        f"（{kind_txt}）；本次刷（{'/'.join(kinds)}）：{len(targets)} 条（{run_txt}）")
    # 未完成条目类型分布（视频/课件/作业/其他，按页面原始类型文字统计）
    type_cnt = {}
    for it in all_items:
        t = it.get("type", "其他") or "其他"
        type_cnt[t] = type_cnt.get(t, 0) + 1
    if type_cnt:
        dist = " · ".join(f"{k}×{v}" for k, v in type_cnt.items())
        log(f"[{course_name}] 未完成类型分布：{dist}")
    for i, t in enumerate(targets[:10]):
        log(f"  [{i}] {t['name']} ({t['status']})")
    if len(targets) > 10:
        log(f"  ... 共 {len(targets)} 条")

    if max_lessons > 0:
        targets = targets[: max_lessons]
        log(f"本次该课程仅刷前 {len(targets)} 条（--max-lessons）")

    if not targets:
        if state == "empty":
            log(f"[{course_name}] 暂无未完成待办，视为已刷完。")
            return 0, 0, 0, type_cnt, {}, {}
        if state == "fail":
            log(f"[{course_name}] 多次重试（5 次）后待办仍为空（多半是浏览器被关/网络断了），"
                f"本次不写进度记录，留待下次重新评估。")
            return 0, None, 0, type_cnt, {}, {}  # remaining=None → 调用方跳过写盘，避免把课程误标成“无任务”
        remaining_all = sum(kinds_all.values())
        if remaining_all:
            # 本次只跑了一类，而待办里剩下的都是另一类 —— 不能当成“已刷完”
            log(f"[{course_name}] 本次只刷（{'/'.join(kinds)}），"
                f"但待办里还有 {remaining_all} 条（{kind_txt}）未完成，本课跳过，留待刷另一类时处理。")
            return 0, remaining_all, remaining_all, type_cnt, kinds_all, dict(kinds_all)
        log(f"[{course_name}] 待办已加载但没有目标条目（视频/课件），视为已刷完（刷课跳过，下次检查重新评估）。")
        return 0, 0, 0, type_cnt, {}, {}  # remaining 归零：与 target=0 保持自洽（GUI 显示"无任务"）

    done = 0
    total = len(targets)
    visited = set()  # 快速模式下：不播放就不会从未完成消失，记已检测过的避免死循环

    if quick:
        # 快速模式 v2：纯列表遍历——连“点击视频”都跳过，只记录有哪些目标
        for i, t in enumerate(targets):
            if t["name"] in visited:
                continue
            visited.add(t["name"])
            done += 1
            log(f"[快速模式] ({i + 1}/{total}) 跳过点击，直接切换: {t['name']}")
        log(f"[{course_name}] 快速模式遍历完成：共检测 {done} 条目标（未点击、未播放）")
        all_rem = sum(kinds_all.values())
        return done, all_rem, all_rem, type_cnt, kinds_all, dict(kinds_all)  # remaining 保持原目标数（未实际刷）

    for i, t in enumerate(targets):
        if t["name"] in visited:
            log(f"跳过已处理过的条目: {t['name']}")
            continue
        log(f"--- {course_name} 进度 {i + 1}/{total} ---")
        try:
            ok = study_one(page, context, t, dwell, unfinished_url)
            if ok:
                done += 1
        except Exception as e:
            log(f"条目 {t['name']} 出现异常: {e}")
        # 重新加载未完成列表，更新状态（已完成的条目会消失）
        try:
            page.goto(unfinished_url, timeout=60000, wait_until="domcontentloaded")
            all_items, targets, _ = refresh_todo_list(page, kinds=kinds)
            # 分类剩余用「全部目标类型」统计，避免只刷一类时把另一类清零
            kind_rem = count_target_kinds(all_items) or kind_rem
            targets = [t for t in targets if t["name"] not in visited]
            if not targets:
                log(f"[{course_name}] 本次类型已无目标条目。")
                break
        except Exception:
            pass

    remaining_all = sum(kind_rem.values())
    return done, remaining_all, sum(kinds_all.values()), type_cnt, kinds_all, dict(kind_rem)


def win_toast(title, message):
    """Windows 10+ 系统通知（右下角 toast）。纯 PowerShell + WinRT，无需额外模块。"""
    try:
        import subprocess
        ps = r'''
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastText02"><text id="1"><![CDATA[TITLE]]></text><text id="2"><![CDATA[MSG]]></text></binding></visual></toast>')
$toast = New-Object Windows.UI.Notifications.ToastNotification($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('yuketang-auto').Show($toast)
'''
        title = title.replace("]]>", "]] >")
        message = message.replace("]]>", "]] >")
        ps = ps.replace("TITLE", title).replace("MSG", message)
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps], timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def notify_done(prog, total_done, course_count):
    """刷课结束弹窗（PySide6 深色风格，与控制台一致），显示在屏幕顶部居中，并发送 Windows 通知"""
    log("========== 刷课结束 ==========")
    try:
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtGui import QColor, QFont, QGuiApplication
        from PySide6.QtWidgets import (
            QApplication, QDialog, QHBoxLayout, QHeaderView, QLabel,
            QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
        )

        DONE_QSS = """
QDialog { background-color: #1e1f26; color: #e6e6e6; }
QLabel { color: #e6e6e6; }
QTableWidget {
    background-color: #262833; alternate-background-color: #2b2d3a;
    border: 1px solid #3a3d4d; border-radius: 8px;
    gridline-color: #333647; font-size: 13px;
}
QTableWidget::item { padding: 6px 4px; }
QHeaderView::section {
    background-color: #2f3242; color: #c9cdd8; border: none;
    border-bottom: 2px solid #3f4456; padding: 8px 4px; font-size: 13px; font-weight: bold;
}
QPushButton {
    background-color: #5b6ee1; color: white; border: none;
    border-radius: 6px; padding: 8px 22px; font-size: 13px; font-weight: 500;
}
QPushButton:hover { background-color: #6c7fe8; }
QPushButton:pressed { background-color: #4a5bc0; }
"""

        app = QApplication.instance() or QApplication([])
        app.setStyle("Fusion")
        app.setStyleSheet(DONE_QSS)

        dlg = QDialog()
        dlg.setWindowTitle("雨课堂刷课完成")
        dlg.setFixedSize(520, 420)
        # 弹窗显示在屏幕顶部居中（避免 Qt 默认摆到屏幕底部）
        try:
            screen = QGuiApplication.primaryScreen().availableGeometry()
            dlg.move(screen.x() + (screen.width() - 520) // 2, screen.y() + 24)
        except Exception:
            pass

        root = QVBoxLayout(dlg)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(10)

        title = QLabel("✓ 雨课堂刷课完成")
        title.setFont(QFont("Microsoft YaHei UI", 16, QFont.Weight.Bold))
        root.addWidget(title)

        sub = QLabel(f"本次处理 {course_count} 门课程，共刷完 {total_done} 个视频条目。浏览器已关闭。")
        sub.setStyleSheet("color: #9aa0b4; font-size: 12px;")
        sub.setWordWrap(True)
        root.addWidget(sub)

        table = QTableWidget(0, 2)
        table.setHorizontalHeaderLabels(["课程", "状态"])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        hh = table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)

        courses = prog.get("courses", {})
        table.setRowCount(len(courses))
        for row, (name, info) in enumerate(courses.items()):
            item_name = QTableWidgetItem(name)
            item_name.setForeground(QColor("#e6e6e6"))
            table.setItem(row, 0, item_name)

            status, st_color = "刷课中", "#d29922"
            if info.get("target", 0) == 0:
                status, st_color = "无任务", "#8a91a8"
            elif info.get("done"):
                status, st_color = "已刷完 ✓", "#2ea043"
            else:
                status = f"剩余 {info.get('remaining', 0)} 条"
            item_st = QTableWidgetItem(status)
            item_st.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_st.setForeground(QColor(st_color))
            table.setItem(row, 1, item_st)
        root.addWidget(table, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        ok_btn = QPushButton("确认")
        ok_btn.clicked.connect(dlg.accept)
        btns.addWidget(ok_btn)
        root.addLayout(btns)

        dlg.show()
        # 同步发一条 Windows 系统通知（右下角 toast）
        win_toast("雨课堂刷课完成", f"共处理 {course_count} 门课程，刷完 {total_done} 个视频条目。")
        # 60 秒无操作自动关闭：防止弹窗没人点「确认」时进程一直挂着，
        # GUI 靠 proc.poll() 判断结束会一直显示"刷课中"
        QTimer.singleShot(60000, dlg.accept)
        app.exec()
    except Exception as e:
        # 极端环境下 PySide6 不可用则退回纯文字记录
        log(f"完成弹窗显示失败（已降级为日志提示）: {str(e)[:100]}")


def run_parallel_worker(profile_dir, dwell, max_lessons, quick, headless, q, kinds=TARGET_KINDS):
    """并行 worker（独立进程）：从共享任务池动态领取课程，刷完领下一个；
    没有可刷课程时自动关闭本窗口退出。结果 (updates, total_done) 放入队列 q。"""
    worker_tag = os.path.basename(profile_dir)
    updates = {}
    total_done = 0
    try:
        with sync_playwright() as p:
            log(f"【{worker_tag}】正在启动 Edge（独立窗口，静音）……")
            context = p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                channel="msedge",
                headless=headless,
                viewport={"width": 1280, "height": 800},
                args=["--mute-audio"],  # 浏览器内核级静音，页面 JS 无法取消
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(INDEX_URL, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            if not wait_login(page):
                log(f"【{worker_tag}】登录检查未通过（请确认登录态），关闭本窗口。")
                context.close()
                q.put((updates, total_done))
                return
            log("点击“我听的课”……")
            click_tab(page, "我听的课")
            page.wait_for_timeout(3000)

            # 动态领取循环：刷完一门 → 领下一门 → 没有可刷的 → 关窗口退出
            while True:
                task = claim_next_task(worker_tag)
                if task is None:
                    log(f"【{worker_tag}】没有可刷的课程了，关闭本窗口退出。")
                    break
                course_name = task["name"]
                log(f"########## 【{worker_tag}】领取课程：{course_name} ##########")
                entry = {"txt": task["txt"]}
                try:
                    done, remaining, n_target, type_dist, kind_cnt, kind_rem = study_course(
                        page, context, entry, dwell, max_lessons, quick=quick, kinds=kinds)
                except Exception as e:
                    log(f"[{course_name}] 本门课程出现异常，跳过: {str(e)[:150]}")
                    go_to_my_courses(page)
                    continue
                total_done += done

                # 更新进度记录（只更新本 worker 的课程，带锁合并写盘）
                if remaining is None:
                    log(f"[{course_name}] 待办列表加载失败，本次跳过；进度记录保持不变。")
                    go_to_my_courses(page)
                    continue
                info = updates.get(course_name)
                if info is None:
                    # 第一次写这课：读 progress.json 里已存的总数，避免把"总数"覆盖成当次未完成数
                    saved_total = 0
                    try:
                        saved_total = (load_progress().get("courses", {}).get(course_name, {}) or {}).get("total", 0) or 0
                    except Exception:
                        pass
                    info = {"total": saved_total, "remaining": 0, "done": False}
                # 总目标只增不减：总数固定为历史最大值（刷掉的部分通过 remaining 体现，不会让总数变小）
                info["total"] = max(info.get("total", 0), n_target)
                info["target"] = n_target  # 该课的目标条数（视频+课件，0 = 无任务）
                info["remaining"] = remaining
                info["done"] = (remaining == 0)
                info["type_dist"] = type_dist
                if kind_cnt:
                    info["target_split"] = kind_cnt  # 目标里视频/课件各几条
                # 分类进度（视频/课件各自的 总数/剩余），供控制台显示「视频完成度 / 课件完成度 / 未刷」
                old_split = info.get("kind_split") or {}
                keys = set(old_split) | set(kind_cnt or {}) | set(kind_rem or {})
                if keys:
                    split = {}
                    for k in keys:
                        old = old_split.get(k) or {}
                        t = max(int(old.get("total") or 0), int((kind_cnt or {}).get(k, 0) or 0))
                        if remaining == 0:
                            r = 0  # 已全部完成（或本课已无未完成目标）
                        elif k in (kind_rem or {}):
                            r = int(kind_rem[k])
                        else:
                            r = 0  # 该类型在本次统计里没有未完成条目 → 已刷完
                        if t:
                            r = min(r, t)
                        split[k] = {"total": t, "remaining": r}
                    info["kind_split"] = split
                    log(f"[{course_name}] 分类进度："
                        + " · ".join(f"{k} {v['total'] - v['remaining']}/{v['total']}" for k, v in split.items()))
                updates[course_name] = info
                save_progress_merge(updates)
                if n_target == 0:
                    status_txt = "无任务（无视频/课件目标）"
                elif info["done"]:
                    status_txt = "已刷完 ✓"
                else:
                    status_txt = f"剩余 {remaining} 条"
                log(f"[{course_name}] 本次刷完 {done} 条，{status_txt}（进度已记录到 progress.json）")

                # 切回“我听的课”列表，准备领下一门
                go_to_my_courses(page)

            log(f"【{worker_tag}】本窗口刷课结束，正在关闭浏览器……")
            context.close()
    except Exception as e:
        log(f"【{worker_tag}】异常退出: {str(e)[:200]}")
    q.put((updates, total_done))


def init_task_pool(entries, skip_no_task=False, kinds=TARGET_KINDS):
    """把课程列表写入共享任务池（tasks.json），供并行 worker 动态领取。
    skip_no_task=True（刷课模式）：跳过已确认无任务（target=0）的课程；
    未检查过的课程正常进池，刷课时会现场检查一次（5 次重试仍空则按无任务记录，之后跳过）。
    直到下次检查状态重新评估。"""
    no_task_names = set()
    if skip_no_task:
        try:
            prog = load_progress()
            courses = prog.get("courses") or {}
            saved_types = prog.get("types")
        except Exception:
            courses, saved_types = {}, None
        if saved_types and list(saved_types) != list(kinds):
            log(f"刷课类型已变更（{list(saved_types)} → {list(kinds)}），本次重新评估所有课程，不跳过旧记录的“无任务”课程。")
        else:
            no_task_names = {n for n, i in courses.items() if i.get("target", 0) == 0}
        if no_task_names:
            log(f"跳过 {len(no_task_names)} 门已确认无任务的课程（下次检查状态会重新评估）：")
            for n in sorted(no_task_names):
                log(f"  - {n}")
    tasks = []
    for e in entries:
        name = course_name_of(e["txt"])
        if name in no_task_names:
            continue
        tasks.append(
            {"name": name, "txt": e["txt"],
             "status": "pending", "owner": ""}
        )
    try:
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump({"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                       "tasks": tasks}, f, ensure_ascii=False, indent=2)
        log(f"任务池已初始化：{len(tasks)} 门课程待领取。")
    except Exception as e:
        log(f"初始化任务池失败: {e}")


def claim_next_task(worker_tag):
    """带锁领取一个没人刷的课程；没有可刷课程时返回 None（该 worker 应关闭退出）"""
    lock_path = TASKS_FILE + ".lock"
    fd = None
    for _ in range(50):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            time.sleep(0.2)
    if fd is None:
        # 拿不到锁放弃本次领取：避免无锁裸写导致同一课程被两个窗口重复领
        log("任务领取：锁占用超时（10 秒），本窗口本次放弃领取。")
        return None
    try:
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        for task in data["tasks"]:
            if task["status"] == "pending":
                task["status"] = "running"
                task["owner"] = worker_tag
                try:
                    with open(TASKS_FILE, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    log(f"任务领取：写 tasks.json 失败（{e}），放弃该任务，避免重复领取。")
                    return None
                return task
        return None
    finally:
        if fd is not None:
            os.close(fd)
            try:
                os.remove(lock_path)
            except Exception:
                pass


def ensure_parallel_profiles(parallel):
    """并行模式：把主登录态 profile 复制出 edge_profile_2/3（cookie 一起复制，无需重新扫码）"""
    for i in range(2, parallel + 1):
        dst = f"{PROFILE_DIR}_{i}"
        if os.path.exists(dst):
            log(f"【profile】{os.path.basename(dst)} 已存在，直接复用。")
            continue
        log(f"【profile】正在复制登录态 → {os.path.basename(dst)} ……")
        try:
            time.sleep(1)  # 等主 profile 的 Edge 完全释放
            shutil.copytree(
                PROFILE_DIR, dst,
                ignore=shutil.ignore_patterns("Singleton*", "*.lock", "lockfile"),
            )
        except Exception as e:
            log(f"【profile】复制失败: {str(e)[:150]}")


def cleanup_stale_locks():
    """启动时清理残留的 .lock 文件（GUI「结束」强杀 worker 的瞬间可能残留，
    不清理会导致之后所有写盘都拿不到锁、降级为放弃写入）"""
    for p in (PROGRESS_FILE + ".lock", TASKS_FILE + ".lock"):
        try:
            if os.path.exists(p):
                os.remove(p)
                log(f"已清理残留锁文件: {os.path.basename(p)}")
        except Exception:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="雨课堂自动刷课（跨课程，视频+课件，只刷未完成条目）")
    ap.add_argument("--course", default=None, help="课程名（模糊匹配），只刷该课程；不填则刷所有课程")
    ap.add_argument("--dwell", type=int, default=10, help="课件每页停留秒数（默认 10，视频用不到）")
    ap.add_argument("--max-lessons", type=int, default=0, help="每门课程最多刷几个条目（0=全部）")
    ap.add_argument("--headless", action="store_true", help="无头模式，不显示浏览器窗口")
    ap.add_argument("--all-semesters", action="store_true", help="刷全部学期的课程（默认只刷最新学期）")
    ap.add_argument("--quick", action="store_true", help="检查模式：遍历目标列表，不点击不播放")
    ap.add_argument("--parallel", type=int, default=1, choices=[1, 2, 3],
                    help="同时刷课的课程数（最多 3，默认 1）")
    ap.add_argument("--types", default="video,courseware",
                    help="刷哪些类型：video,courseware（默认两者都刷；只写 video 则只刷视频）")
    ap.add_argument("--status", action="store_true", help="只查看进度记录，不刷课")
    if argv is None:
        argv = sys.argv[1:]
    argv = [a for a in argv if a != "--auto-cli"]  # 单文件模式：剔除自身 CLI 标记
    args = ap.parse_args(argv)

    raw_types = [t.strip().lower() for t in (args.types or "").split(",") if t.strip()]
    kinds = []
    if any(t in ("video", "视频") for t in raw_types):
        kinds.append("视频")
    if any(t in ("courseware", "ppt", "课件", "document") for t in raw_types):
        kinds.append("课件")
    if not kinds:
        kinds = list(TARGET_KINDS)
    kinds = tuple(kinds)

    log("=== 雨课堂自动刷课脚本 v7 启动 ===")
    cleanup_stale_locks()
    write_run_lock(kinds, args.parallel)

    prog = load_progress()
    if args.status:
        print_progress_summary(prog)
        return

    # 1) 用主 profile 启动 Edge 解析课程列表
    with sync_playwright() as p:
        log("正在启动 Edge（独立 profile，可最小化窗口，脚本照常运行）……")
        context = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            channel="msedge",
            headless=args.headless,
            viewport={"width": 1280, "height": 800},
            args=["--mute-audio"],  # 浏览器内核级静音，页面 JS 无法取消
        )
        page = context.pages[0] if context.pages else context.new_page()

        page.goto(INDEX_URL, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        if not wait_login(page):
            context.close()
            return

        log("点击“我听的课”……")
        click_tab(page, "我听的课")
        page.wait_for_timeout(3000)

        entries = find_course_entries(page)
        for attempt in range(3):
            if entries:
                break
            log(f"未识别到课程卡片（第 {attempt + 1} 次重试）……")
            dismiss_popups(page)
            click_tab(page, "我听的课")
            page.wait_for_timeout(3000)
            entries = find_course_entries(page)
        if not entries:
            log("多次重试仍未识别到课程卡片，请检查登录状态或页面是否正常。")
            log("当前页面文本：" + page_text(page, 800).replace("\n", " "))
            context.close()
            return

        if args.course:
            matched = [e for e in entries if args.course in e["txt"]]
            if not matched:
                log(f"未找到课程“{args.course}”，可选课程：")
                for e in entries:
                    log(f"  {e['txt'][:60]}")
                context.close()
                return
            entries = matched
        elif not args.all_semesters:
            semester, entries = filter_latest_semester(entries)
            log(f"默认只刷最新学期【{semester}】的课程（--all-semesters 可刷全部学期）")

        log(f"共识别到 {len(entries)} 门课程：")
        for e in entries:
            log(f"  - {e['txt'][:60]}")
        print_progress_summary(prog)
        # with 退出即关闭解析用的 Edge

    # 2) 并行准备：复制 profile + 初始化共享任务池（worker 动态领取，互不重复）
    parallel = max(1, min(3, args.parallel))
    if args.course:
        parallel = 1  # 指定单门课程时不需要并行
    if args.quick:
        parallel = 1  # 检查模式固定单窗口，不受并行数影响
    if parallel > 1:
        ensure_parallel_profiles(parallel)
    init_task_pool(entries, skip_no_task=not args.quick, kinds=kinds)
    log(f"{'并行' if parallel > 1 else '串行'}刷课：{parallel} 个窗口，动态领取课程（同一课程不会被重复领取）。")
    log(f"本次刷课类型：{' + '.join(kinds)}（--types 可调整）")

    # 3) 启动 worker（并行 = 多进程动态领取；串行 = 单进程同样动态领取）
    mp_ctx = multiprocessing.get_context("spawn")
    q = mp_ctx.Queue()
    jobs = []
    for i in range(parallel):
        profile_dir = PROFILE_DIR if i == 0 else f"{PROFILE_DIR}_{i + 1}"
        jobs.append(mp_ctx.Process(
            target=run_parallel_worker,
            args=(profile_dir, args.dwell, args.max_lessons, args.quick, args.headless, q, kinds),
            daemon=True,  # 主进程退出时子进程自动终止，防止结束/停止后残留孤儿进程
        ))
    for j in jobs:
        j.start()
    results = [q.get() for _ in jobs]
    for j in jobs:
        j.join()

    # 4) 汇总进度并弹窗
    total_done = 0
    final = load_progress()
    for updates, done in results:
        final["courses"].update(updates)
        total_done += done
    final["types"] = list(kinds)  # 记录本次刷课类型，类型变更时下次不再沿用“无任务”跳过
    save_progress(final)
    notify_done(final, total_done, len(entries))


if __name__ == "__main__":
    # PyInstaller 打包后 multiprocessing spawn 需要此行，否则并行子进程会重复执行主程序
    multiprocessing.freeze_support()
    try:
        main(sys.argv[1:] if len(sys.argv) > 1 else None)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，脚本退出")
    except Exception as e:
        # 控制台点「结束」会直接关掉 Edge/进程，Playwright 抛 TargetClosedError，属正常收尾
        if type(e).__name__ in ("TargetClosedError", "Error") and "closed" in str(e).lower():
            log("浏览器已被关闭（手动结束），脚本退出")
        else:
            raise
    finally:
        clear_run_lock()
