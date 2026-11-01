# -*- coding: utf-8 -*-
"""用 Python subprocess 字节安全截屏(PowerShell *> 会破坏二进制, 勿用)"""
import subprocess
from pathlib import Path

ADB = r"D:\桌面\xdf\Agent工程与具身智能\安卓手机控制\platform-tools\platform-tools\adb.exe"
OUT = Path(__file__).resolve().parent / "screen_tmp.png"

with open(OUT, "wb") as f:
    subprocess.run([ADB, "exec-out", "screencap", "-p"], stdout=f, check=True)
print(f"截屏成功: {OUT}  大小={OUT.stat().st_size}字节")
