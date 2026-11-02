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

# ===== 断点测试模式(仅调试用): 设置环境变量 STEP_MODE=1 =====
# 拦截脚本里所有 >=1 秒的等待, 每次暂停:
#   回车 = 按计划等待(观察这一步实际停多久)
#   s    = 跳过这次等待(判断该等太久/不必要)
#   q    = 退出脚本
STEP_MODE = os.getenv("STEP_MODE", "") == "1"
if STEP_MODE:
    print("[断点] STEP_MODE 已开启: 每次>=1s等待会暂停 (回车=等待 / s=跳过 / q=退出)", flush=True)
    _real_sleep = time.sleep

    def _debug_sleep(seconds, *args, **kwargs):
        try:
            if seconds >= 1.0:
                cmd = input(f"[断点] sleep {seconds:.1f}s -> 回车=等待 / s=跳过 / q=退出 > ").strip().lower()
                if cmd == "q":
                    print("[断点] 用户终止", flush=True)
                    sys.exit(0)
                if cmd == "s":
                    print(f"[断点] 跳过 {seconds:.1f}s", flush=True)
                    return
        except EOFError:
            pass
        _real_sleep(seconds, *args, **kwargs)

    time.sleep = _debug_sleep

# ===== 版本2: 金币页硬编码优先(减少AI调用) =====
# 用户指定只刷: ①看视频领金币/看视频得高额金币(按钮文本=看视频) ②抽奖赢金币(立即抽奖) ③金币抽奖页(看广告抽奖)
# ④广告闭环弹窗(浏览5秒/观看下一条/观看完成)——这些是看完广告后连刷入口, 必须保留
# 其余任务(定时领金币/翻卡/红包雨/听歌/分享/收藏/关注/下载等)一律不点, 用户自己刷
UA_TASK_KEYWORDS = [
    "看视频",                                     # 图1看视频领金币 + 图2看视频得高额金币 的按钮(文本=看视频)
    "立即抽奖",                                   # 图2 抽奖赢金币 -> 跳金币抽奖页
    "看广告抽奖",                                 # 图3/图4 金币抽奖页按钮
    "浏览5秒", "浏览广告",                          # 看完广告后的连刷弹窗(保留)
    "观看下一条", "观看完成",                        # 连看弹窗(保留)
]
# 命中后排除: 用户明确不刷的任务按钮(点了没收益或跳第三方/需人工操作)
UA_EXCLUDE_KEYWORDS = [
    "已完成", "去完成", "打开", "去下载", "下载", "安装",
    "去分享", "分享", "去收藏", "收藏", "去关注", "关注",
    "红包雨", "摇钱树", "小组件", "听歌", "听有声书",
    "去购物", "去app", "去应用", "立即下载",
    "定时领金币", "去翻卡", "翻卡", "领额外奖励", "每日免费抽奖",
    "去浏览", "去看看", "去添加", "去听歌", "免费领",
    "每日可以获得", "看视频机会",                     # 金币页提示文字"每日可以获得20次看视频机会..."不是按钮, 排除误命中
    "领金币",                                     # 金币页活动区任务行右侧的"领金币"按钮一律不点(只有看视频领金币板块内的AI视觉区分)
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
    "- 用户指定只刷以下入口: ①【看视频领金币】板块(标题含'看视频领金币', 下方按钮可能显示【看视频】也可能显示【领金币】, 两种都点) "
    "②【看视频得高额金币】的【看视频】按钮 "
    "③【抽奖赢金币】的【立即抽奖】按钮(点击后进入【天天抽奖/免费得好礼】活动页, 和金币转盘不是同一个页面) "
    "④【金币抽奖/天天抽奖/免费得好礼】页的【看广告抽奖】按钮(页面通常显示'剩余看广告次数 X/X'), 点击后看广告抽奖, 抽奖期间等待 "
    "⑤【浏览5秒再领金币/浏览广告/看广告领金币】弹窗按钮(看完广告后弹出的连刷入口, 弹多次是正常的, 每次点它都能再看一条广告再拿金币) "
    "⑥【观看下一条/观看完成+金币】弹窗按钮(看完广告后弹出的连看入口)。\n"
    "- 优先级: 页面同时有【立即抽奖】和【看视频】时, 【优先点立即抽奖】(抽奖次数有限先刷完, 抽完自动回金币页再刷看视频)。\n"
    "- 【看视频板块必须刷完】只要当前屏幕还能看到【看视频领金币】或【看视频得高额金币】板块里的【看视频/领金币/去完成】按钮, 必须先点它, "
    "严禁SWIPE下滑; 只有当前屏幕内这些按钮都不可见或都已领完, 才允许下滑。\n"
    "- 其他所有任务(定时领金币/翻卡/听歌/听有声书/分享/收藏/关注/红包雨/摇钱树/小组件/下载/打开/去/免费领礼物/领额外奖励等) 全部跳过不点击, 用户自己刷。\n"
    "- 特别注意: 【领金币】按钮只有【看视频领金币】板块内的才点(它和板块标题'看视频领金币'在一起); "
    "定时领金币/红包雨来啦/听歌满5分钟等任务行右侧的【领金币】按钮一律不点。\n"
    "- 【金币抽奖页】页面有抽奖转盘/今日剩余抽奖次数/观看视频再抽1次/看广告抽奖等字样时, 这仍属于金币流程页, "
    "严禁输出BACK; 只点【看广告抽奖】或【观看视频再抽1次】, 次数用完就SWIPE。\n"
    "\n"
    "输出规则:\n"
    "- 金币页看到可点击任务按钮(上述6类): {\"action\":\"CLICK\",\"x\":<0~1000>,\"y\":<0~1000>,\"reason\":\"点击什么\"}\n"
    "- 屏幕任务全部不可点/已领过/只剩用户自己刷的任务: {\"action\":\"SWIPE\",\"reason\":\"下滑看更多\"}\n"
    "- 拿不准: {\"action\":\"SWIPE\",\"reason\":\"不确定,下滑看看\"}\n"
    "- 【未知页面识别(重要)】截图既不是金币任务页、也不是广告奖励页(没有任务按钮列表), 而是以下形态时: "
    "歌曲播放页/视频播放页(有播放进度条、暂停/播放按钮、歌曲名或视频画面) → "
    "{\"action\":\"PLAY_WAIT\",\"reason\":\"播放页,听满15秒金币到账后再返回\"}; "
    "空白引导页、加载页、异常页、其他无法判断页面 → {\"action\":\"BACK\",\"reason\":\"非金币页,返回\"}。\n"
    "严禁点击: 活动任务区、定时领金币/翻卡/听歌/分享/收藏/关注/红包雨/下载/领额外奖励类任务、任何带【去xx/打开xx/下载xx/免费领】字样的任务、广告页里的任何按钮(广告页状态机自动处理)。"
)

