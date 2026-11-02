"""本地视觉感知层(纯离线, 不调用任何视觉模型)。

给 agent 提供三个运行时原语:
  1. full_fp / dist        整屏缩略指纹 —— 判断"这两帧是不是同一页"
  2. strip_sig / wait_change  只看顶部/底部 UI 条带的差分(避开视频画面区域),
                            用来替代固定 sleep, 并能判定"点了之后页面到底变没变"
  3. Policy                视觉指纹 → 点击动作 的记忆库(命中就不调 VLM),
                            带成功/失败计数、近邻合并、容量淘汰

为什么不用整屏差分: 广告是视频, 画面一直在变, 整屏差分永远"变了";
只看顶部 10% + 底部 15% 的 UI 条带, 才能反映"页面跳转"这种真实状态变化。
"""

import base64
import json
import os
import subprocess
import time

from PIL import Image

FP_W, FP_H = 24, 54          # 整屏指纹尺寸(宽, 高)
STRIP_W, STRIP_H = 48, 10    # 每条 UI 条带缩略尺寸
TOP_RATIO, BOTTOM_RATIO = 0.10, 0.15   # 顶部 10% / 底部 15%
# 页面指纹(用于"这是哪一页"): 同样只用 UI 条带, 但分辨率更高。
# 实测: 静止页面上整屏指纹相邻帧差 5 左右(红包雨/横幅动画), 条带差 0.0 —— 所以页面识别也必须用条带。
PAGE_TOP_RATIO, PAGE_BOTTOM_RATIO = 0.12, 0.20
PAGE_W, PAGE_H = 64, 14


class Vision:
    def __init__(self, adb, tmp_path="_vp_frame.png"):
        self.adb = adb
        self.tmp = tmp_path
        self.last_error = ""

    # ---------- 采集 ----------
    def grab(self, timeout=15):
        """截一张全屏图, 返回 PIL.Image(RGB); 失败返回 None"""
        try:
            with open(self.tmp, "wb") as f:
                subprocess.run([self.adb, "exec-out", "screencap", "-p"],
                               stdout=f, stderr=subprocess.DEVNULL, timeout=timeout)
            if os.path.getsize(self.tmp) < 1000:
                return None
            img = Image.open(self.tmp)
            img.load()
            return img.convert("RGB")
        except Exception as e:
            self.last_error = str(e)[:60]
            return None

    # ---------- 指纹 ----------
    @staticmethod
    def full_fp(img):
        """整屏缩略灰度指纹(bytes)"""
        if img is None:
            return b""
        return img.convert("L").resize((FP_W, FP_H)).tobytes()

    @staticmethod
    def strip_sig(img):
        """UI 条带指纹: 顶部 10% + 底部 15%(避开中间视频画面)"""
        if img is None:
            return b""
        w, h = img.size
        g = img.convert("L")
        top = g.crop((0, 0, w, int(h * TOP_RATIO))).resize((STRIP_W, STRIP_H))
        bot = g.crop((0, int(h * (1 - BOTTOM_RATIO)), w, h)).resize((STRIP_W, STRIP_H))
        return top.tobytes() + bot.tobytes()

    @staticmethod
    def page_fp(img):
        """页面指纹(供"这是哪一页"识别用): 顶部 12% + 底部 20% 的高分辨率条带。
        用条带而不是整屏, 是因为视频/动画只污染中间区域, 条带才是稳定的页面特征。"""
        if img is None:
            return b""
        w, h = img.size
        g = img.convert("L")
        top = g.crop((0, 0, w, int(h * PAGE_TOP_RATIO))).resize((PAGE_W, PAGE_H))
        bot = g.crop((0, int(h * (1 - PAGE_BOTTOM_RATIO)), w, h)).resize((PAGE_W, PAGE_H))
        return top.tobytes() + bot.tobytes()

    @staticmethod
    def dist(a, b):
        """两段指纹的平均绝对差(0~255); 任一方为空返回 999"""
        if not a or not b or len(a) != len(b):
            return 999.0
        return sum(abs(x - y) for x, y in zip(a, b)) / len(a)

    # ---------- 等待 ----------
    def wait_change(self, prev_sig, timeout=2.0, interval=0.35, th=3.0):
        """轮询直到 UI 条带发生变化(或超时)。
        返回 (是否变化, 最新条带指纹, 实际耗时秒)"""
        t0 = time.time()
        sig = prev_sig
        while time.time() - t0 < timeout:
            time.sleep(interval)
            img = self.grab()
            if img is None:
                continue
            sig = self.strip_sig(img)
            if self.dist(prev_sig, sig) > th:
                return True, sig, time.time() - t0
        return False, sig, time.time() - t0


