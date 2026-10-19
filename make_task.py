"""生成 Windows 任务计划程序的 XML，用于准点自动提交。

用法：
    python make_task.py                    # 用 wjx_config.json 里的报名开始时间
    python make_task.py "2026-10-22 12:00:00"   # 手动指定
    python make_task.py --lead 180         # 提前 N 秒启动（预热浏览器守候）

为什么要提前启动（--lead）：
    问卷星的开放闸门在服务端，未到点页面里没有题目结构。任务若正好
    12:00:00 启动，冷启动 Edge + 加载页面要 3~5 秒，等于白送几秒。
    提前 180 秒启动，脚本用 --wait-open 把浏览器热着，到点立刻拿到题目。

为什么不用 schtasks 命令行？
    本机 C:\\Windows\\system32\\schtasks.exe 在 WorkBuddy Program Blacklist 里，
    命令直接返回 Permission denied 且不可绕过。所以只能生成 XML 走 GUI 导入。

编码坑（2026-10-10 真实踩到）：
    任务计划程序导入时报「文件名无效」。原因是文件存成 UTF-16 LE 带 BOM，
    但 XML 声明写的也是 UTF-16，解析器对变体处理不一致，解析失败后
    抛了个毫无关联性的「文件名无效」。
    修法：**用 UTF-8 写文件 + 声明 UTF-8**，taskschd.msc 完全接受。
"""
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wjx_kit as K

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "wjx_task.xml")
OUT_UTF8 = os.path.join(BASE, "wjx_task_utf8.xml")


def _account():
    """当前账号，形如 LAPTOP-XXXX\\user。任务计划的 UserId 必须是
    真实账号或 SID，写死一个假名字（旧的 `DESKTOP`）会让导入报错。"""
    dom = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME") or "."
    usr = os.environ.get("USERNAME") or "user"
    return "%s\\%s" % (dom, usr)


def build_xml(start, bat_path, enc="UTF-16"):
    """返回 XML 文本。

    schema 层面的硬约束（这几条以前都踩过）：
    1) TimeTrigger 的子元素只能是 StartBoundary / EndBoundary / Enabled /
       Repetition —— 旧版里塞了个 `<Delay>`，那是 BootTrigger/LogonTrigger
       的字段，放在 TimeTrigger 里不合法，导入即报格式错误。
    2) RegistrationInfo 是 sequence：Author 必须在 Description 之前。
    3) Settings 的子元素按 MSDN 的字母序写（settingsType 是 all 语义，
       但字母序能同时满足 all 和 sequence 两种解释，最稳）。
    4) UserId 必须是真实 账号/SID。
    5) 编码必须和声明一致。任务计划程序的导入器用的是 MSXML，
       喂 UTF-8 无 BOM 的文件会报
       「(1,57):ERROR: unable to switch the encoding」（2026-10-17 实测）。
       Windows 自己导出的任务就是 UTF-16 LE + BOM，所以默认写 UTF-16；
       另外再存一份 UTF-8 备用。
    6) cmd.exe /c 后面用单层引号；双层 `""..""` 只在路径含空格时才必要，
       本机路径无空格，单层更不容易被解析歧义。
    """
    args = '/c "%s"' % bat_path
    return """<?xml version="1.0" encoding="%s" standalone="yes"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>WorkBuddy</Author>
    <Version>1.0</Version>
    <Description>WJX autofill signup (wait for open, then submit)</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>%s</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>%s</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <AllowHardTerminate>true</AllowHardTerminate>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <DisallowStartOnRemoteAppSession>false</DisallowStartOnRemoteAppSession>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Hidden>false</Hidden>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <Priority>4</Priority>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <StartWhenAvailable>true</StartWhenAvailable>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <WakeToRun>true</WakeToRun>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>cmd.exe</Command>
      <Arguments>%s</Arguments>
      <WorkingDirectory>%s</WorkingDirectory>
    </Exec>
  </Actions>
</Task>""" % (enc, start, _account(), args, BASE)


# TimeTrigger 只允许这些子元素（schema: timeTriggerType）
TIMETRIGGER_ALLOWED = ("StartBoundary", "EndBoundary", "Enabled", "Repetition")


