"""视觉定位兜底: 固定坐标搞不定的广告页, 直接让云端多模态模型给出按钮坐标。

为什么需要:
    金币中心的广告创意一直在换(京东型底部CTA在2130 / 微信型在1541 / 白屏直停型没有CTA /
    听书导流型整个页面都不同), 写死坐标永远在追着改, 而且改完下次又变。
    所以固定坐标只当"快路径", 一旦点不动就用这个模块问模型。

约定:
    返回值统一成 (x, y, info), x/y 是**真实像素**, rel 用 0~1000 归一化(与 flow_step 的常量一致)。
    任何异常/超时都返回 None, 调用方按"没找到"处理, 绝不阻塞主流程。
"""
import base64
import json
import os
import re
import time

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# 与 qqmusic_agent.py 同一套降级链(去掉纯OCR那个, 因为它给不出坐标)
PROVIDERS = [
    {
        "name": "Agnes(主号)",
        "base_url": "https://apihub.agnes-ai.com/v1",
        "api_key_env": "AGNES_API_KEY",
        "model": os.getenv("AGNES_MODEL") or "agnes-3.0-flash",
    },
    {
        "name": "Agnes(备用号)",
        "base_url": "https://apihub.agnes-ai.com/v1",
        "api_key_env": "AGNES_API_KEY2",
        "model": os.getenv("AGNES_BACKUP_MODEL") or os.getenv("AGNES_MODEL") or "agnes-3.0-flash",
    },
    {
        "name": "智谱GLM-4V",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key_env": "ZHIPUAI_API_KEY",
        "model": os.getenv("ZHIPUAI_MODEL") or "glm-4v-flash",
    },
]

_TEMPLATE = (
    "你是手机屏幕点击定位助手。这是一张 {ow}x{oh} 的安卓手机截图。\n"
    "请找出【{target}】在屏幕上的中心位置。\n"
    "只输出一行严格 JSON, 不要解释、不要 markdown:\n"
    '{{"found": true, "x": <中心x, 0-1000的整数, 0=最左, 1000=最右>, '
    '"y": <中心y, 0-1000的整数, 0=最上, 1000=最下>, "text": "<该元素上的文字>"}}\n'
    '如果屏幕上根本没有这个元素, 输出: {{"found": false}}'
)

_calls = 0


def call_count():
    return _calls


def _parse(text):
    """从模型回复里抠出 JSON。模型常带 ```json 或前后废话, 所以用正则兜一层。"""
    if not text:
        return None
    m = re.search(r"\{[^{}]*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def locate(img_path, target, ow, oh, timeout=25, tag=""):
    """看图找【target】, 返回 (x, y, info) 或 None。x/y 已是真实像素。"""
    global _calls
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    prompt = _TEMPLATE.format(ow=ow, oh=oh, target=target)
    for p in PROVIDERS:
        key = os.getenv(p["api_key_env"])
        if not key:
            continue
        t0 = time.time()
        try:
            client = OpenAI(base_url=p["base_url"], api_key=key, timeout=timeout, max_retries=0)
            resp = client.chat.completions.create(
                model=p["model"],
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    ],
                }],
                temperature=0.0,
                max_tokens=200,
            )
            _calls += 1
            txt = (resp.choices[0].message.content or "").strip()
            data = _parse(txt)
            used = time.time() - t0
            if not data or not data.get("found"):
                print(f"[视觉定位{tag}] {p['name']} {used:.1f}s 未找到 -> {txt[:60]}", flush=True)
                continue
            rx, ry = float(data.get("x", -1)), float(data.get("y", -1))
            if not (0 <= rx <= 1000 and 0 <= ry <= 1000):
                print(f"[视觉定位{tag}] {p['name']} 坐标越界 {data}", flush=True)
                continue
            x, y = int(rx / 1000 * ow), int(ry / 1000 * oh)
            print(f"[视觉定位{tag}] {p['name']} {used:.1f}s 命中 "
                  f"'{data.get('text', '')}' -> 相对({rx:.0f},{ry:.0f}) 像素({x},{y})", flush=True)
            return x, y, data
        except Exception as e:
            print(f"[视觉定位{tag}] {p['name']} 失败 {type(e).__name__}: {e}", flush=True)
    return None
