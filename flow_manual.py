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

from PIL import Image
import numpy as np

# ================= 基础环境 =================
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
    for m in pat.finditer(xml):
        t = m.group(1).strip()
        if "观看广告" in t and "可获得奖励" in t:
            sec_m = re.search(r"观看广告\s*(\d+)\s*秒", t)
            sec = int(sec_m.group(1)) if sec_m else None
            done = t.startswith("已观看")
            x1, y1, x2, y2 = (int(m.group(i)) for i in range(2, 6))
            # 点「退出」(文本右端偏右, 竖线|后)
            tx = x2 - int((x2 - x1) * 0.06)
            ty = (y1 + y2) // 2
            log(f"[文字门页] 读到: {t} | 秒数={sec} 完成={done} | 退出按钮约({tx},{ty})")
            return sec, done, t, (x1, y1, x2, y2), tx, ty
    log("[文字门页] 未读到「观看广告XX秒,可获得奖励」文字 -> 非文字门页")
    return None


def _plain_need_stay(sec):
    """广告要求秒数归档(带区间容错): 一进去可能倒计时变少(30变29),
    28-30 都按 30 算, 14-15 按 15, 9-10 按 10。确保停留到广告要求时长。"""
    if sec >= 28:
        return 30.0
    if sec >= 14:
        return 15.0
    if sec >= 9:
        return 10.0
    return float(sec)


def plain_door_exit_flow():
    """单开门/纯白说明型门页出门(文字判断版, 2026-11-04):
    读「观看广告XX秒后/已观看广告XX秒，可获得奖励｜退出」,
       - 进行中(「观看广告XX秒后」): 停留到归档秒数(28-30都按30)+缓冲, 覆盖10/15/30秒
       - 已完成(「已观看广告XX秒」): 直接点「退出」
    文字判断而非纯白页颜色判断(用户明确)。点「退出」用文本右端坐标。"""
    info = read_plain_door_text()
    if info is None:
        log("[文字门页] 不是文字门页, 不做出门")
        return False
    sec, done, text, box, tx, ty = info
    if not done and sec:
        need = _plain_need_stay(sec)
        stay = need + random.uniform(1.0, 2.0)
        log(f"[文字门页] 进行中(广告要求{sec}s, 归档{need:.0f}s), 停留 {stay:.1f}s 再点退出")
        time.sleep(stay)
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
    if read_plain_door_text() is not None:
        log("[进门分流] 单开门文字门页 -> 走单开门出门(停留到秒数+点退出)")
        plain_door_exit_flow()
        return "plain"
    # 2) 双开门: 点门页 CTA
    log("[进门分流] 非单开门 -> 按双开门处理: 点门页 CTA")
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
    log("[进门分流] 仍在QQ音乐(未跳转) -> 停住待命")
    return "normal"


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
    """看视频领金币 全流程(2026-11-04 串联分流):
    启动先 find_popup 判断当前页面, 避免盲点:
      - 当前已是 激励弹窗/观看下一条 -> 直接走对应流程(不再点看视频)
      - 当前无弹窗(应在金币中心)   -> step1 点看视频 -> ad_exit_flow 出门
    出门后循环 find_popup 分流: 激励->incentive_entry_flow / 观看下一条->next_entry_flow,
    直到无弹窗(max_rounds 兜底防死循环)。"""
    first = find_popup()
    if first is not None:
        kind = first[0]
        log(f"[全流程] 启动时已有弹窗({kind}), 直接走对应流程, 不点看视频")
        if kind == "incentive":
            r = incentive_entry_flow()
        elif kind == "next":
            r = next_entry_flow()
        else:
            log(f"[全流程] 未知弹窗 {first}, 停止")
            return
        log(f"[全流程] 启动弹窗处理结束: {r}")
    else:
        step1_watch_video()          # 任务入口: 点「看视频」
        door_entry_flow()            # 进门统一分流: 单开门(文字门页)/双开门(点CTA判断跳转)
    for i in range(1, max_rounds + 1):
        popup = find_popup()
        if popup is None:
            log(f"[全流程] 第{i}轮无弹窗 -> 任务完成")
            break
        kind = popup[0]
        if kind == "incentive":
            log(f"[全流程] 第{i}轮: 激励弹窗")
            r = incentive_entry_flow()
        elif kind == "next":
            log(f"[全流程] 第{i}轮: 观看下一条")
            r = next_entry_flow()
        else:
            log(f"[全流程] 第{i}轮: 未知弹窗 {popup}, 停止")
            break
        log(f"[全流程] 第{i}轮结束: {r}")


if __name__ == "__main__":
    main()
