# -*- coding: utf-8 -*-
"""
QQ音乐金币广告 视觉Agent自动点击器 (Agnes 免费多模态版)
====================================================
链路: adb截屏 -> 压缩图片 -> 调 Agnes agnes-3.0-flash 看图 -> 返回JSON -> adb点击/等待/返回 -> 循环

使用前提:
  1. 手机开启USB调试并连接电脑(见下方"手机设置")
  2. Python依赖: pip install openai pillow python-dotenv
  3. .env 里有 AGNES_API_KEY(你已有)

运行:
  python qqmusic_agent.py

反风控提示(务必看):
  本脚本仅用于你自己低频少量使用。QQ音乐用户协议禁止脚本自动化刷金币,
  存在账号被风控/金币清零/QQ连带限制的风险。已内置随机等待+坐标偏移+随机跳过,
  但仍不建议长时间挂机, 一旦发现金币不增加请立即停止。
"""
import os
import sys
import json
import time
import random
import base64
import io
import re
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image

# ================= 配置区 =================
# adb 完整路径(避免没进 PATH)
ADB = r"D:\桌面\xdf\Agent工程与具身智能\安卓手机控制\platform-tools\platform-tools\adb.exe"

# .env 加载(与 llm_client.py 同目录)
_LLM_CLIENT_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=_LLM_CLIENT_DIR / ".env")

AGNES_BASE_URL = "https://apihub.agnes-ai.com/v1"
AGNES_MODEL = os.getenv("AGNES_MODEL", "agnes-3.0-flash")

MAX_ROUNDS = 200          # 单次运行最多轮数
SHOT_INTERVAL = 2.0       # 连续截图最小间隔(秒)
WAIT_MIN, WAIT_MAX = 6.0, 15.0   # 每次动作后随机等待(反风控)
OFFSET = 12               # 点击坐标随机偏移(千分比坐标下偏移1.2%)
SKIP_PROB = 0.08          # 随机跳过本轮概率(反风控)
RESIZE_WIDTH = 800        # 送模型的图片宽度
SCREEN_PATH = "screen_tmp.png"

# ===== 版本2: 金币页硬编码优先(减少AI调用) =====
# uiautomator dump 命中这些任务文本就直接代码点击, 不调AI; dump拿不到才走AI视觉
UA_TASK_KEYWORDS = [
    "看视频领金币", "看视频得", "看广告领金币",      # 看视频类
    "浏览5秒", "浏览广告",                          # 浏览类(看完广告后的福利/任务)
    "观看下一条", "观看完成",                        # 连看弹窗
    "定时领金币", "每日免费抽奖", "领额外奖励",      # 定时/抽奖
    "去翻卡", "翻卡", "领金币",                     # 金币中心翻卡
]
# 命中后排除: 已完成/活动任务区跳转/分享收藏类(点了没收益或跳第三方)
UA_EXCLUDE_KEYWORDS = [
    "已完成", "去完成", "打开", "去下载", "下载", "安装",
    "去分享", "分享", "去收藏", "收藏", "去关注", "关注",
    "红包雨", "摇钱树", "小组件", "听歌", "听有声书",
    "去购物", "去app", "去应用", "立即下载",
]
# ==========================================