def validate_xml(text, path):
    """按任务计划程序 schema 的硬规则自检，返回 (ok, [问题])。

    没有 XSD 可以离线跑（schtasks 在程序黑名单里，MSXML 又被沙箱挡），
    所以把「已知会导致导入失败」的规则逐条写成断言。其中最关键的一条是
    **声明编码必须与实际字节一致** —— 不一致就报
    「(1,57):ERROR: unable to switch the encoding」。
    """
    import xml.etree.ElementTree as ET
    problems = []
    bits = open(path, "rb").read()

    # ---- 实际编码判定 ----
    if bits[:2] == b"\xff\xfe":
        actual, body = "utf-16", bits
        body_text = bits.decode("utf-16")
    elif bits[:2] == b"\xfe\xff":
        actual, body_text = "utf-16-be", bits.decode("utf-16")
    elif bits[:3] == b"\xef\xbb\xbf":
        actual, body_text = "utf-8-bom", bits.decode("utf-8-sig")
        problems.append("文件带 UTF-8 BOM：MSXML 会报 unable to switch the encoding")
    else:
        actual = "utf-8"
        try:
            body_text = bits.decode("utf-8")
        except Exception:
            actual = "unknown"
            body_text = bits.decode("utf-8", "replace")
            problems.append("字节不是合法 UTF-8，也没 BOM，编码无法判定")

    m = re.search(r'encoding\s*=\s*"([^"]+)"', body_text[:200])
    declared = (m.group(1).lower() if m else "")
    if not m:
        problems.append("XML 声明里没有 encoding")
    elif declared.replace("-", "") not in actual.replace("-", ""):
        problems.append("声明 encoding=%s 与实际字节 %s 不一致 -> "
                        "导入器必报 unable to switch the encoding"
                        % (declared, actual))
    if not body_text.lstrip().startswith("<?xml"):
        problems.append("缺少 XML 声明")

    try:
        root = ET.fromstring(body_text)
    except Exception as e:
        return False, ["XML 解析失败：%s" % e]

    if not root.tag.endswith("}Task"):
        problems.append("根元素不是 Task")

    def kids(el):
        return [c.tag.split("}")[-1] for c in el]

    reg = root
    ns = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"

    def find(name):
        return root.find(ns + name)

    for name in ("RegistrationInfo", "Triggers", "Principals", "Settings",
                 "Actions"):
        if find(name) is None:
            problems.append("缺少 <%s>" % name)

    ri = find("RegistrationInfo")
    if ri is not None:
        order = kids(ri)
        if "Author" in order and "Description" in order and \
                order.index("Author") > order.index("Description"):
            problems.append("RegistrationInfo 里 Author 必须在 Description 之前")

    tt = root.find(".//%sTimeTrigger" % ns)
    if tt is None:
        problems.append("没有 TimeTrigger 触发器")
    else:
        for k in kids(tt):
            if k not in TIMETRIGGER_ALLOWED:
                problems.append("TimeTrigger 里出现非法子元素 <%s>（只允许 %s）"
                                % (k, "/".join(TIMETRIGGER_ALLOWED)))
        sb = tt.find(ns + "StartBoundary")
        if sb is None or not (sb.text or "").strip():
            problems.append("TimeTrigger 缺少 StartBoundary")
        else:
            try:
                datetime.strptime(sb.text.strip(), "%Y-%m-%dT%H:%M:%S")
            except Exception:
                problems.append("StartBoundary 不是 yyyy-MM-ddTHH:mm:ss：%s"
                                % sb.text)

    pr = root.find(".//%sPrincipal" % ns)
    uid = pr.find(ns + "UserId") if pr is not None else None
    utxt = (uid.text or "").strip() if uid is not None else ""
    if not utxt:
        problems.append("Principals 缺少 UserId")
    elif " " in utxt or "/c" in utxt or utxt.startswith("/"):
        problems.append("UserId 里混进了命令行参数：「%s」" % utxt)
    elif "\\" not in utxt and not utxt.startswith("S-"):
        problems.append("UserId「%s」不是真实账号/SID" % utxt)
    elif utxt != _account():
        problems.append("UserId「%s」与当前账号「%s」不一致" % (utxt, _account()))

    st = find("Settings")
    if st is not None:
        order = kids(st)
        if order != sorted(order, key=lambda x: x.lower()):
            problems.append("Settings 子元素未按字母序：%s" % ",".join(order))

    ex = root.find(".//%sExec" % ns)
    if ex is None:
        problems.append("Actions 里没有 Exec")
    else:
        if ex.find(ns + "Command") is None:
            problems.append("Exec 缺少 Command")
        ag = ex.find(ns + "Arguments")
        agtxt = (ag.text or "") if ag is not None else ""
        if ag is not None and '""' in agtxt:
            problems.append("Arguments 里有双层引号 \"\"..\"\"，路径无空格时不必")
        if ".bat" not in agtxt:
            problems.append("Arguments 里没有 .bat 目标：「%s」" % agtxt)

    return (not problems), problems


