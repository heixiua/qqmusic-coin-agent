# -*- coding: utf-8 -*-
"""
evolver.py — 广告形态自我进化器
================================
QQ音乐广告形态无法预判(歌曲播放页/30秒视频/登录页/商品流/H5等)。
脚本运行中 watchdog 会把"卡住"的画面采样到 ad_samples/ 目录,
本脚本读取样本, 把 截图+日志上下文+相关代码 打包给云端大模型,
让模型分析"这是什么新广告形态、脚本该怎么改", 生成代码补丁:

  1. 读取 ad_samples/ 下最新的卡住样本(3张截图 + context.txt)
  2. 提取 qqmusic_agent.py 相关函数源码(prompt/状态机/识别函数)
  3. 调用云端多模态大模型(Agnes免费 -> 智谱GLM-4V 兜底)
  4. 模型输出: 新广告类型识别特征 + 具体代码 diff
  5. 自动语法校验 + git 提交到 dev 分支 + 冒烟测试
  6. 冒烟通过合入 main, 失败 git 回滚

用法:
  python evolver.py               # 分析最新样本, 生成补丁(不自动应用)
  python evolver.py --apply       # 生成补丁并自动应用(含语法校验/冒烟/git回滚)
  python evolver.py --rounds 100  # 冒烟测试轮数(默认100, 用户要求多跑几轮)
"""
import os
import sys
import json
import time
import base64
import shutil
import subprocess
import io
import re
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
AGENT_PY = BASE_DIR / "qqmusic_agent.py"
SAMPLES_DIR = BASE_DIR / "ad_samples"
SCREEN_PATH = BASE_DIR / "screen_tmp.png"

# ================= 大模型降级链 (与主脚本一致, 全部免费) =================
EVOLVE_PROVIDERS = [
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
]

# ================= 提示词 =================
EVOLVE_SYSTEM_PROMPT = """你是一个Android GUI自动化脚本的进化工程师。脚本刷QQ音乐金币任务, 广告由第三方提供, 形态无法预判:
歌曲播放页/30秒视频/登录注册页/商品流/H5落地页等。脚本运行时若在某个页面卡住, watchdog会把3张截图(间隔2秒)和日志上下文保存为样本。

现在给你: ①3张卡住时的截图(前后画面, 判断是否静止) ②日志上下文 ③脚本相关函数源码。

你的任务:
1. 分析这个页面是什么(广告形态), 特征是什么(页面文字/布局/控件特征, 用中文描述清楚)。
2. 判断脚本为什么卡住(误判成什么? 应该怎么处理? 等满秒数/点哪里/BACK? 给具体动作)。
3. 输出可执行的代码修改建议: 只允许改这些位置——
   - SYSTEM_PROMPT / AD_ENTRY_PROMPT / AD_CLOSE_PROMPT 里的识别规则(用<代码块>给出整段新prompt规则文字)
   - is_ad_activity() 的 Activity 关键词列表(用<代码块>给出新函数)
   - run_once() / ad_step() 中新增的页面处理分支(用<代码块>给出新函数片段)
   禁止: 硬编码具体像素坐标(坐标必须由AI每轮看图得出)、改状态机核心流转逻辑。
4. 最后输出 JSON, 格式:
   {"page_type":"页面类型名称","features":"识别特征(中文)","stuck_reason":"卡住原因分析","fix_action":"应执行的动作","code_patch":"完整的新代码片段(替换或新增, 必须是可直接粘贴的Python)","target":"SYSTEM_PROMPT|AD_ENTRY_PROMPT|is_ad_activity|run_once|ad_step|其他","confidence":0-100}

注意: code_patch 必须与现有代码风格一致, 只输出增量修改, 不要输出无关内容。"""

# ================= 工具函数 =================

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def latest_sample_dir():
    """返回最新(时间戳最大)的样本目录; 无样本返回None"""
    if not SAMPLES_DIR.exists():
        return None
    dirs = sorted([d for d in SAMPLES_DIR.iterdir() if d.is_dir()],
                  key=lambda d: d.name, reverse=True)
    return dirs[0] if dirs else None


def read_agent_source():
    """读取主脚本, 提取关键函数源码供模型分析"""
    try:
        src = AGENT_PY.read_text(encoding="utf-8")
    except Exception as e:
        return f"(读取失败: {e})"
    # 提取关键段落: SYSTEM_PROMPT/AD_ENTRY_PROMPT/AD_CLOSE_PROMPT/is_ad_activity/run_once/ad_step
    out = []
    for anchor in ("SYSTEM_PROMPT =", "AD_ENTRY_PROMPT =", "AD_CLOSE_PROMPT =",
                   "def is_ad_activity", "def is_in_qqmusic",
                   "def uia_detect_click", "def run_once",
                   "def ad_step", "def main"):
        idx = src.find(anchor)
        if idx == -1:
            continue
        seg = src[idx:idx + 2400]
        out.append(f"\n### {anchor} 源码片段 ###\n{seg}")
    return "\n".join(out)


