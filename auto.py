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
import ctypes
import datetime
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
    prog = load_progress()
    try:
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
    return True


def wait_login(page, timeout_min=5):
    log("检查登录状态……")
    start = time.time()
    while True:
        if is_logged_in(page):
            log("已登录，继续。")
            return True
        if time.time() - start > timeout_min * 60:
            log("等待登录超时，请重新运行脚本并尽快登录。")
            return False
        log("浏览器已打开登录页，请在浏览器中扫码/输入账号登录（等待中……）")
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
                    if (!/(秋|春)/.test(txt) || !/班/.test(txt)) continue;
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


def parse_todo_items(page):
    """解析“未完成”待办列表：返回 (全部条目, 目标条目[视频+计分+未完成])"""
    try:
        items = page.evaluate(
            """() => {
                const out = [];
                document.querySelectorAll('li.unfinished-study__item').forEach(li => {
                    const lines = (li.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
                    if (!lines.length) return;
                    const type = lines[0];
                    const scored = lines.some(l => l.includes('计分'));
                    const status = lines.find(l => /未开始|进行中|已完成|未完成/.test(l)) || '未开始';
                    const name = lines.find(l => /项目|课/.test(l) && l.length > 2) || (lines[2] || '');
                    out.push({type: type, scored: scored, status: status, name: name});
                });
                return out;
            }"""
        )
    except Exception as e:
        log(f"parse_todo_items 失败: {e}")
        return [], []
    items = items or []
    targets = [it for it in items
               if it["type"] == "视频" and it["scored"] and "未" in it["status"]]
    return items, targets


def refresh_todo_list(page, retries=5, wait=3000):
    """确认待办列表状态并解析。返回 (all_items, targets, state)
    state: 'ok'=列表正常 | 'empty'=页面显示“暂无未完成” | 'fail'=多次重试仍加载不出来"""
    all_items, targets = parse_todo_items(page)
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
        all_items, targets = parse_todo_items(page)
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


def lesson_progress(page):
    """读取学习页“学习进度：x/y”（课件类）"""
    try:
        m = re.search(r"学习进度[：:]\s*(\d+)/(\d+)", page_text(page, 1500))
        if m:
            return int(m[1]), int(m[2])
    except Exception:
        pass
    return None, None


def ppt_mode(page, dwell, name):
    """课件（图片 PPT）翻页模式：按大纲页码逐页停留"""
    pages = page.locator(".page")
    total = pages.count()
    log(f"[{name}] PPT 课件，共 {total} 页，每页停留 {dwell} 秒")
    if total == 0:
        log("未找到页码元素(.page)，无法翻页")
        return False
    try:
        cont = page.get_by_role("button", name="继续观看")
        if cont.count() and cont.first.is_visible():
            cont.first.click(timeout=3000)
            page.wait_for_timeout(3000)
    except Exception:
        pass
    for i in range(total):
        try:
            pg = pages.nth(i)
            if not pg.is_visible():
                pg.scroll_into_view_if_needed(timeout=3000)
            pg.click(timeout=5000)
        except Exception as e:
            log(f"点击第 {i + 1} 页失败: {e}")
            continue
        page.wait_for_timeout(1500)
        txt = page_text(page, 800).replace("\n", " ").strip()
        log(f"[{name}] 第 {i + 1}/{total} 页（停留 {dwell} 秒）文字: {txt[:120]}")
        if check_quiz(page):
            handle_quiz(page)
        waited = 0
        while waited < dwell:
            if wait_if_paused(page, name):
                waited = 0
                continue
            page.wait_for_timeout(2000)
            waited += 2
            if check_quiz(page):
                handle_quiz(page)
            a, b = lesson_progress(page)
            if a is not None and b and a >= b:
                log(f"[{name}] 进度已满 {a}/{b}，提前结束翻页")
                return True
    a, b = lesson_progress(page)
    if a is not None and b:
        log(f"[{name}] 翻页完成，学习进度：{a}/{b}")
        return a >= b
    log(f"[{name}] 翻页完成，未能确认进度，视为完成。")
    return True


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
                    const v = document.querySelector('video');
                    if (v && !v.paused) { v.pause(); }
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
                    const v = document.querySelector('video');
                    if (v) { v.muted = true; v.volume = 0; const p = v.play(); if (p) p.catch(()=>{}); }
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


def study_one(page, context, item, dwell, unfinished_url, quick=False):
    """刷单个待办条目（视频/课件），完成后返回未完成列表页"""
    name = item["name"]
    log(f"===== 开始学习: {name} | 类型={item['type']} | 状态={item['status']} =====")
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

    # 学习页判断：有 video 则视频模式，否则按课件翻页
    has_video = False
    try:
        has_video = page.locator("video").count() > 0
    except Exception:
        pass

    if has_video:
        ok = video_mode(page, name, quick=quick)
    else:
        ok = ppt_mode(page, dwell, name)

    # 返回未完成列表页（SPA 同页跳转，直接 goto 带 tab=unfinished 的 URL 最稳）
    try:
        page.goto(unfinished_url, timeout=60000, wait_until="domcontentloaded")
    except Exception as e:
        log(f"返回列表页失败: {e}")
    page.wait_for_timeout(3000)
    return ok