SYSTEM_PROMPT = (
    "你是QQ音乐赚金币页面自动操作Agent。分析截图, 只输出一个JSON对象, 不要任何多余文字和markdown。\n"
    "坐标规则: 0~1000相对值(1000=图片宽或高), x=水平, y=垂直。\n"
    "\n"
    "【页面类型区分(最重要)】\n"
    "- 金币任务页: 有【金币狂欢节/赚金币/兑好礼/看视频领金币/看视频得高额金币/做任务得金币/定时领金币】等字样, 是你的正常操作页面, 按下方规则输出。\n"
    "- 广告页(需跳过判断, 状态机自动处理): 只有出现【点击后看X秒可获得奖励/恭喜已获得奖励/了解详情/进入详情页或第三方应用】+独立的广告内容时才算广告页。"
    "注意: 金币页里的任务名称带【广告】二字【不是】广告页, 例如【看广告领金币】任务按钮仍然要正常点击! \n"
    "\n"
    "【金币任务页判断】\n"
    "- 页面顶部/中部的【活动任务】区域: 打开xx应用/去xx刷/下载xx/打开xx小程序 等跳转第三方App任务一律跳过不点击。\n"
    "- 可点击任务只有这几类: ①【看视频领金币/看视频得高额金币/看视频得金币】②【金币中心看视频翻卡】(领金币/去翻卡) "
    "③【定时领金币】④【每日免费抽奖/领额外奖励】⑤【浏览5秒再领金币/浏览广告/看广告领金币】(点击后进入QQ音乐内嵌广告容器, 由状态机自动处理)"
    "⑥【观看下一条/观看完成+金币】弹窗按钮(看完广告后弹出的连看入口, 弹多次是正常的, 每次点它都能再看一条广告再拿金币)。\n"
    "- 其他所有任务(听歌/听有声书/分享/收藏/关注/红包雨/摇钱树/小组件/下载/打开/去) 全部跳过。\n"
    "\n"
    "输出规则:\n"
    "- 金币页看到可点击任务按钮(上述6类): {\"action\":\"CLICK\",\"x\":<0~1000>,\"y\":<0~1000>,\"reason\":\"点击什么\"}\n"
    "- 屏幕任务全部不可点/已领过: {\"action\":\"SWIPE\",\"reason\":\"下滑看更多\"}\n"
    "- 拿不准: {\"action\":\"SWIPE\",\"reason\":\"不确定,下滑看看\"}\n"
    "严禁点击: 活动任务区、听歌/分享/收藏/关注/红包雨/下载类任务、任何带【去xx/打开xx/下载xx】字样的跳第三方任务、广告页里的任何按钮(广告页状态机自动处理)。"
)

# 广告页专用prompt: 只判断"该点什么按钮", 不自由发挥
AD_ENTRY_PROMPT = (
    "你是广告页按钮定位Agent。分析截图, 只输出一个JSON对象。\n"
    "规则:\n"
    "1. 页面有【了解详情/点击查看/立即咨询/进入详情页或第三方应用】按钮时: {\"action\":\"CLICK\",\"x\":X,\"y\":X,\"reason\":\"点跳转按钮启动计时\"}, "
    "x,y为该按钮中心相对坐标(0~1000)。注意: 页面可能同时有多个入口(如顶部横幅+底部按钮), 点最明显的那个。"
    "【立即咨询】也是合法的跳转按钮(进入保险H5/咨询详情页, 计时照常走, 看完BACK能退出), 不要因为含'咨询'就拒绝点它。\n"
    "2. 页面有【恭喜已获得奖励/已获得奖励/已完成】提示(说明广告已看完): {\"action\":\"CLOSE\",\"reason\":\"广告已看完,点X关闭\"}。\n"
    "3. 两种都不确定: {\"action\":\"WAIT\",\"reason\":\"看不清,再等等\"}。\n"
    "严禁点击: 下载/立即打开/登录/购买/领券/打开微信客服(这些会跳第三方, 不领金币), 严禁输出BACK。只输出JSON。"
)

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def screencap(path):
    """adb截屏保存到文件"""
    with open(path, "wb") as f:
        subprocess.run([ADB, "exec-out", "screencap", "-p"], stdout=f, check=True)

