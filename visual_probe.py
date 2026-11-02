"""独立视觉探头: 每隔 N 秒截一张全屏图到 visual_run/, 用于人工(视觉)复盘每一步。
不依赖 agent 的任何日志/VLM 判断, 纯客观帧序列。用法:
    python visual_probe.py [总时长秒] [间隔秒]
默认 480 秒 / 2 秒一张。
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ADB = os.path.join("platform-tools", "platform-tools", "adb.exe")
OUT = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("visual_run")


def main():
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 480
    gap = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
    OUT.mkdir(exist_ok=True)
    t0 = time.time()
    idx = 0
    while time.time() - t0 < total:
        idx += 1
        ts = time.strftime("%H%M%S")
        p = OUT / f"f{idx:04d}_{ts}.png"
        try:
            with open(p, "wb") as f:
                subprocess.run([ADB, "exec-out", "screencap", "-p"],
                               stdout=f, stderr=subprocess.DEVNULL, timeout=15)
            if p.stat().st_size < 1000:
                p.unlink(missing_ok=True)
        except Exception:
            p.unlink(missing_ok=True)
        time.sleep(max(0.3, gap - 0.6))


if __name__ == "__main__":
    main()
