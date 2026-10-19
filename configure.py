"""填写 / 查看个人信息配置（wjx_config.json）。

给 agent 用：推荐非交互模式，一条命令写完，避免在 cmd 里逐行问答。
     python configure.py --name 张三 --sid 20240001 --gender 男 \
         --college 信息工程学院 --major 计算机科学与技术 --clazz 计算机2301 --grade 大一

给用户用：直接双击或 `python configure.py`，按提示一项项输入。

查看（默认打码）： python configure.py --show
清空重填：        python configure.py --reset
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, "wjx_config.json")

# 只放通用别名，不绑定任何学校。问卷措辞不同也能命中。
TEMPLATE = {
    "_说明": "问卷星自动填报的个人信息。改这里，不用动代码。",
    "字段": {
        "姓名": {"值": None, "别名": ["姓名", "名字", "本人姓名", "学生姓名"],
                 "题型": "auto"},
        "学号": {"值": None, "别名": ["学号", "学生编号", "一卡通号", "学工号"],
                 "题型": "auto"},
        "性别": {"值": None, "别名": ["性别"], "题型": "radio",
                 "选项": ["男", "女"]},
        "学院": {"值": None,
                 "别名": ["学院", "院系", "所在学院", "所属学院", "二级学院"],
                 "题型": "auto"},
        "专业": {"值": None, "别名": ["专业", "所学专业"], "题型": "auto"},
        "班级": {"值": None, "别名": ["班级", "行政班", "所在班级"],
                 "题型": "auto"},
        "年级": {"值": None, "别名": ["年级", "年级层次"], "题型": "radio",
                 "选项": ["大一", "大二", "大三", "大四", "大五", "研一", "研二",
                          "研三"]},
        "手机号": {"值": None,
                   "别名": ["手机号", "手机", "联系电话", "联系方式", "电话"],
                   "题型": "auto"},
        "专业班级": {"别名": ["专业班级", "专业及班级", "专业/班级"],
                     "组合": "{班级}", "题型": "auto",
                     "_说明": "合并栏用「组合」声明：{班级} 只填班级；"
                              "要「临床医学临床2611」改成 \"{专业}{班级}\""},
    },
    "报名时间": {"开始": None, "截止": None, "来源": "", "原文": ""},
    "允许跳过非必答题": False,
    "报告显示明文": False,
}

MENU = [
    ("name", "姓名", "姓名", "auto"),
    ("sid", "学号", "学号", "auto"),
    ("gender", "性别", "性别", "radio"),
    ("college", "学院", "学院", "auto"),
    ("major", "专业", "专业", "auto"),
    ("clazz", "班级", "班级", "auto"),
    ("grade", "年级", "年级", "radio"),
    ("phone", "手机号", "手机号", "auto"),
]


def load():
    if os.path.isfile(CFG):
        with open(CFG, encoding="utf-8") as f:
            return json.load(f)
    return json.loads(json.dumps(TEMPLATE))


def save(cfg):
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print("已写入 %s" % CFG)


def apply(cfg, key, field, value, qtype):
    if value is None or str(value).strip() == "":
        return
    cfg.setdefault("字段", {}).setdefault(field, {})
    cfg["字段"][field]["值"] = str(value).strip()
    cfg["字段"][field]["题型"] = qtype


def mask(v):
    if not v:
        return "（未填）"
    s = str(v)
    if len(s) == 1:
        return s                      # 「男」这种单字，打码等于没打，直接显示
    if len(s) <= 3:
        return s[0] + "*" * (len(s) - 1)
    return s[0] + "*" * (len(s) - 2) + s[-1]


def show(cfg, plain=False):
    print("=" * 46)
    print("当前配置：%s" % CFG)
    for _k, label, field, _t in MENU:
        v = (cfg.get("字段", {}).get(field) or {}).get("值")
        print("  %-6s %s" % (label, v if plain else mask(v)))
    rt = cfg.get("报名时间") or {}
    print("  报名时间 %s ~ %s" % (rt.get("开始") or "未设",
                                  rt.get("截止") or "未设"))
    print("=" * 46)


def interactive(cfg):
    print("按提示输入，直接回车表示跳过（保持原值）。")
    for _k, label, field, qtype in MENU:
        cur = (cfg.get("字段", {}).get(field) or {}).get("值") or ""
        hint = ("（当前 %s）" % cur) if cur else ""
        try:
            v = input("  %s%s: " % (label, hint)).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n中断，未保存。")
            return False
        if v:
            apply(cfg, _k, field, v, qtype)
    return True


def main():
    ap = argparse.ArgumentParser(description="填写问卷星自动填报的个人信息")
    for k, label, _f, _t in MENU:
        ap.add_argument("--" + k, help="%s（例：%s）" % (label, label))
    ap.add_argument("--start", help="报名开始时间，如 '2026-10-22 12:00:00'")
    ap.add_argument("--end", help="报名截止时间")
    ap.add_argument("--source", help="时间来源（截图/钉钉群通知等）")
    ap.add_argument("--show", action="store_true", help="查看当前配置（打码）")
    ap.add_argument("--plain", action="store_true", help="配合 --show：显示明文")
    ap.add_argument("--reset", action="store_true", help="清空重填")
    a = ap.parse_args()

    cfg = json.loads(json.dumps(TEMPLATE)) if a.reset else load()

    if a.show:
        show(cfg, a.plain)
        return 0

    touched = False
    for k, _label, field, qtype in MENU:
        v = getattr(a, k, None)
        if v:
            apply(cfg, k, field, v, qtype)
            touched = True

    if a.start or a.end or a.source:
        rt = cfg.setdefault("报名时间", {})
        if a.start:
            rt["开始"] = a.start
        if a.end:
            rt["截止"] = a.end
        if a.source:
            rt["来源"] = a.source
        touched = True

    if not touched:
        if not interactive(cfg):
            return 1

    save(cfg)
    show(cfg)
    missing = [(label, field) for _k, label, field, _t in MENU
               if not ((cfg.get("字段", {}).get(field) or {}).get("值"))]
    if missing:
        print("还缺：%s（问卷若问到这些题，脚本会停下来提示你）"
              % "、".join(m[0] for m in missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
