"""看视频领金币 —— 逐步搭建用的最小流程代码。

为什么单独一个文件:
    这一版流程由用户(训练助手)逐步确认, 每确认一步, 就把对应函数原样搬进 qqmusic_agent.py。
    所以这里每个函数都写成"可直接搬运"的形式: 只依赖 adb、日志说人话、时间点都打出来。

已确认的事实(实测):
    1) 金币中心这一屏 uiautomator dump 会报 "could not get idle state"(红包一直在动),
       所以**这一屏只能用坐标点**, 不能用文字定位。
    2) "看视频领金币"这一行右侧橙色"看视频"按钮 = 广告入口, 实测中心(900,1645)/相对(833,685);
       同一行下面那排 100/999/100... 是阶段奖励红包, 只展示不要点。
    3) 门页(Type2): 顶部黑条"点击后浏览15秒，可获得奖励" + 底部白卡"点击浏览15秒"。
       底部 CTA 在 dump 里读不到(WebView), 实测蓝色文字中心(540,2147)/相对(500,895)。
    4) 用户确认的节奏: 门页停 1~1.5s 随机 → 点"点击浏览15秒" → 跳转后停 15.5~16s。
"""
import os
import random
import re
import subprocess
import time

from PIL import Image
import numpy as np

try:                      # 视觉兜底: 固定坐标点不动时, 让云端模型看图给坐标
    import vlm_locate
except Exception as _e:   # 缺 openai/dotenv 也不该让主流程起不来
    vlm_locate = None
    print(f"!! vlm_locate 不可用: {_e}", flush=True)

# ================= 基础环境(与 qqmusic_agent.py 保持一致) =================
ADB = r"D:\桌面\xdf\Agent工程与具身智能\安卓手机控制\platform-tools\platform-tools\adb.exe"
QQMUSIC = "com.tencent.qqmusic"

# ================= 已确认的坐标(相对 0~1000) =================
COIN_VIDEO_BTN_REL = (833, 685)    # 金币中心: "看视频领金币"右侧橙色"看视频"按钮
DOOR_CTA_REL = (500, 895)          # 门页: 底部蓝色"点击浏览15秒"(京东型, y≈2147)
DOOR_CTA_CANDIDATES = [            # 门页 CTA 有几种位置, 按顺序试, 每次试完校验页面是否变化
    (500, 895),                    # 京东型: "点击浏览15秒" 蓝色文字 (y≈2147)
    (500, 836),                    # 微信/公众号型: "去微信看看" 蓝色实心按钮 (y≈2007)
]
AD_GIVEUP_REL = (500, 641)         # 挽留窗"放弃奖励" (实测 y≈1538; 旧常量 670 会点偏)
AD_WEBVIEW_X_REL = (150, 65)       # 广告落地页: 顶部左侧"✕" (京东热卖实测 162,155)
AD_WEBVIEW_X2_REL = (129, 58)      # 备用: 老广告列表页的"✕" (实测 139,140)
AD_DOOR_X_REL = (922, 110)         # 门页(恭喜已获得奖励): 右上角"✕" (实测 999,257)
INCENTIVE_BTN_REL = (500, 519)     # 金币中心激励弹窗: 橙色按钮(备用, 主用扫描)

