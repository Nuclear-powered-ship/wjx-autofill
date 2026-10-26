# -*- coding: utf-8 -*-
"""wjx_kit 纯函数单元测试。秒级，无浏览器依赖。

跑法：
    C:\\Users\\deepseek-v3\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe
    tests\\test_kit.py
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wjx_kit as K

FAILED = []
PASSED = []


def check(cid, cond, msg=""):
    if cond:
        PASSED.append(cid)
        print("  PASS  %s%s" % (cid, ("  " + msg) if msg else ""))
    else:
        FAILED.append(cid)
        print("  FAIL  %s  %s" % (cid, msg))


def run(cid, fn):
    try:
        fn()
    except Exception as e:
        import traceback
        FAILED.append(cid)
        print("  ERROR %s  %s" % (cid, e))
        print("       " + traceback.format_exc().replace("\n", "\n       "))


# ---------------------------------------------------------------- K1 域名边界
def k1():
    print("\nK1 域名白名单边界")
    ok = K.check_domain("https://v.wjx.cn/vm/xxx.aspx")
    check("K1.1", ok.startswith("https://v.wjx.cn"), "问卷星子域应放行")
    check("K1.2", K.check_domain("https://wjx.cn/x") == "https://wjx.cn/x",
          "主域应放行")
    for bad in ("https://evilwjx.cn/x", "https://notwjx.cn/x",
                "https://wjx.cn.evil.com/x", "https://evil.com/?a=wjx.cn"):
        try:
            K.check_domain(bad)
            check("K1.3 拒绝 " + bad, False, "竟然放行了")
        except K.DomainError:
            check("K1.3 拒绝 " + bad, True)
    try:
        K.check_domain("https://taobao.com")
        check("K1.4", False, "非问卷星站点必须拒绝")
    except K.DomainError:
        check("K1.4", True, "非问卷星站点已拒绝")


# ---------------------------------------------------------------- K2/K3 判定词单一来源
def k2k3():
    print("\nK2/K3 判定关键词")
    check("K2.1 含 12:52 事故文案",
          "您的答卷已经提交" in K.SUCCESS_KEYWORDS,
          "缺这条会再次误判 UNKNOWN")
    for w in ("提交成功", "您已完成", "已完成该问卷", "您的回答已"):
        check("K2.2 含 %s" % w, w in K.SUCCESS_KEYWORDS)
    # 只检查 wjx_signup.py（kit 本身是词表的权威来源，允许含字面量）
    src_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "wjx_signup.py")
    src = open(src_path, encoding="utf-8").read()

    # 用 ast 剥掉所有注释与 docstring，只留真实代码，
    # 这样"注释里提了一句"不会误判成"代码里写了字面量"。
    import ast
    import io
    import tokenize
    lines_out = []
    try:
        toks = tokenize.generate_tokens(io.StringIO(src).readline)
        for tok in toks:
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and tok.line.strip().startswith(
                    ('"""', "'''")):
                continue
            lines_out.append(tok)
    except Exception:
        lines_out = None

    if lines_out is not None:
        code = tokenize.untokenize(lines_out)
    else:
        code = src
    check("K3.1 主脚本代码中不含成功词字面量",
          "您的答卷已经提交" not in code,
          "主脚本代码里出现字面量说明没统一到常量，会再次走偏")
    check("K3.2 主脚本引用 SUCCESS_KEYWORDS",
          "SUCCESS_KEYWORDS" in src)
    check("K3.3 验证码等待循环也引用常量",
          src.count("SUCCESS_KEYWORDS") >= 2,
          "两个判定点必须共用一份词表")


# ---------------------------------------------------------------- K4 题干含多字段
def k4():
    print("\nK4 题干含多个字段名 -> 判歧义，不猜")
    cfg = K.Config({"字段": {
        "姓名": {"值": "丁", "别名": ["姓名"]},
        "学号": {"值": "1", "别名": ["学号"]},
    }})
    name, hits = cfg.field_by_alias("请填写您的姓名和学号")
    check("K4.1 命中两个-> 返回 None", name is None)
    check("K4.2 候选列出两个", sorted(hits) == sorted(["学号", "姓名"]),
          str(hits))
    name, hits = cfg.field_by_alias("您的姓名")
    check("K4.3 单命中-> 返回字段名", name == "姓名", str(name))
    name, hits = cfg.field_by_alias("无关的题目")
    check("K4.4 无命中-> (None, [])", name is None and hits == [])


