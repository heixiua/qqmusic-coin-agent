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
  python qqmusic_agent.py [轮数]      # 例: python qqmusic_agent.py 20

反风控提示(务必看):
  本脚本仅用于你自己低频少量使用。QQ音乐用户协议禁止脚本自动化刷金币,
  存在账号被风控/金币清零/QQ连带限制的风险。已内置随机等待+坐标偏移+随机跳过,
  但仍不建议长时间挂机, 一旦发现金币不增加请立即停止。

代码结构(2026-11-02 重构):
  配置/常量/prompts 留在模块级; 所有"运行期状态"收进 QQCoinAgent 实例属性,
  消除原脚本里十几个 global 声明。核心方法: run_once(单轮) / ad_step(广告状态机单步)
  / full_scan(终局验证扫描) / run(主循环)。
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
import threading
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

MAX_ROUNDS = 20           # 单次运行最多轮数(用户要求改20, 不跑150那么多)
SHOT_INTERVAL = 1.0       # 轮间隔(秒); AI调用本身已耗时10-20s, 无需再等太多
WAIT_MIN, WAIT_MAX = 1.0, 2.0    # 每次动作后随机等待(反风控, AI调用延迟已提供间隔)
PLAY_WAIT_SECONDS = 15      # 播放页最少听满秒数(金币才到账)
AD_WATCH_SECONDS = 16       # 广告详情页默认等待秒数(读不到入口秒数时兜底; 覆盖5秒/15秒广告)
AD_DEFAULT_AD_SECONDS = 15  # 门页"点击后看X秒"读不到秒数时的默认值(QQ音乐广告多为15秒)
AD_WATCH_MAX_SECONDS = 31   # [已停用] 旧版30秒视频继续等待上限; 现统一16s后直接关闭, 仅保留常量备查
# 广告门页("点击后看15秒, 可获得奖励")上真正能进入广告的区域: 视频画面中心(千分比)。
# 实拍帧确认: 底部"立即下载/立即浏览"按钮点击常被视频层吞掉, BACK/X 也被门页拦截。
AD_DOOR_VIDEO_X_REL, AD_DOOR_VIDEO_Y_REL = 500, 600
EXTERNAL_INCENTIVE_WAIT = 5 # 激励"浏览5秒"跳商店/浏览器后, 至少等5秒再BACK(否则金币不到账)
OFFSET = 12               # 点击坐标随机偏移(千分比坐标下偏移1.2%)
SKIP_PROB = 0.08          # 随机跳过本轮概率(反风控)
RESIZE_WIDTH = 800        # 送模型的图片宽度
SCREEN_PATH = "screen_tmp.png"

# ===== 金币页硬编码优先(减少AI调用) =====
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
    "- 【同类任务可重复(重要)】看视频领金币/看视频得高额金币/观看视频再抽1次等任务每天有多次机会, 只要按钮还在、"
    "且不是【已完成/已领完/次数0/灰色不可点】, 就继续点; 严禁因为'动作历史里出现过CLICK'或'刚才点过一次'就判定这个任务已完成。"
    "动作历史只用于判断连续SWIPE/BACK是否卡住, 不代表任何任务已完成。\n"
    "- 【广告关闭回到金币页后】优先点击当前屏幕内仍可见的看视频/立即抽奖/浏览5秒按钮, 不要先下滑找别的任务; "
    "只有当前屏幕确实没有可点按钮时才允许下滑。\n"
    "- 其他所有任务(定时领金币/翻卡/听歌/听有声书/分享/收藏/关注/红包雨/摇钱树/小组件/下载/打开/去/免费领礼物/领额外奖励等) 全部跳过不点击, 用户自己刷。\n"
    "- 特别注意: 【领金币】按钮只有【看视频领金币】板块内的才点(它和板块标题'看视频领金币'在一起); "
    "定时领金币/红包雨来啦/听歌满5分钟等任务行右侧的【领金币】按钮一律不点。\n"
    "- 【金币抽奖页】页面有抽奖转盘/今日剩余抽奖次数/观看视频再抽1次/看广告抽奖等字样时, 这仍属于金币流程页: "
    "次数没用完就只点【看广告抽奖】或【观看视频再抽1次】; 金币转盘页(下方有可下滑的任务列表)次数用完就SWIPE。\n"
    "- 【天天抽奖/免费得好礼】整页抽奖机页(页面显示'看广告抽奖'和'剩余看广告次数 X/10'): "
    "X>0 → {\"action\":\"CLICK\",\"x\":<0~1000>,\"y\":<0~1000>,\"reason\":\"点看广告抽奖\"}; "
    "X=0/次数已用完且页面不能下滑 → {\"action\":\"BACK\",\"reason\":\"天天抽奖次数已用完,返回金币页\"}, 严禁SWIPE。\n"
    "- 【看广告领好礼/金币抽奖页】页面显示【今日奖品已派发完毕/明日再来】时: "
    "{\"action\":\"BACK\",\"reason\":\"今日奖品已派发完毕,返回金币页\"}, 严禁点击页面按钮或SWIPE。\n"
    "- 【用动作历史判断卡住(重要)】输入会附带'最近动作历史(旧→新)', 这是你前几轮真实执行的动作: "
    "①连续3次以上SWIPE且中间没有任何CLICK = 你在原地重复下滑(页面不可滚动或已到底), 禁止再SWIPE; "
    "整页活动页(天天抽奖/免费得好礼/抽奖机/详情页) → BACK; 金币任务列表页 → TOP(回页面顶部重新扫描); "
    "②动作历史里SWIPE/CLICK交替, 或每轮之间有CLICK(看广告抽奖/浏览5秒/看视频) = 正常的连续刷广告, 不算卡住, 继续按任务规则点击。\n"
    "\n"
    "输出规则:\n"
    "- 金币任务列表连续下滑卡住, 需要回到页顶重新扫描: {\"action\":\"TOP\",\"reason\":\"回顶部重新扫描\"}\n"
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
    "0. 优先留在应用内: 只要页面有 1b【立即浏览/浏览15秒/开始浏览】或 1c【点击商品卡】这类不会跳出QQ音乐的入口, 就优先点它们; "
    "只有在完全没有这类入口时, 才点 1 的【了解详情/进入详情页或第三方应用】(会跳浏览器/应用商店, 尽量少触发, 跳出去既拿不到金币又易卡死)。\n"
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
    "5. 若页面/按钮/弹窗上明确写着'点击后浏览X秒'或'看X秒广告'这类秒数, 请在JSON里额外加一个字段 \"seconds\":X (X为3~60的整数); 读不到秒数就不要加这个字段。\n"
    "严禁点击: 下载/立即打开/登录/注册/购买/领券/打开微信客服(这些会跳第三方, 不领金币), 严禁输出BACK(登录表单页除外, 见规则3)。只输出JSON。"
)

AD_CLOSE_PROMPT = (
    "你是广告关闭按钮定位Agent。分析截图, 只输出一个JSON对象。\n"
    "任务: 找到广告页/广告弹窗的【关闭/退出/返回按钮】并输出其中心相对坐标(0~1000)。\n"
    "关闭按钮常见形态(任选其一, 优先最明显且肯定能关的那一个):\n"
    "- 页面右上角的小X图标(最常见);\n"
    "- 页面左上角的【恭喜获得奖励|退出】提示栏中的【退出】部分(注意是整条提示栏里的退出文字, 不是广告内容);\n"
    "- 页面左上角的【返回】文字或返回箭头(有时就是关闭方式);\n"
    "- 页面/弹窗中的【关闭】文字;\n"
    "- 弹窗右上角的X图标(弹窗自带的关闭)。\n"
    "输出: {\"action\":\"CLICK\",\"x\":<0~1000>,\"y\":<0~1000>,\"reason\":\"点关闭按钮\"}\n"
    "注意: 严禁点击【立即咨询/了解详情/进入详情页/领取/查看价格】等广告跳转按钮(那是AD_ENTRY的活, 点了会重新进广告), 只点关闭/退出/返回按钮。"
    "若截图看不到任何关闭/退出按钮: {\"action\":\"WAIT\",\"reason\":\"看不到关闭按钮\"}。只输出JSON。"
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
    "5. 其他页面(天天抽奖/免费得好礼整页抽奖机且次数已用完/歌曲播放页/登录页/加载页/商品页): "
    "{\"action\":\"BACK\",\"reason\":\"不属于导航链路, 返回一次\"}\n"
    "严禁点击: 活动任务区的下载/打开/分享/收藏/关注类任务、广告按钮。只输出JSON。"
)

# 全量扫描prompt: 逐屏判断四个指定任务是否还有可做的
SCAN_TASKS = ["看视频领金币", "看视频得高额金币", "抽奖赢金币", "金币抽奖"]
SCAN_PROMPT = (
    "你是金币任务全量扫描Agent。判断当前截图是否为【金币狂欢节/金币中心】任务页, 并判断四个指定任务在本屏的状态。"
    "只输出一个JSON对象, 不要多余文字。\n"
    "四个任务: 看视频领金币 / 看视频得高额金币 / 抽奖赢金币 / 金币抽奖(看广告领好礼、金币抽奖格子)。\n"
    "状态定义: available=本屏清晰出现该任务的明确可点按钮(看视频板块内的【看视频/领金币/去完成】、"
    "抽奖赢金币行的【立即抽奖】、金币抽奖格子的【去抽奖/立即抽奖】), 且按钮不是灰色/未显示已完成; "
    "done=显示已完成/已领完/明日再来/次数0/今日奖品已派发完毕; absent=本屏没有该任务的可点按钮。\n"
    "重要: 只看到板块标题、奖金额度卡片、进度条、奖品图标、右下角悬浮【领金币】、"
    "每日任务里定时领金币/红包雨等行右侧的【领金币】, 都不算available, 一律记absent。\n"
    "输出格式: {\"is_coin_page\":true或false, \"看视频领金币\":\"available|done|absent\", "
    "\"看视频得高额金币\":\"...\", \"抽奖赢金币\":\"...\", \"金币抽奖\":\"...\"}\n"
    "只输出JSON。"
)

