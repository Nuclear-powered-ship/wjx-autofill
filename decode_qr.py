r"""解码问卷星二维码 -> 链接。

支持：
    python decode_qr.py --dir qrcode            # 扫整个目录，列出所有候选
    python decode_qr.py --src 某张图.png         # 只解一张
    python decode_qr.py                          # 默认扫 qrcode 目录

只接受 *.wjx.cn / *.wjx.com 的链接（避免误取到别的二维码）。
只读取图片，不修改原文件。
"""
import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wjx_kit as K

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "wjx_url.txt")
DEFAULT_DIR = os.path.join(BASE, "qrcode")
IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif")


def decode_one(path):
    """解一张图。返回 (url, 说明) 或 (None, 原因)。"""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None, "缺少 opencv，无法解码"

    try:
        with open(path, "rb") as f:
            buf = np.frombuffer(f.read(), np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception as e:
        return None, "读取失败: %s" % e
    if img is None:
        return None, "不是有效的图片"

    det = cv2.QRCodeDetector()
    texts = []

    def _try(im):
        try:
            d, _, _ = det.detectAndDecode(im)
            if d:
                texts.append(d)
        except Exception:
            pass

    _try(img)
    if not texts:                                  # 放大重试
        _try(cv2.resize(img, None, fx=3, fy=3,
                        interpolation=cv2.INTER_CUBIC))
    if not texts:                                  # 网格切块重试（二维码可能只是长图一角）
        h, w = img.shape[:2]
        step, size = 240, 520
        for y0 in range(0, h, step):
            for x0 in range(0, w, step):
                _try(img[y0:min(y0 + size, h), x0:min(x0 + size, w)])
            if texts:
                break

    if not texts:
        return None, "没找到二维码"

    wjx = []
    for t in texts:
        for cand in (t.strip(), t.strip().replace(" ", "")):
            if ("wjx.cn" in cand or "wjx.com" in cand) and cand not in wjx:
                wjx.append(cand)
    if not wjx:
        return None, "找到二维码但不是问卷星链接：%s" % texts[0][:80]
    return wjx[0], "问卷星链接"


def main():
    ap = argparse.ArgumentParser(description="解码问卷星二维码")
    ap.add_argument("--src", help="指定单张图片")
    ap.add_argument("--dir", help="指定目录（默认 二维码\\）")
    ap.add_argument("--list", action="store_true",
                    help="只列出结果，不写入 wjx_url.txt")
    args = ap.parse_args()

    if args.src:
        if not os.path.exists(args.src):
            print("[错误] 找不到文件: %s" % args.src)
            return 1
        files = [args.src]
    else:
        scan_dir = args.dir or DEFAULT_DIR
        if not os.path.isdir(scan_dir):
            print("[错误] 目录不存在: %s" % scan_dir)
            print("       建这个目录，把二维码图片放进去；"
                  "或用 --src 指定单张图。")
            return 1
        files = []
        for ext in IMG_EXT:
            files.extend(glob.glob(os.path.join(scan_dir, "*" + ext)))
        # 顺带扫钉钉缓存（用户常直接截图钉钉里的码）
        for d in glob.glob(os.path.expandvars(
                r"%APPDATA%\DingTalk\*\ImageFiles")):
            for f in glob.glob(os.path.join(d, "*")):
                if f.lower().endswith(IMG_EXT):
                    files.append(f)
        files = sorted(set(files))

    if not files:
        print("目录里没有图片: %s" % (args.dir or DEFAULT_DIR))
        print("把二维码截图存成 png/jpg 放进该目录即可。")
        return 1

    print("=" * 56)
    print("扫描 %d 个文件..." % len(files))
    print("=" * 56)

    hits = []
    for f in files:
        url, why = decode_one(f)
        if url:
            print("[OK]   %s" % os.path.basename(f)[:50])
            print("       -> %s" % url)
            hits.append((f, url))
        elif args.src:
            print("[FAIL] %s：%s" % (os.path.basename(f), why))

    if not hits:
        print()
        print("没解出问卷星链接。")
        print("手动做：用记事本打开 wjx_url.txt，把整行换成链接保存。")
        return 2

    if len(hits) > 1:
        print()
        print("找到多个候选：")
        for i, (f, u) in enumerate(hits, 1):
            print("  %d) %s" % (i, u))
        if args.list:
            return 0
        # 取修改时间最新的那张，对应你刚截的码
        hits.sort(key=lambda x: os.path.getmtime(x[0]))
        chosen = hits[-1]
        print()
        print("已选最新那张图片对应的链接。")
    else:
        chosen = hits[0]

    if args.list:
        return 0

    url = chosen[1]
    try:
        K.check_domain(url)
    except K.DomainError as e:
        print("[拒绝] %s" % e)
        return 3

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(url)
    print()
    print("已写入: %s" % OUT)
    print("链接  : %s" % url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