def main():
    cfg = K.load_config()
    argv = sys.argv[1:]
    lead = 180
    # --bat / --out：多个活动并行时用来区分 bat 与输出文件名
    bat_override = None
    out_base = os.path.basename(OUT)
    if "--bat" in argv:
        i = argv.index("--bat")
        bat_override = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    if "--out" in argv:
        i = argv.index("--out")
        out_base = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    if "--lead" in argv:
        i = argv.index("--lead")
        try:
            lead = int(argv[i + 1])
        except Exception:
            print("[错误] --lead 后面要跟秒数，例如 --lead 180")
            return 1
        argv = argv[:i] + argv[i + 2:]
    if argv:
        target = K._parse_dt(" ".join(argv))
        if target is None:
            print("[错误] 无法解析时间: %s" % " ".join(argv))
            print("       格式应为 'YYYY-MM-DD HH:MM:SS'")
            return 1
    else:
        target = cfg.start_dt()
        if target is None:
            print("=" * 56)
            print("wjx_config.json 里没有填「报名时间.开始」，无法生成任务。")
            print()
            print("两种填法：")
            print("  1) 告诉我活动通知截图，我从里面读出报名时间")
            print("  2) 自己打开 wjx_config.json，把「报名时间.开始」填成")
            print("     \"2026-10-11 12:10:00\" 这样")
            print()
            print("也可以先手动提交：运行 2_start_signup.bat")
            print("=" * 56)
            return 2

    if target < datetime.now():
        print("[警告] 时间 %s 已经过去了。" % target.strftime("%Y-%m-%d %H:%M:%S"))
        print("       任务导入后会立刻触发一次。请确认这就是你要的。")
        print("       若想改未来时间，请改 wjx_config.json 里的「报名时间.开始」。")

    bat_path = (os.path.join(BASE, bat_override) if bat_override
                else os.path.join(BASE, "auto_submit.bat"))
    if not os.path.exists(bat_path):
        bat_path = os.path.join(BASE, "2_start_signup.bat")
    if not os.path.exists(bat_path):
        print("[错误] 找不到可执行的 bat")
        return 3

    from datetime import timedelta
    start_at = target - timedelta(seconds=lead)

    # 任务计划要求 ISO 格式：2026-10-22T11:57:00
    iso = start_at.strftime("%Y-%m-%dT%H:%M:%S")
    xml = build_xml(iso, bat_path)

    # 主文件：UTF-16 LE + BOM（Windows 自己导出的任务就是这个格式，
    # 任务计划程序的 MSXML 只认这个；UTF-8 会报 unable to switch the encoding）
    xml16 = build_xml(iso, bat_path, enc="UTF-16")
    # 备用：UTF-8 无 BOM，某些版本/工具链更认这个
    xml8 = build_xml(iso, bat_path, enc="UTF-8")

    out16 = os.path.join(BASE, out_base)
    out8 = os.path.join(BASE, out_base.replace(".xml", "") + "_utf8.xml")
    with open(out16, "w", encoding="utf-16", newline="\r\n") as f:
        f.write(xml16)
    with open(out8, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(xml8)

    ok, problems = validate_xml(xml16, out16)
    ok8, problems8 = validate_xml(xml8, out8)

    print("=" * 56)
    print("已生成计划任务文件：")
    print("  首选 %s  (UTF-16 LE + BOM)" % out16)
    print("  备用 %s  (UTF-8 无 BOM)" % out8)
    print("任务启动：%s（提前 %d 秒）" % (start_at.strftime("%Y-%m-%d %H:%M:%S"),
                                    lead))
    print("问卷开放：%s" % target.strftime("%Y-%m-%d %H:%M:%S"))
    print("执行动作：%s" % bat_path)
    print("账号：%s" % _account())
    print("语法自检(UTF-16)：%s" % ("通过" if ok else "失败"))
    for p in problems:
        print("  [!] %s" % p)
    print("语法自检(UTF-8) ：%s" % ("通过" if ok8 else "失败"))
    for p in problems8:
        print("  [!] %s" % p)
    print()
    print("下一步：Win+R 输入 taskschd.msc -> 导入任务 -> 选这个文件")
    print("=" * 56)
    return 0 if (ok and ok8) else 4


if __name__ == "__main__":
    sys.exit(main())