# 右上角关闭X的位置(相对坐标0~1000, 用户截图标定约925,107)
CLOSE_X_REL, CLOSE_Y_REL = 925, 107        # 右上角X关闭(用户标定点)
CLOSE_LEFT_X_REL, CLOSE_LEFT_Y_REL = 80, 110  # 左上角X/返回箭头(约80,110)
# 关闭X的候选位置(千分比): 不同广告SDK位置不同(红果在下排控制条 y≈107, 抖音在顶部 y≈67),
# 固定一个坐标会漏, 按顺序试、点一次复查一次。
CLOSE_CANDIDATES = [(912, 67), (925, 107), (960, 88), (905, 130)]
CLOSE_NODE_MIN_X = 780     # "右上角区域"判定: x > 78% 屏宽
CLOSE_NODE_MAX_Y = 220     # y < 22% 屏高
# 点X后常弹"现在退出就没有奖励哦 / 点击广告浏览15秒即可获得奖励 / [点击去浏览][放弃奖励]"挽留窗。
# 该弹窗在 WebView 里,uiautomator 读不到文字(所以关键字匹配永远落空), 只能按固定坐标点"放弃奖励"。
AD_GIVEUP_X_REL, AD_GIVEUP_Y_REL = 500, 670

# 主线程心跳 + watchdog
WATCHDOG_TIMEOUT = 90     # 超过90s无任何进展判定为疑似卡死

# 卡住样本采样目录
SAMPLE_DIR = Path("ad_samples")

