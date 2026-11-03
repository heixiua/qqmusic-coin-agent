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
    if focus_is_ad_activity():        # 在广告 Activity 里点「我的」只会更乱, 先抢救
        log("[导航] 前台是广告 Activity, 先抢救")
        if not rescue_from_stuck_ad():
            log("[导航] 抢救失败, 需要人工介入")
            return False
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


# ---- 广告 Activity 检测与卡死抢救 ----
# QQ音乐自家广告跑在两个专用 Activity 里: com.qq.e.tg.ADActivity 和
# com.tencentmusic.ad.tmead.core.activity.TMECoreActivity。包名仍是 com.tencent.qqmusic,
# 所以 ensure_in_app 认不出来; 它们压在最上层时点击/BACK 都可能被吞,
# goto_coin_center 会在广告页里盲点「我的」把页面点得更乱(13:33 实录)。
AD_ACTIVITY_PAT = re.compile(r"ADActivity|tmead|TMECore", re.I)


def focus_is_ad_activity():
    out = _adb("shell", "dumpsys window | grep mCurrentFocus")
    return bool(AD_ACTIVITY_PAT.search(out or ""))


def _current_focus_name():
    """返回当前前台 Activity 的短类名(最后一段), 判断用。取不到返回空串。
    原始格式: mCurrentFocus=Window{cc0d00 u0 包名/包名.activity.AppStarterActivity type=1}
    必须取 "/" 后面那段才是 Activity —— 只截 [\\w.]+ 会停在 "/" 上, 拿到的是包名"qqmusic"。"""
    out = _adb("shell", "dumpsys window | grep mCurrentFocus") or ""
    m = re.search(r"u0 ([\w./]+)", out)
    if not m:
        return ""
    cls = m.group(1).split("/")[-1]
    return cls.split(".")[-1]


def _screen_dark(shot="_fs_dark.png"):
    """整页是否近乎全黑(均值<60)。用途有二:
    1) 跳转/关广告瞬间会闪一帧纯黑过渡(实测 (1,1,1)), 此时任何判定都不可信;
    2) 广告页彻底卡死时页面也全黑且静止。"""
    screencap(shot)
    a = np.asarray(Image.open(shot).convert("RGB")).astype(np.float32)
    mean = float(a.mean())
    return mean < 60.0, mean


def rescue_from_stuck_ad(max_back=3):
    """从卡死的广告 Activity 里抢救回来。逐级升级:
    BACK x N -> am start 主界面 -> force-stop + 重启。返回是否回到可导航状态。"""
    for i in range(max_back):
        if not focus_is_ad_activity():
            break
        log(f"[抢救] 前台是广告 Activity, 第{i + 1}次 BACK")
        back()
        time.sleep(2.5)
        if on_coin_center():
            return True
    if focus_is_ad_activity():
        log("[抢救] BACK 无效 -> am start 主界面")
        _adb("shell", "am", "start", "-n",
             "com.tencent.qqmusic/com.tencent.qqmusic.activity.AppStarterActivity")
        time.sleep(4.0)
    if focus_is_ad_activity():
        log("[抢救] 仍压着 -> force-stop 重启 QQ音乐")
        _adb("shell", "am", "force-stop", "com.tencent.qqmusic")
        time.sleep(2.0)
        _adb("shell", "am", "start", "-n",
             "com.tencent.qqmusic/com.tencent.qqmusic.activity.AppStarterActivity")
        time.sleep(9.0)
    return not focus_is_ad_activity()


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


def is_door_page(shot="_fs_door.png", use_existing=False):
    """门页/广告页判定: 顶部有一条暗色提示条(写着"点击后浏览15秒"/"已观看广告15秒｜退出"等)。
    黑条 y 位置不固定(实测 100~320 之间都会出现), 所以扫一整段找连续暗带;
    并且要求"暗带下方不是暗的" —— 否则夜间模式的整页深色会被误判成广告页。"""
    if not use_existing:
        screencap(shot)
    a = np.asarray(Image.open(shot).convert("RGB")).astype(np.int16)
    oh, ow, _ = a.shape
    xs = list(range(int(0.15 * ow), int(0.85 * ow), 40))
    rows = []
    for y in range(int(0.03 * oh), int(0.17 * oh), 8):
        c = sum(1 for x in xs if int(a[y, x].sum()) < 200) / len(xs)
        if c > 0.6:
            rows.append(y)
    if len(rows) < 3:
        log(f"[门页判定] 顶部无暗带 -> 不是门页")
        return False
    y1 = rows[-1]
    below = min(oh - 1, y1 + 80)
    c_below = sum(1 for x in xs if int(a[below, x].sum()) < 200) / len(xs)
    ok = c_below < 0.4
    log(f"[门页判定] 暗带 y={rows[0]}~{y1}, 其下暗度 {c_below:.0%} -> {'是门页' if ok else '不是门页(整页深色)'}")
    return ok


def is_plain_door(shot="_fs_doorplain.png", use_existing=False):
    """门页是不是"纯白说明型"(整页白底 + 一行灰字, 例如"点击允许打开微信 / 查看内容，获取奖励")。
    这种门页没有 CTA 按钮, 停满 15s 平台就直接发奖励。
    判据用色彩饱和度(实测很干净):
        素材型门页(京东大图): 饱和像素 34%, 亮度 106
        纯白说明型:            饱和像素 0%,  亮度 253"""
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


# ================= 广告形态分发器(2026-11-03 重构, 一次只改一件事) =================
# 设计: 识别当前广告属于哪种形态 -> 按 AD_HANDLERS 分派到独立 handler。
#   每个 handler 自包含该形态的解法、停留参数与出口策略; 新增广告形态 = 新增一个 handler,
#   不用再在 run_round / run_task2_round 里各改一遍。
# 形态清单(对应交接文档 §4 广告8形态速查中由 run_ad_once 覆盖的部分):
#   AD_FORM_PLAIN_DOOR    纯白说明型门页: 无 CTA, 停满 15s 平台直接发奖励
#   AD_FORM_MATERIAL_DOOR 素材型门页: 点 CTA 跳落地页, 浏览满 15s, 落地页✕ 再门页出口
#   AD_FORM_LANDING_PAGE  落地页ADActivity(非门页): 直接计时 -> 落地页✕ -> 门页出口
# 其余形态(插屏/黑屏过渡帧/误入站内页/卡死Activity)仍由原检测链处理, 不在本次改动范围。
# 2026-11-03 实测: 纯白说明型门页两轮均出口#2(黑条"退出" 599,222)命中, 出口#1(右上角✕)无效
#   -> 纯白型出口候选把黑条退出提到最前, 省掉每次白试 ✕ 的 ~1.8s。

AD_FORM_PLAIN_DOOR = "纯白说明型门页"
AD_FORM_MATERIAL_DOOR = "素材型门页"
AD_FORM_LANDING_PAGE = "落地页ADActivity"
AD_FORM_UNKNOWN = "未知形态"