def open_course(page, entry):
    """从课程列表页点击进入课程，返回课程名（找不到卡片时关弹窗+刷新重试）"""
    course_name = entry["txt"].split(" ")[0]
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


def study_course(page, context, entry, dwell, max_lessons, quick=False):
    """刷单门课程：进课程 -> 未完成 -> 逐个刷目标。返回 (本次刷完数, 该课剩余目标数)"""
    course_name = open_course(page, entry)

    log("点击“未完成”……")
    click_text(page, "未完成")
    page.wait_for_timeout(2500)
    unfinished_url = page.url
    log(f"未完成列表 URL: {unfinished_url}")

    # 解析待办（异步加载，0 条时重试；页面显示“暂无未完成”则直接跳过）
    all_items, targets, state = refresh_todo_list(page)
    log(f"[{course_name}] 待办共 {len(all_items)} 条，目标（视频+计分+未完成）：{len(targets)} 条")
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
            return 0, 0, 0
        if state == "fail":
            log(f"[{course_name}] 多次重试后待办仍为空，跳过该课程（下次重跑会自动再试）。")
            return 0, None, None  # None = 加载失败/未知，不更新进度记录
        log(f"[{course_name}] 待办已加载但没有目标视频，视为已刷完。")
        return 0, len(all_items), 0

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
        return done, total, total  # remaining 保持原目标数（未实际刷，进度不算减少）

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
            _, targets, _ = refresh_todo_list(page)
            targets = [t for t in targets if t["name"] not in visited]
            if not targets:
                log(f"[{course_name}] 列表已无目标条目，全部完成。")
                break
        except Exception:
            pass

    return done, len(targets), total


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
        from PySide6.QtCore import Qt
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
        app.exec()
    except Exception as e:
        # 极端环境下 PySide6 不可用则退回纯文字记录
        log(f"完成弹窗显示失败（已降级为日志提示）: {str(e)[:100]}")


def run_parallel_worker(profile_dir, dwell, max_lessons, quick, headless, q):
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
                    done, remaining, n_target = study_course(page, context, entry, dwell, max_lessons,
                                                             quick=quick)
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
                info = updates.get(course_name, {"total": 0, "remaining": 0, "done": False})
                if info.get("total", 0) == 0:
                    info["total"] = n_target  # 总目标 = 视频目标条数（快速模式不翻倍）
                info["target"] = n_target  # 该课的视频目标条数（0 = 无视频任务）
                info["remaining"] = remaining
                info["done"] = (remaining == 0)
                updates[course_name] = info
                save_progress_merge(updates)
                if n_target == 0:
                    status_txt = "无任务（无视频目标）"
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


def init_task_pool(entries):
    """把课程列表写入共享任务池（tasks.json），供并行 worker 动态领取"""
    tasks = [
        {"name": e["txt"].split(" ")[0], "txt": e["txt"],
         "status": "pending", "owner": ""}
        for e in entries
    ]
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
                except Exception:
                    pass
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


def main(argv=None):
    ap = argparse.ArgumentParser(description="雨课堂自动刷课（跨课程，视频+计分+未完成）")
    ap.add_argument("--course", default=None, help="课程名（模糊匹配），只刷该课程；不填则刷所有课程")
    ap.add_argument("--dwell", type=int, default=10, help="课件每页停留秒数（默认 10，视频用不到）")
    ap.add_argument("--max-lessons", type=int, default=0, help="每门课程最多刷几个条目（0=全部）")
    ap.add_argument("--headless", action="store_true", help="无头模式，不显示浏览器窗口")
    ap.add_argument("--all-semesters", action="store_true", help="刷全部学期的课程（默认只刷最新学期）")
    ap.add_argument("--quick", action="store_true", help="检查模式：遍历目标列表，不点击不播放")
    ap.add_argument("--parallel", type=int, default=1, choices=[1, 2, 3],
                    help="同时刷课的课程数（最多 3，默认 1）")
    ap.add_argument("--status", action="store_true", help="只查看进度记录，不刷课")
    if argv is None:
        argv = sys.argv[1:]
    argv = [a for a in argv if a != "--auto-cli"]  # 单文件模式：剔除自身 CLI 标记
    args = ap.parse_args(argv)

    log("=== 雨课堂自动刷课脚本 v7 启动 ===")

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
    init_task_pool(entries)
    log(f"{'并行' if parallel > 1 else '串行'}刷课：{parallel} 个窗口，动态领取课程（同一课程不会被重复领取）。")

    # 3) 启动 worker（并行 = 多进程动态领取；串行 = 单进程同样动态领取）
    mp_ctx = multiprocessing.get_context("spawn")
    q = mp_ctx.Queue()
    jobs = []
    for i in range(parallel):
        profile_dir = PROFILE_DIR if i == 0 else f"{PROFILE_DIR}_{i + 1}"
        jobs.append(mp_ctx.Process(
            target=run_parallel_worker,
            args=(profile_dir, args.dwell, args.max_lessons, args.quick, args.headless, q),
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
    save_progress(final)
    notify_done(final, total_done, len(entries))


if __name__ == "__main__":
    # PyInstaller 打包后 multiprocessing spawn 需要此行，否则并行子进程会重复执行主程序
    multiprocessing.freeze_support()
    main(sys.argv[1:] if len(sys.argv) > 1 else None)
