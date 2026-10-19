"""一键环境准备：建独立虚拟环境 + 装依赖 + 自检。

给全新 Windows 10/11 用。不改系统 Python、不写注册表、不装全局包。

用法：
    python setup_env.py            # 建 .venv、装依赖、自检
    python setup_env.py --check    # 只自检（不装东西）
    python setup_env.py --python PATH   # 指定 python 解释器建 venv
"""
import argparse
import os
import subprocess
import sys
import venv

BASE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(BASE, ".venv")
VPY = os.path.join(VENV, "Scripts", "python.exe")
REQ = os.path.join(BASE, "requirements.txt")

MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def say(m):
    print(m, flush=True)


def run(cmd, **kw):
    say("  $ " + " ".join(cmd))
    return subprocess.call(cmd, **kw)


def find_edge():
    for p in EDGE_PATHS:
        if os.path.exists(p):
            return p
    return None


def check(verbose=True):
    """返回 (ok, 问题列表)。"""
    problems = []
    if not os.path.isfile(VPY):
        problems.append("还没有虚拟环境（.venv）。先跑：python setup_env.py")
    else:
        probe = subprocess.run(
            [VPY, "-c", "import cv2, playwright; print('ok')"],
            capture_output=True, text=True)
        if probe.returncode != 0:
            problems.append("依赖不全：cv2 / playwright 缺失或损坏。"
                            "先跑：python setup_env.py")
    if not find_edge():
        problems.append("没找到 Microsoft Edge。脚本默认用系统 Edge；"
                        "若你用 Chrome，改 wjx_signup.py 里的 channel")
    cfg = os.path.join(BASE, "wjx_config.json")
    if not os.path.isfile(cfg):
        problems.append("还没有个人信息配置。跑：python configure.py")
    if verbose:
        say("")
        say("环境自检：")
        say("  Python      %s" % sys.version.split()[0])
        say("  虚拟环境    %s" % ("OK" if os.path.isfile(VPY) else "缺失"))
        say("  Edge        %s" % (find_edge() or "未找到"))
        say("  配置        %s" % ("OK" if os.path.isfile(cfg) else "缺失"))
        if problems:
            for p in problems:
                say("  [!] " + p)
        else:
            say("  全部就绪。")
    return (not problems), problems


def ensure_venv(base_python):
    if os.path.isfile(VPY):
        say("[1/4] 虚拟环境已存在，跳过创建")
        return True
    say("[1/4] 创建虚拟环境 .venv（隔离，不影响系统 Python）")
    try:
        venv.EnvBuilder(with_pip=True, clear=False).create(VENV)
    except Exception as e:
        say("  venv 创建失败：%s" % e)
        say("  Windows 上若报错，可先装 Visual C++ 运行库，或改用：")
        say("    %s -m venv .venv" % base_python)
        return False
    return True


def pip_install(args):
    return run([VPY, "-m", "pip", "install", "--disable-pip-version-check"] + args)


def install_deps():
    say("[2/4] 安装依赖（首次约 2~6 分钟，取决于网速）")
    if not os.path.isfile(REQ):
        say("  找不到 requirements.txt")
        return False
    rc = pip_install(["-r", REQ])
    if rc != 0:
        say("  官方源失败，改用清华镜像重试…")
        rc = pip_install(["-i", MIRROR, "-r", REQ])
    if rc != 0:
        say("  依赖安装失败。常见原因：没联网 / 公司代理 / 杀软拦截。")
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只自检")
    ap.add_argument("--python", help="用哪个解释器建 venv（默认当前）")
    a = ap.parse_args()

    if a.check:
        ok, _ = check()
        return 0 if ok else 1

    say("=" * 56)
    say("问卷星自动填报 · 环境准备")
    say("=" * 56)
    if sys.version_info < (3, 8):
        say("Python 版本过低（当前 %s），需要 3.8+" % sys.version.split()[0])
        return 2

    if not ensure_venv(a.python or sys.executable):
        return 3
    if not install_deps():
        return 4

    say("[3/4] 确认 Edge（脚本复用系统 Edge，不下载 Chromium 省 300MB）")
    say("  Edge: %s" % (find_edge() or "未找到（可改用 playwright install chromium）"))

    say("[4/4] 自检")
    ok, _ = check()
    say("")
    if ok:
        say("准备完成。下一步：python configure.py 填个人信息")
        return 0
    say("还有问题没解决，见上面的 [!] 提示。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
