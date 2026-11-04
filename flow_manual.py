# -*- coding: utf-8 -*-
"""
flow_manual.py — QQ音乐金币四任务的最新流程文件(由用户逐步带跑构建)。

原则:
    - 完全独立, 不 import flow_step.py(旧文件2000+行, 不再维护)。
    - 本文件自带全部底层基础函数(adb/截图/点击/返回/左滑/前台检测/等待/日志)和
      已经实测确认过的坐标常量, 是唯一要维护的小文件。
    - 每一步"点哪里/点几下/停几秒"按用户逐步确认后固化, 每确认一步再追加一个步骤函数。

用法:
    python flow_manual.py            # 跑 main() 里已确认的步骤

当前已确认步骤:
    [第1步] 金币中心 -> 点「看视频」按钮进广告
    (后续步骤待用户逐步确认后追加)
"""
import os
import random
import re
import subprocess
import time

from PIL import Image, ImageOps, ImageEnhance
import numpy as np
import pytesseract

# ================= 基础环境 =================
ADB = r"D:\桌面\xdf\Agent工程与具身智能\安卓手机控制\platform-tools\platform-tools\adb.exe"
QQMUSIC = "com.tencent.qqmusic"
TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"  # 本地 Tesseract OCR(金币中心是WebView, uiautomator拿不到文字)

# ================= 已确认的坐标(相对 0~1000) =================
COIN_VIDEO_BTN_REL = (833, 685)    # 金币中心: "看视频领金币"右侧橙色"看视频"按钮
COIN_BTN_REGION_PX = (810, 1560, 972, 1704)  # 顶部「看视频」按钮区域(像素), 完成态判断用: 该区域还在=按钮还在=没跳广告
COIN_NO_AD_MAX = 3                 # 看视频领金币完成态: 点看视频连续3次没跳广告则判完成(用户2026-11-04定)
COIN_BTN_DIFF_THRESHOLD = 30.0     # 看视频按钮区域像素差异阈值: >阈值=按钮区域变化(进广告), <阈值=按钮还在(没跳广告)
DOOR_CTA_REL = (500, 895)          # 门页: 底部蓝色"点击浏览15秒"(京东型, y≈2147)
DOOR_CTA_CANDIDATES = [            # 门页 CTA 有几种位置, 按顺序试, 每次试完校验页面是否变化
    (500, 895),                    # 京东型: "点击浏览15秒" 蓝色文字 (y≈2147)
    (500, 836),                    # 微信/公众号型: "去微信看看" 蓝色实心按钮 (y≈2007)
]
AD_GIVEUP_REL = (500, 641)         # 挽留窗"放弃奖励" (实测 y≈1538; 旧常量 670 会点偏)
AD_WEBVIEW_X_REL = (150, 65)       # 广告落地页: 顶部左侧"✕" (京东热卖实测 162,155)
AD_WEBVIEW_X2_REL = (129, 58)      # 备用: 老广告列表页的"✕" (实测 139,140)
AD_DOOR_X_REL = (922, 110)         # 门页(恭喜已获得奖励): 右上角"✕" (实测 999,257)
GIVEUP_BTN_REL = (500, 649)        # 任务奖励弹窗: 底部"放弃福利"按钮 (实测 2026-11-04, OCR中心)
INCENTIVE_BTN_REL = (500, 519)     # 金币中心激励弹窗: 橙色按钮(备用, 主用扫描)
INCENTIVE_TPL = "tpl_incentive_fixed.png"  # 激励按钮固定文案模板(不含金额"浏览5秒再领", 跨金额稳定, 裁自2026-11-04)
INCENTIVE_TPL_X = 372              # 激励按钮固定 x 起点(像素; 弹窗居中, 按钮横向固定)
INCENTIVE_TPL_MAX_DIST = 15.0      # 模板匹配阈值(激励固定文案模板真匹配差异≈0.0)
INCENTIVE_TPL_Y_RANGE = (1200, 1280)   # 激励按钮命中中心 y 区间(实测 y≈1242; 位置不符=跨匹配排除)
NEXT_TPL = "tpl_next_btn.png"       # 「观看下一条」按钮模板(双模板区分用, 裁自2026-11-04)
NEXT_TPL_X = 390                    # 「观看下一条」按钮固定 x 起点(像素; 中心≈544)
NEXT_TPL_MAX_DIST = 15.0            # 观看下一条模板匹配阈值
NEXT_TPL_Y_RANGE = (1360, 1440)     # 「观看下一条」按钮命中中心 y 区间(实测 y≈1402; 位置不符=跨匹配排除)
# 单开门/纯白说明型门页(2026-11-04 新增): 点入口后进纯白页, 无CTA可点, 停满时长平台发奖励
#   出口候选链 copy 旧代码 DOOR_EXIT_PLAIN(黑条「退出」优先, 实测599,222)
DOOR_PLAIN_STAY_RANGE = (31.0, 32.0)            # 纯白页/单开门停留时长(用户确认31~32s, 时长会变需关注)
DOOR_PLAIN_EXIT_CANDIDATES = [(556, 93), (622, 89), (438, 93), (500, 659)]

# ================= 已确认的停留时长(秒) =================
DOOR_STAY_RANGE = (2.0, 5.0)       # 门页停留(随机), 之后点 CTA
AD_STAY_RANGE = (16.0, 18.5)       # 跳转后在广告页停留 —— 广告任务本身要求的 15 秒, 不能缩
INCENTIVE_STAY_RANGE = (5.0, 6.0)  # 激励浏览页停留: 页面写的是"浏览5秒", 5 秒是硬下限

TMP = "_fs_tmp.png"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ================= adb 原语 =================
def _adb(*args, timeout=15):
    return subprocess.run([ADB, *args], capture_output=True, timeout=timeout).stdout.decode("utf-8", "ignore")


def screencap(path):
    """截图存文件(用 exec-out, 避免 adb pull 把 PNG 弄坏)。"""
    with open(path, "wb") as f:
        subprocess.run([ADB, "exec-out", "screencap", "-p"], stdout=f, timeout=20)


def screen_size(path):
    with Image.open(path) as im:
        return im.size


def tap(x, y):
    _adb("shell", "input", "tap", str(int(x)), str(int(y)))
    log(f"  点击 ({int(x)},{int(y)})")