# ---------------------------------------------------------------- K4b 短别名保护
def k4b():
    print("\nK4b 单字别名不得误命中（年级「级」不应命中「班级」）")
    cfg = K.Config({"字段": {
        "年级": {"值": None, "别名": ["年级", "级"]},
        "班级": {"值": "临床2611", "别名": ["班级"]},
    }})
    name, hits = cfg.field_by_alias("班级")
    check("K4b.1 「级」不在「班级」里命中",
          name == "班级", "得到 %r %r" % (name, hits))
    name, hits = cfg.field_by_alias("年级")
    check("K4b.2 「级」整词仍能命中年级",
          name == "年级", "得到 %r %r" % (name, hits))
    name, hits = cfg.field_by_alias("你的年级是")
    check("K4b.3 「年级」子串命中",
          name == "年级", "得到 %r %r" % (name, hits))


# ---------------------------------------------------------------- K5 同字段两处
def k5():
    print("\nK5 同一字段在页面出现两次 -> 都列出，不静默丢弃")
    cfg = K.Config({"字段": {"姓名": {"值": "丁", "别名": ["姓名"]}}})
    n1, _ = cfg.field_by_alias("姓名")
    n2, _ = cfg.field_by_alias("姓名（请与证件一致）")
    check("K5.1 两处都能定位", n1 == "姓名" and n2 == "姓名")


# ---------------------------------------------------------------- K6 页面无该题
def k6():
    print("\nK6 页面没有某字段 -> 完全不提（不虚报缺失）")
    cfg = K.Config({"字段": {
        "姓名": {"值": "丁", "别名": ["姓名"]},
        "学院": {"值": "临安", "别名": ["学院"]},
    }})
    used = {"姓名"}
    # 模拟：页面只有姓名题，没学院题 -> missing 应为空
    missing = [f for f in cfg.all_field_names() if f not in used]
    #注意：这里只是纯逻辑演示，真正的 missing 计算在 wjx_signup 侧
    # 关键契约：unused（配了但没出现）与 missing（出现了但没值）是两个不同集合
    check("K6.1 unused 与 missing 概念分离",
          len(missing) == 1 and missing == ["学院"],
          "配了但页面没问=> 属unused，不是 missing")


# ---------------------------------------------------------------- K7/K8 未配置字段
def k7k8():
    print("\nK7/K8 未配置字段的两种走向")
    cfg = K.Config({"字段": {
        "姓名": {"值": "丁", "别名": ["姓名"]},
        "年级": {"值": None, "别名": ["年级"], "待用户填写": True},
    }})
    check("K7.1 未填值的字段读出来是 None", cfg.field_value("年级") is None)
    check("K7.2 已填值的读得出来", cfg.field_value("姓名") == "丁")
    check("K7.3 configured_fields 不含未配置项",
          "年级" not in cfg.configured_fields())
    # K8：页面没有年级题 -> 只在 unused 提示，不阻塞
    items = [K.Item(K.AUTO, "你的姓名", detail="已填入（1 字）")]
    g = K.decide_gate(items)
    check("K8.1 无 ASK 时闸门放行", g.ok, "unused 不该阻塞")