def capture_and_encode():
    """截屏 -> 压缩 -> 返回(原图尺寸, base64)"""
    screencap(SCREEN_PATH)
    img = Image.open(SCREEN_PATH).convert("RGB")
    orig_w, orig_h = img.size
    scale = RESIZE_WIDTH / orig_w
    img2 = img.resize((RESIZE_WIDTH, int(orig_h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img2.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return (orig_w, orig_h), b64

# 多模态供应商降级链 (全部免费/已有key): Agnes -> 智谱GLM-4V-Flash -> 硅基流动DeepSeek-OCR(纯文字兜底)
VLM_PROVIDERS = [
    {
        "name": "Agnes",
        "base_url": "https://apihub.agnes-ai.com/v1",
        "api_key_env": "AGNES_API_KEY",
        "model": os.getenv("AGNES_MODEL", "agnes-3.0-flash"),
    },
    {
        "name": "智谱GLM-4V",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key_env": "ZHIPUAI_API_KEY",
        "model": "glm-4v-flash",
    },
    {
        "name": "硅基DeepSeek-OCR",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key_env": "SILICONFLOW_API_KEY",
        "model": os.getenv("SILICONFLOW_OCR_MODEL", "deepseek-ai/DeepSeek-OCR"),
        "is_ocr": True,  # 纯OCR, 无坐标, 返回文字后按关键词规则判断动作
    },
]

# OCR 兜底: 识别文字后按关键词决定动作(无坐标, 不做CLICK)
def ocr_to_action(text: str) -> dict:
    t = text
    # 商品/购物/购买/第三方App特征 → 返回(误跳第三方)
    if any(k in t for k in ["商品", "购物", "购买", "加入购物车", "立即购买", "店铺", "客服",
                            "降价通知", "京东", "淘宝", "拼多多", "领券", "下单", "满减", "自营"]):
        return {"action": "BACK", "reason": f"OCR识别到商品页/第三方App关键词: {t[:40]}"}
    # 广告播放/剩余秒 → 等待
    if any(k in t for k in ["剩余", "秒", "广告播放", "倒计时", "播放中"]):
        return {"action": "WAIT", "reason": f"OCR识别到广告播放关键词: {t[:40]}"}
    # 活动任务/打开/去/下载 → 下滑(跳第三方App任务)
    if any(k in t for k in ["活动任务", "打开", "去完成", "去分享", "去关注", "下载", "安装"]):
        return {"action": "SWIPE", "reason": f"OCR识别到活动任务区关键词: {t[:40]}"}
    # 拿不准 → 下滑看看
    return {"action": "SWIPE", "reason": f"OCR无明确关键词, 下滑: {t[:40]}"}

def call_vlm(img_b64, mode="normal"):
    """按降级链调用多模态视觉模型, 返回动作dict; 全部失败返回WAIT
    mode: "normal"=金币页常规判断, "ad"=广告页按钮定位"""
    last_err = None
    sys_prompt = AD_ENTRY_PROMPT if mode == "ad" else SYSTEM_PROMPT
    for p in VLM_PROVIDERS:
        api_key = os.getenv(p["api_key_env"])
        if not api_key:
            continue
        try:
            client = OpenAI(base_url=p["base_url"], api_key=api_key, timeout=60)
            # 纯OCR模型: 让它输出全部文字, 再按关键词规则判断
            if p.get("is_ocr"):
                resp = client.chat.completions.create(
                    model=p["model"],
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": "识别这张截图中的所有中文文字，按顺序输出，不要解释。"},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                    ]}],
                    temperature=0.0,
                    max_tokens=800,
                )
                text = resp.choices[0].message.content or ""
                result = ocr_to_action(text)
                result["_provider"] = p["name"]
                result["_ocr_text"] = text[:60]
                return result
            # 视觉理解模型: 输出结构化JSON动作
            resp = client.chat.completions.create(
                model=p["model"],
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": [
                        {"type": "text", "text": "分析这张手机截图，按规则输出JSON。"},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                    ]},
                ],
                temperature=0.0,
                max_tokens=300,
            )
            text = resp.choices[0].message.content or ""
            text = text.strip()
            # 去掉可能的 ```json 包裹
            if "```" in text:
                text = text.split("```")[1].strip()
                if text.startswith("json"):
                    text = text[4:].strip()
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end == -1:
                return {"action": "WAIT", "reason": f"[{p['name']}]解析失败: {text[:100]}"}
            result = json.loads(text[start:end+1])
            result["_provider"] = p["name"]
            return result
        except Exception as e:
            last_err = e
            print(f"⚠️ [{p['name']}] 调用失败({str(e)[:60]}), 切换下一家...")
    return {"action": "WAIT", "reason": f"全部视觉模型失败: {str(last_err)[:80]}"}