def tap_human(x, y, jitter=8):
    """更真实的点击: 坐标抖动 + DOWN/MOVE/UP 三段(带抖动轨迹与 80~180ms 按压时长)。
    比 `input tap` 好: tap 只有一对 DOWN/UP, 且坐标每次完全一致, 是最容易被识别的脚本特征。
    三段用**一次 shell** 发完(而不是三次 adb), 否则手势会被拆散、偶发不生效。"""
    x = int(x) + random.randint(-jitter, jitter)
    y = int(y) + random.randint(-jitter, jitter)
    hold = random.uniform(0.08, 0.18)
    dx1, dy1 = random.randint(-3, 3), random.randint(-3, 3)
    dx2, dy2 = random.randint(-2, 2), random.randint(-2, 2)
    cmd = (f"input motionevent DOWN {x} {y}; sleep {hold * 0.4:.2f}; "
           f"input motionevent MOVE {x + dx1} {y + dy1}; sleep {hold * 0.35:.2f}; "
           f"input motionevent UP {x + dx2} {y + dy2}")
    _adb("shell", cmd)
    log(f"  点击(拟人) ({x},{y}) 按压{hold * 1000:.0f}ms")


def tap_verified(x, y, shot_before="_fs_vb.png", wait=1.6):
    """拟人点击 -> 校验页面是否变化; 没变化就退回普通 tap 再试一次(防止 motionevent 不被接受)。
    返回是否发生变化。"""
    screencap(shot_before)
    base = _fp(shot_before)
    tap_human(x, y)
    time.sleep(wait)
    screencap("_fs_va.png")
    d = _dist(base, _fp("_fs_va.png"))
    if d >= 6.0:
        log(f"  拟人点击生效 (差异{d:.0f})")
        return True
    log(f"  拟人点击没反应 (差异{d:.1f}), 退回普通 tap 重试")
    tap(x, y)
    time.sleep(wait)
    screencap("_fs_va2.png")
    d2 = _dist(base, _fp("_fs_va2.png"))
    ok = d2 >= 6.0
    log(f"  普通 tap {'生效' if ok else '仍无反应'} (差异{d2:.0f})")
    return ok


def back():
    _adb("shell", "keyevent", "4")
    log("  按 BACK")


def swipe_left(ow=None, oh=None, duration=250):
    """左滑(系统返回/翻页手势)。实测比系统 BACK 好用: 广告落地页的✕是假关,
    BACK 有时被广告页吃掉, 左滑=系统返回更可靠。ow/oh 为空时从截屏取。"""
    if ow is None or oh is None:
        screencap(TMP)
        with Image.open(TMP) as im:
            ow, oh = im.size
    _adb("shell", "input", "swipe", str(int(ow * 0.99)), str(int(oh * 0.5)),
         str(int(ow * 0.15)), str(int(oh * 0.5)), str(duration))
    log("  左滑")


def swipe_up(ow=None, oh=None, duration=300):
    """向上滚动(手指从下往上, 内容上移/看下面内容)。金币中心找下面的任务入口用。"""
    if ow is None or oh is None:
        screencap(TMP)
        with Image.open(TMP) as im:
            ow, oh = im.size
    _adb("shell", "input", "swipe", str(int(ow * 0.5)), str(int(oh * 0.85)),
         str(int(ow * 0.5)), str(int(oh * 0.15)), str(duration))
    log("  向上滚动一屏")


def swipe_down(ow=None, oh=None, duration=300):
    """向下滚动(手指从上往下, 内容下移/回看上面内容)。与 swipe_up 配套还原页面。"""
    if ow is None or oh is None:
        screencap(TMP)
        with Image.open(TMP) as im:
            ow, oh = im.size
    _adb("shell", "input", "swipe", str(int(ow * 0.5)), str(int(oh * 0.15)),
         str(int(ow * 0.5)), str(int(oh * 0.85)), str(duration))
    log("  向下滚动一屏")


def scroll_to_top(reps=3):
    """金币中心任务列表滚回顶部: 连续向下滚(手指从上往下=回看上面) reps 次。
    从滚动一屏后的高额金币入口位置回顶部, 3 次足够(每次一屏, 顶部多余滚动自动到边界)。"""
    for _ in range(reps):
        swipe_down()
        time.sleep(0.8)
    log("  已滚回顶部")