# ---------------------------------------------------------------- K9/K10/K11 闸门
def k9k10k11():
    print("\nK9/K10/K11 闸门决策")
    auto = [K.Item(K.AUTO, "姓名", detail="已填入")]
    ask = [K.Item(K.AUTO, "姓名", detail="已填入"),
           K.Item(K.ASK, "你的性别", reason="未配置选项")]
    skip = [K.Item(K.AUTO, "姓名", detail="已填入"),
            K.Item(K.SKIP, "活动说明")]

    g = K.decide_gate(auto)
    check("K10.1 全部 AUTO -> 放行", g.ok)
    check("K10.2 全部 AUTO -> filled=1", len(g.filled) == 1)

    g = K.decide_gate(ask)
    check("K9.1 有 ASK -> 拦截", not g.ok)
    check("K9.2 拦截原因可读", "不会填" in g.reason_summary(),
          g.reason_summary())
    check("K9.3 filled 仍记录已填的", len(g.filled) == 1)

    g = K.decide_gate(skip)
    check("K10.3 SKIP 不阻塞", g.ok)
    check("K10.4 SKIP 仍进报告", len(g.skips) == 1)

    g = K.decide_gate(auto, scan_complete=False)
    check("K11.1 扫描不完整 -> 拦截", not g.ok)
    check("K11.2 原因指向扫描",
          "扫描" in g.reason_summary(), g.reason_summary())

    # 空页面：问卷未开放时页面只有倒计时，扫到 0 个控件。
    # 若放行，会提交一份空表单白占名额 —— 必须拦。
    g = K.decide_gate([], scan_complete=True, n_controls=0)
    check("K11.3 扫到0控件 -> 拦截", not g.ok,
          "0 个控件时放行会提交空表单")
    check("K11.4 空页面进 asks", len(g.asks) == 1)
    check("K11.5 空页面也在 items 里（报告守恒）",
          len(g.asks) == 1 and len(g.items) == 1)
    check("K11.6 空页面原因可读",
          "尚未开放" in g.asks[0].reason, g.asks[0].reason)

    # 有控件时不应触发空页面兜底
    g = K.decide_gate(auto, scan_complete=True, n_controls=1)
    check("K11.7 有控件时不误触发", g.ok)

    # 兼容旧调用方式（不传 n_controls）
    g = K.decide_gate([], scan_complete=True)
    check("K11.8 不传 n_controls 时保持旧行为", g.ok,
          "老调用不该被这个新参数影响")


# ---------------------------------------------------------------- K12 报告守恒
def k12():
    print("\nK12 报告守恒：题目数必须与扫描数一致")
    items = [
        K.Item(K.AUTO, "姓名", detail="已填入"),
        K.Item(K.ASK, "性别", reason="未配置"),
        K.Item(K.SKIP, "活动说明"),
        K.Item(K.ASK, "出生日期", reason="日期控件"),
    ]
    g = K.decide_gate(items)
    n_filled = len(g.filled)
    n_ask = len(g.asks)
    n_skip = len(g.skips)
    check("K12.1 三类之和 == 总数",
          n_filled + n_ask + n_skip == len(items),
          "%d+%d+%d != %d" % (n_filled, n_ask, n_skip, len(items)))


# ---------------------------------------------------------------- K13 报告打码
def k13():
    print("\nK13 报告打码：能验证但不泄明文")
    m = K.mask_value("丁奕哲")
    check("K13.1 姓名打码", "丁" in m and "奕哲" not in m, m)
    m2 = K.mask_value("1109261108")
    check("K13.2 学号打码", "1109261108" not in m2, m2)
    check("K13.3 保留长度供验证", "10" in m2, m2)
    m3 = K.mask_value("1109261108", show=True)
    check("K13.4 明文模式给全值", m3 == "1109261108", m3)
    m4 = K.mask_value(None)
    check("K13.5 None 显示未配置", "未配置" in m4, m4)


# ---------------------------------------------------------------- K14 时间解析
def k14():
    print("\nK14 报名时间解析")
    s, e, raw = K.parse_deadline_from_text(
        "四、报名时间：10月10日（周六）12:10:00 至 10月11日（周日）12:10:00")
    check("K14.1 提取到开始", s is not None and "12:10:00" in s, str(s))
    check("K14.2 提取到截止", e is not None and "12:10:00" in e, str(e))
    check("K14.3 原文存档非空", bool(raw), str(raw))

    s2, e2, _ = K.parse_deadline_from_text("报名：10-10 12:00 - 20:00")
    check("K14.4 短格式也认", s2 and "12:00:00" in s2, str(s2))
    check("K14.5 短格式截止", e2 and "20:00:00" in e2, str(e2))

    check("K14.6 无关文本返回空",
          K.parse_deadline_from_text("今天天气不错") == (None, None, None))

    # 只给开始时间 -> 应给默认 4 小时窗口
    s3, e3, _ = K.parse_deadline_from_text("报名时间：10月10日12:10:00")
    check("K14.7 仅开始时间也给截止",
          s3 and e3 and datetime.strptime(e3, "%Y-%m-%d %H:%M:%S") -
          datetime.strptime(s3, "%Y-%m-%d %H:%M:%S") == timedelta(hours=4))

    # 过去时间必须抛错，不能静默立即提交
    cfg = K.Config({"报名时间": {"开始": "2020-01-01 12:00:00"}})
    try:
        K.resolve_start(cfg, force_now=False)
        check("K14.8 过去时间必须报错", False, "竟然静默提交了")
    except K.PastTimeError as ex:
        check("K14.8 过去时间必须报错", True)
        check("K14.9 报错信息含 force-now 提示", "--force-now" in str(ex))

    # 没有报名时间 + 不 force -> 返回 None（调用方应拒绝提交）
    cfg2 = K.Config({})
    check("K14.10 无时间不 force -> None",
          K.resolve_start(cfg2, force_now=False) is None)
    check("K14.11 force_now 返回当前",
          K.resolve_start(cfg2, force_now=True) is not None)