# 出口候选排序差异:
#   素材型/落地页: 右上角✕(922,110) -> 黑条退出(556,93)/(622,89) -> 插屏跳过(438,93) -> 关闭广告(500,659)
#   纯白说明型:   黑条退出优先(实测 599,222 命中), 右上角✕对纯白型无效
DOOR_EXIT_PLAIN = [(556, 93), (622, 89), (438, 93), (500, 659)]


def _try_door_exit(ow, oh, tag, candidates):
    """按给定候选顺序试门页出口; 每个候选点完判是否回金币中心, 页面变化了但没回就等渲染。
    全部没反应 -> 视觉兜底找退出入口。返回是否回到金币中心。"""
    before = "_fs_dx_pre.png"
    screencap(before)
    for i, (rx, ry) in enumerate(candidates, 1):
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
    log(f"[广告{tag}] 门页出口候选都没反应 -> 走视觉兜底")
    tap_by_vision(target=EXIT_VISUAL_TARGET, tag=f"-DOOREXIT{tag}")
    return wait_coin_center(timeout=6.0)


def _close_landing_x(ow, oh, tag):
    """点广告落地页左上 ✕(回到门页"恭喜已获得奖励")。
    不同平台的 ✕ 位置差 20~30px, 先点近的那个, 页面没反应再点另一个 —— 只在"没反应"时才点第二次。"""
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
    return False


def _ad_stay(tag):
    """广告/落地页停留(平台规则 15s 硬下限, 不能缩)。"""
    stay = random.uniform(*AD_STAY_RANGE)
    log(f"[广告{tag}] 广告页停留 {stay:.2f}s")
    time.sleep(stay)


def _rescue_giveup(ow, oh, tag):
    """兜底: 出口候选都没回去 -> 多半弹了"现在退出就没有奖励哦"挽留窗
    (没有橙色按钮, 扫描会失明), 点"放弃奖励"解围。"""
    for attempt in (1, 2):
        log(f"[广告{tag}] 未回金币中心, 判定为挽留窗 -> 点'放弃奖励' (第{attempt}次)")
        tap_human(int(AD_GIVEUP_REL[0] / 1000 * ow), int(AD_GIVEUP_REL[1] / 1000 * oh))
        time.sleep(1.8)
        if on_coin_center():
            log(f"[广告{tag}] '放弃奖励'后已回到金币中心")
            return True
    log(f"[广告{tag}] 警告: 仍未能回到金币中心")
    return False


def _handle_plain_door(ow, oh, tag):
    """纯白说明型门页: 没有 CTA 可点, 停满 15s 平台就发奖励。
    出口优先黑条"退出"(实测 599,222), 跳过素材型右上角✕(纯白型没有, 每次白等 1.8s)。"""
    log(f"[广告{tag}] 纯白说明型门页 -> 不点 CTA, 直接计时")
    _ad_stay(tag)
    log(f"[广告{tag}] 一直停在门页, 跳过落地页✕, 直接找门页出口")
    return _try_door_exit(ow, oh, tag, DOOR_EXIT_PLAIN)


def _handle_material_door(ow, oh, tag):
    """素材型门页: 点 CTA 跳落地页 -> 浏览满 15s -> 落地页✕ -> 门页出口。
    CTA 点不动时退化为"纯白型"处理(停满直接找出口, 不硬点)。"""
    landed = tap_door_cta(ow, oh, tag)
    _ad_stay(tag)
    if not landed:
        log(f"[广告{tag}] CTA 没点动, 视为停在门页, 直接找门页出口")
        if _try_door_exit(ow, oh, tag, DOOR_EXIT_CANDIDATES):
            return True
        return _rescue_giveup(ow, oh, tag)
    if _close_landing_x(ow, oh, tag):
        return True
    if _try_door_exit(ow, oh, tag, DOOR_EXIT_CANDIDATES):
        return True
    return _rescue_giveup(ow, oh, tag)


def _handle_landing_page(ow, oh, tag):
    """落地页ADActivity(入口点击后直接进广告落地页, 没有门页): 计时 -> 落地页✕ -> 门页出口。"""
    log(f"[广告{tag}] 不是门页, 视为已在广告/落地页")
    _ad_stay(tag)
    if _close_landing_x(ow, oh, tag):
        return True
    if _try_door_exit(ow, oh, tag, DOOR_EXIT_CANDIDATES):
        return True
    return _rescue_giveup(ow, oh, tag)


def _handle_unknown_ad(ow, oh, tag):
    """未知形态兜底: 计时 -> 视觉找退出; 再不行按挽留窗"放弃奖励"解围。"""
    log(f"[广告{tag}] 未知广告形态 -> 计时后视觉找出口")
    _ad_stay(tag)
    if _try_door_exit(ow, oh, tag, DOOR_EXIT_CANDIDATES):
        return True
    return _rescue_giveup(ow, oh, tag)


AD_HANDLERS = {
    AD_FORM_PLAIN_DOOR: _handle_plain_door,
    AD_FORM_MATERIAL_DOOR: _handle_material_door,
    AD_FORM_LANDING_PAGE: _handle_landing_page,
    AD_FORM_UNKNOWN: _handle_unknown_ad,
}


def handle_ad(tag="", entry_shot="_fs_entry.png"):
    """广告形态分发器: 识别形态 -> 分派到 AD_HANDLERS 对应 handler。
    返回是否成功回到金币中心。"""
    changed, used, d, _ = wait_change(entry_shot, timeout=4.0)
    log(f"[广告{tag}] 入口点击后 {used:.1f}s 页面{'已跳转' if changed else '没变化'}")

    if is_door_page():
        stay = random.uniform(*DOOR_STAY_RANGE)
        log(f"[广告{tag}] 门页停留 {stay:.2f}s")
        time.sleep(stay)
        ow, oh = screen_size("_fs_door.png")
        if is_plain_door():
            form = AD_FORM_PLAIN_DOOR
        else:
            form = AD_FORM_MATERIAL_DOOR
    else:
        form = AD_FORM_LANDING_PAGE
        ow, oh = screen_size(entry_shot)

    log(f"[广告{tag}] 形态={form}")
    handler = AD_HANDLERS.get(form, AD_HANDLERS[AD_FORM_UNKNOWN])
    return handler(ow, oh, tag)


def run_ad_once(tag="", entry_shot="_fs_entry.png"):
    """兼容包装: 等价于 handle_ad。保留函数名, 调用点(run_round/run_task2_round)零改动。"""
    return handle_ad(tag=tag, entry_shot=entry_shot)


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