# ================= 已确认的停留时长(秒) =================
# 2026-11-03 01:33 用户要求: 3~8 秒太久, 人工停留统一改成 2~5 秒随机
DOOR_STAY_RANGE = (2.0, 5.0)       # 门页停留(随机), 之后点 CTA
AD_STAY_RANGE = (16.0, 18.5)       # 跳转后在广告页停留 —— 这条是广告任务本身要求的 15 秒, 不能缩
INCENTIVE_STAY_RANGE = (5.0, 6.0)  # 激励浏览页停留: 页面写的是"浏览5秒", 5 秒是硬下限, 低于它可能不给奖励

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
    三段用**一次 shell** 发完(而不是三次 adb), 否则手势会被拆散、偶发不生效。
    注意: motionevent 无法设置 pressure/接触面积(需 root 走 sendevent), 只能做到这一步。"""
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


def focus_package():
    out = _adb("shell", "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
    m = re.search(r"([a-zA-Z][\w.]+)/[\w.$]+", out)
    return m.group(1) if m else ""


def _fp(path):
    """缩图灰度指纹, 用于粗判"页面有没有明显变化"。
    注意: 整屏比对会被视频动画污染, 只作"有没有跳页"的粗判。"""
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


# ================= 第 1 步: 金币中心 -> 点"看视频"进广告 =================
def step1_tap_video_entry(before="s_before.png", after="s_after.png", wait=2.0):
    """点"看视频领金币"右侧的"看视频"按钮。"""
    screencap(before)
    ow, oh = screen_size(before)
    x = int(COIN_VIDEO_BTN_REL[0] / 1000 * ow)
    y = int(COIN_VIDEO_BTN_REL[1] / 1000 * oh)
    log(f"[第1步] 屏幕 {ow}x{oh}, 点'看视频'入口 ({x},{y})")
    tap(x, y)
    time.sleep(wait)
    screencap(after)
    changed, used, d, _ = wait_change(before, timeout=0.1)   # 仅取差异值
    pkg = focus_package()
    log(f"[第1步] 点后 {wait:.1f}s: 前台={pkg or '未知'}, 画面差异={d if d else 'n/a'}")
    return True


# ================= 第 2 步: 门页停 1~1.5s -> 点"点击浏览15秒" =================
def step2_door_stay_then_cta(shot="s_door.png"):
    """门页停留 1~1.5s(随机), 然后点底部 CTA。返回点击时刻。"""
    screencap(shot)
    ow, oh = screen_size(shot)
    stay = random.uniform(*DOOR_STAY_RANGE)
    log(f"[第2步] 门页停留 {stay:.2f}s (区间 {DOOR_STAY_RANGE[0]}~{DOOR_STAY_RANGE[1]}s)")
    time.sleep(stay)
    x = int(DOOR_CTA_REL[0] / 1000 * ow)
    y = int(DOOR_CTA_REL[1] / 1000 * oh)
    log(f"[第2步] 点底部 CTA '点击浏览15秒' ({x},{y})  [相对 {DOOR_CTA_REL}]")
    tap(x, y)
    return time.time()


# ================= 第 3 步: 等跳转 -> 广告页停 15.5~16s =================
def step3_land_then_stay(t_click, shot="s_inad.png"):
    """从点击时刻算: 先等真的跳转(最多4s), 再停留 15.5~16s。返回落地时刻与停留时长。"""
    # 用点之前的门页截图做基准, 判断"是否已跳到广告页"
    changed, used, d, t_land = wait_change("s_door.png", timeout=4.0)
    if changed:
        log(f"[第3步] 点击后 {used:.1f}s 检测到跳转 (差异{d:.0f}), 此刻开始计时")
        t_land = time.time()
    else:
        log("[第3步] 4s 内未检测到明显跳转, 仍按点击后 1s 作为起点继续(避免卡死)")
        t_land = t_click + 1.0

    stay = random.uniform(*AD_STAY_RANGE)
    remain = t_land + stay - time.time()
    if remain > 0:
        log(f"[第3步] 广告页停留 {stay:.2f}s (区间 {AD_STAY_RANGE[0]}~{AD_STAY_RANGE[1]}s), 等待 {remain:.2f}s")
        time.sleep(remain)
    else:
        log(f"[第3步] 警告: 已超时 {abs(remain):.2f}s, 不再等待")
    screencap(shot)
    pkg = focus_package()
    log(f"[第3步] 停留结束: 前台={pkg or '未知'} -> 已截图 {shot}")
    return t_land, stay


# ================= 第 4 步: 广告页点"✕"返回 =================
def step4_tap_x(shot1="s_back1.png", shot2="s_back2.png"):
    """点广告落地页顶部的"✕"返回(用户指定: 统一用✕; 不同广告也可能只有"<")。
    点完连抓两帧(1.2s / 再1.5s), 用来判断落在哪一层。"""
    screencap("_fs_pre_x.png")
    ow, oh = screen_size("_fs_pre_x.png")
    x = int(AD_WEBVIEW_X_REL[0] / 1000 * ow)
    y = int(AD_WEBVIEW_X_REL[1] / 1000 * oh)
    log(f"[第4步] 点广告页左上'✕' ({x},{y})  [相对 {AD_WEBVIEW_X_REL}]")
    tap(x, y)
    time.sleep(1.2)
    screencap(shot1)
    changed, used, d, _ = wait_change("_fs_pre_x.png", timeout=0.1)
    log(f"[第4步] 1.2s 后: 前台={focus_package() or '未知'} -> {shot1}")
    time.sleep(1.5)
    screencap(shot2)
    log(f"[第4步] 再等1.5s: 前台={focus_package() or '未知'} -> {shot2}")
    return shot1, shot2


# ================= 第 5 步: 门页(恭喜已获得奖励) -> 尝试返回金币中心 =================
def step5_try_back(shot1="s_exit1.png", shot2="s_exit2.png"):
    """用户要求: 先试系统 BACK 能否回金币中心(不同广告退出方式不同, 目前样本只有1)。
    只按一次 BACK, 连抓两帧, **不做任何后续动作**, 由人看帧决定下一步。"""
    screencap("_fs_pre_back.png")
    log(f"[第5步] 当前前台={focus_package() or '未知'}, 按一次系统 BACK 试探")
    back()
    time.sleep(1.5)
    screencap(shot1)
    log(f"[第5步] 1.5s 后: 前台={focus_package() or '未知'} -> {shot1}")
    time.sleep(1.5)
    screencap(shot2)
    log(f"[第5步] 再等1.5s: 前台={focus_package() or '未知'} -> {shot2}")
    return shot1, shot2


# ================= 第 6 步: 门页(恭喜已获得奖励) -> 点右上角 X 回金币中心 =================
def step6_tap_x_exit(shot1="s_coin1.png", shot2="s_coin2.png"):
    """在"恭喜已获得奖励"门页点右上角 X。用户确认这种操作必定回金币中心。
    点完只截图, 不做任何后续动作。"""
    screencap("_fs_pre_x2.png")
    ow, oh = screen_size("_fs_pre_x2.png")
    x = int(AD_DOOR_X_REL[0] / 1000 * ow)
    y = int(AD_DOOR_X_REL[1] / 1000 * oh)
    log(f"[第6步] 点门页右上角'✕' ({x},{y})  [相对 {AD_DOOR_X_REL}]")
    tap(x, y)
    time.sleep(1.5)
    screencap(shot1)
    log(f"[第6步] 1.5s 后: 前台={focus_package() or '未知'} -> {shot1}")
    time.sleep(1.5)
    screencap(shot2)
    log(f"[第6步] 再等1.5s: 前台={focus_package() or '未知'} -> {shot2}")
    return shot1, shot2


# ================= 第 7 步: 激励弹窗 -> 点"浏览5秒再领XXX金币" =================
def step7_tap_incentive(shot1="s_inc1.png", shot2="s_inc2.png"):
    """点金币中心激励弹窗的橙色按钮。注意: XXX 是随机激励金额, 不是固定值, 不能写死。"""
    screencap("_fs_pre_inc.png")
    ow, oh = screen_size("_fs_pre_inc.png")
    x = int(INCENTIVE_BTN_REL[0] / 1000 * ow)
    y = int(INCENTIVE_BTN_REL[1] / 1000 * oh)
    log(f"[第7步] 点激励橙色按钮'浏览5秒再领XXX金币' ({x},{y})  [相对 {INCENTIVE_BTN_REL}]")
    tap(x, y)
    time.sleep(1.5)
    screencap(shot1)
    log(f"[第7步] 1.5s 后: 前台={focus_package() or '未知'} -> {shot1}")
    time.sleep(2.0)
    screencap(shot2)
    log(f"[第7步] 再等2.0s: 前台={focus_package() or '未知'} -> {shot2}")
    return shot1, shot2


# ================= 第 8 步: 激励页停留 -> BACK 返回 =================


def step8_incentive_stay_then_back(t_click=None, shot1="s_back_inc1.png", shot2="s_back_inc2.png"):
    """激励浏览页(京东超市那种, 无 X 无文字) 停 5.5~6s 后按 BACK。
    t_click=None 表示已经超过停留窗口(直接 BACK)。"""
    if t_click:
        stay = random.uniform(*INCENTIVE_STAY_RANGE)
        remain = t_click + stay - time.time()
        if remain > 0:
            log(f"[第8步] 激励页停留 {stay:.2f}s (区间 {INCENTIVE_STAY_RANGE[0]}~{INCENTIVE_STAY_RANGE[1]}s), 再等 {remain:.2f}s")
            time.sleep(remain)
        else:
            log(f"[第8步] 已停留 {time.time() - t_click:.1f}s, 超过下限直接返回")
    else:
        log("[第8步] 已超过停留窗口, 直接 BACK")
    screencap("_fs_pre_back2.png")
    back()
    time.sleep(1.5)
    screencap(shot1)
    log(f"[第8步] 1.5s 后: 前台={focus_package() or '未知'} -> {shot1}")
    time.sleep(2.0)
    screencap(shot2)
    log(f"[第8步] 再等2.0s: 前台={focus_package() or '未知'} -> {shot2}")
    return shot1, shot2


# ================= 激励弹窗是否存在(用户强调: 不是每次都有, 必须 if 判断) =================
def has_incentive_popup(shot="_fs_pop.png", x_rel=500, y_rel=519):
    """判断金币中心是否弹了"恭喜您获得XXX金币 / 浏览5秒再领XXX金币"激励弹窗。
    原理: 那个橙色按钮颜色很固定(实测 RGB≈(234,118,26)), 在按钮中心取一小块看橙色占比。
    不依赖文字(dump 在这一屏会 idle 失败), 也不调模型。"""
    screencap(shot)
    with Image.open(shot) as im:
        ow, oh = im.size
        cx, cy = int(x_rel / 1000 * ow), int(y_rel / 1000 * oh)
        box = im.convert("RGB").crop((cx - 40, cy - 15, cx + 40, cy + 15))
    px = list(box.getdata())
    orange = sum(1 for r, g, b in px if r > 190 and 70 < g < 170 and b < 80)
    ratio = orange / len(px)
    ok = ratio > 0.5
    log(f"[激励弹窗检测] 橙色占比 {ratio:.0%} -> {'有激励弹窗' if ok else '无激励弹窗'}")
    return ok


# ================= 第 9 步: BACK 无效时改"左滑"返回 =================
def swipe_left_back(shot1="s_swipe1.png", shot2="s_swipe2.png"):
    """从屏幕右边缘向左滑(手势返回)。用户说：BACK 不行时左滑能退出。"""
    screencap("_fs_pre_sw.png")
    ow, oh = screen_size("_fs_pre_sw.png")
    y = int(oh * 0.5)
    log(f"[第9步] 左滑返回: 从 ({int(ow * 0.99)},{y}) 滑到 ({int(ow * 0.15)},{y})")
    _adb("shell", "input", "swipe", str(int(ow * 0.99)), str(y), str(int(ow * 0.15)), str(y), "250")
    time.sleep(1.5)
    screencap(shot1)
    log(f"[第9步] 1.5s 后: 前台={focus_package() or '未知'} -> {shot1}")
    time.sleep(2.0)
    screencap(shot2)
    log(f"[第9步] 再等2.0s: 前台={focus_package() or '未知'} -> {shot2}")
    return shot1, shot2


# ================= 第 10 步: 完整跑一次激励(点 -> 停5.5~6s -> BACK/左滑 -> 回金币中心) =================
def step10_incentive_once(shot="s_inc_done.png"):
    """跑一次激励链路。退出策略(用户确认): 先 BACK, 2s 内页面没变就左滑。"""
    screencap("_fs_pre10.png")
    ow, oh = screen_size("_fs_pre10.png")
    x = int(INCENTIVE_BTN_REL[0] / 1000 * ow)
    y = int(INCENTIVE_BTN_REL[1] / 1000 * oh)
    log(f"[激励] 点橙色按钮 ({x},{y})")
    tap(x, y)

    changed, used, d, t_land = wait_change("_fs_pre10.png", timeout=3.0)
    if not changed:
        log("[激励] 3s 内没检测到跳转, 仍按点击时刻计时")
        t_land = time.time()
    else:
        log(f"[激励] 点击后 {used:.1f}s 进激励页")

    stay = random.uniform(*INCENTIVE_STAY_RANGE)
    remain = t_land + stay - time.time()
    if remain > 0:
        log(f"[激励] 停留 {stay:.2f}s (区间 {INCENTIVE_STAY_RANGE[0]}~{INCENTIVE_STAY_RANGE[1]}s)")
        time.sleep(remain)

    # --- 退出: 先 BACK, 不行再左滑 ---
    screencap("_fs_outexit.png")
    base = _fp("_fs_outexit.png")
    log("[激励] 先按 BACK 试探")
    back()
    time.sleep(1.5)
    screencap("_fs_afterback.png")
    moved = _dist(base, _fp("_fs_afterback.png")) >= 6.0
    log(f"[激励] BACK 后页面{'有变化' if moved else '没变化'}")
    if not moved:
        log("[激励] BACK 无效 -> 改左滑返回")
        _adb("shell", "input", "swipe", str(int(ow * 0.99)), str(int(oh * 0.5)),
             str(int(ow * 0.15)), str(int(oh * 0.5)), "250")
        time.sleep(1.5)
    screencap(shot)
    pkg = focus_package()
    popup = has_incentive_popup()
    log(f"[激励] 结束: 前台={pkg or '未知'}, 又弹激励? {popup} -> {shot}")
    return popup


# ================= 第 11 步: 一次完整激励(点 -> 停5.5~6s -> 左滑 -> 复查) =================
def _is_gray_mask(shot="_fs_mask.png"):
    """金币中心被激励弹窗的遮罩压暗后, 四边底色稳定在 RGB≈(98,99,100)(实测);
    广告页(白底)或别的页面不会是这个值。用来判断"是否真的回到了金币中心"。"""
    screencap(shot)
    with Image.open(shot) as im:
        ow, oh = im.size
        pts = [(int(ow * 0.03), int(oh * 0.5)), (int(ow * 0.97), int(oh * 0.5)),
               (int(ow * 0.03), int(oh * 0.75)), (int(ow * 0.97), int(oh * 0.75))]
        px = [im.convert("RGB").getpixel(p) for p in pts]
    ok = 0
    for r, g, b in px:
        if abs(r - 98) < 18 and abs(g - 99) < 18 and abs(b - 100) < 18:
            ok += 1
    log(f"[金币中心指纹] 四角底色 {px} -> 命中 {ok}/4")
    return ok >= 3


def step11_incentive_once():
    """跑一次激励, 返回 (是否又弹激励)。退出用左滑(BACK 会在广告站内翻页, 会假退出)。"""
    if not has_incentive_popup():
        log("[激励] 当前没有激励弹窗, 跳过")
        return False
    screencap("_fs_pre11.png")
    ow, oh = screen_size("_fs_pre11.png")
    x = int(INCENTIVE_BTN_REL[0] / 1000 * ow)
    y = int(INCENTIVE_BTN_REL[1] / 1000 * oh)
    log(f"[激励] 点橙色按钮 ({x},{y})")
    tap(x, y)

    changed, used, d, t_land = wait_change("_fs_pre11.png", timeout=3.0)
    if changed:
        log(f"[激励] {used:.1f}s 进激励页")
        t_land = time.time()
    else:
        log("[激励] 3s 未检测到跳转, 按点击时刻计时")
        t_land = time.time()

    stay = random.uniform(*INCENTIVE_STAY_RANGE)
    remain = t_land + stay - time.time()
    if remain > 0:
        log(f"[激励] 停留 {stay:.2f}s")
        time.sleep(remain)

    # 退出: 直接左滑(实测 BACK 只会在广告站内翻页)
    for attempt in (1, 2):
        log(f"[激励] 左滑返回 (第{attempt}次)")
        _adb("shell", "input", "swipe", str(int(ow * 0.99)), str(int(oh * 0.5)),
             str(int(ow * 0.15)), str(int(oh * 0.5)), "250")
        time.sleep(1.5)
        if on_coin_center():          # 注意: 不能用 _is_gray_mask, 它只认"有弹窗遮罩"的金币中心
            log("[激励] 已确认回到金币中心")
            break
        log("[激励] 还没回到金币中心, 再滑一次")
    popup = has_incentive_popup()
    log(f"[激励] 本轮结束: 又弹激励? {popup}")
    return popup


# ================= 弹窗橙色按钮: 扫描定位(两种弹窗按钮高度不同) =================
def _is_orange(px):
    r, g, b = px
    return r > 190 and 70 < g < 180 and b < 95


def find_orange_btn(shot="_fs_scan.png", y_top=850, y_bottom=1580, x_rel=500):
    """扫屏幕中轴线, 找橙色按钮的纵向范围并返回中心 (x,y); 找不到返回 None。
    为什么要扫: 金币中心有两种弹窗, 按钮高度不一样——
      A) 激励弹窗「浏览5秒再领XXX金币」→ 按钮 y≈1245
      B) 观看完成「观看下一条」        → 按钮 y≈1400
    写死一个坐标会漏掉另一种。"""
    screencap(shot)
    with Image.open(shot) as im:
        im = im.convert("RGB")
        ow, oh = im.size
        x0, x1 = int(0.36 * ow), int(0.64 * ow)      # 按钮横向范围(避开圆角)
        xs = list(range(x0, x1, 8))
        rows = []
        for y in range(y_top, y_bottom, 4):
            cnt = 0
            for x in xs:
                r, g, b = im.getpixel((x, y))
                if r > 190 and 70 < g < 180 and b < 95:
                    cnt += 1
            if cnt / len(xs) > 0.6:                  # 整行 60% 以上是橙色 = 按钮行
                rows.append(y)
    if not rows:
        log("[橙色按钮扫描] 未找到")
        return None
    # 取最长连续段
    best, cur = [], [rows[0]]
    for y in rows[1:]:
        if y - cur[-1] <= 8:
            cur.append(y)
        else:
            best = cur if len(cur) > len(best) else best
            cur = [y]
    best = cur if len(cur) > len(best) else best
    y1, y2 = best[0], best[-1]
    # 向上/下"长"到按钮真实边缘: 按钮上有白字会把橙色切断, 单看连续段会偏小
    for y in range(y1 - 4, max(y_top, y1 - 60), -4):
        cnt = sum(1 for x in xs if _is_orange(im.getpixel((x, y))))
        if cnt / len(xs) > 0.35:
            y1 = y
        else:
            break
    for y in range(y2 + 4, min(y_bottom, y2 + 60), 4):
        cnt = sum(1 for x in xs if _is_orange(im.getpixel((x, y))))
        if cnt / len(xs) > 0.35:
            y2 = y
        else:
            break
    if y2 - y1 < 40:
        log(f"[橙色按钮扫描] 命中但太矮 ({y1}~{y2}), 视为未找到")
        return None
    log(f"[橙色按钮扫描] 命中 y={y1}~{y2} (高{y2 - y1}px) -> 中心 ({(x0 + x1) // 2},{(y1 + y2) // 2})")
    return (x0 + x1) // 2, (y1 + y2) // 2


def on_coin_center(shot="_fs_cc.png"):
    """是否在金币中心。两种外观都要认:
    A) 有弹窗: 整屏被遮罩压暗, 四边底色稳定 RGB≈(98,99,100)(实测 9/9 命中)
    B) 没弹窗: 顶部是金色渐变(r 高, b 低)
    """
    screencap(shot)
    with Image.open(shot) as im:
        im = im.convert("RGB")
        ow, oh = im.size
        pts = [(int(ow * 0.03), int(oh * 0.5)), (int(ow * 0.97), int(oh * 0.5)),
               (int(ow * 0.03), int(oh * 0.72)), (int(ow * 0.97), int(oh * 0.72))]
        px = [im.getpixel(p) for p in pts]
        top = [im.getpixel((int(ow * f), int(oh * 0.06))) for f in (0.2, 0.5, 0.8)]
    gray = sum(1 for r, g, b in px if abs(r - 98) < 20 and abs(g - 99) < 20 and abs(b - 100) < 20)
    gold = sum(1 for r, g, b in top if r > 200 and g > 150 and b < 170)
    ok = gray >= 3 or gold >= 2
    log(f"[金币中心] 遮罩灰 {gray}/4 顶部金 {gold}/3 -> {'在金币中心' if ok else '不在'}")
    return ok


def goto_coin_center():
    """兜底导航: 首页/别处 -> 我的 -> 领现金 -> 金币中心。
    坐标是这台手机实测的(我的 tab 940,2280; 领现金卡片 888,373), 换机型要改。"""
    if on_coin_center():
        return True
    log("[导航] 尝试 我的 -> 领现金 -> 金币中心")
    tap_human(940, 2280)          # 底部"我的"
    time.sleep(2.0)
    tap_human(888, 373)           # 我的页里的"领现金"
    time.sleep(2.5)
    if on_coin_center():
        return True
    # 有时领现金页还要再点一次"金币中心/看广告赚金币"
    tap_human(540, 900)
    time.sleep(2.0)
    ok = on_coin_center()
    log(f"[导航] {'成功进入金币中心' if ok else '失败, 需要人工介入'}")
    return ok


def wait_coin_center(timeout=9.0, interval=0.9):
    """轮询等"真的回到金币中心"。为什么要等: 退出广告后 WebView 重渲染要 2~5s,
    单次判定会把"正在加载的白屏"误判成"没回去", 接着盲点左上角 —— 而那个位置在
    金币中心上就是"返回"箭头, 会把人直接踢出金币中心(页面就乱了)。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if on_coin_center():
            log(f"[等待] {time.time() - t0:.1f}s 已回到金币中心")
            return True
        time.sleep(interval)
    log(f"[等待] {timeout:.0f}s 内没回到金币中心")
    return False