# 广告页专用prompt: 只判断"该点什么按钮", 不自由发挥
AD_ENTRY_PROMPT = (
    "你是广告页按钮定位Agent。分析截图, 只输出一个JSON对象。\n"
    "规则:\n"
    "1. 页面有【了解详情/点击查看/立即咨询/进入详情页或第三方应用】按钮时: {\"action\":\"CLICK\",\"x\":X,\"y\":X,\"reason\":\"点跳转按钮启动计时\"}, "
    "x,y为该按钮中心相对坐标(0~1000)。注意: 页面可能同时有多个入口(如顶部横幅+底部按钮), 点最明显的那个。"
    "【立即咨询】也是合法的跳转按钮(进入保险H5/咨询详情页, 计时照常走, 看完BACK能退出), 不要因为含'咨询'就拒绝点它。\n"
    "1b. 页面弹出【点击后浏览15秒/点击立即浏览/立即浏览/开始浏览/浏览15秒可获得奖励】这类确认弹窗时: {\"action\":\"CLICK\",\"x\":X,\"y\":Y,\"reason\":\"点立即浏览开始计时\"}, "
    "点弹窗里最显眼的红色/主按钮【点击立即浏览】(不是灰色【放弃奖励】, 点了放弃就没有奖励)。\n"
    "1c. 页面顶部有【点击商品可获得额外100金币/点击商品打开XX再得额外奖励/点击商品查看更多】这类浏览奖励横幅、下方是商品网格列表时: {\"action\":\"CLICK\",\"x\":X,\"y\":Y,\"reason\":\"点第一个商品卡进入详情页计时\"}, "
    "点商品网格里最左上角的第一个商品卡片中心(不是横幅文字本身)。\n"
    "2. 页面有【恭喜已获得奖励/已获得奖励/已完成】提示(说明广告已看完): {\"action\":\"CLOSE\",\"reason\":\"广告已看完,点X关闭\"}。\n"
    "3. 页面是【登录/注册/输入手机号验证码】类表单页(如京东登录页, 特征是手机号/验证码输入框+登录/注册按钮, 没有广告计时的跳转/浏览按钮): {\"action\":\"BACK\",\"reason\":\"登录表单页无广告计时入口,直接返回\"}。\n"
    "4. 两种都不确定: {\"action\":\"WAIT\",\"reason\":\"看不清,再等等\"}。\n"
    "严禁点击: 下载/立即打开/登录/注册/购买/领券/打开微信客服(这些会跳第三方, 不领金币), 严禁输出BACK(登录表单页除外, 见规则3)。只输出JSON。"
)