def adb_tap(x, y):
    subprocess.run([ADB, "shell", "input", "tap", str(int(x)), str(int(y))], check=True)

def adb_back():
    subprocess.run([ADB, "shell", "input", "keyevent", "4"], check=True)

def adb_swipe_up():
    """向上滑动(查看下方任务); 用屏幕中下部滑到上部"""
    subprocess.run([ADB, "shell", "input", "swipe", "500", "1600", "500", "400", "500"], check=True)

# 右上角关闭X的位置(相对坐标0~1000, 用户截图标定约925,107)
CLOSE_X_REL, CLOSE_Y_REL = 925, 107

def current_focus_window():
    """返回当前真实焦点窗口字符串(取最后一个非null的mCurrentFocus)"""
    try:
        out = subprocess.run([ADB, "shell", "dumpsys", "window"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""
    # dumpsys输出可能有多条mCurrentFocus记录(含null残留), 取最后一个非null的
    last = ""
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("mCurrentFocus="):
            if "null" not in s:
                last = s
    return last

def is_ad_activity():
    """检查当前焦点是否仍在广告Activity(广告页屏蔽返回键)
    只查当前焦点窗口, 不查整个dumpsys输出(避免历史残留误判)
    覆盖两类容器: TMECoreActivity(广告页本体) + TransPortraitADActivity(点跳转后进入的新页面)"""
    focus = current_focus_window()
    return any(k in focus for k in
               ("TMECoreActivity", "tmead", "TransPortraitADActivity", "com.qq.e.tg"))

def is_in_qqmusic():
    """当前焦点是否在QQ音乐内(主界面/金币页/广告页都属于QQ音乐包)"""
    return "com.tencent.qqmusic" in current_focus_window()

def tap_close(ow, oh):
    """点击右上角X关闭广告(用户截图标定约925,107)"""
    x = int(CLOSE_X_REL / 1000 * ow)
    y = int(CLOSE_Y_REL / 1000 * oh)
    log(f"[点X关闭] 坐标({x},{y})")
    adb_tap(x, y)

def uia_detect_click():
    """版本2: 金币页硬编码优先. uiautomator dump命中白名单任务文本 -> 返回像素中心坐标; 未命中返回None"""
    try:
        subprocess.run([ADB, "shell", "uiautomator", "dump", "/sdcard/uidump.xml"],
                       capture_output=True, timeout=8)
        out = subprocess.run([ADB, "exec-out", "cat", "/sdcard/uidump.xml"],
                             capture_output=True, timeout=8)
        xml = out.stdout.decode("utf-8", errors="ignore")
    except Exception as e:
        log(f"[硬编码] uiautomator dump失败: {str(e)[:40]}")
        return None
    if not xml or "<node" not in xml:
        return None
    click_hits = []   # (面积, cx, cy, txt, kw)
    any_hits = []
    for m in re.finditer(r'<node[^>]*>', xml):
        node = m.group(0)
        t = re.search(r'text="([^"]*)"', node)
        d = re.search(r'content-desc="([^"]*)"', node)
        txt = (t.group(1) if t else "") or (d.group(1) if d else "")
        if not txt:
            continue
        kw = _match_task_keyword(txt)
        if kw is None:
            continue
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
        if not b:
            continue
        x1, y1, x2, y2 = (int(b.group(i)) for i in range(1, 5))
        w, h = x2 - x1, y2 - y1
        # 过滤无效bounds: 宽高必须>20px且落在屏幕内(有些节点是0尺寸装饰/异常节点)
        if w < 20 or h < 20 or x2 > 1300 or y2 > 2700:
            continue
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        clickable = re.search(r'clickable="([^"]*)"', node)
        area = w * h
        if clickable and clickable.group(1) == "true":
            click_hits.append((area, cx, cy, txt, kw))
        else:
            any_hits.append((area, cx, cy, txt, kw))
    # 优先可点击节点中面积最大的(真正的任务按钮); 无则退回任意命中中面积最大的(父容器可点)
    if click_hits:
        click_hits.sort(key=lambda x: x[0], reverse=True)
        _, cx, cy, txt, kw = click_hits[0]
        log(f"[硬编码命中] 可点击节点 文本={txt[:26]} 命中={kw} 坐标({cx},{cy})")
        return cx, cy
    if any_hits:
        any_hits.sort(key=lambda x: x[0], reverse=True)
        _, cx, cy, txt, kw = any_hits[0]
        log(f"[硬编码命中] 非clickable节点(父容器可点) 文本={txt[:26]} 命中={kw} 坐标({cx},{cy})")
        return cx, cy
    return None

def _match_task_keyword(txt):
    """返回txt命中的白名单关键词; 命中排除词或非白名单则返回None"""
    if any(ek in txt for ek in UA_EXCLUDE_KEYWORDS):
        return None
    for kw in UA_TASK_KEYWORDS:
        if kw in txt:
            return kw
    return None

def run_once():
    """单轮: 硬编码优先 -> 截屏识图兜底"""
    global _last_had_click
    if random.random() < SKIP_PROB:
        w = random.uniform(4, 10)
        log(f"[跳过本轮] 随机暂停 {w:.1f}s")
        time.sleep(w)
        return

    # ===== 版本2: 金币页硬编码优先(不调AI) =====
    # 只在非广告状态下执行(广告页由状态机接管, 不进run_once)
    uia_hit = uia_detect_click()
    if uia_hit:
        cx, cy = uia_hit
        cx += random.randint(-OFFSET, OFFSET)
        cy += random.randint(-OFFSET, OFFSET)
        log(f"[点击:硬编码] 坐标({cx},{cy})")
        try:
            adb_tap(cx, cy)
        except Exception as e:
            log(f"[点击失败] {e}")
        _last_had_click = True
        w = random.uniform(WAIT_MIN, WAIT_MAX)
        log(f"[等待] {w:.1f}s")
        time.sleep(w)
        return "CLICK"

    try:
        (ow, oh), b64 = capture_and_encode()
    except Exception as e:
        log(f"[截屏失败] {e} —— 检查手机是否连接/USB调试是否开启")
        time.sleep(5)
        return "FAIL"

    action = call_vlm(b64)
    act = action.get("action", "WAIT")
    provider = action.get("_provider", "?")
    reason = action.get("reason", "")

    if act == "CLICK":
        # 相对坐标(0~1000)映射回原图坐标; 模型可能返回字符串, 先转float
        try:
            rx = float(action.get("x", 500))
            ry = float(action.get("y", 500))
        except (TypeError, ValueError):
            rx, ry = 500, 500
        x = int(rx / 1000 * ow)
        y = int(ry / 1000 * oh)
        # 随机偏移反风控
        x += random.randint(-OFFSET, OFFSET)
        y += random.randint(-OFFSET, OFFSET)
        log(f"[点击:{provider}] 坐标({x},{y}) reason={reason}")
        try:
            adb_tap(x, y)
        except Exception as e:
            log(f"[点击失败] {e}")
        # 点击即视为广告流程有进展, 重置卡死计数
        _last_had_click = True
    elif act == "SWIPE":
        log(f"[下滑:{provider}] reason={reason}")
        adb_swipe_up()
    elif act == "BACK":
        log(f"[返回:{provider}] reason={reason}")
        adb_back()
        # BACK后如果还在广告页: 广告流程尚未走完, 交给主循环ad_streak判断是否重启, 不在此打断
    else:
        log(f"[等待:{provider}] reason={reason}")

    w = random.uniform(WAIT_MIN, WAIT_MAX)
    log(f"[等待] {w:.1f}s")
    time.sleep(w)
    return act

def main():
    if not os.getenv("AGNES_API_KEY"):
        log("!! .env 里没有 AGNES_API_KEY")
        sys.exit(1)
    if not Path(ADB).exists():
        log(f"!! adb 路径不存在: {ADB}")
        sys.exit(1)
    global _last_had_click
    _last_had_click = False

    # 支持命令行传轮数: python qqmusic_agent.py 5 (试跑5轮)
    rounds = MAX_ROUNDS
    if len(sys.argv) > 1:
        try:
            rounds = int(sys.argv[1])
        except ValueError:
            pass

    try:
        out = subprocess.run([ADB, "devices"], capture_output=True, text=True).stdout
        log("设备状态:\n" + out.strip())
        if "device\n" not in out and "\tdevice" not in out:
            log("!! 没有检测到已连接的手机。请先完成下方手机设置再运行。")
    except Exception as e:
        log(f"!! adb 调用失败: {e}")
        sys.exit(1)

    n = 0
    wait_streak = 0   # 连续WAIT计数, 连续3次强制BACK兜底
    swipe_streak = 0  # 连续SWIPE计数, 连续8次强制BACK回顶部

    # ===== 广告流程状态机 (代码强制流转, 不靠模型猜状态) =====
    # AD_IDLE=0 正常金币页 | AD_ENTRY=1 刚进广告页→点跳转按钮(了解详情/立即咨询)
    # AD_WATCH=2 新页面(TransPortraitADActivity)等15秒 | AD_BACK=3 点弹窗右上角X回广告页
    # AD_CLOSE=4 广告页→点右上角X关闭
    AD_IDLE, AD_ENTRY, AD_WATCH, AD_BACK, AD_CLOSE = 0, 1, 2, 3, 4
    ad_state = AD_IDLE
    ad_watch_start = 0.0   # 进入AD_WATCH的时间戳
    ad_close_streak = 0    # AD_CLOSE连续点X次数, >=2改BACK退出
    AD_WATCH_SECONDS = 16  # 详情页需浏览秒数(15秒+1余量)

    def ad_step(ow, oh):
        """广告状态机单步执行, 返回状态(是否仍需继续广告流程)"""
        nonlocal ad_state, ad_watch_start, ad_close_streak
        if ad_state == AD_IDLE:
            return False
        if ad_state == AD_ENTRY:
            # 广告页: 让模型定位"了解详情/点击查看/进入详情页"按钮
            try:
                (w2, h2), b64 = capture_and_encode()
                action = call_vlm(b64, mode="ad")
                act = action.get("action", "WAIT")
                if act == "CLICK":
                    rx = float(action.get("x", 500)); ry = float(action.get("y", 500))
                    x = int(rx / 1000 * w2); y = int(ry / 1000 * h2)
                    x += random.randint(-OFFSET, OFFSET); y += random.randint(-OFFSET, OFFSET)
                    log(f"[广告-点了解详情] 坐标({x},{y}) reason={action.get('reason','')}")
                    adb_tap(x, y)
                    ad_state = AD_WATCH
                    ad_watch_start = time.time()
                    return True
                elif act == "CLOSE":
                    # 模型看到"恭喜已获得奖励" → 直接进入关闭
                    log(f"[广告-已看完] {action.get('reason','')}")
                    ad_state = AD_CLOSE
                    return True
                else:
                    log(f"[广告-等待] {action.get('reason','')} —— 再等几秒")
                    time.sleep(random.uniform(4, 7))
                    return True
            except Exception as e:
                log(f"[广告-入口异常] {e}")
                time.sleep(3)
                return True
        if ad_state == AD_WATCH:
            # 详情页/新页面: 强制等待满15秒(倒计时数字会卡住不走, 不能等它归零)
            elapsed = time.time() - ad_watch_start
            if elapsed < AD_WATCH_SECONDS:
                remain = max(1, AD_WATCH_SECONDS - elapsed)
                log(f"[广告-详情页浏览] 已看{elapsed:.0f}s, 还需{remain:.0f}s")
                time.sleep(min(remain, random.uniform(3, 5)))
                return True
            # 等满后: 先让模型看当前页面(可能已自动变"恭喜已获得奖励", 也可能还弹着咨询窗/H5页)
            try:
                (w3, h3), b64 = capture_and_encode()
                action = call_vlm(b64, mode="ad")
                act = action.get("action", "WAIT")
            except Exception:
                act = "WAIT"
            if act == "CLOSE":
                log(f"[广告-已看完] {action.get('reason','')}")
                ad_state = AD_CLOSE
            else:
                # 双路径退出: ①H5页面(如保险登录页)可BACK返回 ②咨询弹窗可点弹窗右上角X
                log("[广告-详情页看完] 先BACK返回(处理H5页), 再点弹窗右上角X(处理弹窗)")
                adb_back()
                time.sleep(2)
                adb_tap(int(935 / 1000 * ow), int(622 / 1000 * oh))
                ad_state = AD_BACK
            time.sleep(2)
            return True
        if ad_state == AD_BACK:
            # 已点弹窗X/BACK, 应回到广告页(显示"恭喜已获得奖励") → 点右上角X关闭
            if is_ad_activity():
                log("[广告-回广告页] 点右上角X关闭")
                tap_close(ow, oh)
                ad_state = AD_CLOSE
            else:
                log("[广告-BACK] 已离开广告Activity, 回金币页")
                ad_state = AD_IDLE
            time.sleep(random.uniform(3, 5))
            return True
        if ad_state == AD_CLOSE:
            # 点过右上角X, 确认是否已退出广告
            if not is_ad_activity():
                log("[广告-关闭完成] 已回金币页")
                ad_state = AD_IDLE
            else:
                # 仍判定在广告Activity但右上角X无效 → 可能是H5页/弹窗残留, 换BACK退出, 避免死循环点X
                if ad_close_streak >= 2:
                    log("[广告-仍在广告页且X无效] 连续点X无效, 改BACK退出")
                    adb_back()
                    ad_close_streak = 0
                    time.sleep(random.uniform(3, 5))
                else:
                    log("[广告-仍在广告页] 再点一次右上角X")
                    tap_close(ow, oh)
                    ad_close_streak += 1
                    time.sleep(random.uniform(3, 5))
            return True
        return False

    while n < rounds:
        n += 1
        log(f"===== 第 {n}/{rounds} 轮 =====")

        # 兜底0(最高优先): 误点了跳转第三方App的任务(京东/淘宝等), 焦点已离开QQ音乐 → 强制返回
        if not is_in_qqmusic() and not is_ad_activity():
            log("[兜底0] 误跳第三方App, 强制返回QQ音乐")
            for _ in range(5):
                adb_back()
                time.sleep(1.5)
            ad_state = AD_IDLE
            time.sleep(random.uniform(3, 6))
            continue

        # 广告状态机优先: 一旦进入广告流程, 由状态机接管直到回金币页
        if is_ad_activity():
            if ad_state == AD_IDLE:
                ad_state = AD_ENTRY
            ad_step(1080, 2400)
            time.sleep(SHOT_INTERVAL)
            continue

        # 不在广告Activity但状态机未复位 → 视为已回金币页
        if ad_state != AD_IDLE:
            log("[状态机] 已离开广告Activity, 复位")
            ad_state = AD_IDLE

        # 兜底1: 连续多次WAIT说明可能卡在某个页面, 强制返回一次
        if wait_streak >= 3:
            log("[兜底] 连续3次无动作, 强制返回上一页")
            adb_back()
            wait_streak = 0
            time.sleep(random.uniform(3, 6))
        # 兜底2: 连续多次下滑说明可能滑到底了, 强制BACK回任务页顶部
        if swipe_streak >= 8:
            log("[兜底] 连续8次下滑, 强制返回重新扫描")
            adb_back()
            swipe_streak = 0
            time.sleep(random.uniform(3, 6))
        act = run_once()
        if act == "WAIT":
            wait_streak += 1
        else:
            wait_streak = 0
        if act == "SWIPE":
            swipe_streak += 1
        else:
            swipe_streak = 0
        time.sleep(SHOT_INTERVAL)
    log("到达轮数上限, 结束。")

if __name__ == "__main__":
    main()