def ensure_in_app():
    """广告可能把人带去淘宝/微信/应用商店; 前台不是 QQ音乐 就按 BACK 拉回来。"""
    pkg = focus_package()
    if pkg and pkg != "com.tencent.qqmusic":
        log(f"[兜底] 前台={pkg} 不是 QQ音乐 -> BACK")
        back()
        time.sleep(2.2)
        return True
    return False


# ---- 视觉兜底: 固定坐标点不动时, 让云端多模态模型看图给坐标 ----
CTA_VISUAL_TARGET = (
    "进入广告/详情页的那个按钮或文字(例如: 点击浏览15秒、点击允许打开微信、"
    "去微信看看、点击卡片、点击查看详情)"
)
EXIT_VISUAL_TARGET = (
    "广告页上的关闭或退出入口(例如: 左上角✕、左上角返回箭头<、"
    "顶部黑条右侧的『退出』、右上角✕)"
)


def tap_by_vision(shot="_fs_vis.png", target=CTA_VISUAL_TARGET, tag=""):
    """让模型看一眼截图, 返回目标中心坐标并点它。返回是否点动(页面发生变化)。
    为什么要"再截一张比对": 模型返回要 8~12s, 这期间广告可能自己跳页了,
    用旧坐标就会点到别的元素上。"""
    if vlm_locate is None:
        log(f"[视觉兜底{tag}] vlm_locate 不可用")
        return False
    screencap(shot)
    ow, oh = screen_size(shot)
    got = vlm_locate.locate(shot, target, ow, oh, tag=f"-{tag}")
    if not got:
        log(f"[视觉兜底{tag}] 模型也没找到")
        return False
    x, y, info = got
    screencap("_fs_vis_after.png")
    if _dist(_fp(shot), _fp("_fs_vis_after.png")) > 6.0:
        log(f"[视觉兜底{tag}] 页面在等模型期间已变化, 放弃这次坐标")
        return False
    log(f"[视觉兜底{tag}] 点 '{info.get('text', '')}' ({x},{y})")
    return tap_verified(x, y, shot_before="_fs_vis_after.png", wait=2.2)