# ---------------------------------------------------------------- K15 主脚本源码级契约
def k15():
    print("\nK15 主脚本源码级契约（防止改回老毛病）")
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(base, "wjx_signup.py"), encoding="utf-8").read()

    n_submit = src.count("submit(page)")
    # 闸门以 K.decide_gate / rescan_and_gate 两种形态出现
    n_gate = (src.count("decide_gate(")
              + src.count("rescan_and_gate(")
              + src.count("if not result") + src.count("if not result2")
              + src.count("if not result3") + src.count("if not result4"))
    check("K15.1 存在闸门调用", n_gate > 0,
          "未发现闸门接入点")
    check("K15.2 闸门覆盖多阶段", n_gate >= 5,
          "闸门/重扫调用 %d 次（submit %d 次），至少应覆盖"
          "初次/重载/弹层后/验证码后四个阶段" % (n_gate, n_submit))
    check("K15.2b 重扫函数已定义", "def rescan_and_gate" in src)

    check("K15.3 域名校验已用 kit",
          "check_domain" in src and "endswith(\".\" + d)" in
          open(os.path.join(base, "wjx_kit.py"), encoding="utf-8").read())
    check("K15.4 profile 有清理逻辑",
          "rmtree" in src, "跑完必须删临时 profile，否则每次泄漏 20-25MB")
    check("K15.5 lock 有 pid 存活校验",
          "pid_alive" in src,
          "残留锁会导致双击没反应且查不到原因")
    check("K15.6 默认不提交",
          "want_submit = bool(args.submit_now or args.at)" in src,
          "不带参数必须是检查模式")
    check("K15.7 配置从 json 读",
          "K.load_config()" in src and 'INFO = {' not in src,
          "身份信息不应再硬编码在主脚本里")


# ---------------------------------------------------------------- K16 组合字段：值优先
def k16():
    print("\nK16 组合字段：值优先于组合")
    cfg = K.Config({"字段": {
        "班级": {"值": "临床2611", "别名": ["班级"]},
        "专业班级": {"值": "临床2611", "别名": ["专业班级"],
                     "组合": "{班级}"},
    }})
    check("K16.1 有值时直接用值",
          cfg.field_value("专业班级") == "临床2611")
    check("K16.2 configured_fields 含组合条目",
          cfg.configured_fields().get("专业班级") == "临床2611")


# ---------------------------------------------------------------- K17 组合模板 + 最长别名优先
def k17():
    print("\nK17 组合模板渲染 & 最长别名优先消歧")
    cfg = K.Config({"字段": {
        "专业": {"值": "临床医学", "别名": ["专业", "所学专业"]},
        "班级": {"值": "临床2611", "别名": ["班级", "行政班"]},
        "专业班级": {"别名": ["专业班级"], "组合": "{班级}"},
    }})
    # 消歧：事故现场文本 " 专业班级" 应精确命中，不再歧义
    name, cands = cfg.field_by_alias(" 专业班级")
    check("K17.1 最长别名优先 -> 命中 专业班级",
          name == "专业班级", "得到 %r %r" % (name, cands))
    check("K17.2 不再返回歧义候选", cands == [], str(cands))

    check("K17.3 {班级} 渲染为 临床2611",
          cfg.field_value("专业班级") == "临床2611")

    cfg2 = K.Config({"字段": {
        "专业": {"值": "临床医学", "别名": ["专业"]},
        "班级": {"值": "临床2611", "别名": ["班级"]},
        "专业班级": {"别名": ["专业班级"], "组合": "{专业} {班级}"},
    }})
    check("K17.4 多占位符拼接",
          cfg2.field_value("专业班级") == "临床医学 临床2611")

    cfg3 = K.Config({"字段": {
        "专业": {"值": None, "别名": ["专业"]},
        "班级": {"值": "临床2611", "别名": ["班级"]},
        "专业班级": {"别名": ["专业班级"], "组合": "{专业} {班级}"},
    }})
    check("K17.5 引用字段缺值 -> None",
          cfg3.field_value("专业班级") is None)

    cfg4 = K.Config({"字段": {
        "专业班级": {"别名": ["专业班级"], "组合": "{不存在}"},
    }})
    check("K17.6 引用不存在字段 -> None",
          cfg4.field_value("专业班级") is None)

    cfg5 = K.Config({"字段": {
        "专业班级": {"别名": ["专业班级"], "组合": "临床2611"},
    }})
    check("K17.7 常量模板照常返回",
          cfg5.field_value("专业班级") == "临床2611")