# 多模态供应商降级链: Agnes主号 -> Agnes备用号(同学) -> 智谱GLM-4V -> 硅基DeepSeek-OCR(纯文字兜底)
VLM_PROVIDERS = [
    {
        "name": "Agnes(主号)",
        "base_url": "https://apihub.agnes-ai.com/v1",
        "api_key_env": "AGNES_API_KEY",
        "model": os.getenv("AGNES_MODEL", "agnes-3.0-flash"),
    },
    {
        "name": "Agnes(备用号-同学)",
        "base_url": "https://apihub.agnes-ai.com/v1",
        "api_key_env": "AGNES_API_KEY2",
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


# ================= 模块级工具(无状态) =================
def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    # 同时落盘 agent_run.log (供 evolver 采样分析, gitignore)
    try:
        with open("agent_run.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ===== 广告时长解析 =====
# QQ音乐广告入口/倒计时一般带秒数: "点击后浏览15秒" / "浏览5秒再领X金币" / "看30秒广告".
# 从文字里正则出秒数, 决定我们自己的计时要等多久. 读不到就用默认 AD_WATCH_SECONDS(16).
_LAST_TEXT_MATCH = ""   # tap_text_priority/ui_detect_click 最近一次命中的完整文字(供解析秒数)
_LAST_DUMP_XML = ""     # 最近一次 uiautomator dump 的全文(供判断"已获得奖励/已完成")


def parse_ad_seconds(text, default=AD_WATCH_SECONDS):
    """从文字中解析广告秒数(5/15/30等); 解析不到返回 default. 只接受 3~60 的合理值."""
    m = re.search(r'(\d{1,2})\s*秒', text or "")
    if m:
        v = int(m.group(1))
        if 3 <= v <= 60:
            return v
    return default


def parse_seconds_from_screen(button_text=""):
    """先看按钮/命中文字, 再看整屏 dump(门页标题常写"点击后浏览15秒"), 都读不到用默认16s."""
    for src in (button_text, _LAST_DUMP_XML):
        s = parse_ad_seconds(src, default=0)
        if s:
            return s
    return AD_WATCH_SECONDS


def screencap(path):
    """adb截屏保存到文件(15s超时, 避免adb无响应时主线程无限阻塞)"""
    with open(path, "wb") as f:
        subprocess.run([ADB, "exec-out", "screencap", "-p"], stdout=f, check=True, timeout=15)


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


def adb_tap(x, y):
    subprocess.run([ADB, "shell", "input", "tap", str(int(x)), str(int(y))], check=True, timeout=8)


def adb_back():
    subprocess.run([ADB, "shell", "input", "keyevent", "4"], check=True, timeout=8)


def adb_swipe_up():
    """向上滑动(查看下方任务); 用屏幕中下部滑到上部"""
    subprocess.run([ADB, "shell", "input", "swipe", "500", "1600", "500", "400", "500"], check=True, timeout=8)


def adb_swipe_down():
    """向下滑动(回到页面顶部); 用屏幕上中部滑到下部"""
    subprocess.run([ADB, "shell", "input", "swipe", "500", "600", "500", "1800", "500"], check=True, timeout=8)


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


def current_focus_package():
    """返回当前焦点窗口的包名(用于 force-stop 兜底); 取 'pkg/Activity' token 的包名部分"""
    focus = current_focus_window()
    if not focus:
        return ""
    for tok in focus.split():
        if "/" in tok and "." in tok:
            return tok.split("/")[0]
    return ""


def is_ad_activity():
    """检查当前焦点是否仍在广告Activity(广告页屏蔽返回键)"""
    focus = current_focus_window()
    return any(k in focus for k in
               ("TMECoreActivity", "tmead", "TransPortraitADActivity", "com.qq.e.tg"))


def is_in_qqmusic():
    """当前焦点是否在QQ音乐内(主界面/金币页/广告页都属于QQ音乐包)"""
    return "com.tencent.qqmusic" in current_focus_window()


def is_external_app():
    """当前焦点是否落在浏览器/应用商店等外部App(广告误跳目标); 返回命中的包名片段, 未命中返回空串"""
    focus = current_focus_window()
    if not focus:
        return ""
    _ext_markers = (
        "com.android.chrome", "com.chrome", "com.tencent.mtt", "com.huawei.browser",
        "com.miui.browser", "com.UCMobile", "com.ucmobile", "org.chromium",
        "com.oppo.browser", "com.vivo.browser", "com.sec.android.app.sbrowser",
        "com.android.vending", "com.bbk.appstore", "com.huawei.appmarket",
        "com.xiaomi.market", "com.oppo.market", "com.tencent.android.qqdownloader",
        "com.android.packageinstaller", "com.estrongs.android.pop",
        # 电商类(广告常跳转目标): 淘宝/天猫、京东、拼多多
        "com.taobao.taobao", "com.taobao.litetao", "com.jingdong.app.mall",
        "com.jd.jrapp", "com.xunmeng.pinduoduo",
    )
    for m in _ext_markers:
        if m in focus:
            return m
    return ""


def tap_close(ow, oh):
    """点击右上角X关闭广告(用户截图标定约925,107)"""
    x = int(CLOSE_X_REL / 1000 * ow)
    y = int(CLOSE_Y_REL / 1000 * oh)
    log(f"[点X关闭-右上] 坐标({x},{y})")
    adb_tap(x, y)


def tap_close_left(ow, oh):
    """点击左上角X/返回箭头关闭广告(约80,110)"""
    x = int(CLOSE_LEFT_X_REL / 1000 * ow)
    y = int(CLOSE_LEFT_Y_REL / 1000 * oh)
    log(f"[点X关闭-左上] 坐标({x},{y})")
    adb_tap(x, y)


def tap_close_smart(ow, oh):
    """智能关闭广告: 先在右上角区域找广告SDK自己的可点小节点(位置随广告而变), 找不到再按候选坐标逐个试,
    每点一次立即复查是否已离开广告, 命中就停手。
    实测不同广告的关闭X位置不同(红果在下排控制条 y≈107‰, 抖音在顶部 y≈67‰), 单一固定坐标必然漏。"""
    xml = dump_uia_xml()
    if xml:
        cands = []
        for m in re.finditer(r'<node[^>]*?clickable="true"[^>]*?>', xml):
            node = m.group(0)
            b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
            if not b:
                continue
            x1, y1, x2, y2 = (int(v) for v in b.groups())
            if x2 <= x1 or y2 <= y1:
                continue                       # [0,0][0,0] 这类不可见节点
            if x1 < CLOSE_NODE_MIN_X / 1000 * ow or y1 > CLOSE_NODE_MAX_Y / 1000 * oh:
                continue                       # 只要右上角区域
            cands.append(((x2 - x1) * (y2 - y1), (x1 + x2) // 2, (y1 + y2) // 2))
        if cands:
            cands.sort()
            _, cx, cy = cands[0]
            log(f"[点X关闭-智能] 右上角可点节点({cx},{cy})")
            adb_tap(cx, cy)
            time.sleep(1.2)
            if not is_ad_activity():
                return True
    for rx, ry in CLOSE_CANDIDATES:
        adb_tap(int(rx / 1000 * ow), int(ry / 1000 * oh))
        log(f"[点X关闭-候选] ({rx},{ry})")
        time.sleep(1.2)
        if not is_ad_activity():
            return True
    # 还关不掉: 多半是弹了"现在退出就没有奖励哦"挽留窗 → 按固定坐标点"放弃奖励"
    adb_tap(int(AD_GIVEUP_X_REL / 1000 * ow), int(AD_GIVEUP_Y_REL / 1000 * oh))
    log(f"[点X关闭-放弃奖励] 挽留窗固定坐标({AD_GIVEUP_X_REL},{AD_GIVEUP_Y_REL})")
    time.sleep(1.5)
    return not is_ad_activity()


def dump_uia_xml():
    """uiautomator dump 返回 xml 字符串; 失败返回 '' """
    global _LAST_DUMP_XML
    _LAST_DUMP_XML = ""   # 先清空, 避免dump失败时残留上一次的旧文本导致误判
    try:
        subprocess.run([ADB, "shell", "uiautomator", "dump", "/sdcard/uidump.xml"],
                      capture_output=True, timeout=8)
        out = subprocess.run([ADB, "exec-out", "cat", "/sdcard/uidump.xml"],
                            capture_output=True, timeout=8)
        xml = out.stdout.decode("utf-8", errors="ignore")
        if "<node" in xml:
            _LAST_DUMP_XML = xml   # 缓存最近一次dump全文, 供"已获得奖励/已完成"等判断复用(免二次dump)
        return xml
    except Exception:
        return ""


def tap_text_node(keyword, exclude=(), exact=False, bias_right=False):
    """uiautomator dump 中找含 keyword 文本的节点并点击其像素中心; 命中返回True.
    exact=True 时只匹配 text/desc 完全等于 keyword (用于'我的'等易误匹配的短词).
    bias_right=True 时点击点偏向节点右侧(用于'退出'文字在提示栏右端的场景).
    多个候选时优先最窄(最具体)的节点, 避免点到父容器."""
    xml = dump_uia_xml()
    if "<node" not in xml:
        return False
    candidates = []
    for m in re.finditer(r'<node[^>]*>', xml):
        node = m.group(0)
        t = re.search(r'text="([^"]*)"', node)
        d = re.search(r'content-desc="([^"]*)"', node)
        txt = (t.group(1) if t else "") or (d.group(1) if d else "")
        if not txt:
            continue
        if exact:
            if txt != keyword:
                continue
        else:
            if keyword not in txt:
                continue
        if any(ex in txt for ex in exclude):
            continue
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
        if not b:
            continue
        x1, y1, x2, y2 = (int(b.group(i)) for i in range(1, 5))
        if x2 - x1 < 5 or y2 - y1 < 5:
            continue
        candidates.append((x2 - x1, x1, x2, y1, y2, txt))
    if not candidates:
        return False
    candidates.sort(key=lambda c: c[0])  # 优先最窄(最具体)的节点
    _, x1, x2, y1, y2, txt = candidates[0]
    cx = x1 + (x2 - x1) * 4 // 5 if bias_right else (x1 + x2) // 2
    cy = (y1 + y2) // 2
    log(f"[点文字] 命中文本={txt[:24]} 坐标({cx},{cy})")
    adb_tap(cx, cy)
    return True


def _parse_text_nodes(xml):
    """从 uiautomator xml 解析出所有带文本/描述的有效节点: [(txt, x1, y1, x2, y2), ...]"""
    nodes = []
    for m in re.finditer(r'<node[^>]*>', xml):
        node = m.group(0)
        t = re.search(r'text="([^"]*)"', node)
        d = re.search(r'content-desc="([^"]*)"', node)
        txt = (t.group(1) if t else "") or (d.group(1) if d else "")
        if not txt:
            continue
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
        if not b:
            continue
        x1, y1, x2, y2 = (int(b.group(i)) for i in range(1, 5))
        if x2 - x1 < 5 or y2 - y1 < 5:
            continue
        nodes.append((txt, x1, y1, x2, y2))
    return nodes


def tap_text_priority(specs):
    """一次 uiautomator dump, 按 specs 顺序匹配并点击第一个命中的关键字.
    specs = [(keyword, exclude_tuple, exact_bool, bias_right_bool), ...]; 返回命中的 keyword 或 None.
    相比连续调用多次 tap_text_node(每次都要 dump 一次, 约8s), 这里只 dump 一次,
    关闭广告/收尾退出时可省去大量重复 dump 时间(实测单次迭代从~27s降到~3s)."""
    xml = dump_uia_xml()
    if "<node" not in xml:
        return None
    nodes = _parse_text_nodes(xml)
    for keyword, exclude, exact, bias_right in specs:
        candidates = []
        for txt, x1, y1, x2, y2 in nodes:
            ok = (txt == keyword) if exact else (keyword in txt)
            if not ok:
                continue
            if any(ex in txt for ex in exclude):
                continue
            candidates.append((x2 - x1, x1, x2, y1, y2, txt))
        if not candidates:
            continue
        candidates.sort(key=lambda c: c[0])  # 优先最窄(最具体)节点
        _, x1, x2, y1, y2, txt = candidates[0]
        cx = x1 + (x2 - x1) * 4 // 5 if bias_right else (x1 + x2) // 2
        cy = (y1 + y2) // 2
        log(f"[点文字] 命中文本={txt[:24]} 坐标({cx},{cy})")
        global _LAST_TEXT_MATCH
        _LAST_TEXT_MATCH = txt   # 记录完整文字, 供 parse_ad_seconds 读秒数
        adb_tap(cx, cy)
        return keyword
    return None


# 关闭/退出广告的关键字优先级: 必须"放弃奖励"在最前。
# 应用下载类广告点X会弹"现在退出就没有奖励哦 / 点击去浏览 / 放弃奖励"挽留弹窗,
# 只有点"放弃奖励"才能真退出; 弹窗文案本身含"退出"二字, 故"退出"要排除"退出就/没有奖励"避免误点弹窗正文。
AD_CLOSE_TEXT_SPECS = [
    ("放弃奖励", (), False, False),
    ("放弃", ("放弃领取", "放弃红包"), False, False),
    ("退出", ("退出登录", "退出账号", "退出登录账号", "退出就", "没有奖励"), False, True),
    ("返回", ("返回首页", "返回顶部", "返回上一级"), False, False),
    ("关闭", ("关闭广告", "关闭推送", "关闭免密"), False, False),
]


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


def _match_task_keyword(txt):
    """返回txt命中的白名单关键词; 命中排除词或非白名单则返回None"""
    if any(ek in txt for ek in UA_EXCLUDE_KEYWORDS):
        return None
    for kw in UA_TASK_KEYWORDS:
        if kw in txt:
            return kw
    return None


def ocr_to_action(text: str) -> dict:
    """纯OCR模型: 把识别到的中文文字按关键词规则转成动作(无坐标)"""
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


# ================= 主Agent(状态封装) =================
class QQCoinAgent:
    def __init__(self):
        # 心跳 / watchdog
        self._last_heartbeat = time.time()
        self._watchdog_tripped = False
        self._current_round = 0
        # 防卡死 / 播放页计时
        self._last_had_click = False
        self._last_uia_pos = None      # 上一次硬编码点击的坐标(防卡死用)
        self._last_uia_time = 0.0      # 上一次硬编码点击的时间戳
        self._uia_streak = 0           # 同一按钮连续命中计数(>=2 即第3次 -> 下滑换任务)
        self._play_wait_start = 0.0    # 播放页计时起点
        self._back_streak = 0          # 连续BACK计数
        # 任务用完标记(到期时间戳)
        self._tian_draw_exhausted_until = 0.0
        self._video_top_exhausted_until = 0.0
        self._last_click_reason = ""
        # 全量扫描 / 定向探测
        self._need_scan = False
        self._only_probe_draw = False
        self._probe_draw_attempts = 0
        # 动作历史(给模型的短期记忆)
        self._recent_actions = []
        # 调用统计
        self._vlm_calls = 0
        # 广告状态机运行时(每次run重置)
        self.ad_state = 0
        self.ad_watch_start = 0.0
        self.ad_close_streak = 0
        self.ad_start_checked = False  # 是否已确认广告真正开始播放(避免只跳进内层页却傻等16s)
        self._ad_wait_streak = 0
        self._post_ad_round = False
        # 广告/激励时长控制: 从"点进门页CTA"那刻起算, 到 _wait_until 才算看完(跳商店也等满再回)
        self._ad_expected_wait = AD_WATCH_SECONDS
        self._wait_until = 0.0
        # 坐标缓存: 首次用VLM点中"看视频"后记住坐标, 后续轮直接点(省VLM). 校验失败自动清空.
        self._video_btn_pos = None
        self._door_tap_ts = 0.0   # 上次点"广告门页视频区"的时间(20s冷却, 防每轮重复点)

    # ---------- 记忆 / 心跳 ----------
    def remember_action(self, action, reason=""):
        self._recent_actions.append((str(action), str(reason)[:20]))
        if len(self._recent_actions) > 10:
            del self._recent_actions[:-10]

    def action_history_text(self, n=6, with_reason=True):
        if not self._recent_actions:
            return "（无）"
        items = self._recent_actions[-n:]
        if with_reason:
            return " -> ".join(f"{a}({r})" if r else a for a, r in items)
        return " -> ".join(a for a, _ in items)

    def heartbeat(self):
        self._last_heartbeat = time.time()

    def watchdog_check_pause(self):
        if not self._watchdog_tripped:
            return
        log("[watchdog] 已暂停: 请处理手机上的广告/登录/验证码等页面, 处理完在终端按回车继续")
        try:
            input()
        except Exception:
            time.sleep(30)
        self._watchdog_tripped = False
        self._last_heartbeat = time.time()
        log("[watchdog] 已恢复, 继续执行")

    def start_watchdog(self):
        def _loop():
            while True:
                time.sleep(10)
                idle = time.time() - self._last_heartbeat
                if idle > WATCHDOG_TIMEOUT and not self._watchdog_tripped:
                    self._watchdog_tripped = True
                    log(f"[watchdog] 超过{int(idle)}s无进展, 疑似卡死, 开始采样并暂停等用户处理")
                    save_sample(self._current_round, f"watchdog {int(idle)}s无进展")
                    log("[watchdog] 采样完成(ad_samples/), 主循环将在下一检查点暂停")
        threading.Thread(target=_loop, daemon=True).start()

    # ---------- 任务用完判断 ----------
    def tian_draw_exhausted(self):
        return time.time() < self._tian_draw_exhausted_until

    def video_top_exhausted(self):
        return time.time() < self._video_top_exhausted_until

    def play_wait_then_back(self, reason=""):
        """播放页: 首轮开始计时, 等满15秒再BACK一次; 未满先等待. 返回PLAY_WAIT或BACK"""
        if self._play_wait_start <= 0:
            self._play_wait_start = time.time()
            log(f"[播放页] 检测到播放页, 开始计时(需听满{PLAY_WAIT_SECONDS}s) reason={reason}")
        elapsed = time.time() - self._play_wait_start
        if elapsed < PLAY_WAIT_SECONDS:
            remain = PLAY_WAIT_SECONDS - elapsed
            log(f"[播放页] 已听{elapsed:.0f}s, 还需{remain:.0f}s, 先等待")
            time.sleep(min(remain, random.uniform(5, 9)))
            return "PLAY_WAIT"
        log(f"[播放页] 已听满{elapsed:.0f}s, BACK返回(金币应已到账)")
        adb_back()
        self._play_wait_start = 0.0
        return "BACK"

    # ---------- VLM 调用(降级链) ----------
    def call_vlm(self, img_b64, mode="normal"):
        """按降级链调用多模态视觉模型, 返回动作dict; 全部失败返回WAIT
        mode: normal=金币页常规判断, ad=广告页按钮定位, close=广告关闭按钮, nav=导航回金币页, scan=全量扫描
        硬上限: 单次请求20s且不重试, 整条降级链总耗时不超过60s"""
        self._vlm_calls += 1
        last_err = None
        start_ts = time.time()
        VLM_TOTAL_BUDGET = 60   # 整条降级链总预算(秒), 超过直接返回WAIT, 防止主线程长时间静默
        sys_prompt = (AD_ENTRY_PROMPT if mode == "ad" else
                      AD_CLOSE_PROMPT if mode == "close" else
                      NAV_PROMPT if mode == "nav" else
                      SCAN_PROMPT if mode == "scan" else SYSTEM_PROMPT)
        user_text = "分析这张手机截图，按规则输出JSON。"
        if mode == "scan":
            user_text = "扫描这张截图，按规则输出四个任务状态JSON。"
            if self.tian_draw_exhausted():
                user_text += " (抽奖赢金币本会话已确认0/10, 直接按done处理)"
            if self.video_top_exhausted():
                user_text += " (看视频领金币本会话已确认次数用完, 直接按done处理)"
        elif mode == "normal":
            user_text = f"最近动作历史(旧→新): {self.action_history_text(with_reason=False)}\n" + user_text
            if self.tian_draw_exhausted():
                user_text = ("当前状态: 天天抽奖/抽奖赢金币的看广告次数已用完(0/10), "
                             "本轮严禁再点【立即抽奖】; 优先点看视频领金币/看视频得高额金币, 没有就SWIPE/TOP。\n") + user_text
            if self.video_top_exhausted():
                user_text = ("当前状态: 看视频领金币的次数已用完, 本轮严禁再点该板块; "
                             "优先点看视频得高额金币/金币抽奖, 没有就SWIPE/TOP。\n") + user_text
            if self._only_probe_draw:
                user_text = ("当前状态: 四个任务只剩【抽奖赢金币】待确认。本轮只允许点【立即抽奖】(抽奖赢金币那一行); "
                             "其他任何任务(看视频/浏览广告/领金币/摇钱树/签到/活动任务)一律不点; "
                             "看不到立即抽奖就SWIPE或TOP。\n") + user_text
        for p in VLM_PROVIDERS:
            if time.time() - start_ts >= VLM_TOTAL_BUDGET:
                log(f"[VLM] 降级链总耗时已超{VLM_TOTAL_BUDGET}s, 停止重试, 返回WAIT")
                break
            api_key = os.getenv(p["api_key_env"])
            if not api_key:
                continue
            try:
                client = OpenAI(base_url=p["base_url"], api_key=api_key, timeout=20, max_retries=0)
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
                resp = client.chat.completions.create(
                    model=p["model"],
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": [
                            {"type": "text", "text": user_text},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                        ]},
                    ],
                    temperature=0.0,
                    max_tokens=300,
                )
                text = resp.choices[0].message.content or ""
                text = text.strip()
                if "```" in text:
                    text = text.split("```")[1].strip()
                    if text.startswith("json"):
                        text = text[4:].strip()
                start, end = text.find("{"), text.rfind("}")
                if start == -1 or end == -1:
                    return {"action": "WAIT", "reason": f"[{p['name']}]解析失败: {text[:100]}"}
                result = json.loads(text[start:end + 1])
                result["_provider"] = p["name"]
                return result
            except Exception as e:
                last_err = e
                log(f"[VLM] {p['name']} 调用失败({str(e)[:60]}), 切换下一家 (已耗时{time.time() - start_ts:.0f}s)")
        return {"action": "WAIT", "reason": f"全部视觉模型失败: {str(last_err)[:80]}"}

    # ---------- 金币页硬编码优先 ----------
    def uia_detect_click(self):
        """金币页硬编码优先. uiautomator dump命中白名单任务文本 -> 返回像素中心坐标; 未命中返回None"""
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
        # 浮层检测: 转盘/抽奖类浮层盖住金币页时, dump拿不到浮层自己的按钮(WebView), 反而命中下层无效节点
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
            if w < 20 or h < 20 or x2 > 1300 or y2 > 2700:
                continue
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            clickable = re.search(r'clickable="([^"]*)"', node)
            area = w * h
            if clickable and clickable.group(1) == "true":
                click_hits.append((area, cx, cy, txt, kw))
        if click_hits:
            click_hits.sort(key=lambda x: x[0], reverse=True)
            _, cx, cy, txt, kw = click_hits[0]
            global _LAST_TEXT_MATCH
            _LAST_TEXT_MATCH = txt   # 记录文字, 供激励"浏览X秒"解析时长
            log(f"[硬编码命中] 可点击节点 文本={txt[:26]} 命中={kw} 坐标({cx},{cy})")
            return cx, cy
        return None

    # ---------- 单轮: 硬编码优先 -> 截屏识图兜底 ----------
    def run_once(self):
        post_ad = self._post_ad_round
        self._post_ad_round = False
        last_click = self._last_click_reason
        self._last_click_reason = ""
        if random.random() < SKIP_PROB:
            w = random.uniform(4, 10)
            log(f"[跳过本轮] 随机暂停 {w:.1f}s")
            time.sleep(w)
            return "SKIP"

        # 版本2: 金币页硬编码优先(不调AI)
        uia_hit = self.uia_detect_click()
        if uia_hit:
            cx, cy = uia_hit
            self._play_wait_start = 0.0
            self._back_streak = 0
            now = time.time()
            if (self._last_uia_pos is not None and
                    abs(cx - self._last_uia_pos[0]) < 30 and abs(cy - self._last_uia_pos[1]) < 30 and
                    now - self._last_uia_time < 45):
                self._uia_streak += 1
            else:
                self._uia_streak = 0
            self._last_uia_pos = (cx, cy)
            self._last_uia_time = now
            if self._uia_streak >= 2:   # 第3次命中同一按钮
                log(f"[防卡死] 连续3次命中同一按钮({cx},{cy}), 判定无效, 改下滑换任务")
                self._uia_streak = 0
                self._last_uia_pos = None
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
            self._last_had_click = True
            self._probe_draw_attempts = 0
            self.remember_action("CLICK", "硬编码")
            # 激励"浏览5秒再领X金币"类: 从文字读时长, 设等待截止点; 跳商店后等满再回
            if "秒" in _LAST_TEXT_MATCH:
                _s = parse_ad_seconds(_LAST_TEXT_MATCH)
                self._wait_until = time.time() + _s
                log(f"[激励] 点'{_LAST_TEXT_MATCH[:16]}' → 计时 {_s}s(跳商店也等满再回)")
            else:
                self._wait_until = 0.0
            w = random.uniform(WAIT_MIN, WAIT_MAX)
            log(f"[等待] {w:.1f}s")
            time.sleep(w)
            self.heartbeat()
            return "CLICK"

        # 快路径(坐标缓存): 若本会话曾用VLM成功点中"看视频"按钮, 记住坐标, 后续轮先直接点该坐标,
        # 点后校验是否真的离开金币页(进广告/外部App/离开QQ音乐), 成功则省去一次VLM(~15-19s);
        # 若没进入广告说明布局变了/被弹窗挡住, 立即清缓存回退VLM(自纠正, 不会一直点偏).
        if self._video_btn_pos is not None:
            vx, vy = self._video_btn_pos
            try:
                log(f"[坐标缓存] 复用上次'看视频'坐标({vx},{vy})直接点击, 尝试省去VLM")
                adb_tap(vx, vy)
                time.sleep(1.5)
                if is_ad_activity():
                    log("[坐标缓存] 命中: 已进入广告, 本次省去VLM")
                    self._last_had_click = True
                    self.remember_action("CLICK", "坐标缓存")
                    w = random.uniform(WAIT_MIN, WAIT_MAX)
                    log(f"[等待] {w:.1f}s")
                    time.sleep(w)
                    return "CLICK"
                if is_external_app() or not is_in_qqmusic():
                    # 跳到外部App/商店(下载类广告): 本次算已点击, 但清缓存避免"跳商店→BACK→再跳"死循环
                    log("[坐标缓存] 跳到外部App(下载类广告), 清除缓存避免商店循环")
                    self._video_btn_pos = None
                    self._last_had_click = True
                    self.remember_action("CLICK", "坐标缓存-外跳")
                    w = random.uniform(WAIT_MIN, WAIT_MAX)
                    log(f"[等待] {w:.1f}s")
                    time.sleep(w)
                    return "CLICK"
                log("[坐标缓存] 未命中: 仍在金币页(可能布局变化/弹窗遮挡), 清除缓存改走VLM")
                self._video_btn_pos = None
            except Exception as e:
                log(f"[坐标缓存] 点击异常, 清除缓存 ({str(e)[:40]})")
                self._video_btn_pos = None

        try:
            (ow, oh), b64 = capture_and_encode()
        except Exception as e:
            log(f"[截屏失败] {e} —— 检查手机是否连接/USB调试是否开启")
            time.sleep(5)
            return "FAIL"

        log(f"[记忆] 最近动作: {self.action_history_text()}")
        action = self.call_vlm(b64)
        act = action.get("action", "WAIT")
        provider = action.get("_provider", "?")
        reason = action.get("reason", "")

        # 看视频领金币次数用完确认
        exhaustion_words = ("次数已用完", "次数0", "0次", "明日再来", "已领完", "已完成", "派发完毕")
        if "看视频领金币" in reason and any(k in reason for k in exhaustion_words):
            self._video_top_exhausted_until = time.time() + 600
            self._need_scan = True
            log("[任务状态] 看视频领金币次数已用完/已完成, 本会话不再点该板块")
        elif "看视频领金币" in last_click and any(k in reason for k in exhaustion_words):
            self._video_top_exhausted_until = time.time() + 600
            self._need_scan = True
            log("[任务状态] 点击看视频领金币后出现次数用完/明日再来, 本会话不再点该板块")

        if act == "CLICK":
            if self._only_probe_draw and not ("立即抽奖" in reason or "抽奖赢金币" in reason):
                log(f"[定向探测] 只差抽奖赢金币确认, 跳过其他任务点击 -> 上滑 reason={reason}")
                adb_swipe_up()
                act = "SWIPE"
            elif self.video_top_exhausted() and "看视频领金币" in reason:
                log(f"[防循环] 看视频领金币次数已用完, 跳过该板块点击 -> 上滑 reason={reason}")
                adb_swipe_up()
                act = "SWIPE"
            elif self.tian_draw_exhausted() and ("立即抽奖" in reason or "抽奖赢金币" in reason):
                log(f"[防循环] 天天抽奖次数已用完, 跳过立即抽奖 -> 上滑找看视频 reason={reason}")
                adb_swipe_up()
                act = "SWIPE"
            else:
                try:
                    rx = float(action.get("x", 500))
                    ry = float(action.get("y", 500))
                except (TypeError, ValueError):
                    rx, ry = 500, 500
                x = int(rx / 1000 * ow)
                y = int(ry / 1000 * oh)
                x += random.randint(-OFFSET, OFFSET)
                y += random.randint(-OFFSET, OFFSET)
                log(f"[点击:{provider}] 坐标({x},{y}) reason={reason}")
                try:
                    adb_tap(x, y)
                except Exception as e:
                    log(f"[点击失败] {e}")
                if "立即抽奖" in reason or "抽奖赢金币" in reason:
                    self._probe_draw_attempts = 0
                self._last_click_reason = reason
                self._last_had_click = True
                if "看视频" in reason:
                    # 记住VLM点中的"看视频"坐标, 后续轮直接复用, 省去重复VLM(布局稳定时命中率高)
                    self._video_btn_pos = (x, y)
                    log(f"[坐标缓存] 记录'看视频'按钮坐标({x},{y})供后续轮复用")
        elif act == "SWIPE":
            log(f"[下滑:{provider}] reason={reason}")
            adb_swipe_up()
        elif act == "TOP":
            log(f"[回顶部:{provider}] reason={reason}")
            for _ in range(6):
                adb_swipe_down()
                time.sleep(0.6)
        elif act == "PLAY_WAIT":
            if post_ad:
                log(f"[播放页:{provider}] 广告刚关闭, 判定为残留播放页, 直接BACK")
                adb_back()
                act = "BACK"
            else:
                log(f"[播放页:{provider}] reason={reason}")
                act = self.play_wait_then_back(reason)
        elif act == "BACK":
            exhausted_now = ("天天抽奖" in reason and ("用完" in reason or "0/" in reason or "0次" in reason))
            if exhausted_now:
                self._tian_draw_exhausted_until = time.time() + 600
                self._only_probe_draw = False
                self._need_scan = True
                log("[抽奖状态] 天天抽奖次数已用完(0/10), 本会话不再点立即抽奖; 立即做一次全量扫描确认是否全部完成")
            # Q3优化: 当前仍在QQ音乐内(金币页/子页面/广告页)时, 直接BACK即可, 不浪费一次导航VLM;
            # 只有真正离开QQ音乐(桌面/外部App)才调用导航VLM逐步找回金币页。
            if is_in_qqmusic():
                nact = "BACK"
                log(f"[导航] 仍在QQ音乐内, 跳过导航VLM直接BACK")
            else:
                try:
                    nav = self.call_vlm(b64, mode="nav")
                    nact = nav.get("action", "BACK")
                    log(f"[导航:{nav.get('_provider', '?')}] {nact} reason={nav.get('reason', '')}")
                except Exception as e:
                    log(f"[导航异常] {str(e)[:40]}")
                    nact = "BACK"
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
                self._back_streak = 0
            elif nact == "SWIPE":
                adb_swipe_up()
                act = "SWIPE"
                self._back_streak = 0
            elif nact == "NONE":
                act = "WAIT"
                self._back_streak = 0
            if act == "BACK":
                self._back_streak += 1
                if self._back_streak >= 2:
                    log(f"[导航失败] 连续{self._back_streak}次无法回到金币页, 尝试自动导航进入金币中心")
                    self.goto_coin_center()
                    if self._on_coin_center():
                        log("[导航] 已自动回到金币中心, 继续")
                    else:
                        log("[导航] 自动导航未成功, 暂停10秒后重试")
                        time.sleep(10)
                    self._back_streak = 0
                else:
                    log(f"[返回:{provider}] reason={reason}")
                    adb_back()
        else:
            log(f"[等待:{provider}] reason={reason}")

        if act != "PLAY_WAIT":
            self._play_wait_start = 0.0
        if act in ("CLICK", "SWIPE", "TOP", "PLAY_WAIT", "WAIT"):
            self._back_streak = 0

        self.remember_action(act, reason)
        w = random.uniform(WAIT_MIN, WAIT_MAX)
        log(f"[等待] {w:.1f}s")
        time.sleep(w)
        self.heartbeat()
        return act

    # ---------- 终局验证扫描 ----------
    def full_scan(self):
        """全量扫描: 回到金币页顶部, 逐屏下滑到底, 检查四个指定任务是否还有可做的.
        只有确认在金币页且整页扫描都没发现可做任务时, 才返回 True(今日任务全部完成)"""
        log("[全量扫描] 开始: 回到金币页顶部并逐屏扫描四个任务")
        self.heartbeat()
        for _ in range(6):
            adb_swipe_down()
            time.sleep(0.4)
        seen_available = set()
        seen_done = set()
        coin_page_seen = False
        draw_step = None
        for step in range(8):
            try:
                (ow, oh), b64 = capture_and_encode()
                result = self.call_vlm(b64, mode="scan")
            except Exception as e:
                log(f"[全量扫描] 第{step + 1}屏失败: {str(e)[:40]}")
                break
            if result.get("is_coin_page"):
                coin_page_seen = True
            states = []
            for t in SCAN_TASKS:
                st = str(result.get(t, "absent")).strip().lower()
                if t == "看视频领金币" and step >= 4:
                    st = "absent"
                states.append(f"{t}={st}")
                if st == "available":
                    seen_available.add(t)
                    if t == "抽奖赢金币" and draw_step is None:
                        draw_step = step
                elif st == "done":
                    seen_done.add(t)
            log(f"[全量扫描] 第{step + 1}屏: " + "  ".join(states))
            self.heartbeat()
            if step < 7:
                adb_swipe_up()
                time.sleep(0.6)
        if self.tian_draw_exhausted():
            seen_available.discard("抽奖赢金币")
            seen_done.add("抽奖赢金币")
        if self.video_top_exhausted():
            seen_available.discard("看视频领金币")
            seen_done.add("看视频领金币")
        elif "看视频领金币" in seen_done and "看视频领金币" not in seen_available:
            self._video_top_exhausted_until = time.time() + 600
            seen_done.add("看视频领金币")
        if seen_available == {"抽奖赢金币"}:
            self._probe_draw_attempts += 1
            if self._probe_draw_attempts >= 2:
                log("[定向探测] 连续2次全量扫描只剩抽奖赢金币且期间无成功点击, 判定为0/10假available, 按已完成处理")
                self._tian_draw_exhausted_until = time.time() + 600
                seen_available.discard("抽奖赢金币")
                seen_done.add("抽奖赢金币")
                self._only_probe_draw = False
            else:
                self._only_probe_draw = True
                log("[全量扫描] 只剩抽奖赢金币待确认, 正常循环改为定向探测(只点立即抽奖)")
                if draw_step is not None:
                    back = 7 - draw_step
                    if back > 0:
                        log(f"[全量扫描] 滚回抽奖赢金币所在位置(约第{draw_step + 1}屏, 下滚{back}屏)")
                        for _ in range(back):
                            adb_swipe_down()
                            time.sleep(0.4)
        else:
            self._only_probe_draw = False
            self._probe_draw_attempts = 0
        log(f"[全量扫描] 结果: 可做={sorted(seen_available) or '无'} | 已完成={sorted(seen_done) or '无'} | 确认金币页={coin_page_seen}")
        return coin_page_seen and not seen_available

    # ---------- 自动导航到金币中心 ----------
    def _on_ad_door_page(self):
        """是否停在广告"门页"(左上角'点击后看15秒, 可获得奖励' + 底部'立即下载/立即浏览').
        该页只是外壳, 不是真广告; 点视频区才会真正进入广告并开始计时。"""
        xml = _LAST_DUMP_XML or dump_uia_xml()
        if not xml:
            return False
        return ("点击后" in xml) and (("可得奖励" in xml) or ("可获得奖励" in xml))

    def _on_ledger_page(self):
        """是否停在"记录(金币明细)"页。该页是金币中心同一页面栈里的覆盖层,
        uiautomator dump 里仍保留"金币中心/看视频领金币"等底层节点,
        只按文字判断会把记录页误判成金币中心(于是收尾误报成功、且在记录页上空点)。"""
        xml = dump_uia_xml()
        if not xml:
            return False
        return ("记录" in xml) and ("收入" in xml) and ("支出" in xml)

    def _leave_ledger(self):
        """若在"记录"页则 BACK 一次退回金币中心(该页 BACK 不会退出金币中心)。"""
        if self._on_ledger_page():
            log("[记录页] 当前停在'金币明细(记录)'页, BACK退回金币中心")
            adb_back()
            time.sleep(1.5)
            return True
        return False

    def _on_coin_center(self):
        """uiautomator 粗判当前是否在金币中心页(看视频领金币/天天抽奖等存在)。
        注意: "记录"覆盖层会让底层节点仍然存在, 必须先排除。"""
        xml = dump_uia_xml()
        if not xml:
            return False
        if ("记录" in xml) and ("收入" in xml) and ("支出" in xml):
            return False   # 停在"记录/金币明细"覆盖层, 不算金币中心
        for kw in ("看视频领金币", "金币狂欢节", "天天抽奖", "抽奖赢金币", "金币中心"):
            if kw in xml:
                return True
        return False

    def _tap_any(self, keywords, exclude=(), exact=False, bias_right=False):
        for kw in keywords:
            if tap_text_node(kw, exclude=exclude, exact=exact, bias_right=bias_right):
                return True
        return False

    def _tap_ad_start_button(self):
        """广告内层页常需再点一次才真正开始计时(如'点击广告15秒后领取金币'/'立即观看').
        用一次 uiautomator dump 优先匹配最明确的开始观看类文字, 不耗API(旧版6次dump≈48s, 现1次≈8s)."""
        specs = [
            ("点击浏览", (), False, False),
            ("立即浏览", (), False, False),
            ("开始浏览", (), False, False),
            ("去浏览", (), False, False),
            ("点击广告", (), False, False),
            ("点击观看", (), False, False),
            ("立即观看", (), False, False),
            ("点击此处", (), False, False),
            ("点击播放", (), False, False),
        ]
        return bool(tap_text_priority(specs))

    def goto_coin_center(self):
        """从任意QQ音乐页面(首页/我的/领现金页)自动导航到金币中心.
        链路: 直接入口(看广告赚金币) -> 否则 我的 -> 领现金 -> 金币中心 ; 末用VLM兜底.
        优先uiautomator文本点击(不耗API). 返回是否成功进入金币中心."""
        log("[导航] 尝试自动进入金币中心(我的-领现金-金币中心)...")
        self.heartbeat()
        self._leave_ledger()   # 若停在"记录"覆盖层先退出, 否则下面的文字匹配会点到被遮挡的底层节点
        if self._on_coin_center():
            log("[导航] 已在金币中心")
            return True
        # 步骤0: 直接入口(首页右侧浮窗 / 任意页的'看广告赚金币')
        if self._tap_any(("看广告赚金币", "金币中心", "做任务赚金币")):
            time.sleep(2.5)
            if self._on_coin_center():
                return True
        # 步骤1: 底部'我的' tab (exact 避免匹配'我的音乐'等子项)
        if self._tap_any(("我的",), exact=True,
                         exclude=("我的收藏", "我的音乐", "我的听歌", "我的关注",
                                  "我的订单", "我的钱包", "我的客服", "我的动态")):
            time.sleep(1.5)
            # 步骤2: '领现金'
            if self._tap_any(("领现金",)):
                time.sleep(2.0)
                # 步骤3: 领现金页内的金币中心入口
                if self._tap_any(("看广告赚金币", "金币中心", "做任务赚金币")):
                    time.sleep(2.5)
                    if self._on_coin_center():
                        return True
        # 兜底: VLM 导航
        try:
            (ow, oh), b64 = capture_and_encode()
            nav = self.call_vlm(b64, mode="nav")
            nact = nav.get("action", "BACK")
            log(f"[导航兜底:{nav.get('_provider', '?')}] {nact} reason={nav.get('reason', '')}")
            if nact == "CLICK":
                try:
                    rx = float(nav.get("x", 500)); ry = float(nav.get("y", 500))
                except (TypeError, ValueError):
                    rx, ry = 500, 500
                x = int(rx / 1000 * ow) + random.randint(-OFFSET, OFFSET)
                y = int(ry / 1000 * oh) + random.randint(-OFFSET, OFFSET)
                adb_tap(x, y)
                time.sleep(2.5)
        except Exception as e:
            log(f"[导航兜底失败] {str(e)[:40]}")
        return self._on_coin_center()

    # ---------- 广告状态机单步 ----------
    def ad_step(self, ow, oh):
        """广告状态机单步执行, 返回状态(是否仍需继续广告流程)"""
        if self.ad_state == 0:   # AD_IDLE
            return False
        if self.ad_state == 1:   # AD_ENTRY
            # 门页专项(实拍截图确认): "点击后看15秒, 可获得奖励" 这层只是一张皮,
            # 底部"立即下载/立即浏览"按钮常被视频层吞掉点了没反应, BACK/X 也被拦;
            # 真正能进入广告(并开始计时)的是点视频画面本身。故先按固定比例点视频区, 省一次VLM。
            if self._on_ad_door_page() and time.time() - self._door_tap_ts > 20:
                self._door_tap_ts = time.time()
                _secs = parse_seconds_from_screen(_LAST_DUMP_XML) or AD_DEFAULT_AD_SECONDS
                x = int(AD_DOOR_VIDEO_X_REL / 1000 * ow)
                y = int(AD_DOOR_VIDEO_Y_REL / 1000 * oh)
                log(f"[广告-门页] 识别到'点击后看{_secs}秒'门页, 点视频区({AD_DOOR_VIDEO_X_REL},{AD_DOOR_VIDEO_Y_REL})进入广告")
                adb_tap(x, y)
                time.sleep(1.5)
                self.ad_state = 2   # AD_WATCH
                self.ad_watch_start = time.time()
                self._ad_expected_wait = _secs
                self._wait_until = time.time() + _secs
                self.ad_start_checked = True
                self._ad_wait_streak = 0
                return True
            # 快路径: 广告页若已有明确的"点击浏览/立即观看/开始浏览"类按钮, 先用 uiautomator 直接点
            # (一次dump, 不耗API), 命中即可省去一次 VLM(~7-19s); 未命中再走 VLM 兜底。
            if self._tap_ad_start_button():
                _secs = parse_seconds_from_screen(_LAST_TEXT_MATCH)
                log(f"[广告-入口] uiautomator命中开始/浏览按钮(读到{_secs}s), 直接点击(省去VLM)")
                time.sleep(1.5)
                # 点门页CTA那一刻 = 计时起点; 无论进容器广告还是跳商店, 都等满 _secs 再回
                self._ad_expected_wait = _secs
                self._wait_until = time.time() + _secs
                self.ad_state = 2   # AD_WATCH
                self.ad_watch_start = time.time()
                self.ad_start_checked = True   # 已点开始按钮, 无需再确认双击
                self._ad_wait_streak = 0
                if is_ad_activity() or is_in_qqmusic():
                    log(f"[广告-已进入广告/详情页, 计时 {_secs}s](入口即开始按钮)")
                else:
                    log(f"[广告-跳转外部App, 按{_secs}s计时, 满后再BACK回QQ音乐]")
                return True
            # 门页若已显示"已获得奖励/已完成"(广告已看完), 不要重复点入口, 直接转关闭返回
            if any(w in _LAST_DUMP_XML for w in ("已获得奖励", "已完成", "恭喜已获得", "已领取", "获得奖励")):
                log("[广告-入口] 门页已是'已获得奖励/已完成', 不重复点击, 转关闭返回")
                self.ad_state = 4   # AD_CLOSE
                return True
            try:
                (w2, h2), b64 = capture_and_encode()
                action = self.call_vlm(b64, mode="ad")
                act = action.get("action", "WAIT")
                if act == "CLICK":
                    rx = float(action.get("x", 500)); ry = float(action.get("y", 500))
                    x = int(rx / 1000 * w2); y = int(ry / 1000 * h2)
                    x += random.randint(-OFFSET, OFFSET); y += random.randint(-OFFSET, OFFSET)
                    _reason = action.get("reason", "") or ""
                    # 秒数优先取VLM直接返回的seconds字段, 其次从文字/dump解析, 最后默认16s
                    _secs = 0
                    try:
                        _sv = int(action.get("seconds", 0) or 0)
                        if 3 <= _sv <= 60:
                            _secs = _sv
                    except (TypeError, ValueError):
                        _secs = 0
                    if not _secs:
                        _secs = parse_seconds_from_screen(_reason)
                    log(f"[广告-点跳转] 坐标({x},{y}) reason={_reason} (计时{_secs}s)")
                    adb_tap(x, y)
                    time.sleep(1.5)
                    # 计时起点 = 点门页CTA此刻; 跳商店也算已开始, 等满 _secs 再回
                    self._ad_expected_wait = _secs
                    self._wait_until = time.time() + _secs
                    self.ad_watch_start = time.time()
                    _start_words = ("点击浏览", "立即浏览", "开始浏览", "点击广告",
                                    "立即观看", "点击观看", "开始观看", "去浏览")
                    self.ad_start_checked = any(w in _reason for w in _start_words)
                    self._ad_wait_streak = 0
                    self.ad_state = 2   # AD_WATCH
                    if is_ad_activity() or is_in_qqmusic():
                        log(f"[广告-已进入广告/详情页, 计时 {_secs}s]" +
                            ("(入口即开始按钮)" if self.ad_start_checked else ""))
                    else:
                        log(f"[广告-跳转外部App, 按{_secs}s计时, 满后再BACK回QQ音乐]")
                    return True
                elif act == "CLOSE":
                    log(f"[广告-已看完] {action.get('reason', '')}")
                    self.ad_state = 4   # AD_CLOSE
                    self._ad_wait_streak = 0
                    return True
                elif act == "BACK":
                    log(f"[广告-登录页] {action.get('reason', '')} —— 直接BACK返回")
                    adb_back()
                    self._ad_wait_streak = 0
                    time.sleep(random.uniform(2, 3))
                    self.ad_state = 3   # AD_BACK
                    return True
                else:
                    # 已砍掉"看不清就再问一次VLM"的分支: 实测这类"视频播放中/没有明确入口"的回答
                    # 每次要 21-40s, 两次就 60s+, 而它什么也没做, 最后还强制BACK把页面退掉。
                    # 现在直接当作"已在播放/无需再点" → 交给 AD_WATCH(固定点视频区 + 开始计时), 一次都不问。
                    log(f"[广告-等待] VLM={action.get('reason', '')} —— 不再追问, 直接按已开播计时")
                    self._ad_wait_streak = 0
                    self.ad_watch_start = time.time()
                    self._wait_until = time.time() + self._ad_expected_wait
                    # VLM 说"视频播放中" → 已经在播, 别再点(点了会点进广告主页面跳外部);
                    # 只是"看不清/加载中" → 仍让 AD_WATCH 补一次固定点视频区, 确保真的进去了。
                    _playing = any(w in action.get("reason", "") for w in ("播放", "视频", "观看中", "倒计时"))
                    self.ad_start_checked = _playing
                    self.ad_state = 2   # AD_WATCH
                    return True
            except Exception as e:
                log(f"[广告-入口异常] {e}")
                time.sleep(3)
                return True
        if self.ad_state == 2:   # AD_WATCH
            elapsed = time.time() - self.ad_watch_start
            # 进入广告后需确认是否"真正开始播放": 有些广告点第一下只是跳进内层页,
            # 还需再点一次(点击广告/立即观看)才真正计时. 否则会傻等16s(实际没在看)→变相长等.
            if not self.ad_start_checked and elapsed < 4:
                time.sleep(2)  # 给内层页加载时间
                # 先便宜地用uiautomator找"开始观看"类按钮(不耗API)
                if self._tap_ad_start_button():
                    log("[广告-开始观看] uiautomator命中开始观看按钮, 点击后重置计时")
                    # 校验未跳到外部App
                    if is_in_qqmusic() or is_ad_activity():
                        self.ad_watch_start = time.time()
                        self._wait_until = time.time() + self._ad_expected_wait
                        self.ad_start_checked = True
                        return True
                    log("[广告-开始观看跳转外部App] 放弃本次广告")
                    self.ad_state = 0
                    self.ad_start_checked = True
                    return True
                # 广告页是 WebView/视频浮层, dump 里读不到"点击浏览/立即观看"这类文字,
                # 再问一次VLM要25-30s(实测) → 各广告门页布局一致, 直接按固定比例点一次视频画面即可开播。
                x = int(AD_DOOR_VIDEO_X_REL / 1000 * ow)
                y = int(AD_DOOR_VIDEO_Y_REL / 1000 * oh)
                log(f"[广告-开始观看-固定点] 点视频区({AD_DOOR_VIDEO_X_REL},{AD_DOOR_VIDEO_Y_REL}) 启动计时(省一次VLM)")
                adb_tap(x, y)
                time.sleep(1.5)
                self.ad_watch_start = time.time()
                self._wait_until = time.time() + self._ad_expected_wait
                self.ad_start_checked = True
                return True
            _need = max(5, self._ad_expected_wait)
            if elapsed < _need:
                remain = max(1, _need - elapsed)
                log(f"[广告-详情页浏览] 已看{elapsed:.0f}s/{_need}s, 还需{remain:.0f}s")
                time.sleep(min(remain, random.uniform(3, 5)))
                return True
            # 等满入口声明的秒数(5/15/30)后: 真广告多为详情/下载页(无倒计时), BACK回门页/上一页
            log(f"[广告-详情页浏览] 已看满{elapsed:.0f}s, BACK回门页/上一页")
            adb_back()
            time.sleep(1.5)
            self.ad_state = 3   # AD_BACK: 评估BACK后是否还在广告容器
            return True
        if self.ad_state == 3:   # AD_BACK
            if is_ad_activity():
                log("[广告-回广告页] 交给AI定位关闭按钮")
                self.ad_state = 4   # AD_CLOSE
            else:
                log("[广告-BACK] 已离开广告Activity, 回金币页")
                self.ad_state = 0   # AD_IDLE
            time.sleep(random.uniform(3, 5))
            return True
        if self.ad_state == 4:   # AD_CLOSE
            if not is_ad_activity():
                log("[广告-关闭完成] 已回金币页")
                self.ad_state = 0
            else:
                if self.ad_close_streak == 0:
                    # 优先: 一次 uiautomator dump 检索多个关键字(放弃奖励/退出/返回/关闭), 不耗API.
                    # 关键: 应用下载类广告的"现在退出就没有奖励哦"挽留弹窗必须点"放弃奖励"才能退出,
                    #       弹窗文案含"退出"会被误匹配, 故"放弃奖励"排第一并给"退出"加排除词。
                    hit = tap_text_priority(AD_CLOSE_TEXT_SPECS)
                    if hit:
                        log(f"[广告-点关闭] 命中'{hit}'文字, 尝试关闭广告")
                        self.ad_close_streak += 1
                        time.sleep(random.uniform(2, 4))
                    elif any(w in _LAST_DUMP_XML for w in ("已获得奖励", "已完成", "恭喜已获得", "已领取", "获得奖励")):
                        # 门页"点击后浏览15秒"看完后左上角会变"恭喜已获得奖励/已完成XXX" → 说明已成功, BACK回金币页
                        log("[广告-门页完成] 识别到'已获得奖励/已完成', BACK回金币页领币")
                        adb_back()
                        self.ad_close_streak += 1
                        time.sleep(random.uniform(2, 4))
                    else:
                        # 不再让VLM定位关闭按钮(实测单次25-30s, 且给出的坐标常点不掉):
                        # 直接用智能关闭器(右上角可点节点 → 多候选X → 挽留窗"放弃奖励"), 全程不耗API。
                        log("[广告-关闭] 用智能关闭器(节点/候选X/放弃奖励), 不调VLM")
                        if tap_close_smart(ow, oh):
                            log("[广告-关闭] 已在广告页关闭成功, 收手")
                            self.ad_close_streak = 0
                            self.ad_state = 0
                            return True
                        self.ad_close_streak += 1
                        time.sleep(random.uniform(2, 4))
                elif self.ad_close_streak == 1:
                    # 先智能定位右上角关闭按钮(广告SDK可点节点) + 多候选坐标, 每点一次立即复查。
                    # 旧版"右上X + 左上X"两下连点, 若第一下已成功关闭, 第二下会落在金币中心的
                    # "金币收益"卡片上 → 直接点进"记录(金币明细)"页, 之后整轮空转、收尾还误判成功。
                    log("[广告-角标X] 智能关闭(右上角节点/多候选), 点一次复查一次")
                    if tap_close_smart(ow, oh):
                        log("[广告-角标X] 已离开广告容器, 收手")
                        self.ad_close_streak = 0
                        self.ad_state = 0
                        return True
                    log("[广告-角标X] 候选都无效, 再试左上角X/返回箭头")
                    tap_close_left(ow, oh)      # 左上角X/返回箭头
                    self.ad_close_streak += 1
                    time.sleep(random.uniform(2, 4))
                else:
                    # 实测: 广告门页会拦截 BACK、角标X也可能无效, 旧逻辑靠轮数耗完(12轮≈4分钟打一支广告)。
                    # 这里设死磕上限: 连续2轮关不掉, 直接走收尾逻辑(必要时force-stop重拉QQ音乐), 把损失钉死在~20s。
                    log("[广告-死磕上限] 连续关不掉(门页拦BACK/角标无效), 直接收尾重拉QQ音乐, 不再耗轮次")
                    self.ad_close_streak = 0
                    self._ad_wait_streak = 0
                    self.ad_state = 0
                    self._return_to_qqmusic()
            return True
        return False

    # ---------- 主循环 ----------
    def run(self, rounds=None):
        if not os.getenv("AGNES_API_KEY"):
            log("!! .env 里没有 AGNES_API_KEY")
            sys.exit(1)
        if not Path(ADB).exists():
            log(f"!! adb 路径不存在: {ADB}")
            sys.exit(1)
        # 重置本次运行状态
        self._last_had_click = False
        self._play_wait_start = 0.0
        self._back_streak = 0
        self._post_ad_round = False
        self._tian_draw_exhausted_until = 0.0
        self._video_top_exhausted_until = 0.0
        self._last_click_reason = ""
        self._need_scan = False
        self._only_probe_draw = False
        self._probe_draw_attempts = 0
        self._last_uia_pos = None
        self._last_uia_time = 0.0
        self._uia_streak = 0
        self._current_round = 0
        self._last_heartbeat = time.time()
        self._vlm_calls = 0
        self.ad_state = 0
        self.ad_watch_start = 0.0
        self.ad_close_streak = 0
        self._ad_wait_streak = 0
        self._video_btn_pos = None   # 每次run重置坐标缓存
        self._ad_expected_wait = AD_WATCH_SECONDS
        self._wait_until = 0.0

        AD_IDLE, AD_ENTRY, AD_WATCH, AD_BACK, AD_CLOSE = 0, 1, 2, 3, 4

        rounds = rounds if rounds is not None else MAX_ROUNDS
        if len(sys.argv) > 1:
            try:
                rounds = int(sys.argv[1])
            except ValueError:
                pass

        try:
            out = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=10).stdout
            log("设备状态:\n" + out.strip())
            if "device\n" not in out and "\tdevice" not in out:
                log("!! 没有检测到已连接的手机。请先完成下方手机设置再运行。")
                sys.exit(1)
        except Exception as e:
            log(f"!! adb 调用失败: {e}")
            sys.exit(1)

        self.start_watchdog()

        n = 0
        wait_streak = 0
        swipe_streak = 0
        no_click = 0

        log("[启动] 自动进入金币中心(支持从首页开始)...")
        self.goto_coin_center()
        if self._on_coin_center():
            log("[启动] 已就位金币中心, 开始刷任务")
        else:
            log("[启动] 未自动进入金币中心, 直接开始(若已在金币页附近仍可工作)")

        while n < rounds:
            n += 1
            self._current_round = n
            self.heartbeat()
            self.watchdog_check_pause()
            log(f"===== 第 {n}/{rounds} 轮 =====")

            # 兜底0(最高优先): 误跳外部App(浏览器/应用商店/电商App等), 焦点已离开QQ音乐 → 强制返回
            ext = is_external_app()
            if ext or (not is_in_qqmusic() and not is_ad_activity()):
                # 跳商店/浏览器时先等满再BACK: 广告/激励没看够时长就返回, 金币不会到账
                _left = self._wait_until - time.time()
                if _left > 0.5:
                    log(f"[兜底0] 已跳外部, 但广告/激励还差{_left:.0f}s, 等满再BACK(否则不到账)")
                    time.sleep(min(_left, 35))
                elif ext:
                    log(f"[兜底0] 外部跳转, 先等{EXTERNAL_INCENTIVE_WAIT}s(激励浏览)再BACK")
                    time.sleep(EXTERNAL_INCENTIVE_WAIT)
                self._wait_until = 0.0
                log(f"[兜底0] 检测到离开QQ音乐(外部App={ext or '未知'}), 尝试逐次BACK回到QQ音乐/广告页")
                recovered = False
                for i in range(8):
                    adb_back()
                    time.sleep(1.0)
                    if is_in_qqmusic():
                        log(f"[兜底0] 第{i + 1}次BACK已回到QQ音乐")
                        recovered = True
                        break
                    if is_ad_activity():
                        log(f"[兜底0] 第{i + 1}次BACK回到广告页, 交给状态机退出")
                        self.ad_state = AD_CLOSE
                        recovered = True
                        break
                if not recovered:
                    log("[兜底0] BACK无法返回, 尝试 monkey 重新拉起QQ音乐")
                    try:
                        subprocess.run([ADB, "shell", "monkey", "-p", "com.tencent.qqmusic",
                                        "-c", "android.intent.category.LAUNCHER", "1"],
                                       capture_output=True, timeout=10)
                    except Exception as e:
                        log(f"[兜底0] monkey失败: {str(e)[:40]}")
                    time.sleep(3)
                self.ad_state = AD_IDLE
                self._last_uia_pos = None
                self._uia_streak = 0
                self._play_wait_start = 0.0
                self._ad_wait_streak = 0
                time.sleep(random.uniform(3, 6))
                continue

            # 兜底0.5: 误入"记录(金币明细)"覆盖层 → 该页 UI 树里仍有金币中心节点, 必须先退出,
            # 否则后续文字匹配会点到被遮挡的底层节点(点了没反应/整轮空转)。
            if is_in_qqmusic() and self._leave_ledger():
                self._last_uia_pos = None
                self._uia_streak = 0
                time.sleep(1.0)
                continue

            # 广告状态机优先
            if is_ad_activity():
                no_click = 0
                if self.ad_state == AD_IDLE:
                    self.ad_state = AD_ENTRY
                self.ad_step(1080, 2400)
                self.heartbeat()
                self.watchdog_check_pause()
                time.sleep(SHOT_INTERVAL)
                continue

            # 不在广告Activity但状态机未复位 → 视为已回金币页
            if self.ad_state != AD_IDLE:
                log("[状态机] 已离开广告Activity, 复位")
                self.ad_state = AD_IDLE
                self._ad_wait_streak = 0
                self._last_uia_pos = None
                self._uia_streak = 0
                self._post_ad_round = True

            # 兜底1: 连续多次WAIT
            if wait_streak >= 3:
                log("[兜底] 连续3次无动作, 强制返回上一页")
                adb_back()
                wait_streak = 0
                time.sleep(random.uniform(3, 6))
            # 兜底2: 连续下滑到底
            if swipe_streak >= 6:
                log("[兜底] 连续6次下滑到底, 采样并滚回金币页顶部重新扫描")
                save_sample(n, "连续6次下滑到底")
                for _ in range(6):
                    adb_swipe_down()
                    time.sleep(0.8)
                swipe_streak = 0
                time.sleep(random.uniform(2, 4))
            act = self.run_once()
            self.watchdog_check_pause()
            if act == "WAIT":
                wait_streak += 1
            else:
                wait_streak = 0
            if act == "SWIPE":
                swipe_streak += 1
            else:
                swipe_streak = 0

            # 刚确认某个任务已用完 -> 立即全量扫描一次
            if self._need_scan and not is_ad_activity():
                self._need_scan = False
                if self.full_scan():
                    log("[全量扫描] 今日四个任务全部完成, 退出")
                    self._finish(rounds)
                    return
                no_click = 0
                wait_streak = 0
                swipe_streak = 0

            if act == "CLICK":
                no_click = 0
            elif act in ("SWIPE", "TOP", "BACK", "PLAY_WAIT"):
                no_click += 1
            else:
                no_click = 0
            if no_click >= 8:
                if self.full_scan():
                    log("[全量扫描] 今日四个任务全部完成, 退出")
                    self._finish(rounds)
                    return
                no_click = 0
                wait_streak = 0
                swipe_streak = 0

            time.sleep(SHOT_INTERVAL)
        log("到达轮数上限, 结束。")
        self._return_to_qqmusic()
        self._finish(rounds)

    def _return_to_qqmusic(self, max_back=2):
        """轮数到上限/收尾时, 主动把焦点拉回QQ音乐金币中心, 避免停在第三方App/广告页。
        广告页/H5常拦截BACK键且dump很慢(8s), 故最多试2轮(放弃奖励/退出/关闭文字 + 角标X + BACK),
        仍失败立即 force-stop 当前前台包并重拉QQ音乐, 重拉后自动导航进入金币中心。
        (force-stop 是确定性逃出, 不再像旧版死磕12轮BACK导致几分钟卡死)"""
        log("[收尾] 主动兜底回到QQ音乐(金币中心)...")
        for _ in range(max_back):
            self.heartbeat()   # 收尾也要心跳, 否则watchdog误判"93s无进展"卡死
            if is_in_qqmusic() and not is_ad_activity():
                # 可能停在首页/其他子页/记录页, 确保进入金币中心
                self._leave_ledger()
                if not self._on_coin_center():
                    self.goto_coin_center()
                if self._on_coin_center():
                    log("[收尾] 已回到QQ音乐金币中心")
                    return
            # 广告页: 先试关闭文字(放弃奖励/退出/返回/关闭), 再试固定右上角X; 每次点完立即复查, 不盲连点
            if is_ad_activity():
                hit = tap_text_priority(AD_CLOSE_TEXT_SPECS)
                if hit:
                    log(f"[收尾] 已点'{hit}'尝试关闭广告")
                    time.sleep(1.5)
                    continue
                try:
                    screencap(SCREEN_PATH)
                    ow, oh = Image.open(SCREEN_PATH).size
                except Exception:
                    ow, oh = 1080, 2400
                if tap_close_smart(ow, oh):
                    log("[收尾] 智能关闭命中, 收手(不补点左上角, 否则会误点金币中心卡片)")
                    continue
                tap_close_left(ow, oh)     # 左上角X/返回箭头
                time.sleep(0.8)
            adb_back()
            time.sleep(0.8)
        # 收尾仍未回到目标 -> 强制结束当前前台包 + 重拉QQ音乐(可确定性逃出BACK陷阱)
        pkg = current_focus_package()
        if is_ad_activity() or (pkg and pkg != "com.tencent.qqmusic"):
            log(f"[收尾] 常规退出失败(当前包={pkg}), force-stop 并重拉QQ音乐")
            try:
                if pkg and pkg != "com.tencent.qqmusic":
                    subprocess.run([ADB, "shell", "am", "force-stop", pkg], capture_output=True, timeout=10)
            except Exception:
                pass
            time.sleep(1)
            try:
                subprocess.run([ADB, "shell", "am", "force-stop", "com.tencent.qqmusic"], capture_output=True, timeout=10)
            except Exception:
                pass
            time.sleep(2)
            try:
                subprocess.run([ADB, "shell", "monkey", "-p", "com.tencent.qqmusic",
                                "-c", "android.intent.category.LAUNCHER", "1"],
                               capture_output=True, timeout=10)
            except Exception as e:
                log(f"[收尾] monkey失败: {str(e)[:40]}")
            time.sleep(3)
        # 重拉后确保进入金币中心
        self.heartbeat()
        if not self._on_coin_center():
            self.goto_coin_center()
        self._watchdog_tripped = False   # 收尾完成, 清除收尾期间可能触发的watchdog标记, 避免后续误暂停
        if self._on_coin_center():
            log("[收尾] 已回到QQ音乐金币中心")
        else:
            log("[收尾] 警告: 仍未进入金币中心, 请手动处理")

    def _finish(self, rounds):
        log(f"[统计] 本轮共调用视觉模型 {self._vlm_calls} 次 (轮数上限={rounds})")


if __name__ == "__main__":
    QQCoinAgent().run()