# ================= 「看视频得高额金币」这一行的定位(WebView, dump 读不到文字) =================
# 教训: 旧模板是 70x9 缩略图, 把「听有书得金币」误认成「看视频得高额金币」, 点成了「去听书」,
#       直接跳进听书频道。现在改用 340x74 全分辨率灰度模板, 只有这八个字本尊才匹配得上。
TASK2_TPL = "tpl_task2_row.png"     # 340x74 全分辨率灰度, 内容="看视频得高额金币"
TASK2_TPL_X = 96                    # 模板在屏幕上的固定 x 起点(列表左边距固定)
TASK2_BTN_X = 903                   # 该行按钮中心 x (实测 827~980)
TASK2_BTN_DY = 41                   # 按钮中心相对模板顶部的 y 偏移(实测 1590~1668, 中心1629)
TASK2_MAX_DIST = 10.0               # 灰度平均绝对差阈值(同字≈0~2, 近义行≈20+)
TASK2_SAFE_TOP = 320                # 按钮 y 允许范围: 再高会被顶部吸顶栏挡
TASK2_SAFE_BOTTOM = 1980            # 再低就是半截在屏外/压在底部播放条上, 点了会点错


def find_task2_row(shot="_fs_t2.png", max_dist=TASK2_MAX_DIST, unsafe_ok=False):
    """全分辨率灰度模板匹配, 找「看视频得高额金币」那一行, 返回同行按钮坐标 (x,y) 或 None。
    为什么必须全分辨率: 金币中心是 WebView, uiautomator 读不到任务名; 而列表里
    「听有书得金币」「关注一位歌手得金币」等行字数和排版很像, 缩略图模板分不开。"""
    screencap(shot)
    a = np.asarray(Image.open(shot).convert("L")).astype(np.int16)
    tpl = np.asarray(Image.open(TASK2_TPL).convert("L")).astype(np.int16)
    oh, ow = a.shape
    th, tw = tpl.shape
    x0 = TASK2_TPL_X
    lo = 200 if unsafe_ok else TASK2_SAFE_TOP - TASK2_BTN_DY
    hi = (oh - th - 40) if unsafe_ok else TASK2_SAFE_BOTTOM - TASK2_BTN_DY
    best_d, best_y = 999.0, -1
    for y in range(lo, hi):          # 逐像素扫(实测整屏 ~0.15s), 步长2 会因为奇偶错位漏掉精确位置
        d = float(np.abs(a[y:y + th, x0:x0 + tw] - tpl).mean())
        if d < best_d:
            best_d, best_y = d, y
    log(f"[任务行匹配] 最小差异 {best_d:.1f} @ y={best_y}")
    if best_y < 0 or best_d > max_dist:
        return None
    return int(TASK2_BTN_X / 1080 * ow), best_y + TASK2_BTN_DY