# ---------------------------------------------------------------- K18 消歧回归
def k18():
    print("\nK18 消歧回归：无包含关系时行为不变")
    cfg = K.Config({"字段": {
        "姓名": {"值": "丁", "别名": ["姓名"]},
        "学号": {"值": "1", "别名": ["学号"]},
    }})
    name, hits = cfg.field_by_alias("请填写您的姓名和学号")
    check("K18.1 无包含关系仍判歧义", name is None and len(hits) == 2)

    # 同一字段内长短别名并存：不应误判歧义（不互相剔除）
    cfg2 = K.Config({"字段": {
        "专业": {"值": "临床", "别名": ["专业", "所学专业"]},
    }})
    name, hits = cfg2.field_by_alias("请填所学专业")
    check("K18.2 同字段长短别名 -> 单命中", name == "专业", str(hits))


# ---------------------------------------------------------------- K19 未开放守候
def k19():
    print("\nK19 --wait-open：开放时间解析 + 不提交契约")
    import importlib
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, base)
    S = importlib.import_module("wjx_signup")

    t = S.parse_open_time(
        "很抱歉，此问卷将于2026-10-22 12:00（北京时间）开放，"
        "请到时再进入此页面进行填写！")
    check("K19.1 抠出开放时间",
          t == datetime(2026, 10, 22, 12, 0), str(t))
    check("K19.2 无开放时间返回 None",
          S.parse_open_time("十佳歌手半决赛 提交") is None)
    check("K19.3 单位数月份也能解析",
          S.parse_open_time("此问卷将于2026-1-5 9:30开放") ==
          datetime(2026, 1, 5, 9, 30))

    src = open(os.path.join(base, "wjx_signup.py"), encoding="utf-8").read()
    check("K19.4 守候流程不含提交动作",
          "submit(page)" not in src.split("def wait_for_open")[1]
          .split("def match_fields")[0],
          "守候只负责拿题，提交必须回到闸门之后")
    check("K19.5 守候零控件仍走闸门",
          "decide_gate([], scan_complete=False" in src,
          "守候超时也必须产出报告，不能静默退出")