AD_CLOSE_PROMPT = (
    "你是广告关闭按钮定位Agent。分析截图, 只输出一个JSON对象。\n"
    "任务: 找到广告页/广告弹窗的【关闭/退出按钮】并输出其中心相对坐标(0~1000)。\n"
    "关闭按钮常见形态:\n"
    "- 页面右上角的小X图标(最常见);\n"
    "- 页面左上角的【恭喜获得奖励|退出】提示栏中的【退出】部分(注意是整条提示栏里的退出文字, 不是广告内容);\n"
    "- 弹窗右上角的X图标(弹窗自带的关闭)。\n"
    "输出: {\"action\":\"CLICK\",\"x\":<0~1000>,\"y\":<0~1000>,\"reason\":\"点关闭按钮\"}\n"
    "注意: 严禁点击【立即咨询/了解详情/进入详情页/领取/查看价格】等广告跳转按钮(那是AD_ENTRY的活, 点了会重新进广告), 只点关闭/退出按钮。"
    "若截图看不到任何关闭/退出按钮: {\"action\":\"WAIT\",\"reason\":\"看不到关闭按钮\"}。只输出JSON。"
)

# watchdog 卡住自救专用prompt: 先保住奖励再退出, BACK只作最后手段
RESCUE_PROMPT = (
    "你是卡死自救Agent。脚本已连续10轮截图几乎不变(判定卡住), 现在要决定一次自救动作。"
    "分析截图, 只输出一个JSON对象, 不要多余文字。\n"
    "原则: 优先保住金币奖励, 能点按钮就不要退出; 尽量不BACK, BACK只作最后手段且只返回一次。\n"
    "1. 金币页弹出【继续浏览可额外获得XX金币/浏览5秒再领/观看下一条/观看完成+金币】任务弹窗: "
    "{\"action\":\"CLICK\",\"x\":<0~1000>,\"y\":<0~1000>,\"reason\":\"点继续浏览领奖励, 不要退出\"}。"
    "点奖励按钮本身(继续浏览/立即领取/看视频), 严禁点X或返回, 否则丢奖励任务。\n"
    "2. 金币任务页(有金币狂欢节/看视频领金币等任务列表)但没有可点任务: "
    "{\"action\":\"SWIPE\",\"reason\":\"金币页下滑找任务\"}。\n"
    "3. 广告页/H5详情页/广告弹窗(有广告内容、倒计时、恭喜获得奖励、右上角X/关闭/退出): "
    "{\"action\":\"CLOSE\",\"x\":<0~1000>,\"y\":<0~1000>,\"reason\":\"点关闭X退出广告\"}。"
    "只点关闭/退出按钮, 严禁点了解详情/立即咨询/下载等广告跳转按钮。\n"
    "4. 歌曲播放页/视频播放页(专辑封面、播放进度条、暂停键、歌曲名): "
    "{\"action\":\"PLAY_WAIT\",\"reason\":\"播放页,听满15秒再返回\"}。\n"
    "5. QQ音乐首页(顶部推荐/刷歌/乐馆标签、底部导航栏): 点底部【我的】→ "
    "{\"action\":\"CLICK\",\"x\":<0~1000>,\"y\":<0~1000>,\"reason\":\"点底部我的, 导航回金币页\"}。\n"
    "6. 我的个人中心页(头像/昵称/我的资产): 找【领现金】入口→ "
    "{\"action\":\"CLICK\",\"x\":<0~1000>,\"y\":<0~1000>,\"reason\":\"点领现金, 导航回金币页\"}。\n"
    "7. 登录/注册/商品页/第三方App/空白页/其他无法判断: "
    "{\"action\":\"BACK\",\"reason\":\"无法利用的页面, 返回一次\"}。\n"
    "只输出JSON。"
)