def ensure_task2_row(max_scroll=8):
    """确保「看视频得高额金币」在屏幕上: 找到直接返回; 找不到就一屏一屏往下滚(先回到顶部)。"""
    btn = find_task2_row()
    if btn:
        return btn
    log("[任务行] 不在当前屏, 先回顶部再往下找")
    for _ in range(6):                      # 回顶部
        before = "_fs_top.png"
        screencap(before)
        _adb("shell", "input", "swipe", "540", "700", "540", "1750", "400")
        time.sleep(1.2)
        screencap("_fs_top2.png")
        if _dist(_fp(before), _fp("_fs_top2.png")) < 3.0:
            break
    for i in range(max_scroll):
        btn = find_task2_row()
        if btn:
            return btn
        log(f"[任务行] 第{i + 1}次下滚查找")
        screencap("_fs_scr_pre.png")
        _adb("shell", "input", "swipe", "540", "1700", "540", "800", "450")
        time.sleep(1.3)
        screencap("_fs_scr_post.png")
        if _dist(_fp("_fs_scr_pre.png"), _fp("_fs_scr_post.png")) < 3.0:
            # 滑不动 = 有弹窗盖着把滚动手势吃掉了; 不查出来就会白滚 8 次然后放弃
            log("[任务行] 滑不动 -> 判定有弹窗压着, 先关弹窗")
            if not close_popup():
                return None
    return find_task2_row()


# ================= 第 12 步: 看视频领金币 —— 完整循环 =================
# 依据用户逐步确认的规则:
#   入口: 弹窗「观看下一条」 == 第1步的「看视频」按钮; 没有弹窗才点右上那个「看视频」
#   门页: 停 1~1.5s -> 点底部「点击浏览15秒」
#   广告: 停 15.5~16s -> 点左上「✕」-> 回门页(恭喜已获得奖励) -> 点右上「✕」-> 金币中心
#   激励: 停 5.5~6s -> 左滑 -> 金币中心; 链式最多 3 次
def detect_popup(shot="_fs_pop2.png"):
    """返回 (类型, 按钮坐标) 或 (None, None)。类型: incentive / next_video
    两种弹窗按钮高度不同(激励≈1245, 观看完成≈1400), 用扫描到的 y 区分。"""
    btn = find_orange_btn(shot=shot)
    if not btn:
        return None, None
    kind = "incentive" if btn[1] < 1320 else "next_video"
    log(f"[弹窗] 类型={kind} 按钮={btn}")
    return kind, btn


def is_door_page(shot="_fs_door.png"):
    """门页判定: 顶部有一条几乎纯黑的提示条(y≈170~260, 写着"点击后浏览15秒")。
    金币中心是金色底、广告列表页是白底, 都不会有这条黑带。"""
    screencap(shot)
    with Image.open(shot) as im:
        im = im.convert("RGB")
        ow, oh = im.size
        y = int(0.075 * oh)
        xs = range(int(0.15 * ow), int(0.85 * ow), 40)
        dark = 0
        for x in xs:
            r, g, b = im.getpixel((x, y))
            if r + g + b < 200:
                dark += 1
        ratio = dark / max(1, len(list(xs)))
    ok = ratio > 0.6
    log(f"[门页判定] 顶部黑带占比 {ratio:.0%} -> {'是门页' if ok else '不是门页'}")
    return ok