class Policy:
    """视觉指纹 → 点击动作 的记忆库(规则库的视觉版)。

    一条记录 = 一个页面指纹 + 若干备选动作(相对坐标 0~1000) + 每个动作的成功/失败次数。
    查表: 找最近的指纹, 距离小于阈值且存在"成功过"的动作 → 直接复用, 不调 VLM。
    回流: VLM 点成功时把 (指纹, 坐标) 写回; 复用后页面没变化则记一次失败, 失败多于成功就弃用。
    """

    def __init__(self, path="visual_policy.json", max_entries=200, lookup_th=2.5, merge_th=1.5):
        """lookup_th/merge_th 用条带指纹时噪声接近 0, 所以阈值可以很小(2.5/1.5)。
        整屏指纹(噪声约5)不用于查表, 只做调试参考。"""
        self.path = path
        self.max_entries = max_entries
        self.lookup_th = lookup_th
        self.merge_th = merge_th
        self.entries = []
        self._load()

    # ---------- 存取 ----------
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.entries = data.get("entries", [])
        except Exception:
            self.entries = []

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "entries": self.entries}, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # ---------- 内部 ----------
    @staticmethod
    def _d(a, b):
        return Vision.dist(a, b)

    @staticmethod
    def _fp_of(entry):
        try:
            return base64.b64decode(entry["fp"])
        except Exception:
            return b""

    def nearest(self, fp):
        best, best_d = None, 999.0
        for e in self.entries:
            d = self._d(fp, self._fp_of(e))
            if d < best_d:
                best, best_d = e, d
        return best, best_d

    # ---------- 查表 ----------
    def lookup(self, fp):
        """返回 (x_rel, y_rel) 或 None"""
        if not fp:
            return None
        e, d = self.nearest(fp)
        if not e or d > self.lookup_th:
            return None
        acts = [a for a in e.get("acts", []) if a.get("ok", 0) > a.get("bad", 0)]
        if not acts:
            return None
        acts.sort(key=lambda a: (a.get("ok", 0) - a.get("bad", 0)), reverse=True)
        e["hits"] = e.get("hits", 0) + 1
        return int(acts[0]["x"]), int(acts[0]["y"])

    # ---------- 回流 ----------
    def record(self, fp, x_rel, y_rel):
        """VLM 点成功后回流: 同一页面合并坐标, 坐标差 60 以上视为另一个动作"""
        if not fp:
            return
        x_rel, y_rel = int(x_rel), int(y_rel)
        e, d = self.nearest(fp)
        if not e or d > self.merge_th:
            self.entries.append({
                "fp": base64.b64encode(fp).decode("ascii"),
                "acts": [{"x": x_rel, "y": y_rel, "ok": 1, "bad": 0}],
                "hits": 1,
            })
        else:
            for a in e.get("acts", []):
                if abs(a["x"] - x_rel) < 60 and abs(a["y"] - y_rel) < 60:
                    a["ok"] = a.get("ok", 0) + 1
                    break
            else:
                e.setdefault("acts", []).append({"x": x_rel, "y": y_rel, "ok": 1, "bad": 0})
        self._trim()
        self.save()

    def penalize(self, fp, x_rel=None, y_rel=None):
        """复用后页面无变化: 记一次失败"""
        if not fp:
            return
        e, d = self.nearest(fp)
        if not e or d > self.lookup_th:
            return
        for a in e.get("acts", []):
            if x_rel is None or (abs(a["x"] - int(x_rel)) < 60 and abs(a["y"] - int(y_rel)) < 60):
                a["bad"] = a.get("bad", 0) + 1
                break
        self._trim()
        self.save()

    def _trim(self):
        if len(self.entries) <= self.max_entries:
            return
        self.entries.sort(key=lambda e: e.get("hits", 0), reverse=True)
        del self.entries[self.max_entries:]

    def stats(self):
        return len(self.entries), sum(len(e.get("acts", [])) for e in self.entries)