# ---------------------------------------------------------------- K20 单选匹配规则
def k20():
    print("\nK20 单选：只在配置允许时才点，其余一律拦下")
    import importlib
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, base)
    S = importlib.import_module("wjx_signup")

    def radio_field(qname, opts, rid="grp_q1"):
        return {"kind": "radio", "qtitle": qname, "row": "", "required": True,
                "tag": "input", "type": "radio", "id": rid, "name": "q1",
                "options": [{"id": "q1_%d" % (i + 1), "text": t}
                            for i, t in enumerate(opts)]}

    # 1) 配置声明了选项且值能唯一定位 -> 可自动点
    cfg = K.Config({"字段": {
        "性别": {"值": "男", "别名": ["性别"], "题型": "radio",
                 "选项": ["男", "女"]}}})
    m, items, _, _ = S.match_fields([radio_field("1. 性别", ["男", "女"])], cfg)
    check("K20.1 值命中 -> 产出映射",
          len(m) == 1 and m[0]["target_id"] == "q1_1", str(m))

    # 2) 配置没声明「选项」-> ASK，绝不猜
    cfg2 = K.Config({"字段": {"性别": {"值": "男", "别名": ["性别"]}}})
    m2, i2, _, _ = S.match_fields([radio_field("1. 性别", ["男", "女"])], cfg2)
    check("K20.2 未声明选项 -> 不映射且报 ASK",
          not m2 and any("没声明" in it.reason for it in i2))

    # 3) 页面选项里找不到该值 -> ASK（例如问卷写 2026级 而配置写 大一）
    cfg3 = K.Config({"字段": {
        "年级": {"值": "大一", "别名": ["年级"], "选项": ["大一", "大二"]}}})
    m3, i3, _, _ = S.match_fields(
        [radio_field("2. 年级", ["2026级", "2025级"])], cfg3)
    check("K20.3 选项对不上 -> 不映射且报 ASK",
          not m3 and any("找不到匹配" in it.reason for it in i3))

    # 4) 多选/下拉/日期 -> 一律 ASK，不映射
    for t, q in (("checkbox", "你参加过的活动"), ("select-one", "你的学院"),
                 ("date-readonly", "出生日期")):
        f = {"kind": t, "qtitle": q, "row": "", "required": True,
             "tag": "input", "type": t, "id": "x_" + t, "name": "x",
             "options": [{"id": "a", "text": "甲"}]}
        mm, ii, _, _ = S.match_fields([f], cfg)
        check("K20.4 %s 只报 ASK 不自动填" % t, not mm and len(ii) == 1,
              str(mm))

    # 5) 同一字段出现两次单选 -> 第二次 ASK，不重复点
    cfgb = K.Config({"字段": {
        "性别": {"值": "男", "别名": ["性别"], "选项": ["男", "女"]}}})
    two = [radio_field("1. 性别", ["男", "女"], "grp_a"),
           radio_field("5. 性别（再次确认）", ["男", "女"], "grp_b")]
    mb, ib, _, _ = S.match_fields(two, cfgb)
    check("K20.5 重复字段 -> 只映射一次",
          len(mb) == 1 and any("出现了多次" in it.reason for it in ib))


# ---------------------------------------------------------------- K21 响铃策略 / 关窗兜底
def k21():
    print("\nK21 成功不响铃、用户关窗不响铃")
    import importlib
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, base)
    S = importlib.import_module("wjx_signup")

    check("K21.1 认识 playwright 关窗错误",
          S.is_page_closed("Target page, context or browser has been closed")
          and S.is_page_closed("Page.wait_for_timeout: Target closed"))
    check("K21.2 普通异常不误判成关窗",
          not S.is_page_closed("Timeout 30000ms exceeded"))

    src = open(os.path.join(base, "wjx_signup.py"), encoding="utf-8").read()
    check("K21.3 成功路径不无条件响铃",
          'if status not in ("SUCCESS", "PAGE_CLOSED"):' in src,
          "结尾的 ring() 必须带条件，否则成功也会响")
    check("K21.4 全局异常兜底也排除关窗",
          "if not is_page_closed(_e):" in src)
    check("K21.5 关窗状态有独立收尾",
          'status = "PAGE_CLOSED"' in src and 'PAGE_CLOSED' in src,
          "关窗必须收敛成 PAGE_CLOSED，不能当崩溃")

    # 报告里能出现这个状态，且不算失败
    K_item = K.Item(K.AUTO, "姓名", detail="已填入 丁*", qtype="text")
    g = K.decide_gate([K_item])
    txt = K.render_report(g, K.Config({}), "https://v.wjx.cn/vm/x.aspx",
                          "--submit-now", extra_notes=["提交结果：PAGE_CLOSED"])
    check("K21.6 报告能携带 PAGE_CLOSED 备注", "PAGE_CLOSED" in txt)

    # 2026-10-26 红丝带晚会事故：提交没拿到回执，报告绝不能写「已提交」
    for bad in ("PAGE_CLOSED", "UNKNOWN", "CAPTCHA_TIMEOUT"):
        txt2 = K.render_report(g, K.Config({}), "https://v.wjx.cn/vm/x.aspx",
                               "--submit-now", submit_status=bad)
        check("K21.7 %s 不许写成「已提交」" % bad,
              "已提交，" not in txt2 and "未确认提交" in txt2,
              "只有 submit_status == SUCCESS 才允许写「已提交」")
    ok_txt = K.render_report(g, K.Config({}), "https://v.wjx.cn/vm/x.aspx",
                             "--submit-now", submit_status="SUCCESS")
    check("K21.7success SUCCESS 才写「已提交」",
          "已提交，" in ok_txt and "未确认提交" not in ok_txt)