def is_plain_door(shot="_fs_doorplain.png"):
    """门页是不是"纯白说明型"(整页白底 + 一行灰字, 例如"点击允许打开微信 / 查看内容，获取奖励")。
    这种门页没有 CTA 按钮, 停满 15s 平台就直接发奖励。
    判据用色彩饱和度(实测很干净):
        素材型门页(京东大图): 饱和像素 34%, 亮度 106
        纯白说明型:            饱和像素 0%,  亮度 253"""
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
    log(f"[门页类型] 亮度 {bright:.0f} 彩色占比 {colorful:.1%} -> "
        f"{'纯白说明型(不点CTA)' if ok else '素材型(要点CTA)'}")
    return ok


# 门页/广告页的关闭入口候选(按命中概率排):
#   (922,110) 素材型门页右上角✕   (实测 999,261)
#   (556,93)  黑条右侧"退出"      (实测 599,228)
#   (622,89)  黑条右侧"退出"偏右  (实测 672,214 / 675,206 / 663,216)
#   (438,93)  插屏广告黑条"跳过"  (实测 473,222)
#   (500,659) 插屏广告"关闭广告"灰字 (实测 540,1582)
DOOR_EXIT_CANDIDATES = [(922, 110), (556, 93), (622, 89), (438, 93), (500, 659)]


def tap_door_cta(ow, oh, tag=""):
    """门页 CTA 有多套位置(京东型 y≈2148 / 微信型 y≈1541 / 2006), 逐个试并校验页面变化。
    全都点不动就交给视觉兜底 —— 广告创意一直在换, 写死坐标追不完。"""
    for i, (rx, ry) in enumerate(DOOR_CTA_CANDIDATES, 1):
        x, y = int(rx / 1000 * ow), int(ry / 1000 * oh)
        log(f"[广告{tag}] 试 CTA#{i} ({x},{y})")
        if tap_verified(x, y):
            log(f"[广告{tag}] CTA#{i} 命中, 已进入广告页")
            return True
    log(f"[广告{tag}] 所有 CTA 候选都没反应 -> 走视觉兜底")
    if tap_by_vision(target=CTA_VISUAL_TARGET, tag=f"-CTA{tag}"):
        log(f"[广告{tag}] 视觉兜底命中, 已进入广告页")
        return True
    log(f"[广告{tag}] 视觉兜底也没点动(可能是新广告形态)")
    return False


def run_ad_once(tag="", entry_shot="_fs_entry.png"):
    """从"已点入口"开始, 走完一次广告并退回金币中心。返回是否成功回到金币中心。
    entry_shot 是"点击入口之前"那一帧, 用来判断点击后页面有没有跳转。"""
    changed, used, d, _ = wait_change(entry_shot, timeout=4.0)
    log(f"[广告{tag}] 入口点击后 {used:.1f}s 页面{'已跳转' if changed else '没变化'}")

    landed = True                # 有没有真的进到广告落地页(纯白说明型门页永远不跳转)
    if is_door_page():
        stay = random.uniform(*DOOR_STAY_RANGE)
        log(f"[广告{tag}] 门页停留 {stay:.2f}s")
        time.sleep(stay)
        ow, oh = screen_size("_fs_door.png")
        if is_plain_door():
            # 纯白说明型(页面只有一行"点击允许打开微信/查看内容"): 没有 CTA 可点,
            # 停满 15s 平台就发奖励。以前这里会硬点 CTA + 问模型, 白烧 30s。
            log(f"[广告{tag}] 纯白说明型门页 -> 不点 CTA, 直接计时")
            landed = False
        else:
            landed = tap_door_cta(ow, oh, tag)
        t_land = time.time()
    else:
        log(f"[广告{tag}] 不是门页, 视为已在广告/落地页")
        t_land = time.time()
        ow, oh = screen_size("_fs_entry.png")

    stay = random.uniform(*AD_STAY_RANGE)
    remain = t_land + stay - time.time()
    if remain > 0:
        log(f"[广告{tag}] 广告页停留 {stay:.2f}s")
        time.sleep(remain)

    if not landed:
        log(f"[广告{tag}] 一直停在门页, 跳过落地页✕, 直接找门页出口")
    else:
        # ① 点广告落地页左上 ✕ (回到门页"恭喜已获得奖励")
        #    不同广告平台的落地页 ✕ 位置差 20~30px(京东热卖实测 162,155; 老列表页 139,140),
        #    所以先点近的那个, 页面没反应再点另一个 —— 只在"没反应"时才点第二次, 不会误退两层。
        log(f"[广告{tag}] 点广告页左上'✕'")
        screencap("_fs_adx_pre.png")
        tap_human(int(AD_WEBVIEW_X_REL[0] / 1000 * ow), int(AD_WEBVIEW_X_REL[1] / 1000 * oh))
        time.sleep(1.8)
        if on_coin_center():
            return True
        screencap("_fs_adx_post.png")
        if _dist(_fp("_fs_adx_pre.png"), _fp("_fs_adx_post.png")) < 6.0:
            log(f"[广告{tag}] ✕ 没反应, 换第二个候选点")
            tap_human(int(AD_WEBVIEW_X2_REL[0] / 1000 * ow), int(AD_WEBVIEW_X2_REL[1] / 1000 * oh))
            time.sleep(1.8)
            if on_coin_center():
                return True
            screencap("_fs_adx_post2.png")
            if _dist(_fp("_fs_adx_pre.png"), _fp("_fs_adx_post2.png")) < 6.0:
                log(f"[广告{tag}] 两个 ✕ 都没反应 -> 走视觉兜底找关闭入口")
                tap_by_vision(target=EXIT_VISUAL_TARGET, tag=f"-EXIT{tag}")
                time.sleep(1.2)
                if on_coin_center():
                    return True
    # ② 找门页的关闭入口: 右上角✕(素材型) 或 顶部黑条右侧"退出"(纯白说明型, 实测 600,222)
    log(f"[广告{tag}] 找门页关闭入口")
    before = "_fs_dx_pre.png"
    screencap(before)
    for i, (rx, ry) in enumerate(DOOR_EXIT_CANDIDATES, 1):
        tap_human(int(rx / 1000 * ow), int(ry / 1000 * oh))
        time.sleep(1.8)
        if on_coin_center():
            log(f"[广告{tag}] 门页出口#{i} 命中, 回到金币中心")
            return True
        screencap("_fs_dx_post.png")
        if _dist(_fp(before), _fp("_fs_dx_post.png")) >= 6.0:
            log(f"[广告{tag}] 门页出口#{i} 让页面变了, 等它渲染")
            if wait_coin_center(timeout=4.0):
                return True
            break
    else:
        log(f"[广告{tag}] 门页出口候选都没反应 -> 走视觉兜底")
        tap_by_vision(target=EXIT_VISUAL_TARGET, tag=f"-DOOREXIT{tag}")
        if wait_coin_center(timeout=6.0):
            return True
    # ③ 没回去 → 多半弹了"现在退出就没有奖励哦"挽留窗(没有橙色按钮, 扫描会失明)
    for attempt in (1, 2):
        log(f"[广告{tag}] 未回金币中心, 判定为挽留窗 -> 点'放弃奖励' (第{attempt}次)")
        tap_human(int(AD_GIVEUP_REL[0] / 1000 * ow), int(AD_GIVEUP_REL[1] / 1000 * oh))
        time.sleep(1.8)
        if on_coin_center():
            log(f"[广告{tag}] '放弃奖励'后已回到金币中心")
            return True
    log(f"[广告{tag}] 警告: 仍未能回到金币中心")
    return False