# ================= 第 14 步: 抽奖赢金币(天天抽奖老虎机) =================
# 链路(用户确认):
#   金币中心「抽奖赢金币→立即抽奖」-> 老虎机页
#   点主按钮: 有次数就是「立即抽奖」, 没次数就是「看广告抽奖」
#   广告: 纯白微信型, 黑条倒计时 15s -> 点「退出」-> 回老虎机(次数+1, 可能累积成2次)
#   抽奖: 点「立即抽奖」-> 转 3s -> 结果窗 -> 点「开心收下」
#   规则: 最多 10 次; 出现「明天再来」= 今日次数用完; 券类奖品也继续抽
DRAW_TPL = "tpl_draw_row.png"       # 194x52 全分辨率灰度, 内容="抽奖赢金币"
DRAW_TPL_X = 96                     # 模板在屏上的固定 x
DRAW_BTN_DY = 24                    # 按钮中心相对模板顶部
DRAW_MAIN_BTN = (540, 1563)         # 老虎机页主按钮(看广告抽奖/立即抽奖)
DRAW_ACCEPT = (540, 1696)           # 结果窗「开心收下」
DRAW_POPUP_X = (540, 1881)          # 结果窗底部圆形✕
DRAW_MAX_SPINS = 10                 # 每日上限(页面写 10/10)


def _match_text_row(tpl_path, tpl_x, shot, max_dist=10.0, y_lo=320, y_hi=1980,
                    dy=0, btn_x=903, tag=""):
    """通用的"按文字模板找列表行", 返回同行右侧按钮坐标。全分辨率灰度逐像素比对。"""
    screencap(shot)
    a = np.asarray(Image.open(shot).convert("L")).astype(np.int16)
    tpl = np.asarray(Image.open(tpl_path).convert("L")).astype(np.int16)
    oh, ow = a.shape
    th, tw = tpl.shape
    lo = max(0, y_lo - dy)
    hi = min(oh - th - 10, y_hi - dy)
    best_d, best_y = 999.0, -1
    for y in range(lo, hi):
        d = float(np.abs(a[y:y + th, tpl_x:tpl_x + tw] - tpl).mean())
        if d < best_d:
            best_d, best_y = d, y
    log(f"[{tag}行匹配] 最小差异 {best_d:.1f} @ y={best_y}")
    if best_y < 0 or best_d > max_dist:
        return None
    return int(btn_x / 1080 * ow), best_y + dy


def draw_page_state(shot="_fs_draw.png", use_existing=False):
    """老虎机页状态。判据(实测):
        正常页   顶部天空 (46,133,255)  -> r<130 且 b>200 且 g>100 (必须卡 r, 否则白底广告页 255,255,255 会误判)
        结果窗   整页被压暗 (18,53,102) -> 70<=b<=150 且 r<45
        都不是   -> 广告页/别的页面"""
    if not use_existing:
        screencap(shot)
    a = np.asarray(Image.open(shot).convert("RGB")).astype(np.int16)
    r, g, b = (int(v) for v in a[180, 540])
    if r < 130 and b > 200 and g > 100:
        st = "draw"
    elif 70 <= b <= 150 and r < 45:
        st = "overlay"
    else:
        st = "other"
    log(f"[抽奖页] 顶部色 ({r},{g},{b}) -> {st}")
    return st