def focus_package():
    out = _adb("shell", "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
    m = re.search(r"([a-zA-Z][\w.]+)/[\w.$]+", out)
    return m.group(1) if m else ""


def _fp(path):
    """缩图灰度指纹, 用于粗判"页面有没有明显变化"。整屏比对会被视频动画污染, 只作粗判。"""
    with Image.open(path) as im:
        return im.convert("L").resize((60, 130)).tobytes()


def _dist(a, b):
    return sum(abs(p - q) for p, q in zip(a, b)) / (60 * 130)


def wait_change(before_path, timeout=4.0, interval=0.5, th=6.0):
    """轮询等页面变化。返回 (是否变化, 用时秒, 差异值, 变化时刻time.time())"""
    base = _fp(before_path)
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(interval)
        screencap(TMP)
        d = _dist(base, _fp(TMP))
        if d >= th:
            return True, time.time() - t0, d, time.time()
    return False, timeout, 0.0, 0.0


# ================= 第1步: 金币中心 -> 点「看视频」进广告 =================
def step1_watch_video():
    """金币中心点「看视频领金币」右侧橙色「看视频」按钮。
    按钮坐标来自 COIN_VIDEO_BTN_REL = (833, 685)(相对, 千分比),
    换算成当前屏真实像素后点击。点完不动作, 由调用方决定下一步。"""
    screencap("_fs_step1_before.png")
    ow, oh = screen_size("_fs_step1_before.png")
    x = int(COIN_VIDEO_BTN_REL[0] / 1000 * ow)
    y = int(COIN_VIDEO_BTN_REL[1] / 1000 * oh)
    log(f"[第1步] 屏幕 {ow}x{oh}, 点「看视频」入口 ({x},{y})")
    tap(x, y)
    log("[第1步] 已点击")


def _btn_region_diff(ref_shot, cur_shot):
    """比较两张截图在顶部「看视频」按钮区域(COIN_BTN_REGION_PX)的像素差异均值。
    用于完成态判断: 点看视频后若该区域仍与金币中心参考一致(差异小)=看视频按钮还在=没跳广告;
    区域变化大=进入广告页(按钮消失)。"""
    r1 = np.asarray(Image.open(ref_shot).convert("RGB").crop(COIN_BTN_REGION_PX), dtype=np.int16)
    r2 = np.asarray(Image.open(cur_shot).convert("RGB").crop(COIN_BTN_REGION_PX), dtype=np.int16)
    return float(np.abs(r1 - r2).mean())


# ================= 跳转广告页出门方案 =================
def ad_exit_flow(already_cta=False):
    """跳转广告页出门方案(点完「看视频」进入广告后的全部动作, 由用户逐步确认):
    1) 停 2~3 秒随机 -> 点门页 CTA(DOOR_CTA_REL = 500,895)
    2) 停 17~18 秒随机 -> 左滑 3 次(每次间隔 0.8~1.3 秒随机; BACK 无效, 用户实测左滑)
    3) 停 1~1.5 秒随机 -> 点右上角「✕」(AD_DOOR_X_REL = 922,110)
    4) 点任务奖励弹窗底部「放弃福利」(GIVEUP_BTN_REL = 500,649)

    注意: 点「看视频」这个入口动作不封装在这里, 由任务入口单独负责。
    already_cta=True: 调用方已点过门页 CTA, 跳过第 1) 步只做出门段(停17~18s->左滑->✕->放弃福利)。"""
    # --- 1) 停2~3s -> 点门页 CTA (already_cta 时跳过) ---
    if not already_cta:
        stay = random.uniform(2.0, 3.0)
        log(f"[出门] 停留 {stay:.2f}s")
        time.sleep(stay)
        screencap("_fs_cta.png")
        ow, oh = screen_size("_fs_cta.png")
        x = int(DOOR_CTA_REL[0] / 1000 * ow)
        y = int(DOOR_CTA_REL[1] / 1000 * oh)
        log(f"[出门] 屏幕 {ow}x{oh}, 点门页 CTA ({x},{y})")
        tap(x, y)

    # --- 2) 停17~18s -> 左滑 3 次 ---
    stay = random.uniform(17.0, 18.0)
    log(f"[出门] 停留 {stay:.2f}s")
    time.sleep(stay)
    for i in range(1, 4):
        log(f"[出门] 左滑 第{i}次")
        swipe_left()
        time.sleep(random.uniform(0.8, 1.3))

    # --- 3) 停1~1.5s -> 点右上角「✕」 ---
    stay = random.uniform(1.0, 1.5)
    log(f"[出门] 停留 {stay:.2f}s")
    time.sleep(stay)
    screencap("_fs_x.png")
    ow, oh = screen_size("_fs_x.png")
    x = int(AD_DOOR_X_REL[0] / 1000 * ow)
    y = int(AD_DOOR_X_REL[1] / 1000 * oh)
    log(f"[出门] 屏幕 {ow}x{oh}, 点右上角「✕」 ({x},{y})")
    tap(x, y)

    # --- 4) 点弹窗「放弃福利」 ---
    screencap("_fs_giveup.png")
    ow, oh = screen_size("_fs_giveup.png")
    x = int(GIVEUP_BTN_REL[0] / 1000 * ow)
    y = int(GIVEUP_BTN_REL[1] / 1000 * oh)
    log(f"[出门] 屏幕 {ow}x{oh}, 点「放弃福利」 ({x},{y})")
    tap(x, y)
    log("[出门] 跳转广告页出门方案执行完毕")


# ================= 页面判断 =================
def is_plain_door(shot="_fs_plain.png", use_existing=False):
    """判断当前是不是"纯白说明型门页"(单开门广告, 整页白底 + 灰字, 例如"点击允许打开微信/查看内容获取奖励")。
    这种门页没有 CTA 按钮, 停满时长平台直接发奖励。判据用色彩饱和度(实测很干净):
        素材型门页(京东大图): 饱和像素 ~34%, 亮度 106
        纯白说明型:          饱和像素 0%,  亮度 253"""
    if not use_existing:
        screencap(shot)
    a = np.asarray(Image.open(shot).convert("RGB")).astype(np.int16)
    oh, ow, _ = a.shape
    reg = a[int(0.28 * oh):int(0.62 * oh), int(0.05 * ow):int(0.95 * ow), :]
    mx = reg.max(axis=2)
    mn = reg.min(axis=2)
    sat = (mx - mn) / np.maximum(mx, 1)
    colorful = float((sat > 0.25).mean())
    bright = float(reg.mean())
    ok = bright > 235 and colorful < 0.03
    log(f"[纯白页判定] 亮度 {bright:.0f} 彩色占比 {colorful:.1%} -> "
        f"{'纯白说明型门页(单开门)' if ok else '非纯白'}")
    return ok


def read_plain_door_text():
    """用 uiautomator dump 读广告页文字, 匹配「观看广告XX秒后/已观看广告XX秒，可获得奖励」。
    返回 (秒数int, 已完成bool, text, 文本bounds盒, 点击x, 点击y) 或 None。
    用户明确: 单开门/双开门判定要用文字, 不用纯白页颜色(2026-11-04)。"""
    subprocess.run([ADB, "shell", "uiautomator", "dump", "/sdcard/ui.xml"], capture_output=True)
    rr = subprocess.run([ADB, "shell", "cat", "/sdcard/ui.xml"], capture_output=True, text=True)
    xml = rr.stdout
    pat = re.compile(r'text="([^"]+)"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')
    texts = [m.group(1).strip() for m in pat.finditer(xml) if m.group(1).strip()]
    log(f"[文字门页] dump 读到 {len(texts)} 段文字: " + "; ".join(texts[:30]))
    for m in pat.finditer(xml):
        t = m.group(1).strip()
        # 单开门文字门页可能显示「观看广告XX秒」或「观看视频XX秒」(2026-11-04 实测出现"观看视频15秒")
        if ("观看广告" in t or "观看视频" in t) and "可获得奖励" in t:
            sec_m = re.search(r"观看(?:广告|视频)\s*(\d+)\s*秒", t)
            sec = int(sec_m.group(1)) if sec_m else None
            done = t.startswith("已观看")
            x1, y1, x2, y2 = (int(m.group(i)) for i in range(2, 6))
            # 点「退出」(文本右端偏右, 竖线|后)
            tx = x2 - int((x2 - x1) * 0.06)
            ty = (y1 + y2) // 2
            log(f"[文字门页] 读到: {t} | 秒数={sec} 完成={done} | 退出按钮约({tx},{ty})")
            return sec, done, t, (x1, y1, x2, y2), tx, ty
    log("[文字门页] dump 未读到「观看广告XX秒,可获得奖励」文字 -> 尝试 OCR 兜底")
    return _read_plain_door_ocr()


def _read_plain_door_ocr():
    """dump 读不到时的 OCR 兜底(2026-11-04): 「观看广告XX秒+跳过」型单开门变体。
    判单开门 = 顶部广告栏识别到「可获得奖励」+「跳过/退出」结构, 秒数不强求(用户2026-11-04:
    出门统一停留31-32秒, 不依赖秒数识别)。返回 (秒数int|None, 已完成bool, text, box, 退出x, 退出y) 或 None。"""
    shot = "_fs_door_ocr.png"
    # dump 读不到后立即截图太早, 倒计时文字还没加载 -> 等1-2秒再截图 OCR
    time.sleep(random.uniform(1.0, 2.0))
    screencap(shot)
    words = ocr_words(shot)
    log(f"[文字门页OCR] 截图={shot} 识别到 {len(words)} 个词: " +
        "; ".join(f"'{t}'@({cx},{cy})" for t, x1, y1, x2, y2, cx, cy in words))
    top_words = [w for w in words if w[2] < 400]  # 顶部广告栏
    # 退出/跳过按钮(顶部; 「跳过」到时间变「退出」同位置, OCR可能把「跳过」拆成「跳」+「过」)
    skip_words = [w for w in top_words if any(k in w[0] for k in ("跳过", "退出", "跳", "过"))]
    exit_pos = None
    if skip_words:
        exit_pos = (int(sum(w[5] for w in skip_words) / len(skip_words)),
                    int(sum(w[6] for w in skip_words) / len(skip_words)))
    log(f"[文字门页OCR] 退出/跳过按钮定位: {exit_pos} (候选{len(skip_words)}个)")
    # psm7 整行识别顶部广告栏(提高整行识别率)
    img = Image.open(shot).convert("RGB")
    top_img = img.crop((0, 0, img.width, int(img.height * 0.35)))
    top_img = top_img.resize((top_img.width * 2, top_img.height * 2), Image.LANCZOS)
    top_img = ImageOps.grayscale(top_img)
    top_img = ImageEnhance.Contrast(top_img).enhance(1.5)
    line = pytesseract.image_to_string(top_img, lang="chi_sim", config="--psm 7").strip()
    log(f"[文字门页OCR] 顶部psm7整行: '{line}'")
    # 单开门判定: 顶部识别到「可获得奖励」+「跳过/退出」结构(秒数不强求)
    has_reward = ("可获得奖励" in line) or any("可获得奖励" in w[0] for w in top_words)
    has_skip = (exit_pos is not None) or ("跳过" in line or "退出" in line)
    if not (has_reward and has_skip):
        log("[文字门页OCR] 无「可获得奖励+跳过」单开门结构 -> 非文字门页")
        return None
    # 秒数: 尝试识别(可选; 出门统一停31-32秒, 识别不出返回 None)
    sec = None
    m = re.search(r"观看(?:广告|视频|广|证|告)\s*(\d+)\s*秒", line)
    if m:
        sec = int(m.group(1))
    done = ("已观看" in line) or any("已观看" in w[0] for w in top_words)
    box = (0, 0, img.width, int(img.height * 0.35))
    tx, ty = exit_pos if exit_pos else (695, 213)
    log(f"[文字门页OCR] 单开门结构命中: 秒数={sec} 完成={done} 退出=({tx},{ty})")
    return sec, done, line, box, tx, ty


def _plain_need_stay(sec):
    """广告要求秒数归档(带区间容错): 一进去可能倒计时变少(30变29),
    28-30 都按 30 算, 14-15 按 15, 9-10 按 10。确保停留到广告要求时长。"""
    if sec >= 25:   # 25-30 都停30秒(等1-2秒截图倒计时已走掉, 用户2026-11-04)
        return 30.0
    if sec >= 12:   # 12-15 停15秒(同理放宽)
        return 15.0
    if sec >= 8:    # 8-10 停10秒(同理放宽)
        return 10.0
    return float(sec)


def plain_door_exit_flow(info):
    """单开门/纯白说明型门页出门(文字判断版, 2026-11-04):
    接收 door_entry_flow 已判定的 info(sec, done, text, box, tx, ty), 不再重新判断
    (同一广告页 OCR 两次结果不稳定, 重新判断会产生「判单开门后又判非单开门」冲突)。
       - 已完成(「已观看广告XX秒」): 直接点「退出」
       - 进行中: 逐秒轮询读完成态, 读到「已观看」后等1-2秒确认广告真实播完再点退出
         (用户2026-11-04: 不判断停留秒数, 读到「已观看/退出」就点退出; dump 完成文案可能
         早于广告真实播完, 故读到后再等1-2秒确认; OCR 不稳定返回 None 时继续轮询不放弃)。"""
    if info is None:
        log("[文字门页] 不是文字门页, 不做出门")
        return False
    sec, done, text, box, tx, ty = info
    if not done:
        # 逐秒轮询读完成态, 读到「已观看」后等1-2秒确认再点退出(用户2026-11-04)
        deadline = time.time() + 45  # 上限45秒, 避免死循环
        while time.time() < deadline:
            time.sleep(1.0)
            info2 = read_plain_door_text()
            if info2 is not None and info2[1]:
                confirm = random.uniform(1.0, 2.0)
                log(f"[文字门页] 读到完成态({info2[2]}), 再等 {confirm:.1f}s 确认广告真实播完")
                time.sleep(confirm)
                info3 = read_plain_door_text()
                if info3 is not None and info3[1]:
                    sec, done, text, box, tx, ty = info3
                    break
                log("[文字门页] 完成态未稳定, 继续轮询")
            # info2 为 None(OCR 不稳定)时继续轮询, 不放弃出门(2026-11-04)
        if not done:
            log(f"[文字门页] {45}秒内未读到稳定完成态, 兜底点退出")
    log(f"[文字门页] 点「退出」({tx},{ty})")
    tap(tx, ty)
    time.sleep(1.5)
    return True


def door_entry_flow():
    """看视频/观看下一条 进入广告页后的统一分流(2026-11-04, 用户设计):
      单开门(文字门页「观看XX秒,可获得奖励｜退出」) -> plain_door_exit_flow(停留到秒数+点退出)
      双开门(有CTA按钮) -> 点CTA -> 判断跳转:
          跳外部(应用商店/浏览器) -> ad_exit_flow(already_cta=True, 跳过点CTA)
          未跳转(仍在QQ音乐)     -> 停住待命"""
    # 1) 先文字判断是否单开门(「观看XX秒」文字门页)
    info = read_plain_door_text()
    if info is not None:
        log("[进门分流] 单开门文字门页 -> 走单开门出门")
        plain_door_exit_flow(info)
        return "plain"
    # 2) 非单开门: 先判断是否真广告门页(区分红包雨等非广告, 用户2026-11-04)
    if not _detect_ad_cta():
        log("[进门分流] 非广告门页(红包雨等) -> 不点CTA, 判没刷广告")
        return "not_ad"
    # 3) 双开门: 点门页 CTA
    log("[进门分流] 广告门页(双开门) -> 点门页 CTA")
    screencap("_fs_cta.png")
    ow, oh = screen_size("_fs_cta.png")
    x = int(DOOR_CTA_REL[0] / 1000 * ow)
    y = int(DOOR_CTA_REL[1] / 1000 * oh)
    log(f"[进门分流] 屏幕 {ow}x{oh}, 点门页 CTA ({x},{y})")
    tap(x, y)
    time.sleep(3.0)
    if is_external_jump():
        log("[进门分流] 已跳外部(应用商店/浏览器) -> 走跳转出门")
        ad_exit_flow(already_cta=True)
        return "external"
    log("[进门分流] 仍在QQ音乐(未跳转) -> 停16-17秒 -> 左滑 -> 点右上角X(2026-11-04, 用户手动验证)")
    stay = random.uniform(16.0, 17.0)
    log(f"[进门分流] 未跳转, 停留 {stay:.2f}s")
    time.sleep(stay)
    log("[进门分流] 左滑")
    swipe_left()
    time.sleep(random.uniform(1.0, 1.5))
    # 点右上角「✕」(同 ad_exit_flow, AD_DOOR_X_REL=922,110 -> 像素995,264)
    screencap("_fs_x.png")
    ow, oh = screen_size("_fs_x.png")
    x = int(AD_DOOR_X_REL[0] / 1000 * ow)
    y = int(AD_DOOR_X_REL[1] / 1000 * oh)
    log(f"[进门分流] 屏幕 {ow}x{oh}, 点右上角「✕」 ({x},{y})")
    tap(x, y)
    time.sleep(1.0)
    return "normal"


def _detect_ad_cta():
    """用 OCR 屏幕内容判断当前是否广告门页(双开门CTA页)(2026-11-04):
    WebView 广告页 uiautomator dump 读到的是金币中心(拿不到广告文字), 所以不依赖 dump,
    直接截图 OCR 看当前屏幕是不是广告页。返回 True=广告门页(双开门), False=非广告页。
    标志: 「可获得奖励」(金币中心任务列表无此文案) 或 「广告+正在/加载」(广告正在加载中)。
    OCR 常拆词(「点击后浏览15秒可获得奖励」→拆单字), 所以用词组合检测而非整词。"""
    shot = "_fs_ad_cta.png"
    screencap(shot)
    words = ocr_words(shot)
    # 1) 「可获得奖励」: 整词 或 「获得」+「奖励」同行组合
    has_reward = any(("可获得奖励" in w[0]) or (("获得" in w[0]) and ("奖励" in w[0])) for w in words)
    if not has_reward:
        for w in words:
            t, y1 = w[0], w[2]
            if "获得" in t or "奖励" in t:
                for w2 in words:
                    t2, y2 = w2[0], w2[2]
                    if abs(y2 - y1) < 60 and ((("获得" in t) and ("奖励" in t2)) or (("奖励" in t) and ("获得" in t2))):
                        has_reward = True
                        break
                if has_reward:
                    break
    # 2) 「广告正在加载中」: 同时有「广告」词 和「正在/加载」词
    has_ad_word = any("广告" in w[0] for w in words)
    has_loading = any(("加载" in w[0]) or ("正在" in w[0]) for w in words)
    ok = has_reward or (has_ad_word and has_loading)
    log(f"[广告门页] OCR: 可获得奖励={has_reward} 广告词={has_ad_word} 加载词={has_loading} -> "
        f"{'广告门页(双开门)' if ok else '非广告页'}")
    return ok


def find_popup(shot="_fs_popup.png"):
    """区分金币中心弹窗是「激励弹窗」还是「观看下一条」。
    双模板 + y 位置校验: 每个模板只在它自己的预期按钮位置区间内才算命中,
    跨匹配(把激励按钮当观看下一条/反之, 命中中心y在错误位置)直接排除。
    返回 ("incentive"|"next", cx, cy) 或 None。
    原理: 两弹窗按钮同为橙色(单模板会互相误判), 但文字不同且按钮 y 位置不同
    (激励 y≈1242, 观看下一条 y≈1402, 差约155px)。此前只取差异最小者,
    会把"观看下一条模板在激励按钮位置的跨匹配"(差异可能反而更小)误判为观看下一条。"""
    screencap(shot)
    a = np.asarray(Image.open(shot).convert("L")).astype(np.int16)
    oh, ow = a.shape

    def scan(tpl, x0):
        th, tw = tpl.shape
        lo, hi = max(0, 1000), min(oh - th - 5, 1560)
        bd, by = 999.0, -1
        for y in range(lo, hi):
            d = float(np.abs(a[y:y + th, x0:x0 + tw] - tpl).mean())
            if d < bd:
                bd, by = d, y
        return bd, by, th, tw

    cands = []
    bd, by, th, tw = scan(np.asarray(Image.open(INCENTIVE_TPL).convert("L")).astype(np.int16), INCENTIVE_TPL_X)
    cy = by + th // 2
    in_y = INCENTIVE_TPL_Y_RANGE[0] <= cy <= INCENTIVE_TPL_Y_RANGE[1]
    log(f"[弹窗] 激励模板差异 {bd:.1f} 命中中心y={cy} {'在区间✓' if in_y else '位置不符✗'}")
    if in_y and bd <= INCENTIVE_TPL_MAX_DIST:
        cands.append(("incentive", bd, by, th, tw))
    if os.path.exists(NEXT_TPL):
        bd2, by2, th2, tw2 = scan(np.asarray(Image.open(NEXT_TPL).convert("L")).astype(np.int16), NEXT_TPL_X)
        cy2 = by2 + th2 // 2
        in_y2 = NEXT_TPL_Y_RANGE[0] <= cy2 <= NEXT_TPL_Y_RANGE[1]
        log(f"[弹窗] 观看下一条模板差异 {bd2:.1f} 命中中心y={cy2} {'在区间✓' if in_y2 else '位置不符✗'}")
        if in_y2 and bd2 <= NEXT_TPL_MAX_DIST:
            cands.append(("next", bd2, by2, th2, tw2))
    if not cands:
        log("[弹窗] 两者都不命中 -> 无激励/观看下一条弹窗")
        return None
    kind, d, by, th, tw = min(cands, key=lambda c: c[1])
    x0 = INCENTIVE_TPL_X if kind == "incentive" else NEXT_TPL_X
    cx, cy = int(x0 + tw / 2), by + th // 2
    log(f"[弹窗] 判定为 {'激励弹窗' if kind == 'incentive' else '观看下一条'} ({cx},{cy}) 差异 {d:.1f}")
    return kind, cx, cy


def find_incentive(shot="_fs_scan_inc.png"):
    """模板匹配找金币中心激励弹窗的橙色按钮, 返回中心 (cx,cy) 或 None。
    为什么固定 x: 激励弹窗居中, 按钮横向固定(x0≈372), 只有纵向随弹窗高度变, 所以固定 x 只扫 y。
    注意: 按钮文字"浏览5秒再领XXX金币"的金额(XXX)会变, 若金额变化导致差异超阈值需裁剪不含金额的模板。"""
    screencap(shot)
    a = np.asarray(Image.open(shot).convert("L")).astype(np.int16)
    tpl = np.asarray(Image.open(INCENTIVE_TPL).convert("L")).astype(np.int16)
    oh, ow = a.shape
    th, tw = tpl.shape
    x0 = INCENTIVE_TPL_X
    lo, hi = max(0, 1000), min(oh - th - 5, 1560)   # 弹窗按钮纵向范围
    best_d, best_y = 999.0, -1
    for y in range(lo, hi):
        d = float(np.abs(a[y:y + th, x0:x0 + tw] - tpl).mean())
        if d < best_d:
            best_d, best_y = d, y
    log(f"[激励检测] 模板最小差异 {best_d:.1f} @ y={best_y}")
    if best_y < 0 or best_d > INCENTIVE_TPL_MAX_DIST:
        return None
    return int(x0 + tw / 2), best_y + th // 2


def is_external_jump():
    """判断当前是否已跳转到外部应用(应用商店/浏览器等)。
    前台包 != QQ音乐 即认为跳转(QQ音乐的防脚本操作会把激励/广告带到应用商店)。
    返回 True = 已跳外部(走跳转出门), False = 仍在QQ音乐内(走普通流程)。"""
    pkg = focus_package()
    jumped = bool(pkg and pkg != QQMUSIC)
    log(f"[判断] 当前前台={pkg or '未知'} -> {'已跳外部' if jumped else '仍在QQ音乐'}")
    return jumped


# ================= Tesseract OCR 文字识别(金币中心是 WebView, uiautomator dump 拿不到文字) =================
def ocr_words(shot):
    """用本地 Tesseract OCR 识别截图, 返回 [(text, x1,y1,x2,y2, cx,cy), ...]。
    金币中心任务列表是 WebView, dump 拿不到文字, 用 Tesseract(chi_sim) 识别+取词坐标。
    预处理: 放大2x + 灰度 + 对比度增强(实测原图识别不出橙色按钮上的「领金币」,
    2x 后能识别出「领」+「金币」, 2026-11-04)。坐标按 scale 还原。"""
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    img = Image.open(shot).convert("RGB")
    scale = 2
    big = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    gray = ImageOps.grayscale(big)
    gray = ImageEnhance.Contrast(gray).enhance(1.5)
    data = pytesseract.image_to_data(gray, lang="chi_sim",
                                     output_type=pytesseract.Output.DICT)
    words = []
    n = len(data["text"])
    for i in range(n):
        t = (data["text"][i] or "").strip()
        if not t:
            continue
        x, y = data["left"][i] // scale, data["top"][i] // scale
        w, h = data["width"][i] // scale, data["height"][i] // scale
        words.append((t, x, y, x + w, y + h, x + w // 2, y + h // 2))
    return words


def find_watch_video_btn(shot):
    """OCR 找「看视频得高额金币」任务右侧的「看视频」按钮, 返回中心 (cx,cy) 或 None。
    页面有两个「看视频」: 标题「看视频得高额金币」在左侧(x<500), 按钮在右侧(x>800)。
    按钮可能被 OCR 切成一词「看视频」或两词「看」+「视频」, 都处理。"""
    words = ocr_words(shot)
    # 情况1: 单个词「看视频」且在右侧
    for t, x1, y1, x2, y2, cx, cy in words:
        if ("看视频" in t and cx > 800) or (t == "看" and cx > 800):
            # 若是「看」, 找紧随其后的「视频」合并
            if t == "看":
                for t2, a1, b1, a2, b2, c2x, c2y in words:
                    if t2 == "视频" and abs(b1 - y1) < 60 and a1 > x1:
                        return (x1 + a2) // 2, (y1 + b2) // 2
            return cx, cy
    return None


def find_tomorrow_text(shot):
    """OCR 找「明日再来」(任务完成态, 看视频按钮变灰点击无反应), 返回中心或 None。"""
    for t, x1, y1, x2, y2, cx, cy in ocr_words(shot):
        if "明日" in t or "再来" in t:
            return cx, cy
    return None


def find_watch_coin_btn(shot, max_y=900):
    """OCR 找金币中心顶部【看视频领金币】右侧「看视频」按钮(完成态=按钮消失)。
    顶部看视频领金币按钮 y<max_y; 高额金币按钮在滚动后(y>max_y)。
    返回中心 (cx,cy) 或 None(按钮消失=任务完成)。"""
    for t, x1, y1, x2, y2, cx, cy in ocr_words(shot):
        if ("看视频" in t or t == "看") and cx > 800 and cy < max_y:
            return cx, cy
    return None


def high_coin_locate():
    """从金币中心顶部向下滚动一屏, 定位【看视频得高额金币】入口(2026-11-04):
    滚动一屏只做一次(定位入口), OCR 找右侧「看视频」按钮, 找到即定位成功。
    返回按钮位置 (cx,cy) 或 None(滚动后未见入口)。"""
    log("[高额金币-定位] 从顶部向下滚动一屏")
    swipe_up()
    time.sleep(1.0)
    screencap("_fs_high_locate.png")
    pos = find_watch_video_btn("_fs_high_locate.png")
    if pos is None:
        log("[高额金币-定位] 滚动一屏后未找到「看视频」入口按钮")
    else:
        log(f"[高额金币-定位] 入口定位成功: 看视频按钮 ({pos[0]},{pos[1]})")
    return pos


def locate_high_coin_btn(shot="_fs_high_btn.png"):
    """OCR 定位【看视频得高额金币】按钮(2026-11-04): 按钮文字(看视频/领金币)OCR 识别不稳定,
    但标题行「看视频得高额金币 每天+1000」识别稳定。按钮在同一行右侧且 x 固定(~855),
    所以只识别标题行的 y, 返回 (855, 标题行cy)。不依赖滚动到精确位置(标题行实时OCR)。
    返回 (cx,cy) 或 None(滚动后未见入口/未识别到标题行)。"""
    words = ocr_words(shot)
    ty = None
    # 标题行特征: 「得高额」或「+1000」或「每天+1000」 (高额金币专属, 避免误识别顶部看视频领金币)
    for t, x1, y1, x2, y2, cx, cy in words:
        if ("得高额" in t) or ("+1000" in t) or ("每天" in t):
            ty = cy
            break
    # 兜底: 「看视频」/「视频」+ 同行右侧「得」(看视频得高额金币 拆词)
    if ty is None:
        for t, x1, y1, x2, y2, cx, cy in words:
            if ("看视频" in t or "视频" in t) and cx < 500:
                for t2, a1, b1, a2, b2, c2x, c2y in words:
                    if ("得高额" in t2 or "高额" in t2 or "1000" in t2) and abs(b1 - y1) < 60:
                        ty = cy
                        break
                if ty is not None:
                    break
    if ty is None:
        log("[高额金币-定位] OCR 未识别到「看视频得高额金币」标题行")
        return None
    log(f"[高额金币-定位] 标题行 y={ty}, 按钮=(855,{ty})")
    return 855, ty


def find_high_coin_btn(shot):
    """一次 OCR 定位【看视频得高额金币】右侧按钮, 返回 (cx,cy,kind) 或 None。
    标题「看视频得高额金币」OCR 会拆词(如「看」「视频」「得」「高」「额」「金币」), 锁定行
    用左侧「视频」(cx<500); 右侧按钮文字也拆词(「领」+「金币」/「看」+「视频」), 合并处理。
    kind: watch=「看视频」/ claim=「领金币」/ done=「明日再来」。按钮同一位置文字随状态变。"""
    words = ocr_words(shot)
    ty1 = ty2 = None
    # 1) 标题锁定行: 先整词, 再「视频」cx<500(校验同行右侧有「得」), 再「高额」
    for t, x1, y1, x2, y2, cx, cy in words:
        if "看视频得高额金币" in t and cx < 500:
            ty1, ty2 = y1, y2
            break
    if ty1 is None:
        for t, x1, y1, x2, y2, cx, cy in words:
            if "视频" in t and cx < 500:
                for t2, a1, b1, a2, b2, c2x, c2y in words:
                    if t2 == "得" and abs(b1 - y1) < 40 and a1 > x1:
                        ty1, ty2 = y1, y2
                        break
                if ty1 is not None:
                    break
    if ty1 is None:
        for t, x1, y1, x2, y2, cx, cy in words:
            if ("高额" in t or "+1000" in t) and cx < 500:
                ty1, ty2 = y1, y2
                break
    # 2) 标题行右侧(x>800)按钮文字, 处理拆词
    for t, x1, y1, x2, y2, cx, cy in words:
        if cx > 800 and (ty1 is None or (y1 >= ty1 - 25 and y2 <= ty2 + 25)):
            if "领金币" in t:
                return cx, cy, "claim"
            if "明日" in t or "再来" in t:
                return cx, cy, "done"
            if "看视频" in t:
                return cx, cy, "watch"
            if t == "领":  # 「领」+「金币」拆词
                for t2, a1, b1, a2, b2, c2x, c2y in words:
                    if "金币" in t2 and abs(b1 - y1) < 40 and a1 > x1:
                        return (x1 + a2) // 2, (y1 + b2) // 2, "claim"
                # 兜底: 该行右侧按钮只可能是三态之一, 找不到「金币」也直接判 claim
                return cx, cy, "claim"
            if t == "看":  # 「看视频」拆词; OCR 常把「视」误识别成「钢/频」, 第一个字「看」即锁 watch
                # (2026-11-04: 实测右侧识别成「看」+「钢」+「频」, 原逻辑找「视频」匹配不上 -> 误判找不到按钮)
                return cx, cy, "watch"
    return None


def high_coin_task(max_tries=8, roll_top_each=False):
    """【看视频得高额金币】批量跑(2026-11-04 改版): 按钮文字(看视频/领金币)OCR 识别不稳定,
    改「标题行定位 + 行为判断」: 点按钮(855, 标题行y) -> 停3s -> 判断:
      进广告门页(单/双开门) -> door_entry_flow 出门(复用看视频领金币) -> 下一轮按钮变「领金币」
      弹激励/观看下一条 -> find_popup -> incentive/next 出门 -> 下一轮按钮变回「看视频」
      点按钮无广告无激励(连续3次) -> 「明日再来」= 任务完成
    roll_top_each=True: 每轮滚回顶部+滚动一屏再定位(测试用, 验证滚动稳定性);
       False=直接在入口连续跑(定位一次后按钮位置固定)。"""
    no_adv_count = 0
    for t in range(1, max_tries + 1):
        stay = random.uniform(2.0, 3.0)
        log(f"[高额金币] 第{t}轮前停 {stay:.2f}s(等领取成功弹窗消失)")
        time.sleep(stay)
        if roll_top_each:
            log(f"[高额金币] 第{t}轮: 滚回顶部+滚动一屏")
            scroll_to_top()
            time.sleep(1.0)
            swipe_up()
            time.sleep(1.0)
        screencap("_fs_high_btn.png")
        pos = locate_high_coin_btn("_fs_high_btn.png")
        if pos is None:
            # 定位失败: 可能完成态(明日再来)或滚动位置不对 -> 滚顶重定位一次
            if find_tomorrow_text("_fs_high_btn.png") is not None:
                log(f"[高额金币] 第{t}轮发现「明日再来」 -> 任务完成")
                return True
            log(f"[高额金币] 第{t}轮定位按钮失败, 滚回顶部重试")
            scroll_to_top()
            time.sleep(1.0)
            swipe_up()
            time.sleep(1.0)
            screencap("_fs_high_btn.png")
            pos = locate_high_coin_btn("_fs_high_btn.png")
            if pos is None:
                if find_tomorrow_text("_fs_high_btn.png") is not None:
                    log(f"[高额金币] 第{t}轮(重试)发现「明日再来」 -> 任务完成")
                    return True
                log(f"[高额金币] 第{t}轮重试后仍定位不到按钮 -> 停止")
                return False
        cx, cy = pos
        log(f"[高额金币] 第{t}轮点击按钮 ({cx},{cy})")
        tap(cx, cy)
        time.sleep(3.0)
        ret = door_entry_flow()
        if ret != "not_ad":
            no_adv_count = 0
            log(f"[高额金币] 第{t}轮: 进广告出门({ret}) -> 按钮应变「领金币」")
            continue
        # not_ad: 没进广告 -> 领金币态(弹激励) 或 完成态(明日再来/点无反应)
        popup = find_popup()
        if popup and popup[0] == "incentive":
            incentive_entry_flow()
            no_adv_count = 0
            log(f"[高额金币] 第{t}轮: 弹激励 -> 出门 -> 按钮应变「看视频」")
            continue
        if popup and popup[0] == "next":
            next_entry_flow()
            no_adv_count = 0
            log(f"[高额金币] 第{t}轮: 弹观看下一条 -> 出门")
            continue
        # 无广告无激励 -> 点按钮无反应, 可能是完成态(明日再来)
        no_adv_count += 1
        log(f"[高额金币] 第{t}轮: 无广告无激励(第{no_adv_count}次) -> 判完成")
        if no_adv_count >= 3:
            log(f"[高额金币] 连续{no_adv_count}次无反应 -> 任务完成")
            return True
    log("[高额金币] 多轮未到完成态, 停止")
    return False



# ================= 激励跳转广告出门方案 =================
def incentive_jump_ad_flow():
    """激励跳转广告出门方案(2026-11-04 用户确认): 点完激励入口后这次也跳转到应用商店
    (QQ音乐的防脚本操作)。出门只需 停5.5~6s -> 左滑3次 即回金币中心,
    不需要点右上角✕/放弃福利(那两步用户实测是多余的, 已删除)。"""
    # --- 停5.5~6s -> 左滑 3 次 ---
    stay = random.uniform(5.5, 6.0)
    log(f"[激励跳转] 停留 {stay:.2f}s")
    time.sleep(stay)
    for i in range(1, 4):
        log(f"[激励跳转] 左滑 第{i}次")
        swipe_left()
        time.sleep(random.uniform(0.8, 1.3))
    log("[激励跳转] 激励跳转广告出门方案执行完毕")


def normal_incentive_flow():
    """普通激励出门(2026-11-04 用户确认, 不跳外部): 点激励入口 -> 停5.5~6s -> 左滑。
    注意: 点击激励入口由调用方完成(incentive_entry_flow 点击后判断未跳转才进本函数),
    本函数只做「停留 -> 左滑」的出门动作, 做完不做任何操作。"""
    stay = random.uniform(5.5, 6.0)
    log(f"[普通激励] 停留 {stay:.2f}s")
    time.sleep(stay)
    swipe_left()
    log("[普通激励] 已左滑, 普通激励出门执行完毕")


def incentive_entry_flow():
    """激励入口完整流程(2026-11-04): 判断是激励弹窗 -> 点击 -> 判断跳转 -> 分流出门。
    先判断再点击(必须确认当前是激励弹窗才点, 防误触弹窗内嵌广告/误点其他橙色按钮)。
    跳转外部 -> 激励跳转出门(停5.5~7s -> 左滑3次)。
    不跳转(仍在QQ音乐) -> 普通激励出门(未搭建, 停住待命交给用户)。"""
    popup = find_popup()
    if not popup or popup[0] != "incentive":
        log(f"[激励入口] 当前不是激励弹窗({popup}), 不点击")
        return False
    kind, cx, cy = popup
    log(f"[激励入口] 确认激励弹窗, 点击 ({cx},{cy})")
    tap(cx, cy)
    time.sleep(3.0)
    if is_external_jump():
        log("[激励入口] 已跳外部(应用商店/浏览器) -> 走激励跳转出门")
        incentive_jump_ad_flow()
        return "external"
    log("[激励入口] 仍在QQ音乐(未跳转) -> 走普通激励出门")
    normal_incentive_flow()
    return "normal"


def next_entry_flow():
    """观看下一条入口流程(2026-11-04): 判断观看下一条弹窗 -> 点击(=点看视频) -> 进门统一分流。
    点观看下一条等同于点「看视频」, 进门后由 door_entry_flow 统一处理:
    单开门(文字门页)->plain_door_exit_flow; 双开门->点CTA->判断跳转->出门/停住。"""
    popup = find_popup()
    if not popup or popup[0] != "next":
        log(f"[观看下一条] 当前不是观看下一条弹窗({popup}), 不点击")
        return False
    kind, cx, cy = popup
    log(f"[观看下一条] 确认弹窗, 点击 ({cx},{cy})  (=点看视频)")
    tap(cx, cy)
    time.sleep(3.0)
    return door_entry_flow()


def main(max_rounds=4):
    """全流程串联(2026-11-04):
    阶段1【看视频领金币】(金币中心顶部, 不需OCR, 点固定坐标833,685):
      循环点看视频 -> 若进入广告走 door_entry_flow 出门 -> 直到连续 COIN_NO_AD_MAX 次
      点看视频都没跳广告(看视频按钮区域不再变化) = 任务完成。
      期间有激励/下一条弹窗先处理。
    阶段2: swipe_up 滚动一屏 露出【看视频得高额金币】入口(需OCR定位).
    阶段3【看视频得高额金币】: high_coin_task 循环 看视频->广告/领金币->激励
      直到按钮「明日再来」= 完成。
    前提: 运行前手机在金币中心顶部。"""
    completed = False
    # 阶段1: 点看视频 -> 连续 COIN_NO_AD_MAX 次没跳广告则判完成
    screencap("_fs_phase1_ref.png")   # 金币中心参考: 顶部看视频按钮区域
    no_ad = 0
    ad_rounds = 0
    while no_ad < COIN_NO_AD_MAX and ad_rounds < 20:
        ad_rounds += 1
        # 若当前有激励/下一条弹窗, 先处理
        popup = find_popup()
        if popup is not None:
            kind = popup[0]
            log(f"[看视频领金币] 第{ad_rounds}轮先处理弹窗({kind})")
            if kind == "incentive":
                incentive_entry_flow()
            elif kind == "next":
                next_entry_flow()
            else:
                log(f"[看视频领金币] 未知弹窗 {popup}, 停止")
                return
            continue
        step1_watch_video()            # 任务入口: 点「看视频」(固定坐标, 不需OCR)
        time.sleep(3.5)
        result = door_entry_flow()     # 进门统一分流: 单开门/双开门/非广告(红包雨)
        if result in ("plain", "external", "normal"):
            # 走了单开门或双开门出门 = 刷到广告(用户2026-11-04: 完成判断靠「没走单开门/双开门」)
            log(f"[看视频领金币] 第{ad_rounds}轮 走了广告出门({result}) -> 刷广告, no_ad清零")
            no_ad = 0
        else:
            # not_ad(红包雨等, 没走单开门/双开门) -> 计一次没刷广告
            no_ad += 1
            log(f"[看视频领金币] 第{ad_rounds}轮 没走单开门/双开门({result}) -> no_ad={no_ad}/{COIN_NO_AD_MAX}")
    if no_ad >= COIN_NO_AD_MAX:
        log(f"[看视频领金币] 连续 {COIN_NO_AD_MAX} 次点看视频没跳广告 -> 任务完成")
        completed = True
    else:
        log("[看视频领金币] 达到最大轮次仍有点广告, 未判完成, 停止")
    if not completed:
        log("[串联] 看视频领金币未正常完成, 不继续")
        return
    # ===== 阶段2: 滚动一屏 -> 高额金币入口 =====
    log("[串联] 看视频领金币完成, 滚动一屏定位高额金币入口")
    high_coin_locate()
    # ===== 阶段3: 高额金币批量跑(OCR) =====
    high_coin_task()


if __name__ == "__main__":
    main()