def run_incentive_chain(max_n=6, tag=""):
    """链式激励: 有激励就点, 靠"弹窗类型变成 next_video / 没有弹窗"自然结束;
    max_n 只是**保险丝**(防无限链), 不是预期次数。实测每轮 3 次。"""
    for i in range(max_n):
        kind, btn = detect_popup()
        if kind != "incentive":
            log(f"[激励{tag}] 第{i + 1}次: 当前弹窗={kind}, 不再有激励")
            return kind
        log(f"[激励{tag}] 第{i + 1}次: 点 ({btn[0]},{btn[1]})")
        screencap("_fs_inc_size.png")
        ow, oh = screen_size("_fs_inc_size.png")
        # 点击并校验: 没进激励页就不要乱滑(否则左滑=系统返回, 会直接退出金币中心)
        if not tap_verified(btn[0], btn[1], wait=2.2):
            log(f"[激励{tag}] 第{i + 1}次 点击没反应, 跳过本次(不乱滑)")
            continue
        log(f"[激励{tag}] 第{i + 1}次 已进激励页")
        time.sleep(random.uniform(*INCENTIVE_STAY_RANGE))
        ok = False
        for attempt in (1, 2, 3):
            screencap("_fs_pre_sw.png")
            base = _fp("_fs_pre_sw.png")
            _adb("shell", "input", "swipe", str(int(ow * 0.99)), str(int(oh * 0.5)),
                 str(int(ow * 0.15)), str(int(oh * 0.5)), "250")
            time.sleep(1.6)
            if on_coin_center():          # 用统一判定: 有弹窗(灰遮罩) / 无弹窗(金顶) 都算回来了
                ok = True
                break
            screencap("_fs_post_sw.png")
            d = _dist(base, _fp("_fs_post_sw.png"))
            if d >= 6.0:
                # 左滑**生效了**而且已经不是金币中心 => 说明人已经被滑出金币中心了,
                # 再滑就是一路退回首页(之前就是这里连续左滑把页面滑乱的)。改用导航。
                log(f"[激励{tag}] 左滑生效(差异{d:.0f})但不在金币中心 -> 不再滑, 改用导航")
                if wait_coin_center(timeout=4.0):
                    ok = True
                else:
                    ok = goto_coin_center()
                break
            log(f"[激励{tag}] 左滑没生效(差异{d:.0f}), 重滑一次")
        log(f"[激励{tag}] 第{i + 1}次 退出{'成功' if ok else '失败'}")
    return detect_popup()[0]


def run_round(n):
    log(f"===== 第 {n} 轮开始 =====")
    if not on_coin_center():
        log(f"[第{n}轮] 不在金币中心, 先导航回去")
        if not goto_coin_center():
            return None
    kind, btn = detect_popup()
    if kind == "incentive":
        # 先把残留激励刷完; 刷完后弹窗会变成"观看完成", 要重新判定入口
        kind = run_incentive_chain(max_n=6, tag=f"R{n}-前置")
        kind, btn = detect_popup()

    if kind == "next_video":
        # 「观看下一条」== 「看视频」, 但它只会在"本流程跑过一次"后才弹;
        # 首次进入、或不小心退回首页再回来, 入口都还是「看视频」按钮。
        log(f"[第{n}轮] 入口=「观看下一条」弹窗 ({btn[0]},{btn[1]})")
        screencap("_fs_entry.png")
        if not tap_verified(btn[0], btn[1], wait=2.2):
            log(f"[第{n}轮] 「观看下一条」点不动(可能已被手动关掉), 改点「看视频」")
            ow, oh = screen_size("_fs_entry.png")
            ex, ey = int(COIN_VIDEO_BTN_REL[0] / 1000 * ow), int(COIN_VIDEO_BTN_REL[1] / 1000 * oh)
            tap_verified(ex, ey, wait=2.0)
    else:
        # 没有弹窗: 点「看视频」入口
        screencap("_fs_entry.png")
        ow, oh = screen_size("_fs_entry.png")
        ex, ey = int(COIN_VIDEO_BTN_REL[0] / 1000 * ow), int(COIN_VIDEO_BTN_REL[1] / 1000 * oh)
        log(f"[第{n}轮] 入口=「看视频」按钮 ({ex},{ey})")
        if not tap_verified(ex, ey, wait=2.0):
            # 连入口都点不动 → 屏幕上可能压着挽留窗/未知弹窗, 先试着点"放弃奖励"解围
            log(f"[第{n}轮] 入口点不动, 试点'放弃奖励'解围")
            tap_human(int(AD_GIVEUP_REL[0] / 1000 * ow), int(AD_GIVEUP_REL[1] / 1000 * oh))
            time.sleep(1.8)
            tap_verified(ex, ey, wait=2.0)

    ok = run_ad_once(tag=f"R{n}")
    end_kind = run_incentive_chain(max_n=6, tag=f"R{n}") if ok else None
    log(f"===== 第 {n} 轮结束, 回金币中心={ok}, 当前弹窗={end_kind} =====")
    return end_kind


# 弹窗底部圆形 ✕ 的已知位置(弹窗高度不同, 依次试, 关掉就停)
#   (545,1680) 普通"观看下一条"弹窗  (540,2064) "恭喜获得XXX金币"高弹窗(实测)
POPUP_CLOSE_POS = [(545, 1680), (540, 2064), (540, 1572), (540, 2013)]
POPUP_CLOSE_TARGET = ("弹窗上用来关闭它的按钮或文字(优先圆形✕; 也可能是"
                      "'关闭'/'关闭广告'/'暂不领取'/'残忍拒绝'这类文字)")


def close_popup():
    """金币中心只要有弹窗就先把它们关掉(否则弹窗会吃掉滑动手势, 页面滚不动)。
    先试已知位置, 都不行再让模型找关闭按钮。返回是否已无弹窗。"""
    if not find_orange_btn():
        return True
    log("[弹窗] 检测到弹窗, 先关闭(否则页面无法滚动)")
    for px, py in POPUP_CLOSE_POS:
        tap_human(px, py)
        time.sleep(1.4)
        if not find_orange_btn():
            log("[弹窗] 已关闭")
            return True
    log("[弹窗] 已知位置都没关掉 -> 视觉兜底找关闭按钮")
    if tap_by_vision(target=POPUP_CLOSE_TARGET, tag="-POPUP"):
        time.sleep(1.2)
        if not find_orange_btn():
            log("[弹窗] 视觉兜底关掉了")
            return True
    log("[弹窗] 仍有关不掉的弹窗")
    return False