def encode_screenshot(path, max_w=800):
    """压缩截图转base64(与主脚本一致)"""
    try:
        img = Image.open(path).convert("RGB")
        ow, oh = img.size
        scale = max_w / ow
        img2 = img.resize((max_w, int(oh * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img2.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        log(f"截图编码失败 {path}: {str(e)[:40]}")
        return None


def call_evolve_model(shots, context_text, agent_source):
    """按降级链调用多模态模型分析样本, 返回JSON结果dict"""
    last_err = None
    for p in EVOLVE_PROVIDERS:
        api_key = os.getenv(p["api_key_env"])
        if not api_key:
            continue
        try:
            log(f"调用进化模型: {p['name']}")
            client = OpenAI(base_url=p["base_url"], api_key=api_key, timeout=90)
            content = [
                {"type": "text", "text": f"样本上下文(日志+焦点):\n{context_text}\n\n脚本源码:\n{agent_source}\n\n请分析并输出JSON。"},
            ]
            for b64 in shots:
                if b64:
                    content.append({"type": "image_url",
                                    "image_url": {"url": f"data:image/png;base64,{b64}"}})
            resp = client.chat.completions.create(
                model=p["model"],
                messages=[
                    {"role": "system", "content": EVOLVE_SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                temperature=0.2,
                max_tokens=min(1024, 2000) if p["name"] == "智谱GLM-4V" else 2000,
            )
            raw = resp.choices[0].message.content or ""
            # 提取JSON: 兼容模型输出夹带文字/代码块的情况
            s, e = raw.find("{"), raw.rfind("}")
            if s == -1 or e == -1:
                raise ValueError("模型未输出JSON")
            # 宽松解析: 先尝试标准json, 失败则修复单引号
            body = raw[s:e + 1]
            try:
                result = json.loads(body)
            except json.JSONDecodeError:
                import ast
                result = ast.literal_eval(body)
            if not isinstance(result, dict):
                raise ValueError("JSON非对象")
            result["_provider"] = p["name"]
            return result
        except Exception as e:
            last_err = f"{p['name']}: {e}"
            log(f"  {p['name']} 失败: {str(e)[:80]}")
            continue
    log(f"!! 所有进化模型失败: {last_err}")
    return None


def extract_patch(result):
    """从模型结果提取代码补丁, 落盘到 patches/ 目录"""
    code = (result.get("code_patch") or "").strip()
    if not code:
        return None
    # 清理markdown围栏
    if code.startswith("```"):
        code = code.split("\n", 1)[1] if "\n" in code else code
        code = code.rsplit("```", 1)[0] if code.endswith("```") else code
        code = code.strip()
    patches_dir = BASE_DIR / "patches"
    patches_dir.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = patches_dir / f"patch_{ts}.py"
    path.write_text(code, encoding="utf-8")
    return path


def syntax_check(path):
    """语法校验补丁文件"""
    try:
        subprocess.run([sys.executable, "-m", "py_compile", str(path)],
                       capture_output=True, timeout=30, check=True)
        return True
    except subprocess.CalledProcessError as e:
        log(f"语法校验失败:\n{e.stderr.decode('utf-8', errors='ignore')[-400:]}")
        return False


def smoke_test(rounds):
    """冒烟测试: 跑主脚本 rounds 轮, 返回是否通过(无watchdog采样/无入口异常)"""
    log(f"冒烟测试: 跑 {rounds} 轮 (运行中请勿动手机)...")
    # 记录当前样本数, 测试后对比
    before = set(p.name for p in SAMPLES_DIR.glob("*")) if SAMPLES_DIR.exists() else set()
    try:
        p = subprocess.run([sys.executable, str(AGENT_PY), str(rounds)],
                           capture_output=True, timeout=rounds * 40 + 300)
        tail = (p.stdout or b"").decode("utf-8", errors="ignore")[-2500:]
    except subprocess.TimeoutExpired:
        log("冒烟测试超时(可能卡死)")
        return False, "超时"
    after = set(p.name for p in SAMPLES_DIR.glob("*")) if SAMPLES_DIR.exists() else set()
    new_samples = after - before
    if new_samples:
        return False, f"新增卡住样本: {new_samples}"
    if "入口异常" in tail or "[watchdog]" in tail:
        return False, "出现入口异常或watchdog触发"
    return True, tail[-300:]


def git_safe_apply(result):
    """应用补丁(带git回滚兜底): 提交dev分支 -> 语法校验 -> 冒烟 -> 合入或回滚"""
    # 1. 先确认当前是干净工作区
    git = ["git", "-C", str(BASE_DIR)]
    st = subprocess.run(git + ["status", "--porcelain"], capture_output=True, text=True)
    if st.stdout.strip():
        log("!! 工作区有未提交改动, 先提交或回滚, 再运行 --apply")
        return False
    # 2. 建 dev 分支
    subprocess.run(git + ["checkout", "-b", f"evolve_{time.strftime('%Y%m%d_%H%M%S')}"],
                   capture_output=True)
    # 3. 应用补丁: 替换/插入到对应位置
    #    简化实现: 补丁作为独立新增文件不现实, 需要注入主脚本。
    #    这里采用"按target锚点替换/追加"策略:
    target = result.get("target", "")
    patch = (result.get("code_patch") or "").strip()
    src = AGENT_PY.read_text(encoding="utf-8")
    ok = False
    # 只允许安全注入: is_ad_activity / is_in_qqmusic 全函数替换(纯函数, 无副作用)
    if target in ("is_ad_activity", "is_in_qqmusic"):
        # 替换整个函数: 从 def 到下一个 def/类外声明
        start = src.find(f"def {target}()")
        # 找下一个顶层 def (行首)
        end = None
        for m in re.finditer(r"\ndef (\w+)\(\)", src[start + 4:]):
            end = start + 4 + m.start()
            break
        if start != -1 and end and start < end:
            src = src[:start] + patch.rstrip("\n") + "\n\n" + src[end:]
            ok = True
    elif target in ("SYSTEM_PROMPT", "AD_ENTRY_PROMPT", "AD_CLOSE_PROMPT", "run_once", "ad_step"):
        log(f"target={target} 涉及提示词/核心逻辑, 自动注入易破坏能跑的主脚本 -> 已保存到 patches/, 请人工合并")
        return False
    if not ok:
        log("!! 补丁无法自动定位插入点, 已保存到 patches/ 目录, 请人工合并")
        return False
    AGENT_PY.write_text(src, encoding="utf-8")
    # 4. 语法校验
    if not syntax_check(AGENT_PY):
        log("语法校验失败 -> git回滚")
        subprocess.run(git + ["checkout", "."], capture_output=True)
        subprocess.run(git + ["checkout", "-"], capture_output=True)  # 回到原分支
        return False
    # 5. 提交到 dev 分支
    subprocess.run(git + ["add", "qqmusic_agent.py"], capture_output=True)
    subprocess.run(git + ["commit", "-m", f"evolve: {result.get('page_type','')} - {result.get('features','')[:60]}"],
                   capture_output=True)
    # 6. 冒烟测试
    ok, info = smoke_test(SMOKE_ROUNDS)
    if not ok:
        log(f"冒烟失败({info}) -> git回滚")
        subprocess.run(git + ["checkout", "-"], capture_output=True)
        return False
    # 7. 合入 main (当前在 dev 分支, 切回 main 再 merge)
    dev_branch = subprocess.run(git + ["branch", "--show-current"],
                                capture_output=True, text=True).stdout.strip()
    subprocess.run(git + ["checkout", "main"], capture_output=True)
    subprocess.run(git + ["merge", dev_branch, "--no-edit"], capture_output=True)
    subprocess.run(git + ["branch", "-D", dev_branch], capture_output=True)
    log("冒烟通过, 已合入 main")
    return True


# ================= 主流程 =================
if __name__ == "__main__":
    apply_mode = "--apply" in sys.argv
    SMOKE_ROUNDS = 100  # 用户要求多跑几轮(20轮跑不了几次广告)
    for a in sys.argv:
        if a.startswith("--rounds"):
            try:
                SMOKE_ROUNDS = int(a.split("=")[1])
            except (IndexError, ValueError):
                pass

    sd = latest_sample_dir()
    if sd is None:
        log(f"!! 没有样本。先跑 qqmusic_agent.py 直到卡住(watchdog会采样到 {SAMPLES_DIR})")
        sys.exit(1)
    log(f"分析样本: {sd}")

    # 1. 读样本
    shots = []
    for i in range(3):
        p = sd / f"shot_{i}.png"
        shots.append(encode_screenshot(p) if p.exists() else None)
    ctx = (sd / "context.txt").read_text(encoding="utf-8") if (sd / "context.txt").exists() else ""
    src = read_agent_source()

    # 2. 调模型
    result = call_evolve_model(shots, ctx, src)
    if result is None:
        sys.exit(1)

    # 3. 展示分析
    log(f"\n===== 进化模型({result.get('_provider','?')})分析结果 =====")
    log(f"页面类型: {result.get('page_type')}")
    log(f"识别特征: {result.get('features')}")
    log(f"卡住原因: {result.get('stuck_reason')}")
    log(f"应执行动作: {result.get('fix_action')}")
    log(f"置信度: {result.get('confidence')}")

    # 4. 保存补丁
    patch_path = extract_patch(result)
    if patch_path:
        log(f"代码补丁已保存: {patch_path}")
        ok = syntax_check(patch_path)
        log(f"补丁语法校验: {'通过' if ok else '失败'}")

    # 5. --apply 自动应用(带回滚兜底)
    if apply_mode:
        log("\n自动应用补丁...")
        git_safe_apply(result)
    else:
        log("\n(未自动应用。确认补丁后运行: python evolver.py --apply)")