# 导航回金币页prompt: 误退到首页/我的时, 让AI一步步点回金币狂欢节(坐标仍由AI看截图给出, 不硬编码)
NAV_PROMPT = (
    "你是QQ音乐页面导航Agent。脚本当前离开了【金币狂欢节】任务页, 需要一步步导航回去。"
    "分析截图, 只输出一个JSON对象, 不要多余文字。\n"
    "1. 已经是金币狂欢节/赚金币任务页(有'金币狂欢节/赚金币/看视频领金币/立即抽奖'等字样): "
    "{\"action\":\"NONE\",\"reason\":\"已到金币页\"}\n"
    "2. QQ音乐首页(顶部有推荐/刷歌/乐馆/听书标签, 底部有首页/视频/刷歌/星光/我的导航栏): "
    "点底部【我的】→ {\"action\":\"CLICK\",\"x\":<0~1000>,\"y\":<0~1000>,\"reason\":\"点底部我的\"}\n"
    "3. 我的个人中心页(有头像/昵称/我的资产/设置等): 找【领现金】入口→ "
    "{\"action\":\"CLICK\",\"x\":<0~1000>,\"y\":<0~1000>,\"reason\":\"点领现金\"}; 看不到就 "
    "{\"action\":\"SWIPE\",\"reason\":\"下滑找领现金\"}\n"
    "4. 领现金/金币中心页(有领现金/金币/现金提现等): 找【金币狂欢节/赚金币/金币任务】入口→ "
    "{\"action\":\"CLICK\",\"x\":<0~1000>,\"y\":<0~1000>,\"reason\":\"进入金币狂欢节\"}\n"
    "5. 其他页面(歌曲播放页/登录页/加载页/商品页): {\"action\":\"BACK\",\"reason\":\"不属于导航链路, 返回一次\"}\n"
    "严禁点击: 活动任务区的下载/打开/分享/收藏/关注类任务、广告按钮。只输出JSON。"
)

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    # 同时落盘 agent_run.log (供 evolver 采样分析, gitignore)
    try:
        with open("agent_run.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ===== watchdog: 画面相似度卡住检测 =====
WD_SIM_ROUNDS = 10      # 连续N轮画面相似度>=90% -> 判卡住(用户指定)
SAMPLE_DIR = Path("ad_samples")   # 卡住样本存放目录

def calc_screen_sig(path, size=(16, 16)):
    """计算截图特征: 缩略图灰度像素列表(用于相似度比较)"""
    try:
        img = Image.open(path).convert("L").resize(size, Image.LANCZOS)
        return list(img.getdata())
    except Exception:
        return None

def sig_similar(a, b, thresh=0.90):
    """两个特征是否相似度>=thresh (像素差>8 视为不同, 相同比例>=thresh)"""
    if not a or not b or len(a) != len(b):
        return False
    diffs = sum(1 for x, y in zip(a, b) if abs(x - y) > 8)
    return 1 - diffs / len(a) >= thresh

def save_sample(round_no, reason):
    """卡住时被动采样: 3张间隔截图 + 上下文(日志尾部/焦点/状态) 到 ad_samples/<ts>_r<n>/"""
    try:
        ts = time.strftime("%Y%m%d_%H%M%S")
        d = SAMPLE_DIR / f"{ts}_r{round_no}"
        d.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            screencap(SCREEN_PATH)
            with open(d / f"shot_{i}.png", "wb") as f:
                f.write(Path(SCREEN_PATH).read_bytes())
            if i < 2:
                time.sleep(2)
        # 上下文: 焦点 + 最近日志尾部
        focus = current_focus_window()
        ctx = [f"round={round_no}  time={time.strftime('%H:%M:%S')}  reason={reason}",
               f"focus={focus}", ""]
        try:
            with open("agent_run.log", "r", encoding="utf-8") as f:
                tail = f.readlines()[-40:]
            ctx.append("===== 最近日志 =====")
            ctx.extend(l.rstrip("\n") for l in tail)
        except Exception:
            pass
        (d / "context.txt").write_text("\n".join(ctx), encoding="utf-8")
        log(f"[watchdog] 已采样卡住样本: {d}")
    except Exception as e:
        log(f"[watchdog] 采样失败: {str(e)[:60]}")

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
    sys_prompt = (AD_ENTRY_PROMPT if mode == "ad" else
                  AD_CLOSE_PROMPT if mode == "close" else
                  RESCUE_PROMPT if mode == "rescue" else
                  NAV_PROMPT if mode == "nav" else SYSTEM_PROMPT)
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

def _action_gate(label):
    """断点模式下执行动作前暂停: 回车=执行 / s=跳过这个动作 / q=退出"""
    if not STEP_MODE:
        return True
    try:
        cmd = input(f"[断点-动作] 即将{label} -> 回车=执行 / s=跳过 / q=退出 > ").strip().lower()
        if cmd == "q":
            print("[断点] 用户终止", flush=True)
            sys.exit(0)
        return cmd != "s"
    except EOFError:
        return True

def adb_tap(x, y):
    if not _action_gate(f"点击({int(x)},{int(y)})"):
        return
    subprocess.run([ADB, "shell", "input", "tap", str(int(x)), str(int(y))], check=True)

def adb_back():
    if not _action_gate("按返回键"):
        return
    subprocess.run([ADB, "shell", "input", "keyevent", "4"], check=True)

def adb_swipe_up():
    """向上滑动(查看下方任务); 用屏幕中下部滑到上部"""
    if not _action_gate("上滑(查看下方)"):
        return
    subprocess.run([ADB, "shell", "input", "swipe", "500", "1600", "500", "400", "500"], check=True)

def adb_swipe_down():
    """向下滑动(回到页面顶部); 用屏幕上中部滑到下部"""
    if not _action_gate("下滑(回页面顶部)"):
        return
    subprocess.run([ADB, "shell", "input", "swipe", "500", "600", "500", "1800", "500"], check=True)

# ===== 播放页等待: 听满15秒金币才到账, 不能立刻BACK =====
PLAY_WAIT_SECONDS = 15
_play_wait_start = 0.0

# 连续BACK计数: 离开金币页后反复BACK只会退到首页/桌面, 第2次起暂停等用户手动回金币页
_back_streak = 0

def play_wait_then_back(reason=""):
    """播放页: 首轮开始计时, 等满PLAY_WAIT_SECONDS再BACK一次; 未满先等待. 返回PLAY_WAIT或BACK"""
    global _play_wait_start
    if _play_wait_start <= 0:
        _play_wait_start = time.time()
        log(f"[播放页] 检测到播放页, 开始计时(需听满{PLAY_WAIT_SECONDS}s) reason={reason}")
    elapsed = time.time() - _play_wait_start
    if elapsed < PLAY_WAIT_SECONDS:
        remain = PLAY_WAIT_SECONDS - elapsed
        log(f"[播放页] 已听{elapsed:.0f}s, 还需{remain:.0f}s, 先等待")
        time.sleep(min(remain, random.uniform(5, 9)))
        return "PLAY_WAIT"
    log(f"[播放页] 已听满{elapsed:.0f}s, BACK返回(金币应已到账)")
    adb_back()
    _play_wait_start = 0.0
    return "BACK"

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
    # ===== 浮层检测: 转盘/抽奖类浮层盖住金币页时, dump拿不到浮层自己的按钮(WebView), 反而命中下层无效节点
    # 命中这些特征说明当前是浮层页面, 直接交给AI视觉(截图能看到浮层真实按钮)
    # 只认【今日剩余抽奖次数】专属特征(金币页任务卡带"幸运大转盘"字样是正常的, 不能误伤转AI)
    for _ft in ("今日剩余抽奖次数",):
        if _ft in xml:
            log(f"[硬编码] 检测到浮层特征[{_ft}], 转AI视觉判断")
            return None
    click_hits = []   # (面积, cx, cy, txt, kw)
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
        # 只收真正clickable的按钮(广告闭环弹窗/原生按钮). 金币页任务列表是WebView,
        # 非clickable文本节点bounds不可靠(全部堆叠在同一行), 硬编码点击必然无效, 一律转AI视觉定位
        if clickable and clickable.group(1) == "true":
            click_hits.append((area, cx, cy, txt, kw))
    # 优先可点击节点中面积最大的(真正的任务按钮)
    if click_hits:
        click_hits.sort(key=lambda x: x[0], reverse=True)
        _, cx, cy, txt, kw = click_hits[0]
        log(f"[硬编码命中] 可点击节点 文本={txt[:26]} 命中={kw} 坐标({cx},{cy})")
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
    global _last_had_click, _last_uia_pos, _last_uia_time, _uia_streak, _play_wait_start, _back_streak
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
        _play_wait_start = 0.0   # 已回到金币页, 播放计时复位
        _back_streak = 0         # 金币页任务可点, BACK计数复位
        # ===== 防卡死: 同一按钮连续命中3次(45秒内, 间距<30px) -> 判定无效, 改下滑 =====
        now = time.time()
        if (_last_uia_pos is not None and
                abs(cx - _last_uia_pos[0]) < 30 and abs(cy - _last_uia_pos[1]) < 30 and
                now - _last_uia_time < 45):
            _uia_streak += 1
        else:
            _uia_streak = 0
        _last_uia_pos = (cx, cy)
        _last_uia_time = now
        if _uia_streak >= 2:   # 第3次命中同一按钮
            log(f"[防卡死] 连续3次命中同一按钮({cx},{cy}), 判定无效, 改下滑换任务")
            _uia_streak = 0
            _last_uia_pos = None
            adb_swipe_up()
            w = random.uniform(WAIT_MIN, WAIT_MAX)
            log(f"[等待] {w:.1f}s")
            time.sleep(w)
            return "SWIPE"
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
    elif act == "PLAY_WAIT":
        # 播放页: 等满15秒金币到账再BACK, 不能立刻返回丢奖励
        log(f"[播放页:{provider}] reason={reason}")
        act = play_wait_then_back(reason)
    elif act == "BACK":
        # 非广告页被判"非金币页": 先让导航AI判断能否从首页/我的一步步点回金币页, 不再盲目BACK退到桌面死循环
        try:
            nav = call_vlm(b64, mode="nav")
            nact = nav.get("action", "BACK")
            log(f"[导航:{nav.get('_provider','?')}] {nact} reason={nav.get('reason','')}")
            if nact == "CLICK":
                try:
                    rx = float(nav.get("x", 500))
                    ry = float(nav.get("y", 500))
                except (TypeError, ValueError):
                    rx, ry = 500, 500
                x = int(rx / 1000 * ow) + random.randint(-OFFSET, OFFSET)
                y = int(ry / 1000 * oh) + random.randint(-OFFSET, OFFSET)
                log(f"[导航点击] 坐标({x},{y})")
                adb_tap(x, y)
                act = "CLICK"
                _back_streak = 0
            elif nact == "SWIPE":
                adb_swipe_up()
                act = "SWIPE"
                _back_streak = 0
            elif nact == "NONE":
                act = "WAIT"   # 导航AI确认已在金币页, 本轮不动作
                _back_streak = 0
        except Exception as e:
            log(f"[导航异常] {str(e)[:40]}")
        if act == "BACK":
            _back_streak += 1
            if _back_streak >= 2:
                # 连导航AI都连续判定要返回: 暂停等用户手动回金币页(最后手段)
                log(f"[导航失败] 连续{_back_streak}次无法回到金币页, 暂停30秒 —— 请手动回到金币狂欢节页面")
                time.sleep(30)
            else:
                log(f"[返回:{provider}] reason={reason}")
                adb_back()
        # BACK后如果还在广告页: 广告流程尚未走完, 交给主循环ad_streak判断是否重启, 不在此打断
    else:
        log(f"[等待:{provider}] reason={reason}")

    if act != "PLAY_WAIT":
        _play_wait_start = 0.0   # 页面已不是播放页, 计时复位
    if act in ("CLICK", "SWIPE", "PLAY_WAIT", "WAIT"):
        _back_streak = 0         # 页面已有正常动作, BACK计数复位

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
    global _last_had_click, _last_uia_pos, _last_uia_time, _uia_streak, _play_wait_start, _back_streak
    _last_had_click = False
    _play_wait_start = 0.0
    _back_streak = 0
    _last_uia_pos = None      # 上一次硬编码点击的坐标(防卡死用)
    _last_uia_time = 0.0      # 上一次硬编码点击的时间戳
    _uia_streak = 0           # 同一按钮连续命中计数(>=2 即第3次 -> 下滑换任务)

    # 广告状态机内连续WAIT计数(登录页/无入口页兜底BACK) — 仅main/ad_step作用域内使用, 用nonlocal传递
    _ad_wait_streak = 0

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

    # ===== watchdog 状态 =====
    _last_sig = None      # 上一轮画面指纹
    _wd_streak = 0        # 连续相似轮数(>=WD_SIM_ROUNDS 判卡住)

    # ===== 广告流程状态机 (代码强制流转, 不靠模型猜状态) =====
    # AD_IDLE=0 正常金币页 | AD_ENTRY=1 刚进广告页→点跳转按钮(了解详情/立即咨询)
    # AD_WATCH=2 新页面(TransPortraitADActivity)等15秒 | AD_BACK=3 点弹窗右上角X回广告页
    # AD_CLOSE=4 广告页→点右上角X关闭
    AD_IDLE, AD_ENTRY, AD_WATCH, AD_BACK, AD_CLOSE = 0, 1, 2, 3, 4
    ad_state = AD_IDLE
    ad_watch_start = 0.0   # 进入AD_WATCH的时间戳
    ad_close_streak = 0    # AD_CLOSE连续点X次数, >=2改BACK退出
    AD_WATCH_SECONDS = 16  # 详情页最少浏览秒数(15秒+1余量)
    AD_WATCH_MAX_SECONDS = 45  # 30秒视频等长广告: AI说还没播完可继续等, 上限45秒(防止提前关闭丢金币)

    def ad_step(ow, oh):
        """广告状态机单步执行, 返回状态(是否仍需继续广告流程)"""
        nonlocal ad_state, ad_watch_start, ad_close_streak, _ad_wait_streak
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
                    _ad_wait_streak = 0
                    return True
                elif act == "CLOSE":
                    # 模型看到"恭喜已获得奖励" → 直接进入关闭
                    log(f"[广告-已看完] {action.get('reason','')}")
                    ad_state = AD_CLOSE
                    _ad_wait_streak = 0
                    return True
                elif act == "BACK":
                    # 登录/注册表单页: 无广告计时入口, 直接返回(不用等)
                    log(f"[广告-登录页] {action.get('reason','')} —— 直接BACK返回")
                    adb_back()
                    _ad_wait_streak = 0
                    time.sleep(random.uniform(2, 3))
                    ad_state = AD_BACK
                    return True
                else:
                    # WAIT: 连续2次仍无入口(登录页/无意义页) -> 强制BACK, 避免空等
                    _ad_wait_streak += 1
                    log(f"[广告-等待] {action.get('reason','')} —— 再等几秒 ({_ad_wait_streak}/2)")
                    if _ad_wait_streak >= 2:
                        log("[广告-等待超限] 连续2次无跳转入口, 强制BACK返回")
                        adb_back()
                        _ad_wait_streak = 0
                        time.sleep(random.uniform(2, 3))
                        ad_state = AD_BACK
                        return True
                    time.sleep(random.uniform(3, 5))
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
            elif act == "BACK":
                # 详情页竟是登录/注册页(浏览没意义): 直接BACK返回, 不再双路径
                log(f"[广告-详情页登录页] {action.get('reason','')} —— 直接BACK返回")
                adb_back()
                time.sleep(2)
                ad_state = AD_BACK
            elif act == "WAIT" and elapsed < AD_WATCH_MAX_SECONDS:
                # 30秒视频/长广告还没播完(AI说看不清/仍在播放): 继续等, 不能16秒就关掉丢金币
                log(f"[广告-视频未看完] 已等{elapsed:.0f}s, 继续等待(上限{AD_WATCH_MAX_SECONDS}s)")
                time.sleep(random.uniform(3, 5))
                return True
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
            # 已点弹窗X/BACK, 应回到广告页(显示"恭喜已获得奖励") → 交给AD_CLOSE由AI定位关闭按钮
            # (不同广告关闭按钮位置不同, 固定坐标(999,256)常点空, 纯AI判断更稳)
            if is_ad_activity():
                log("[广告-回广告页] 交给AI定位关闭按钮")
                ad_state = AD_CLOSE
            else:
                log("[广告-BACK] 已离开广告Activity, 回金币页")
                ad_state = AD_IDLE
            time.sleep(random.uniform(3, 5))
            return True
        if ad_state == AD_CLOSE:
            # 点过关闭按钮, 确认是否已退出广告
            if not is_ad_activity():
                log("[广告-关闭完成] 已回金币页")
                ad_state = AD_IDLE
            else:
                # 仍判定在广告Activity: 先用AI视觉定位关闭按钮(不同广告X/退出按钮位置不同, 固定坐标常点空)
                if ad_close_streak == 0:
                    try:
                        (ow_c, oh_c), b64_c = capture_and_encode()
                        close_action = call_vlm(b64_c, mode="close")
                        if close_action.get("action") == "CLICK":
                            try:
                                rx = float(close_action.get("x", 500))
                                ry = float(close_action.get("y", 500))
                            except (TypeError, ValueError):
                                rx, ry = 500, 500
                            x = int(rx / 1000 * ow_c) + random.randint(-OFFSET, OFFSET)
                            y = int(ry / 1000 * oh_c) + random.randint(-OFFSET, OFFSET)
                            log(f"[广告-关闭按钮AI定位] 坐标({x},{y}) reason={close_action.get('reason','')}")
                            adb_tap(x, y)
                        else:
                            # AI没找到关闭按钮: 大概率是H5页/登录页(无原生X), 直接BACK比固定X更快更稳
                            log(f"[广告-关闭AI未给出按钮] 直接BACK退出(H5页无原生X)")
                            adb_back()
                    except Exception as e:
                        log(f"[广告-关闭AI失败] 直接BACK退出 ({str(e)[:30]})")
                        adb_back()
                    ad_close_streak += 1
                    time.sleep(random.uniform(3, 5))
                elif ad_close_streak == 1:
                    log("[广告-仍在广告页] BACK后再试固定右上角X")
                    tap_close(ow, oh)
                    ad_close_streak += 1
                    time.sleep(random.uniform(3, 5))
                else:
                    # 仍判定在广告Activity且右上角X无效 → 可能是H5页/弹窗残留/未点立即浏览的确认弹窗
                    log("[广告-仍在广告页且关闭无效] 连续关闭失败, 改BACK退出")
                    adb_back()
                    ad_close_streak = 0
                    time.sleep(random.uniform(3, 5))
                    # BACK后可能回到广告页(确认弹窗/广告本体): 交给AD_ENTRY让模型重新判断
                    # (可能是"点击立即浏览"没点、或"恭喜已获得奖励"该点X, 不能继续死点X)
                    ad_state = AD_ENTRY
            return True
        return False

    while n < rounds:
        n += 1
        log(f"===== 第 {n}/{rounds} 轮 =====")

        # ===== watchdog: 画面相似度卡住检测(用户指定: 同广告刷10轮相似度>=90%判卡住) =====
        # 每轮先截屏做画面指纹, 连续WD_SIM_ROUNDS轮相似 -> 画面没动 -> 判定卡住(不再信日志)
        try:
            _wd_sig = calc_screen_sig(SCREEN_PATH)
            if _wd_sig is not None:
                if _last_sig is not None and sig_similar(_last_sig, _wd_sig):
                    _wd_streak += 1
                else:
                    _wd_streak = 0
                _last_sig = _wd_sig
                if _wd_streak >= WD_SIM_ROUNDS:
                    log(f"[watchdog] 连续{WD_SIM_ROUNDS}轮画面相似度>=90%, 判定卡住(画面未动)")
                    save_sample(n, "画面连续相似>=90%")
                    _wd_streak = 0
                    _last_sig = None
                    _last_uia_pos = None; _uia_streak = 0
                    wait_streak = 0; swipe_streak = 0
                    _ad_wait_streak = 0
                    # 自救: 先让AI看页面决定动作(优先点"继续浏览"保奖励/点X关闭, BACK只作最后手段且只1次)
                    ract = "BACK"
                    try:
                        (wd_w, wd_h), wd_b64 = capture_and_encode()
                        rescue = call_vlm(wd_b64, mode="rescue")
                        ract = rescue.get("action", "BACK")
                        log(f"[watchdog-自救] AI判断={ract} reason={rescue.get('reason','')}")
                        if ract in ("CLICK", "CLOSE"):
                            try:
                                rx = float(rescue.get("x", 500)); ry = float(rescue.get("y", 500))
                            except (TypeError, ValueError):
                                rx, ry = 500, 500
                            x = int(rx / 1000 * wd_w) + random.randint(-OFFSET, OFFSET)
                            y = int(ry / 1000 * wd_h) + random.randint(-OFFSET, OFFSET)
                            log(f"[watchdog-自救] 点击({x},{y})")
                            adb_tap(x, y)
                        elif ract == "SWIPE":
                            adb_swipe_up()
                        elif ract == "PLAY_WAIT":
                            if play_wait_then_back("watchdog自救") == "BACK":
                                ract = "BACK"   # 已听满15秒并BACK
                        else:
                            ract = "BACK"
                            log("[watchdog-自救] BACK一次(最后手段)")
                            adb_back()
                    except Exception as e:
                        log(f"[watchdog-自救AI失败] {str(e)[:40]}, BACK一次")
                        ract = "BACK"
                        adb_back()
                    # 自救后状态衔接: 点X->交给关闭状态确认; BACK->确认是否离开广告; 其余回金币页流程
                    if ract == "CLOSE":
                        ad_close_streak = 0
                        ad_state = AD_CLOSE
                    elif ract == "BACK":
                        ad_state = AD_BACK
                    elif ract == "CLICK" and is_ad_activity():
                        ad_state = AD_WATCH       # 点的是广告跳转/浏览按钮, 重新开始计时
                        ad_watch_start = time.time()
                    else:
                        ad_state = AD_IDLE
                    if ract != "PLAY_WAIT":
                        _play_wait_start = 0.0
                    time.sleep(random.uniform(3, 6))
                    continue
        except Exception as e:
            log(f"[watchdog] 检测异常: {str(e)[:50]}")

        # 兜底0(最高优先): 误点了跳转第三方App的任务(京东/淘宝等), 焦点已离开QQ音乐 → 强制返回
        if not is_in_qqmusic() and not is_ad_activity():
            log("[兜底0] 误跳第三方App, 逐次BACK回QQ音乐(不再一次连按5次, 避免退过头到首页)")
            for i in range(5):
                adb_back()
                time.sleep(1.5)
                if is_in_qqmusic():
                    log(f"[兜底0] 第{i+1}次BACK已回到QQ音乐, 停止返回(尽量保留金币页)")
                    break
            ad_state = AD_IDLE
            _last_uia_pos = None; _uia_streak = 0   # 重置防卡死(页面已变)
            _play_wait_start = 0.0                  # 页面已变, 播放计时复位
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
            _ad_wait_streak = 0   # 重置广告WAIT计数(页面已变)
            _last_uia_pos = None; _uia_streak = 0   # 重置防卡死(页面已变)

        # 兜底1: 连续多次WAIT说明可能卡在某个页面, 强制返回一次
        if wait_streak >= 3:
            log("[兜底] 连续3次无动作, 强制返回上一页")
            adb_back()
            wait_streak = 0
            time.sleep(random.uniform(3, 6))
        # 兜底2: 连续下滑到底 → 滚回页面顶部重新扫描(不用BACK, 避免退到首页/我的)
        if swipe_streak >= 6:
            log("[兜底] 连续6次下滑到底, 滚回金币页顶部重新扫描")
            for _ in range(6):
                adb_swipe_down()
                time.sleep(0.8)
            swipe_streak = 0
            time.sleep(random.uniform(2, 4))
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
