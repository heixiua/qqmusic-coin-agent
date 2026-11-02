"""把 visual_run/ 的帧序列去重后拼成联系表(sheet_*.png), 便于人工视觉复盘。
用法: python visual_make_sheets.py
输出: visual_run/sheet_01.png ... ; 同时打印保留帧清单(序号/时间/差异值)
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

import os
SRC = Path(os.getenv("VSHEET_SRC", "visual_run"))
COLS, ROWS = 4, 3
CELL_W = 250
DIFF_TH = 7.0        # 相邻帧平均像素差阈值(0-255)
MAX_GAP = 12.0       # 最长容忍静默秒数


def small_gray(img):
    return img.convert("L").resize((108, 240))


def diff(a, b):
    pa, pb = list(a.getdata()), list(b.getdata())
    return sum(abs(x - y) for x, y in zip(pa, pb)) / len(pa)


def main():
    files = sorted(SRC.glob("f*.png"))
    if not files:
        print("no frames")
        return
    kept, prev_small, prev_ts = [], None, None
    for f in files:
        try:
            img = Image.open(f)
            img.load()
        except Exception:
            continue
        ts = f.stem.split("_")[-1]
        sm = small_gray(img)
        if prev_small is None:
            keep, d = True, 999.0
        else:
            d = diff(sm, prev_small)
            keep = d > DIFF_TH
            if not keep and prev_ts:
                keep = abs(int(ts) - int(prev_ts)) >= MAX_GAP
        if keep:
            kept.append((f, ts, d, img))
            prev_small = sm
            prev_ts = ts

    print(f"总帧 {len(files)} → 保留 {len(kept)}")
    per = COLS * ROWS
    for si in range(0, len(kept), per):
        chunk = kept[si:si + per]
        cell_h = int(CELL_W * 2400 / 1080)
        sheet = Image.new("RGB", (COLS * CELL_W, ROWS * (cell_h + 22)), (18, 18, 18))
        dr = ImageDraw.Draw(sheet)
        for i, (f, ts, d, img) in enumerate(chunk):
            r, c = divmod(i, COLS)
            x0, y0 = c * CELL_W, r * (cell_h + 22)
            thumb = img.convert("RGB").resize((CELL_W, cell_h))
            sheet.paste(thumb, (x0, y0 + 22))
            label = f"#{si + i + 1:03d} {ts[:2]}:{ts[2:4]}:{ts[4:6]} d={d:.0f}"
            dr.text((x0 + 4, y0 + 5), label, fill=(255, 220, 90))
        out = SRC / f"sheet_{si // per + 1:02d}.png"
        sheet.save(out)
        print("saved", out, f"({len(chunk)} 帧)")

    with open(SRC / "frames.txt", "w", encoding="utf-8") as fh:
        for i, (f, ts, d, _) in enumerate(kept):
            fh.write(f"#{i + 1:03d} {ts} diff={d:.1f} {f.name}\n")


if __name__ == "__main__":
    main()