# ================= 第 13 步: 看视频得高额金币(每日任务里那条, 没有"观看下一条") =================
def run_task2_round(n):
    """流程(用户逐步确认):
    点「看视频」-> 白屏广告(顶部"观看视频14秒后…｜跳过"+圆形倒计时, 无门页无CTA)
    -> 等 15s 倒计时走完(黑条变"已观看视频15秒…｜退出") -> 点顶部「退出」
    -> 回金币中心(滚动位置保持) -> 该行按钮变成「领金币」-> 点它 -> 弹激励 -> 走激励链
    """
    log(f"===== T2 第 {n} 轮开始 =====")
    ensure_in_app()
    if not on_coin_center():
        goto_coin_center()
    # 弹窗会吃掉滑动/点击手势, 必须先清场; 但"激励弹窗"要先把激励领掉再关
    kind0, _ = detect_popup()
    if kind0 == "incentive":
        run_incentive_chain(max_n=6, tag=f"T2-{n}-前置")
    close_popup()

    btn = ensure_task2_row()
    if not btn:
        log(f"[T2-{n}] 找不到'看视频得高额金币'这一行, 放弃本轮")
        return None

    screencap("_fs_t2_entry.png")
    ow, oh = screen_size("_fs_t2_entry.png")
    log(f"[T2-{n}] 点任务按钮 ({btn[0]},{btn[1]})")
    tap_verified(btn[0], btn[1], wait=2.2)
    time.sleep(0.6)

    kind, _ = detect_popup()
    if kind:                    # 直接弹激励 => 刚才那个按钮其实是「领金币」
        log(f"[T2-{n}] 点的是「领金币」({kind}), 直接进激励链")
        run_incentive_chain(max_n=6, tag=f"T2-{n}")
        return "incentive"

    # 广告有两种形态, 必须分开走:
    #   A) 白屏 + 顶部黑条倒计时: 页面自己数完 15s, 点「退出」即可(不用点任何按钮)
    #   B) 门页「点击后浏览15秒」+ 底部蓝色 CTA: 必须点 CTA 跳转, 浏览满 15s 才算完成
    time.sleep(1.2)             # 让门页渲染完再判定(判早了会当成"直停型", 白等 17s 不给奖励)
    if is_door_page():
        log(f"[T2-{n}] 门页型广告 -> 走门页流程(点CTA -> 浏览15s -> 返回)")
        ok_ad = run_ad_once(tag=f"T2-{n}", entry_shot="_fs_t2_entry.png")
        if not ok_ad:
            log(f"[T2-{n}] 门页型广告没能回到金币中心, 本轮放弃")
            return None
    else:
        # A 型: 白屏 + 顶部黑条倒计时, 不需要点任何 CTA, 等它数完
        stay = random.uniform(*AD_STAY_RANGE)
        log(f"[T2-{n}] 直停型广告, 停留 {stay:.2f}s (等倒计时自己走完)")
        time.sleep(stay)

        log(f"[T2-{n}] 点顶部「退出」")
        tap_human(int(0.622 * ow), int(0.0888 * oh))     # 实测 (672,213)
        ok_back = wait_coin_center(timeout=9.0)
        if not ok_back:                                  # 可能被带去别的 App
            if ensure_in_app():
                ok_back = wait_coin_center(timeout=6.0)
        if not ok_back:                                  # 再补一次「退出」(此时已确认不在金币中心, 不误伤)
            log(f"[T2-{n}] 还没回去 -> 再点一次「退出」")
            tap_human(int(0.622 * ow), int(0.0888 * oh))
            ok_back = wait_coin_center(timeout=8.0)
        if not ok_back:
            log(f"[T2-{n}] 警告: 未能回到金币中心, 本轮放弃(不乱点, 留页面给人工看)")
            return None

    # 回列表: 按钮已变成「领金币」, 再点一次真正领取
    kind1, _ = detect_popup()        # 偶尔退出后就自动弹了激励, 这种情况不用再点
    if kind1 == "incentive":
        log(f"[T2-{n}] 退出后已自动弹激励, 直接走链")
        end_kind = run_incentive_chain(max_n=6, tag=f"T2-{n}")
        log(f"===== T2 第 {n} 轮结束, 当前弹窗={end_kind} =====")
        return end_kind
    btn = ensure_task2_row()
    if btn:
        log(f"[T2-{n}] 点「领金币」({btn[0]},{btn[1]})")
        tap_verified(btn[0], btn[1], wait=2.2)
        time.sleep(0.8)
    else:
        log(f"[T2-{n}] 回来后找不到任务行")
    end_kind = run_incentive_chain(max_n=6, tag=f"T2-{n}")
    log(f"===== T2 第 {n} 轮结束, 当前弹窗={end_kind} =====")
    return end_kind


if __name__ == "__main__":
    import sys

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    step = sys.argv[1] if len(sys.argv) > 1 else "1"
    if step == "1":
        step1_tap_video_entry()
    elif step == "2":            # 门页 -> 点CTA -> 停15.5~16s
        t = step2_door_stay_then_cta()
        step3_land_then_stay(t)
    elif step == "3":
        step1_tap_video_entry()
        time.sleep(2.5)                        # 等门页加载
        t = step2_door_stay_then_cta()
        step3_land_then_stay(t)
    elif step == "4":            # 广告页 -> 点✕ 返回
        step4_tap_x()
    elif step == "5":            # 门页(恭喜) -> 试 BACK
        step5_try_back()
    elif step == "6":            # 门页(恭喜) -> 点右上角 X
        step6_tap_x_exit()
    elif step == "7":            # 激励弹窗 -> 点浏览5秒再领
        step7_tap_incentive()
    elif step == "8":            # 激励页停5.5~6s -> BACK
        step8_incentive_stay_then_back(t_click=None)
    elif step == "9":            # BACK 无效 -> 左滑
        swipe_left_back()
    elif step == "10":           # 完整跑一次激励
        step10_incentive_once()
    elif step == "11":           # 一次完整激励(点->停->左滑->复查)
        step11_incentive_once()
    elif step == "12":           # 看视频领金币 完整循环(默认3轮)
        n_rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        for i in range(1, n_rounds + 1):
            try:
                run_round(i)
            except Exception as e:
                log(f"[第{i}轮] 异常: {type(e).__name__}: {e}")
    elif step == "13":           # 看视频得高额金币(每日任务)循环
        n_rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        for i in range(1, n_rounds + 1):
            try:
                run_task2_round(i)
            except Exception as e:
                log(f"[T2-{i}] 异常: {type(e).__name__}: {e}")