def wait_draw_page(timeout=7.0, interval=0.9):
    """轮询等"回到老虎机页"。为什么要等: 广告关闭的一瞬间会闪一帧纯黑(实测 top=(1,1,1)),
    单次判定会把这一帧当成"没回到抽奖页", 于是继续乱点, 越点越远。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if draw_page_state() == "draw":
            log(f"[抽奖页] {time.time() - t0:.1f}s 已回到抽奖页")
            return True
        time.sleep(interval)
    log(f"[抽奖页] {timeout:.0f}s 内没回到抽奖页")
    return False


def ensure_draw_page(max_attempt=6):
    """把页面弄到老虎机抽奖页: 已经是就返回; 否则先退到金币中心, 再滚到「抽奖赢金币」点进去。"""
    if draw_page_state() == "draw":
        return True
    if draw_page_state() == "overlay":          # 压着结果窗, 先关掉
        tap_human(*DRAW_ACCEPT)
        time.sleep(1.6)
        if draw_page_state() == "overlay":
            tap_human(*DRAW_POPUP_X)
            time.sleep(1.4)
        if draw_page_state() == "draw":
            return True
    if is_door_page():                          # 明确的广告页(顶部有暗色提示条)
        log("[抽奖] 当前在广告页, 先退出广告(不再等倒计时)")
        if ad_stay_and_exit(tag="-恢复", dwell=False):
            return True
    else:                                       # 黑底广告页/别的页面: 用系统 BACK 退
        for _ in range(3):
            if draw_page_state() == "draw":
                return True
            if on_coin_center():
                break
            log("[抽奖] 按 BACK 退出当前页")
            back()
            if wait_draw_page(timeout=2.6):
                return True
    for _ in range(2):                          # 抽奖页左上角返回 -> 金币中心
        tap_human(67, 155)
        time.sleep(1.8)
        if draw_page_state() == "draw":
            return True
        if on_coin_center():
            break
    if not on_coin_center() and not goto_coin_center():
        log("[抽奖] 回不到金币中心")
        return False
    close_popup()
    for _ in range(6):                          # 回顶部
        before = "_fs_dtop.png"
        screencap(before)
        _adb("shell", "input", "swipe", "540", "700", "540", "1750", "400")
        time.sleep(1.1)
        screencap("_fs_dtop2.png")
        if _dist(_fp(before), _fp("_fs_dtop2.png")) < 3.0:
            break
    for i in range(max_attempt):
        btn = _match_text_row(DRAW_TPL, DRAW_TPL_X, "_fs_drow.png",
                              dy=DRAW_BTN_DY, tag="抽奖")
        if btn:
            log(f"[抽奖] 找到「抽奖赢金币」行, 点 ({btn[0]},{btn[1]})")
            tap_verified(btn[0], btn[1], wait=2.5)
            time.sleep(1.0)
            if draw_page_state() == "draw":
                return True
        log(f"[抽奖] 第{i + 1}次下滚找「抽奖赢金币」")
        _adb("shell", "input", "swipe", "540", "1700", "540", "800", "450")
        time.sleep(1.2)
    return draw_page_state() == "draw"


def ad_stay_and_exit(tag="", dwell=True):
    """抽奖页里的广告: 等倒计时走完(15s) -> 找出口 -> 回老虎机页。返回是否回到抽奖页。
    dwell=False 用于"已经在广告页里但不确定是否看完"的恢复场景, 不再白等 17s。"""
    if dwell:
        stay = random.uniform(*AD_STAY_RANGE)
        log(f"[抽奖广告{tag}] 停留 {stay:.2f}s 等倒计时")
        time.sleep(stay)
    screencap("_fs_drawad.png")          # 必须先截图, screen_size 要读这个文件
    ow, oh = screen_size("_fs_drawad.png")
    # 按页面类型排出口顺序: 纯白说明型(微信)只有"退出", 素材型才有右上角✕
    cands = list(DOOR_EXIT_CANDIDATES)
    try:
        if is_plain_door("_fs_drawad.png", use_existing=True):
            cands = [(556, 93), (622, 89), (922, 110), (438, 93), (500, 659)]
    except Exception:
        pass
    for i, (rx, ry) in enumerate(cands, 1):
        tap_human(int(rx / 1000 * ow), int(ry / 1000 * oh))
        time.sleep(1.4)
        if wait_draw_page(timeout=3.5):
            log(f"[抽奖广告{tag}] 出口#{i} 命中, 回到抽奖页")
            return True
    log(f"[抽奖广告{tag}] 固定出口都没反应 -> 视觉兜底")
    tap_by_vision(target=EXIT_VISUAL_TARGET, tag=f"-DRAWEXIT{tag}")
    ok = wait_draw_page(timeout=5.0)
    log(f"[抽奖广告{tag}] 视觉兜底{'成功' if ok else '失败'}")
    return ok


def run_draw_round(n):
    """跑一轮抽奖(最多 10 次)。返回实际抽奖次数。
    不判断按钮上是"看广告抽奖"还是"立即抽奖" —— 直接点, 看 3.5s 后的结果分支:
      出结果窗 = 抽到一次 / 进广告 = 这次只补次数 / 页面没变 = 次数用完(明天再来)。"""
    log(f"===== 抽奖第 {n} 轮开始 =====")
    ensure_in_app()
    if not ensure_draw_page():
        log(f"[抽奖{n}] 进不去抽奖页, 放弃本轮")
        return None
    spins, tries = 0, 0
    while spins < DRAW_MAX_SPINS and tries < DRAW_MAX_SPINS * 2 + 6:
        tries += 1
        tap_human(*DRAW_MAIN_BTN)
        time.sleep(3.5)
        st = draw_page_state()
        if st == "overlay":
            spins += 1
            log(f"[抽奖{n}] 第{spins}次抽奖 -> 出结果窗")
            tap_human(*DRAW_ACCEPT)
            time.sleep(1.8)
            if draw_page_state() == "overlay":
                log(f"[抽奖{n}] 「开心收下」没关掉, 点底部✕")
                tap_human(*DRAW_POPUP_X)
                time.sleep(1.5)
            if draw_page_state() == "overlay":
                log(f"[抽奖{n}] 结果窗还关不掉 -> 视觉兜底")
                tap_by_vision(target="结果弹窗上的『开心收下』按钮或关闭✕", tag="-DRAWOK")
                time.sleep(1.5)
        elif st == "other":
            if on_coin_center():
                log(f"[抽奖{n}] 跑到金币中心了 -> 重新进抽奖页")
                if not ensure_draw_page():
                    break
            else:
                log(f"[抽奖{n}] 当前不是抽奖页(广告) -> 等倒计时并退出")
                if not ad_stay_and_exit(tag=f"D{n}"):
                    log(f"[抽奖{n}] 广告没退出成功, 尝试重新进抽奖页")
                    if not ensure_draw_page():
                        log(f"[抽奖{n}] 放弃本轮")
                        break
        else:
            log(f"[抽奖{n}] 点主按钮没反应 -> 判定次数用完(明天再来)")
            break
    log(f"===== 抽奖第 {n} 轮结束, 实际抽奖 {spins} 次 =====")
    return spins


# ================= 第 15/16 步: 宝箱 & 定时领金币(都是 5 分钟一轮, 共用同一套金币弹窗) =================
# 两个任务的共同点(实测):
#   点下去 -> 「恭喜您获得 X 金币」(直接到账) -> 「翻倍后 Y 金币 / 看广告后可领取」
#   -> 橙色「浏览5秒再领 Z 金币」 -> 可能是浏览广告, 也可能是门页型广告 -> 底部 ✕ 关闭
#   领完后按钮变成倒计时(约 5 分钟), 冷却期间点了没反应
TIMED_TPL = "tpl_timed_row.png"     # 202x49 全分辨率灰度, 内容="定时领金币"
TIMED_TPL_X = 84
TIMED_BTN_DY = 22                   # 按钮中心相对模板顶部(实测 2060~2142 中心2101)
TIMED_BTN_X = 895


def find_chest_btn(shot="_fs_chest.png"):
    """找右下角宝箱下面的「领金币」红色胶囊。宝箱是随机冒出来的浮层, 位置固定但出现时机不定,
    所以每轮都重新找一次。实测: 红(255,85,86), 胶囊约 226x132, 中心 (927,2095)。"""
    screencap(shot)
    a = np.asarray(Image.open(shot).convert("RGB")).astype(np.int16)
    oh, ow, _ = a.shape
    x0, y0 = int(0.70 * ow), int(0.78 * oh)
    reg = a[y0:int(0.97 * oh), x0:ow, :]
    r, g, b = reg[:, :, 0], reg[:, :, 1], reg[:, :, 2]
    red = (r > 200) & (g < 115) & (b < 115)
    rows = red.mean(axis=1)
    hit = [y for y in range(len(rows)) if rows[y] > 0.45]
    if not hit:
        log("[宝箱] 右下角没有红色胶囊")
        return None
    seg, cur = [], [hit[0]]
    for y in hit[1:]:
        if y - cur[-1] <= 3:
            cur.append(y)
        else:
            seg = cur if len(cur) > len(seg) else seg
            cur = [y]
    seg = cur if len(cur) > len(seg) else seg
    h = seg[-1] - seg[0]
    if not (50 <= h <= 170):            # 胶囊高度约 130px, 太薄/太厚都不是它
        log(f"[宝箱] 红色区域高 {h}px, 不像胶囊")
        return None
    ys, xs = np.where(red[seg[0]:seg[-1] + 1])
    xx = [int(v) + x0 for v in xs]
    cx = sum(xx) // len(xx)
    cy = (seg[0] + seg[-1]) // 2 + y0
    log(f"[宝箱] 找到「领金币」胶囊 高{h}px 中心({cx},{cy})")
    return cx, cy


def run_claim_popup(tag=""):
    """处理「恭喜获得X金币 / 翻倍后Y金币」弹窗: 有橙色按钮就点(可能带广告), 没了就关掉。
    返回是否走过广告。"""
    did_ad = False
    for i in range(4):
        kind, btn = detect_popup()
        if not btn:
            break
        log(f"[{tag}] 弹窗橙色按钮 ({btn[0]},{btn[1]}) 第{i + 1}次")
        screencap("_fs_cl_size.png")
        ow, oh = screen_size("_fs_cl_size.png")
        if not tap_verified(btn[0], btn[1], wait=2.2):
            log(f"[{tag}] 点不动, 退出循环")
            break
        time.sleep(1.0)
        if is_door_page():
            log(f"[{tag}] 这个弹窗广告是门页型 -> 走门页流程")
            run_ad_once(tag=f"{tag}·广告")
        elif _screen_dark()[0]:
            # 13:32 实录: 点击后 WebView 渲染慢, 截到一帧纯黑, 被误判成"浏览型"去左滑,
            # 结果广告页卡死整轮跑飞。所以黑屏时必须等待重判, 绝不左滑。
            log(f"[{tag}] 整页黑屏(过渡帧/加载中) -> 等待重判")
            turned = False
            for _ in range(4):
                time.sleep(2.2)
                dark, _m = _screen_dark()
                if not dark:
                    turned = True
                    break
            if turned and is_door_page():
                log(f"[{tag}] 过渡帧后露出门页 -> 走门页流程")
                run_ad_once(tag=f"{tag}·广告")
            elif turned:
                log(f"[{tag}] 过渡帧后不是门页 -> 按浏览型处理")
                _browse_ad_and_back(tag, ow, oh)
            else:
                log(f"[{tag}] 持续黑屏 -> 抢救")
                if not rescue_from_stuck_ad():
                    log(f"[{tag}] 抢救失败, 结束本弹窗")
                    break
                goto_coin_center()
        else:
            _browse_ad_and_back(tag, ow, oh)
        did_ad = True
        if not on_coin_center():
            goto_coin_center()
    close_popup()
    return did_ad


def _browse_ad_and_back(tag, ow, oh):
    """浏览型激励广告: 停 5~6s -> 左滑一次 -> 等回金币中心(失败先抢救再导航)。"""
    log(f"[{tag}] 浏览型广告 -> 停满 5~6s 后左滑")
    time.sleep(random.uniform(*INCENTIVE_STAY_RANGE))
    screencap("_fs_cl_pre.png")
    base = _fp("_fs_cl_pre.png")
    _adb("shell", "input", "swipe", str(int(ow * 0.99)), str(int(oh * 0.5)),
         str(int(ow * 0.15)), str(int(oh * 0.5)), "250")
    time.sleep(1.6)
    screencap("_fs_cl_post.png")
    if _dist(base, _fp("_fs_cl_post.png")) < 6.0:
        log(f"[{tag}] 左滑没生效, 不重滑(避免退出两层)")
    if not wait_coin_center(timeout=4.0):
        if focus_is_ad_activity() or not on_coin_center():
            rescue_from_stuck_ad()
        goto_coin_center()


def run_chest_once(tag="宝箱"):
    """点右下角宝箱领金币。返回 'ok' 领到 / 'none' 没有宝箱或冷却中。"""
    log(f"===== {tag} 开始 =====")
    ensure_in_app()
    if not on_coin_center() and not goto_coin_center():
        log(f"[{tag}] 回不到金币中心")
        return "no_page"
    if detect_popup()[1]:                 # 先清掉压着的弹窗, 否则点不到宝箱
        run_claim_popup(tag=f"{tag}·清场")
    btn = find_chest_btn()
    if not btn:
        log(f"[{tag}] 没有宝箱(冷却中/本屏没有) -> 结束")
        return "none"
    log(f"[{tag}] 点宝箱 ({btn[0]},{btn[1]})")
    tap_human(*btn)
    time.sleep(2.0)
    if not detect_popup()[1]:
        log(f"[{tag}] 点完没弹窗 -> 可能已领过")
        return "none"
    run_claim_popup(tag=tag)
    log(f"===== {tag} 结束 =====")
    return "ok"


def run_timed_once(tag="定时领金币", max_scroll=10):
    """滚到「定时领金币」点「领金币」。冷却期间点了不弹窗 -> 返回 'none'。"""
    log(f"===== {tag} 开始 =====")
    ensure_in_app()
    if not on_coin_center() and not goto_coin_center():
        log(f"[{tag}] 回不到金币中心")
        return "no_page"
    close_popup()
    for _ in range(5):                    # 回顶部
        before = "_fs_tt_pre.png"
        screencap(before)
        _adb("shell", "input", "swipe", "540", "700", "540", "1750", "400")
        time.sleep(1.1)
        screencap("_fs_tt_post.png")
        if _dist(_fp(before), _fp("_fs_tt_post.png")) < 3.0:
            break
    for i in range(max_scroll):
        btn = _match_text_row(TIMED_TPL, TIMED_TPL_X, "_fs_timed.png",
                              y_hi=2180, dy=TIMED_BTN_DY, btn_x=TIMED_BTN_X, tag="定时")
        if btn:
            log(f"[{tag}] 找到这一行, 点「领金币」({btn[0]},{btn[1]})")
            tap_verified(btn[0], btn[1], wait=2.2)
            time.sleep(1.2)
            if not detect_popup()[1]:
                log(f"[{tag}] 点了没弹窗 -> 冷却中(每5分钟一次)")
                return "none"
            run_claim_popup(tag=tag)
            log(f"===== {tag} 结束 =====")
            return "ok"
        log(f"[{tag}] 第{i + 1}次下滚找「定时领金币」")
        _adb("shell", "input", "swipe", "540", "1700", "540", "900", "450")
        time.sleep(1.2)
    log(f"[{tag}] 没找到「定时领金币」这一行")
    return "notfound"


# ================= 第 18 步: 金币抽奖(每日首次免费抽奖 + 看10次广告抽金币) =================
# 本模块是金币中心底部一个独立小组件, 和任务14「抽奖赢金币」(老虎机页)不是一回事:
#   状态A(今天还没抽过免费): 模块中间是橙色「每日首次免费抽奖」卡片
#       -> 点 -> 抽奖中 -> 弹「恭喜获得75金币」窗 -> 橙钮「浏览5秒再领125金币」
#       -> PortraitADActivity 浏览广告(角落✕是假关, 必须用系统 BACK 退) -> 九宫格出现
#   状态B(已激活): 3x3 九宫格, 中间橙格「看广告抽奖」
#       -> 点 -> TMECoreActivity 视频广告(12~15s) -> 顶部黑条「退出」(556,93)->(600,223)
#       -> 回九宫格抽奖中 -> 弹「恭喜获得+150金币」 -> 底部圆形✕(500,788)关
# 模板: tpl_lottery_free.png(每日首次免费抽奖卡片) / tpl_lottery_draw.png(看广告抽奖格)
# 两个橙块宽度相同(x 都固定在≈538), 只是文字不同, 用灰度模板区分。
LOTTERY_FREE_TPL = "tpl_lottery_free.png"
LOTTERY_DRAW_TPL = "tpl_lottery_draw.png"
LOTTERY_BROWSE_CTA_REL = (500, 896)   # 浏览型广告: 底部「点击浏览15秒」蓝字 (实测 540,2150)
LOTTERY_TPL_X = 385                 # 模板在屏上的固定 x 起点(橙块恒在 x≈402-675)
LOTTERY_MAX_DRAW = 10               # 每日上限(页面写 10 次)


def _match_lottery_block(tpl_path, shot="_fs_lotblock.png", max_dist=15.0, y_lo=120, y_hi=2280, tag=""):
    """金币抽奖专用: 在固定 x 列做全分辨率灰度模板匹配, 返回橙块中心 (cx,cy) 或 None。
    为什么固定 x: 金币中心里这个橙块横向永远居中(x≈402-675), 只有纵向随滚动变, 所以只扫 y。"""
    screencap(shot)
    a = np.asarray(Image.open(shot).convert("L")).astype(np.int16)
    tpl = np.asarray(Image.open(tpl_path).convert("L")).astype(np.int16)
    oh, ow = a.shape
    th, tw = tpl.shape
    x0 = LOTTERY_TPL_X
    lo, hi = max(0, y_lo), min(oh - th - 5, y_hi)
    best_d, best_y = 999.0, -1
    for y in range(lo, hi):
        d = float(np.abs(a[y:y + th, x0:x0 + tw] - tpl).mean())
        if d < best_d:
            best_d, best_y = d, y
    log(f"[金币抽奖·{tag}] 模板最小差异 {best_d:.1f} @ y={best_y}")
    if best_y < 0 or best_d > max_dist:
        return None
    return int(x0 + tw / 2), best_y + th // 2


def _lottery_state():
    """扫一次屏同时匹配两个模板(免费卡 / 看广告抽奖格), 取差异更小的那个。
    返回 ('free','draw',None 之一, 中心坐标)。两个橙块长得很像(跨匹配差异≈7),
    但真匹配差异≈0, 用 <5 阈值 + 取更小差异来区分, 避免「看广告抽奖格」被误认成免费卡。"""
    screencap("_fs_lot_state.png")
    a = np.asarray(Image.open("_fs_lot_state.png").convert("L")).astype(np.int16)
    oh, ow = a.shape
    tf = np.asarray(Image.open(LOTTERY_FREE_TPL).convert("L")).astype(np.int16)
    td = np.asarray(Image.open(LOTTERY_DRAW_TPL).convert("L")).astype(np.int16)

    def scan(tpl):
        th, tw = tpl.shape
        lo, hi = 120, oh - th - 5
        bd, by = 999.0, -1
        for y in range(lo, hi):
            d = float(np.abs(a[y:y + th, LOTTERY_TPL_X:LOTTERY_TPL_X + tw] - tpl).mean())
            if d < bd:
                bd, by = d, y
        return bd, by, th, tw

    df, yf, thf, twf = scan(tf)
    dd, yd, thd, twd = scan(td)
    log(f"[金币抽奖·状态] free差异{df:.1f} draw差异{dd:.1f}")
    cf = (int(LOTTERY_TPL_X + twf / 2), yf + thf // 2)
    cd = (int(LOTTERY_TPL_X + twd / 2), yd + thd // 2)
    if df < 5.0 and df <= dd:
        return "free", cf
    if dd < 5.0:
        return "draw", cd
    return None, None


def find_lottery_free(shot="_fs_lotfree.png", tag="每日首次免费抽奖"):
    return _match_lottery_block(LOTTERY_FREE_TPL, shot, tag=tag)


def find_lottery_draw(shot="_fs_lotcell.png", tag="看广告抽奖"):
    return _match_lottery_block(LOTTERY_DRAW_TPL, shot, tag=tag)


def _wait_popup(timeout=8.0, interval=1.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if detect_popup()[1]:
            return True
        time.sleep(interval)
    return False


def ensure_lottery_module(max_scroll=14):
    """确保在金币中心且金币抽奖模块已滚入视野(免费卡或九宫格任一可见)。"""
    ensure_in_app()
    if not on_coin_center() and not goto_coin_center():
        log("[金币抽奖] 回不到金币中心")
        return False
    close_popup()
    st, _ = _lottery_state()
    if st in ("free", "draw"):
        return True
    # 回顶部
    for _ in range(6):
        before = "_fs_lt_top.png"
        screencap(before)
        _adb("shell", "input", "swipe", "540", "700", "540", "1750", "400")
        time.sleep(1.1)
        screencap("_fs_lt_top2.png")
        if _dist(_fp(before), _fp("_fs_lt_top2.png")) < 3.0:
            break
    # 从顶部往下找
    for _ in range(max_scroll):
        if find_lottery_free() or find_lottery_draw():
            return True
        _adb("shell", "input", "swipe", "540", "1500", "540", "900", "450")
        time.sleep(1.2)
    return bool(find_lottery_free() or find_lottery_draw())


def lottery_browse_ad_flow(ow, oh, tag=""):
    """浏览型广告流程(2026-11-03 两轮实测验证, 广告主京东/1688 结构一致):
    TMECore广告页 停2~5s -> 点底部「点击浏览15秒」(rel 500,896)
    -> 跳落地页(ADActivity) 停16~18s(规则要求15s+) -> 点左上✕(AD_WEBVIEW_X_REL, rel 150,65)
    -> 回外层TMECore(顶部显示「恭喜获得奖励」=奖励已发) -> 点右上✕(rel 922,110) -> 回金币中心。
    返回 True=流程走完 / False=不是浏览型(调用方走视频型兜底 lottery_ad_exit)。
    注意: 落地页里「点击商品再得额外奖励」是二次引流, 绝不点, 只等计时。"""
    stay = random.uniform(2.0, 5.0)
    log(f"[金币抽奖广告{tag}] 浏览型: 广告页停留 {stay:.1f}s")
    time.sleep(stay)
    bx = int(LOTTERY_BROWSE_CTA_REL[0] / 1000 * ow)
    by = int(LOTTERY_BROWSE_CTA_REL[1] / 1000 * oh)
    log(f"[金币抽奖广告{tag}] 点「点击浏览15秒」({bx},{by})")
    tap_human(bx, by)
    time.sleep(2.5)
    if not focus_is_ad_activity():
        return True                      # 点完直接回金币中心(少数情况)
    cur = _current_focus_name()
    if cur != "ADActivity":              # 没跳落地页 -> 不是浏览型
        log(f"[金币抽奖广告{tag}] 未跳落地页(前台={cur or '?'}) -> 交视频型兜底")
        return False
    stay2 = random.uniform(16.0, 18.0)
    log(f"[金币抽奖广告{tag}] 落地页停留 {stay2:.1f}s (要求15s+)")
    time.sleep(stay2)
    xx = int(AD_WEBVIEW_X_REL[0] / 1000 * ow)
    xy = int(AD_WEBVIEW_X_REL[1] / 1000 * oh)
    log(f"[金币抽奖广告{tag}] 点落地页左上✕ ({xx},{xy})")
    tap_human(xx, xy)
    time.sleep(2.0)
    if focus_is_ad_activity():           # 外层 TMECore, 右上✕
        log(f"[金币抽奖广告{tag}] 点外层右上✕ (rel 922,110)")
        tap_human(int(922 / 1000 * ow), int(110 / 1000 * oh))
        time.sleep(2.0)
    return not focus_is_ad_activity()


def lottery_ad_exit(ow, oh, tag=""):
    """视频型广告兜底(倒计时走完后点顶部「退出」): 当浏览型流程没跳落地页时才走这里。
    关键教训: 绝不能用底部「关闭广告/放弃奖励」(500,659) 那个出口——它会放弃奖励,
    导致广告白看、抽奖不计数、也不弹结果窗。若一次没退出来(广告更长), 再等一轮重试。"""
    top_exits = [(556, 93), (622, 89), (922, 110)]
    for attempt in range(3):
        stay = random.uniform(22.0, 28.0)
        log(f"[金币抽奖广告{tag}] 第{attempt + 1}次停留 {stay:.1f}s 等倒计时")
        time.sleep(stay)
        if not focus_is_ad_activity():
            return True
        for (rx, ry) in top_exits:
            tap_human(int(rx / 1000 * ow), int(ry / 1000 * oh))
            time.sleep(2.0)
            if not focus_is_ad_activity():
                log(f"[金币抽奖广告{tag}] 顶部退出命中(第{attempt + 1}轮)")
                return True
    tap_by_vision(target=EXIT_VISUAL_TARGET, tag=f"-LOTEXIT{tag}")
    return not focus_is_ad_activity()


def lottery_close_result(tag=""):
    """关掉抽奖结果弹窗: 先点底部圆形✕(相对500,788), 不行再视觉兜底。"""
    screencap("_fs_lot_rsize.png")
    ow, oh = screen_size("_fs_lot_rsize.png")
    tap_human(int(500 / 1000 * ow), int(788 / 1000 * oh))
    time.sleep(1.5)
    if not detect_popup()[1]:
        return True
    tap_by_vision(target="中奖弹窗的关闭✕或『开心收下』按钮", tag=f"-LOTOK{tag}")
    time.sleep(1.5)
    return not detect_popup()[1]


def lottery_free_once(tag="金币抽奖·免费"):
    """状态A: 每日首次免费抽奖 -> 抽奖中 -> 75币弹窗 -> 浏览5秒再领125 -> 浏览广告(BACK退) -> 九宫格。
    今天若已做过(已是九宫格)返回 False。用 _lottery_state 区分免费卡/看广告抽奖格, 避免误判。"""
    state, card = _lottery_state()
    if state != "free" or not card:
        log(f"[{tag}] 当前不是免费抽奖卡片状态(已激活九宫格/已做过) -> 跳过")
        return False
    log(f"[{tag}] 点每日首次免费抽奖 ({card[0]},{card[1]})")
    tap_human(*card)
    time.sleep(2.5)
    if not _wait_popup(timeout=9):
        log(f"[{tag}] 没等到抽奖结果弹窗")
        return False
    btn = detect_popup()[1]
    if not btn:
        return False
    log(f"[{tag}] 点橙色「浏览5秒再领金币」({btn[0]},{btn[1]})")
    if not tap_verified(*btn, wait=2.2):
        return False
    time.sleep(1.0)
    if focus_is_ad_activity():
        log(f"[{tag}] 进浏览广告 -> 停满5~6s 后系统BACK退出(角落✕是假关)")
        time.sleep(random.uniform(*INCENTIVE_STAY_RANGE))
        back()
        time.sleep(2.5)
        if focus_is_ad_activity():
            rescue_from_stuck_ad()
    return True


def lottery_draw_once(tag=""):
    """点一次「看广告抽奖」并收尾。返回 True=抽到结果 / False=没抽到(可能次数用完)。"""
    cell = find_lottery_draw()
    if not cell:
        log(f"[金币抽奖·{tag}] 找不到看广告抽奖格 -> 跳过本次")
        return False
    log(f"[金币抽奖·{tag}] 点看广告抽奖 ({cell[0]},{cell[1]})")
    tap_human(*cell)
    time.sleep(2.0)
    screencap("_fs_lot_dsize.png")
    ow, oh = screen_size("_fs_lot_dsize.png")
    # 等广告出现(偶尔直接出结果)
    ad = False
    for _ in range(10):
        if focus_is_ad_activity():
            ad = True
            break
        if detect_popup()[1]:
            lottery_close_result(tag)
            return True
        time.sleep(1.0)
    if ad:
        # 先走浏览型(实测主形态: 点「点击浏览15秒」-> 落地页 -> 两层✕), 失败再走视频型兜底
        if not lottery_browse_ad_flow(ow, oh, tag=tag):
            if not lottery_ad_exit(ow, oh, tag=tag):
                log(f"[金币抽奖·{tag}] 广告没退出 -> 抢救")
                if not rescue_from_stuck_ad():
                    return False
        time.sleep(2.5)
    # 等结果弹窗
    for _ in range(10):
        if detect_popup()[1]:
            lottery_close_result(tag)
            return True
        time.sleep(1.5)
    log(f"[金币抽奖·{tag}] 等不到结果弹窗 -> 判定次数用完/结束")
    return False


def run_lottery_round(n, max_draw=LOTTERY_MAX_DRAW):
    log(f"===== 金币抽奖 第 {n} 轮开始 =====")
    ensure_in_app()
    if not ensure_lottery_module():
        log("[金币抽奖] 找不到模块, 放弃本轮")
        return
    lottery_free_once()                 # 状态A(今天若没做过)
    done = 0
    for i in range(max_draw):
        if not find_lottery_draw():
            log(f"[金币抽奖] 看广告抽奖格不见了(次数用完/页面变化) -> 结束")
            break
        if not lottery_draw_once(tag=f"第{i + 1}抽"):
            log(f"[金币抽奖] 第{i + 1}次没抽到 -> 结束")
            break
        done += 1
    log(f"===== 金币抽奖 第 {n} 轮结束, 实际抽了 {done} 次 =====")
    return done


# ================= 第 20 步: 四任务全跑调度器(干净版, 2026-11-03 从 stash 设计恢复) =================
# 设计依据(用户确认过的串联规则 + 交接文档纪律):
#   - 调度顺序: 看视频领金币(12) -> 看视频得高额金币(13) -> 抽奖赢金币(14) -> 金币抽奖(18)
#   - 任务间必须清弹窗、确保回金币中心, 再进下一个任务(否则弹窗吃掉手势)
#   - 连续 2 轮无进展 = 该任务次数用完, 早停, 不硬跑
#   - 宝箱/定时(15/16) 不并入本调度: D9 铁律, 它们会弹窗打断广告流程, 保持 step17 单独轮询
#   - 汇总只打印控制台, 不写文件(用户偏好: 日志仅控制台)
# 用法: python flow_step.py 20 [任务1轮数] [任务2轮数]   (默认 5 3)


def _rec(results, task, status, count, reason):
    """记录一个任务的最终状态并打印。"""
    results.append({"task": task, "status": status, "count": count, "reason": reason})
    log(f"[最终] {task}: {status} | {count} 次 | {reason}")


def _drain_popup_sched(tag):
    """任务间清场: 只有此刻真的检测到弹窗才关, 不在干净页把任务行橙色按钮当弹窗关掉。"""
    kind, _ = detect_popup()
    if kind:
        log(f"[调度·{tag}] 收尾弹窗={kind} -> 关掉再进下一个任务")
        close_popup()
    else:
        log(f"[调度·{tag}] 无收尾弹窗")


def _run_one_task(results, task, n_rounds, run_func, name):
    """跑一个任务 n 轮, 连续 2 轮无进展即早停。返回完成轮数。"""
    fail, done = 0, 0
    for i in range(1, n_rounds + 1):
        try:
            r = run_func(i)
        except Exception as e:
            log(f"[{name}-{i}] 异常 {type(e).__name__}: {e}")
            r = None
        if r is None:
            fail += 1
            log(f"[调度] {name} 第{i}轮无进展 {fail}/2")
            if fail >= 2:
                _rec(results, task, "DONE(连续2轮无进展)", done, f"第{i}轮起早停")
                return done
        else:
            done += 1
            fail = 0
    _rec(results, task, "DONE(跑满设定轮数)", done, f"{n_rounds}轮")
    return done


def scroll_find_task2(max_scroll=6):
    """从当前位置一屏一屏下滑, 找「看视频得高额金币」那一行(不回顶部, 省时间)。
    滑不动 = 有弹窗压着把滚动手势吃掉, 先关弹窗; 全都不行再回顶部逐屏兜底。"""
    btn = find_task2_row()
    if btn:
        return btn
    for i in range(max_scroll):
        pre, post = "_fs_chain_pre.png", "_fs_chain_post.png"
        screencap(pre)
        log(f"[调度] 第{i + 1}次下滚找任务2")
        _adb("shell", "input", "swipe", "540", "1700", "540", "800", "450")
        time.sleep(0.9)          # WebView 滚动惯性短, 0.9s 足够(实测, 1.3s 纯空等)
        screencap(post)
        btn = find_task2_row()
        if btn:
            return btn
        if _dist(_fp(pre), _fp(post)) < 3.0:
            log("[调度] 滑不动 -> 有弹窗压着手势, 先关弹窗")
            if not close_popup():
                break
    log("[调度] 下滚没找到 -> 回顶部逐屏兜底")
    return ensure_task2_row()


def run_all_daily_tasks(n1=5, n2=3):
    """四任务全跑: 看视频领金币 -> 看视频得高额金币 -> 抽奖赢金币 -> 金币抽奖。
    每任务结束清弹窗回金币中心再进下一个; 汇总打印控制台(不写文件)。"""
    results = []
    log("===== 四任务全跑调度开始 =====")
    ensure_in_app()
    if not on_coin_center():
        goto_coin_center()
    _drain_popup_sched("前置")

    # ---------- 任务1: 看视频领金币 ----------
    log("########## 任务1: 看视频领金币 ##########")
    _run_one_task(results, "看视频领金币", n1, run_round, "T1")
    _drain_popup_sched("任务1后")

    # ---------- 任务2: 看视频得高额金币(需下滑找行) ----------
    log("########## 任务2: 看视频得高额金币 ##########")
    btn = scroll_find_task2()
    if not btn:
        _rec(results, "看视频得高额金币", "STOPPED(找不到行)", 0, "下滑后仍找不到任务行")
    else:
        log(f"[调度] 任务2入口 ({btn[0]},{btn[1]})")
        _run_one_task(results, "看视频得高额金币", n2, run_task2_round, "T2")
        _drain_popup_sched("任务2后")

    # ---------- 任务3: 抽奖赢金币(老虎机, 内部跑到10次或明天再来) ----------
    log("########## 任务3: 抽奖赢金币 ##########")
    try:
        spins = run_draw_round(1)
    except Exception as e:
        log(f"[抽奖] 异常 {type(e).__name__}: {e}")
        spins = None
    if spins is None:
        _rec(results, "抽奖赢金币", "STOPPED(进不去抽奖页)", 0, "ensure_draw_page 失败")
    elif spins >= DRAW_MAX_SPINS:
        _rec(results, "抽奖赢金币", "CAPPED(达每日上限)", spins, f"跑满{DRAW_MAX_SPINS}次")
    else:
        _rec(results, "抽奖赢金币", "DONE(明天再来)", spins, "主按钮无反应=次数用完")

    # ---------- 任务4: 金币抽奖(九宫格, 内部跑到10次或抽奖格消失) ----------
    log("########## 任务4: 金币抽奖 ##########")
    try:
        ldone = run_lottery_round(1, max_draw=LOTTERY_MAX_DRAW)
    except Exception as e:
        log(f"[金币抽奖] 异常 {type(e).__name__}: {e}")
        ldone = None
    if ldone is None:
        _rec(results, "金币抽奖", "STOPPED(找不到模块)", 0, "ensure_lottery_module 失败")
    elif ldone >= LOTTERY_MAX_DRAW:
        _rec(results, "金币抽奖", "CAPPED(达每日上限)", ldone, f"跑满{LOTTERY_MAX_DRAW}次")
    else:
        _rec(results, "金币抽奖", "DONE(抽奖格消失)", ldone, "看广告抽奖格消失或点不动")

    log("===== 四任务全部执行完毕 =====")
    return results


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
    elif step == "14":           # 抽奖赢金币(天天抽奖)循环
        n_rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        for i in range(1, n_rounds + 1):
            try:
                run_draw_round(i)
            except Exception as e:
                log(f"[抽奖-{i}] 异常: {type(e).__name__}: {e}")
    elif step == "15":           # 右下角宝箱领金币
        run_chest_once()
    elif step == "16":           # 定时领金币(每5分钟)
        run_timed_once()
    elif step == "17":           # 两个 5 分钟任务各跑一次(轮询脚本用)
        run_chest_once()
        run_timed_once()
    elif step == "18":           # 金币抽奖(每日首次免费抽奖 + 看10次广告抽金币)
        n_rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        max_draw = int(sys.argv[3]) if len(sys.argv) > 3 else LOTTERY_MAX_DRAW
        for i in range(1, n_rounds + 1):
            try:
                run_lottery_round(i, max_draw=max_draw)
            except Exception as e:
                log(f"[金币抽奖-{i}] 异常: {type(e).__name__}: {e}")
    elif step == "20":           # 四任务全跑: 看视频领金币+高额+抽奖+金币抽奖
        n1 = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        n2 = int(sys.argv[3]) if len(sys.argv) > 3 else 3
        run_all_daily_tasks(n1, n2)