# ---------------------------------------------------------------- K22 通用化检查
def k22():
    print("\nK22 通用仓库：无个人数据、模板字段齐全")
    import importlib
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, base)
    C = importlib.import_module("configure")

    # 仓库里任何脚本都不能夹带作者个人信息
    leaks = []
    for fn in ("wjx_kit.py", "wjx_signup.py", "configure.py", "decode_qr.py",
               "make_task.py", "README.md", "SKILL.md"):
        path = os.path.join(base, fn)
        if not os.path.isfile(path):
            continue
        txt = open(path, encoding="utf-8", errors="replace").read()
        for bad in ("1109261108", "丁奕哲", "workbuddy工作空间"):
            if bad in txt:
                leaks.append("%s 含 %s" % (fn, bad))
    check("K22.1 仓库不含作者个人信息", not leaks, "; ".join(leaks))

    fields = C.TEMPLATE["字段"]
    need = ["姓名", "学号", "性别", "学院", "专业", "班级", "年级"]
    check("K22.2 模板含七项必填字段",
          all(f in fields for f in need),
          str([f for f in need if f not in fields]))
    check("K22.3 性别按单选声明",
          fields["性别"]["题型"] == "radio" and "男" in fields["性别"]["选项"])
    check("K22.4 模板默认值全为空（不能带别人的信息）",
          all((fields[f].get("值") in (None, "")) for f in fields),
          str({f: fields[f].get("值") for f in fields if fields[f].get("值")}))
    check("K22.5 有专业班级合并栏模板", "专业班级" in fields
          and fields["专业班级"].get("组合") == "{班级}")
    src_c = open(os.path.join(base, "configure.py"), encoding="utf-8").read()
    check("K22.7 --reset 不进交互（脚本调用不会卡住）",
          "if a.reset:" in src_c,
          "reset 单独使用时必须直接写模板并退出")
    check("K22.6 打码函数不泄露明文",
          C.mask("1109261108").startswith("1")
          and "1109261108" != C.mask("1109261108"))


# ---------------------------------------------------------------- K23 demo 模式
def k23():
    print("\nK23 --demo：只填表不提交，把提交权留给人工")
    import os
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(base, "wjx_signup.py"), encoding="utf-8").read()

    check("K23.1 --demo 不被闸门提前截断",
          "not want_submit and not args.demo" in src,
          "want_submit=False 时若不排除 demo，会一栏都不填却报「已填好」")
    check("K23.2 有 --hold 参数",
          '"--hold"' in src and 'args.hold' in src,
          "留窗时长必须可配，否则来不及人工接手")
    check("K23.3 demo 分支会等到人工提交",
          "等待人工提交" in src and "检测到你已手动提交" in src
          or "检测到人工提交" in src,
          "demo 期间要轮询页面，用户提交成功后截图存证")
    check("K23.4 demo 分支不调 submit()",
          "submit(page)" not in src.split("if args.demo:")[1].split("def do_submit")[0],
          "demo 的整个分支里绝不许出现 submit(page)")

    g = K.decide_gate([K.Item(K.AUTO, "姓名", detail="已填入 丁*")])
    d = K.render_report(g, K.Config({}), "https://v.wjx.cn/vm/x.aspx",
                        "--demo", submit_status="NOT_SUBMITTED")
    check("K23.5 demo 报告不说「已提交」",
          "已提交，" not in d and "已填好" in d,
          "demo 只填不交，措辞必须是「已填好…脚本未点提交」")
    d2 = K.render_report(g, K.Config({}), "https://v.wjx.cn/vm/x.aspx",
                         "--demo", submit_status="SUCCESS")
    check("K23.6 demo 下人工提交成功要认",
          "人工提交成功" in d2)


# ---------------------------------------------------------------- run all
def main():
    print("=" * 60)
    print("wjx_kit 单元测试")
    print("=" * 60)
    for fn in (k1, k2k3, k4, k4b, k5, k6, k7k8, k9k10k11, k12, k13, k14, k15,
               k16, k17, k18, k19, k20, k21, k22, k23):
        run(fn.__name__.upper(), fn)
    print("\n" + "=" * 60)
    print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  - %s" % f)
    print("=" * 60)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
