"""
问卷星自动填报脚本（wjx-bot）

安全约束（硬编码，不可通过参数关闭）：
  1. 只允许对 *.wjx.cn / *.wjx.com 域名操作（wjx_kit.check_domain，点号边界严谨）
  2. 只允许填写 wjx_config.json「字段」里声明的白名单
  3. **提交闸门**：只要有 wjx_kit.ASK 状态的题存在，就绝不提交。
     全文4 个 submit 调用点全部经过闸门，守住守时/默认/弹层重试/验证码重提。
  4. 日志与报告中身份信息默认打码（wjx_config.json 里"报告显示明文"可开）
  5. 遇到填不了的题 -> 停下 + 响铃 + 截图 + 中文报告，等用户处理，绝不猜

模式：
  （默认）--dry-run 侦察，绝不提交
  --submit-now   立刻填报并提交
  --at TIME       守时填报，到点提交
  --demo         填到提交按钮前一步就停（不点提交）
  --force-now    配合 --at 使用：目标时间已过也强行立即提交（需显式声明）
  --wait-open    问卷未开放时守候：保持浏览器热着，到点立刻刷新拿题
  --max-wait N   守候上限秒数（默认 7200）

题型支持：填空 / 矩阵填空 / 单选（配置声明「选项」且值能唯一定位）。
多选·下拉·量表·上传·日期 一律 ASK 停下交人工，绝不猜、绝不提交残缺答卷。

判定关键词统一放wjx_kit.SINGLE_SOURCE，本文件不再自带字面量。
"""
import argparse
import glob
import json
import os
import random
import re
import shutil
import sys
import time
import ctypes
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wjx_kit as K

BASE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(BASE, "logs")
PROFILE_ROOT = BASE                 # profile_* 清理根目录
FIELDMAP = os.path.join(BASE, "fieldmap.json")
URLFILE = os.path.join(BASE, "wjx_url.txt")
LOCK = os.path.join(LOGS, "run.lock")

# 遇到验证码时等待人工处理的秒数
CAPTCHA_WAIT = 300

# 临时 profile 保留时长（小时），超过则下次启动清理
PROFILE_MAX_AGE_H = 24

os.makedirs(LOGS, exist_ok=True)

_log_fh = None


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S.%f")[:-3], msg)
    print(line, flush=True)
    if _log_fh:
        _log_fh.write(line + "\n")
        _log_fh.flush()


def ring():
    """响铃提醒，需要人工介入。"""
    try:
        ctypes.windll.kernel32.Beep(1200, 600)
    except Exception:
        pass


# 页面被关掉时 playwright 抛的这些错误不算故障：多半是用户看到提交成
# 功后自己把窗口关了。当成正常收尾处理，不响铃、不报异常。
PAGE_CLOSED_WORDS = ("Target page, context or browser has been closed",
                     "Target closed", "has been closed",
                     "Browser has been closed", "Target page closed")


def is_page_closed(err):
    s = str(err)
    return any(w in s for w in PAGE_CLOSED_WORDS)


def keep_awake():
    """声明系统持续唤醒，屏幕可关闭。不改系统电源设置。"""
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        log("已声明防休眠（显示器可关闭，系统不休眠）")
    except Exception as e:
        log("防休眠设置失败（不影响提交）: %s" % e)


def release_awake():
    ES_CONTINUOUS = 0x80000000
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass


def now():
    return datetime.now()


def shot(page, tag):
    p = os.path.join(LOGS, "%s_%s.png" % (tag,
            now().strftime("%H%M%S")))
    try:
        page.screenshot(path=p, full_page=True)
        log("截图 -> %s" % os.path.basename(p))
        return p
    except Exception:
        return None


def check_domain(url):
    """域名白名单。逻辑已收敛到 wjx_kit.check_domain（点号边界严谨）。"""
    try:
        norm = K.check_domain(url, allow_test=True)
    except K.DomainError as e:
        log("拒绝执行：%s" % e)
        raise SystemExit(3)
    from urllib.parse import urlparse as _up
    log("域名校验通过: %s" % (_up(norm).hostname or "本地测试页"))
    return norm


# ---------------- 锁与临时目录 ----------------

def pid_alive(pid):
    """检查进程是否还活着（Windows 用 OpenProcess）。"""
    if not pid or pid <= 0:
        return False
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(
                h, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return False


def acquire_lock():
    """获取运行锁。若上次残留且进程已死，自动接管并说明。"""
    if os.path.exists(LOCK):
        old_pid = None
        try:
            with open(LOCK, "r", encoding="utf-8") as f:
                m = re.search(r"pid=(\d+)", f.read())
                if m:
                    old_pid = int(m.group(1))
        except Exception:
            pass
        if old_pid and pid_alive(old_pid):
            log("已有实例在运行 (pid=%d)，本次直接退出。" % old_pid)
            log("如确认它已经死了，删掉这个文件即可：%s" % LOCK)
            return False
        log("发现上次残留的锁 (pid=%s，进程已不存在)，自动接管。" % old_pid)
        try:
            os.remove(LOCK)
        except Exception:
            pass
    with open(LOCK, "w", encoding="utf-8") as f:
        f.write("pid=%s time=%s" % (os.getpid(),
                now().strftime("%Y-%m-%d %H:%M:%S")))
    return True


def release_lock():
    if os.path.exists(LOCK):
        try:
            os.remove(LOCK)
        except Exception:
            pass


def cleanup_profiles(max_age_h=PROFILE_MAX_AGE_H):
    """清理过期的 profile_* 临时目录（每次约 20-25MB，不清会持续堆积）。"""
    cutoff = time.time() - max_age_h * 3600
    removed = []
    for d in glob.glob(os.path.join(PROFILE_ROOT, "profile_*")):
        if not os.path.isdir(d):
            continue
        try:
            if os.path.getmtime(d) < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed.append(os.path.basename(d))
        except Exception:
            pass
    if removed:
        log("已清理过期临时目录: %s" % ", ".join(removed))
    return removed


# ---------------- 页面解析 ----------------

SCAN_JS = r"""
() => {
  const out = {title: document.title, url: location.href, fields: []};
  const push = (o) => { if (o.id) out.fields.push(o); };

  // 控件类型归一化。日期陷阱：问卷星的日期框是 input[type=text][readonly]，
  // JS 赋值不会报错、回读也有值，但提交出去是空串（2026-10 真实踩过）。
  // 所以扫描阶段就标成 date-readonly，直接交给人工。
  const typeOf = (c) => {
    const t = (c.type || '').toLowerCase();
    const cls = (c.className || '') + '';
    if (t === 'text' && (c.readOnly || /date|calendar/i.test(cls))) {
      return 'date-readonly';
    }
    return t;
  };

  // ---- 通用：把页面上每个可填文本控件抓出来 ----
  // 定位策略（按优先级，行标签来源不同）：
  //   A. 矩阵填空 tr#drvN_M  -> 同行第一个 td 是行标签
  //   B. 普通题 div[id^=div] -> 题干 .field-title + 题内第N个控件
  //   C. 其余裸控件 -> 用最近的 label/祖先文本兜底
  const matrixRows = document.querySelectorAll('tr[id^="drv"]');
  const matTable = new Set();
  matrixRows.forEach((tr) => {
    const tds = tr.querySelectorAll('td');
    if (!tds.length) return;
    const rowLabel = (tds[0].innerText || '').trim().replace(/\*$/, '').trim();
    const ctl = tr.querySelector('input[type="text"], textarea, input[type="number"]');
    if (!ctl) return;
    // 所在表格的题干
    const box = tr.closest('fieldset[id^="fieldset"], div[id^="div"]');
    const th = box ? box.querySelector('.field-title, .fieldui-title, .topic, legend') : null;
    matTable.add(box);
    push({
      kind: 'matrix',
      qtitle: (th ? (th.innerText || '').trim() : '').slice(0, 60),
      row: rowLabel.slice(0, 40),
      required: !!(box && /\*|item-reqi|required/.test(box.innerHTML.slice(0, 400))),
      tag: ctl.tagName.toLowerCase(),
      type: typeOf(ctl),
      id: ctl.id || '',
      name: ctl.name || ''
    });
  });

  // B. 普通题（排除已在矩阵里的）
  document.querySelectorAll('div[id^="div"], fieldset[id^="fieldset"]').forEach((box) => {
    if (matTable.has(box)) return;
    const head = box.querySelector('.field-title, .fieldui-title, .topic, legend');
    if (!head) return;
    const qtitle = (head.innerText || '').trim().slice(0, 60);
    const ctrls = box.querySelectorAll('input[type="text"], input[type="number"], textarea');
    ctrls.forEach((c, i) => {
      push({
        kind: 'normal',
        qtitle: qtitle,
        row: ctrls.length > 1 ? ('第' + (i + 1) + '个空') : '',
        required: /\*|item-reqi|required/.test(box.innerHTML.slice(0, 400)),
        tag: c.tagName.toLowerCase(),
        type: typeOf(c),
        id: c.id || '',
        name: c.name || ''
      });
    });
  });

  // C. 兜底：其余未被归类的裸控件
  const known = {};
  out.fields.forEach(f => known[f.id] = 1);
  document.querySelectorAll('input[type="text"], input[type="number"], textarea').forEach((c) => {
    if (known[c.id]) return;
    if (!c.id || c.id === 'matrixinput' || c.id === 'clipVoteCode') return;
    push({
      kind: 'loose',
      qtitle: (c.placeholder || '').trim().slice(0, 40),
      row: '',
      required: false,
      tag: c.tagName.toLowerCase(),
      type: typeOf(c),
      id: c.id,
      name: c.name || ''
    });
  });

  // D. 选择类：同名 input[type=radio|checkbox] 聚成一个"字段"，select 单独算。
  //    实测结构（WJX）：
  //    <div class="ui-radio"><span class="jqRadio"></span>
  //      <input type="radio" id="q3_1" name="q3" value="1">
  //      <label for="q3_1">男</label></div>
  //    单选支持自动点；多选/下拉只做识别并交人工（绝不静默漏掉，
  //    漏掉会导致必答项为空、提交被拒）。
  const radioGroups = {};
  document.querySelectorAll('input[type="radio"], input[type="checkbox"]')
    .forEach((r) => {
    if (!r.name) return;
    if (r.closest('tr[id^="drv"]')) return;
    const key = r.type + '::' + r.name;
    const g = radioGroups[key] || (radioGroups[key] = {
      name: r.name,
      type: (r.type || '').toLowerCase() === 'checkbox' ? 'checkbox' : 'radio',
      box: r.closest('div[id^="div"], fieldset[id^="fieldset"]'),
      options: []
    });
    let lbl = r.id ? document.querySelector('label[for="' + r.id + '"]') : null;
    if (!lbl) lbl = r.closest('label');
    if (!lbl && r.parentElement) {
      lbl = r.parentElement.querySelector('label, a.jqRadio, span.jqRadio');
    }
    let txt = lbl ? (lbl.innerText || '').trim() : '';
    if (!txt && r.parentElement) txt = (r.parentElement.innerText || '').trim();
    txt = txt.replace(/\s+/g, ' ').trim().slice(0, 30);
    g.options.push({id: r.id || '', text: txt});
  });
  Object.keys(radioGroups).forEach((nm) => {
    const g = radioGroups[nm];
    if (!g.options.length) return;
    const head = g.box
      ? g.box.querySelector('.field-title, .fieldui-title, .topic, legend') : null;
    push({
      kind: g.type,
      qtitle: (head ? (head.innerText || '').trim() : '').slice(0, 60),
      row: '',
      required: !!(g.box &&
        /\*|item-reqi|required/.test(g.box.innerHTML.slice(0, 400))),
      tag: 'input',
      type: g.type,
      id: 'grp_' + nm,
      name: g.name,
      options: g.options
    });
  });

  // E. 下拉框：只识别，交人工
  document.querySelectorAll('select').forEach((s, i) => {
    const box = s.closest('div[id^="div"], fieldset[id^="fieldset"]');
    const head = box
      ? box.querySelector('.field-title, .fieldui-title, .topic, legend') : null;
    const opts = Array.from(s.options || []).map(o => (o.text || '').trim())
      .filter(t => t && !/请选择/.test(t)).slice(0, 12);
    push({
      kind: 'select',
      qtitle: (head ? (head.innerText || '').trim() : '第?个下拉').slice(0, 60),
      row: '',
      required: !!(box && /\*|item-reqi|required/.test(box.innerHTML.slice(0, 400))),
      tag: 'select',
      type: 'select-one',
      id: s.id || ('select_' + i),
      name: s.name || '',
      options: opts.map(t => ({id: '', text: t}))
    });
  });

  // 提交按钮：实测是 div#ctlNext.submitbtn.mainBgColor
  const cand = Array.from(document.querySelectorAll(
    '#ctlNext, .submitbtn, a.jqbm, span.jqbm, .jqbm, #jsubmit, ' +
    '.submit_button, input[type="submit"], button[type="submit"]')).filter(e => {
      const t = (e.innerText || e.value || '').trim();
      return /提交|交卷/.test(t) && e.offsetParent !== null && !e.disabled;
    });
  out.submitFound = cand.length;
  out.submit = cand.length ? cand[0].outerHTML.slice(0, 200) : '';
  out.bodyText = document.body.innerText.slice(0, 1500);
  return out;
}
"""


def scan(page):
    try:
        return page.evaluate(SCAN_JS)
    except Exception as e:
        log("扫描异常（可能页面跳转中）: %s" % e)
        return {"title": "", "url": "", "fields": [], "bodyText": ""}


STOP_WORDS = ("尚未开始", "未到", "停止答题", "已满", "已结束",
              "报名已结束", "填写时间", "问卷已过期")


# 题型 -> 中文名（只用于报告措辞，不参与任何判断）
TYPE_NAMES = {
    "radio": "单选",
    "checkbox": "多选",
    "select-one": "下拉",
    "select-multiple": "下拉(多选)",
    "file": "上传文件",
    "date-readonly": "日期（填了也可能提交为空）",
    "number": "数字",
}


def page_blocked(info):
    t = info.get("bodyText", "") or ""
    for w in STOP_WORDS:
        if w in t:
            return w
    return None


# 问卷星"未开放"页面的固定文案：
# 很抱歉，此问卷将于2026-10-22 12:00（北京时间）开放，请到时再进入此页面进行填写！
OPEN_TIME_RE = re.compile(
    r"此问卷将于\s*(\d{4}-\d{1,2}-\d{1,2})\s*(\d{1,2}:\d{2})")


def parse_open_time(text):
    """从页面文案里抠出开放时间；抠不到返回 None。只读，不猜。"""
    m = OPEN_TIME_RE.search(text or "")
    if not m:
        return None
    raw = "%s %s" % (m.group(1), m.group(2))
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except Exception:
            pass
    return None


def wait_for_open(page, url, max_wait):
    """问卷未开放时守候：保持浏览器热着，到点立刻扫描。

    为什么需要它：问卷星的开放闸门在**服务端**，未到点 HTML 里
    根本没有 divQuestion，提前读不到题目结构（实测 5 个入口全部拦下）。
    所以只能提前把浏览器和页面预热好，到点反复刷新抢在第一时间拿到
    题目，省掉冷启动的 3~5 秒。

    返回 (opened: bool, reason: str)。任何情况下都不提交。
    """
    t0 = now()
    deadline = t0 + timedelta(seconds=max_wait)
    open_at = parse_open_time(scan(page).get("bodyText", ""))
    if open_at:
        log("页面提示开放时间: %s（还有 %.0f 秒）"
            % (open_at.strftime("%Y-%m-%d %H:%M"),
               (open_at - t0).total_seconds()))
    else:
        log("页面未给出开放时间，按 %.0f 秒窗口轮询" % max_wait)

    tries = 0
    last_log = t0
    while now() < deadline:
        tries += 1
        # 离开放还早 -> 低频刷新，别把页面刷成风控对象
        if open_at and now() < open_at - timedelta(seconds=6):
            page.wait_for_timeout(2000)
            if (now() - last_log).total_seconds() >= 30:
                log("守候中… 距开放 %d 秒"
                    % max(0, int((open_at - now()).total_seconds())))
                last_log = now()
            continue
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            log("刷新异常（重试）: %s" % e)
            page.wait_for_timeout(400)
            continue
        info = scan(page)
        if info.get("fields"):
            log("问卷已开放！第 %d 次刷新拿到 %d 个控件"
                % (tries, len(info["fields"])))
            return True, "opened"
        blk = page_blocked(info)
        if blk and blk != "未到" and blk != "尚未开始":
            logs = ("已结束", "报名已结束", "问卷已过期", "已满", "停止答题")
            if blk in logs:
                return False, "页面显示「%s」，不再等待" % blk
        page.wait_for_timeout(300)
    return False, "等待 %.0f 秒仍未开放" % max_wait


def match_fields(fields, config):
    """逐个控件匹配白名单字段。返回 (mapping, items, missing, unused)。

    匹配文本 = 题干 + 行标签（矩阵题靠行标签区分）。
    字段值/别名全部来自 wjx_config.json，本函数不再有硬编码。

    返回的 items 是 [K.Item]，直接喂给 wjx_kit.decide_gate 做闸门。
    """
    mapping, items = [], []
    used = set()

    def add_ask(title, reason, next_step="", qtype="", required=False):
        items.append(K.Item(K.ASK, title, reason=reason,
                            next_step=next_step, qtype=qtype,
                            required=required))

    for f in fields:
        if not f.get("id"):
            continue
        text = "%s %s" % (f.get("qtitle", ""), f.get("row", ""))
        label = (f.get("row") or f.get("qtitle") or "第?题")[:40]

        # ---- 单选：唯一支持自动点的非文本题型 ----
        # 只在两件事同时成立时才点：① 配置里明确声明了「选项」；
        # ② 配置的「值」能在页面选项里唯一定位。
        # 绝不"选第一个"、绝不猜。其余非文本题型一律 ASK 停下等人。
        if f.get("type") == "radio":
            opts = [o for o in (f.get("options") or []) if o.get("id")]
            page_texts = [ (o.get("text") or "").strip() for o in opts ]
            hit, cands = config.field_by_alias(text)
            if hit is None and len(cands) > 1:
                add_ask(label, "题干同时包含多个字段：%s" % "、".join(cands),
                        next_step="请在 wjx_config.json 里为该题单独拆开，"
                                  "或手动在浏览器里选",
                        qtype="radio", required=f.get("required"))
                continue
            if hit is None:
                add_ask(label, "单选题，题干没匹配到配置里的任何字段",
                        next_step="页面选项：%s｜若是你的信息，把字段加进 "
                                  "wjx_config.json，否则手动选" % " / ".join(page_texts),
                        qtype="radio", required=f.get("required"))
                continue
            if hit in used:
                add_ask(label, "字段「%s」在页面上出现了多次" % hit,
                        next_step="请在浏览器里确认哪一处才对，然后手动选",
                        qtype="radio", required=f.get("required"))
                continue
            val = config.field_value(hit)
            if val is None:
                add_ask(label, "配置里「%s」还没填值" % hit,
                        next_step='打开 wjx_config.json，找到 "%s"，把 "值" 填上'
                                  % hit,
                        qtype="radio", required=f.get("required"))
                continue
            spec = config.fields.get(hit) or {}
            if not spec.get("选项"):
                # 配置没声明这是单选题 -> 不敢点，但不猜
                add_ask(label, "单选题，但配置里「%s」没声明「选项」" % hit,
                        next_step="页面选项：%s｜确认后在该字段加 "
                                  "\"选项\": [...]，或手动选"
                                  % " / ".join(page_texts),
                        qtype="radio", required=f.get("required"))
                continue
            exact = [o for o in opts if (o.get("text") or "").strip() == val]
            near = exact or [o for o in opts
                             if val and val in (o.get("text") or "")]
            if len(near) != 1:
                add_ask(label,
                        "单选「%s」：页面选项里%s"
                        % (hit, "找不到匹配" if not near else "匹配到多个"),
                        next_step="页面选项：%s｜把「值」改成其中一个，"
                                  "或手动选" % " / ".join(page_texts),
                        qtype="radio", required=f.get("required"))
                continue
            used.add(hit)
            mapping.append({"field": hit, "target_id": near[0]["id"],
                            "target_tag": "input", "kind": "radio",
                            "label": label,
                            "option": (near[0].get("text") or "").strip()})
            continue

        # 非文本控件（多选/下拉/上传/量表/日期...）——如实报告，绝不静默丢弃
        if f.get("type") not in ("text", "number", "textarea", ""):
            tname = TYPE_NAMES.get(f.get("type"), f.get("type") or "未知")
            hint = ""
            if f.get("options"):
                hint = "；页面选项：%s" % " / ".join(
                    (o.get("text") or "") for o in f["options"][:12])
            add_ask(label,
                    "题型是「%s」，工具不自动处理%s" % (tname, hint),
                    next_step="请在浏览器里手动选/填一下，补完自己点提交",
                    qtype=f.get("type"), required=f.get("required"))
            continue

        # 题干同时含多个字段名 -> 歧义，交人工（不猜填哪个）
        hit, cands = config.field_by_alias(text)
        if hit is None and len(cands) > 1:
            add_ask(label,
                    "题干同时包含多个字段：%s" % "、".join(cands),
                    next_step="请在 wjx_config.json 里为该题单独拆开，"
                              "或手动在浏览器里填",
                    qtype=f.get("kind"), required=f.get("required"))
            continue
        if hit is None:
            add_ask(label, "不在配置白名单里，脚本不会乱填",
                    next_step="如果是你的信息，加进 wjx_config.json；"
                              "否则请手动填写",
                    qtype=f.get("kind"), required=f.get("required"))
            continue

        if hit in used:
            # 同名字段出现多次 —— 两处都报出来让人判断，不静默丢弃
            add_ask(label,
                    "字段「%s」在页面上出现了多次" % hit,
                    next_step="请在浏览器里确认哪一处才对，然后手动填那处",
                    qtype=f.get("kind"), required=f.get("required"))
            continue

        # 字段配了但没值 -> 明确告诉用户去填配置
        if config.field_value(hit) is None:
            add_ask(label, "配置里「%s」还没填值" % hit,
                    next_step='打开 wjx_config.json，找到 "%s"，把 "值" 填上'
                              % hit,
                    qtype=f.get("kind"), required=f.get("required"))
            continue

        used.add(hit)
        mapping.append({"field": hit, "target_id": f["id"],
                        "target_tag": f["tag"], "kind": f.get("kind"),
                        "label": label})

    # 配置里有值但页面没问 -> unused（中性提示，不阻塞）
    unused = [f for f, v in config.configured_fields().items()
              if f not in used]
    # 页面有该字段的题但没配值 -> missing（真阻塞，已在上面 add_ask）
    missing = []
    for it in items:
        if "还没填值" in (it.reason or ""):
            for f in config.all_field_names():
                if config.field_value(f) is None and \
                        ("「%s」" % f) in it.reason and f not in missing:
                    missing.append(f)

    for m in mapping:
        if m.get("kind") == "radio":
            items.append(K.Item(
                K.AUTO, m["label"],
                detail="已选中「%s」" % (m.get("option") or ""),
                qtype="radio"))
            continue
        val = config.field_value(m["field"])
        items.append(K.Item(
            K.AUTO, m["label"],
            detail="已填入 %s" % K.mask_value(val, config.show_plaintext),
            qtype=m["kind"]))

    # 守恒定律：mapping + asks + skips 必须等于扫描到的题目数
    if len(items) != len([f for f in fields if f.get("id")]):
        items.append(K.Item(
            K.ASK, "（内部校验）",
            reason="报告条目数 %d 与扫描到 %d 个控件不一致"
                   % (len(items), len([f for f in fields if f.get("id")])),
            next_step="这属于工具 bug，请把 logs/ 下的截图发我"))

    return mapping, items, missing, unused


def dump_unknown(items):
    """把未处理项写成一个更好读的清单。"""
    p = os.path.join(LOGS, "unfilled_list.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("以下项目脚本不会自动填写，需要你处理：\n\n")
        for it in items:
            if it.status != K.ASK:
                continue
            f.write("■ %s\n" % it.title)
            if it.qtype:
                f.write("    题型：%s%s\n" % (it.qtype,
                                          "（必答）" if it.required else ""))
            if it.reason:
                f.write("    原因：%s\n" % it.reason)
            if it.next_step:
                f.write("    下一步：%s\n" % it.next_step)
            f.write("\n")
    return p


# ---------------- 填表 ----------------

FILL_JS = r"""
(payload) => {
  const done = [];
  for (const item of payload) {
    if (item.radio) {
      // 单选：先点 label[for]（最接近真人），不行再点 input，再不行点热区
      const el = document.getElementById(item.id) ||
                 document.querySelector('[name="' + item.id + '"]');
      if (!el) continue;
      const lbl = document.querySelector('label[for="' + item.id + '"]');
      if (lbl) { try { lbl.click(); } catch (e) {} }
      if (!el.checked) { try { el.click(); } catch (e) {} }
      if (!el.checked && el.parentElement) {
        const hot = el.parentElement.querySelector(
          'a.jqRadio, span.jqRadio, .ui-radio-label, .ui-radio');
        if (hot) { try { hot.click(); } catch (e) {} }
      }
      el.dispatchEvent(new Event('change', {bubbles: true}));
      if (el.checked) done.push(item.id);
      continue;
    }
    const el = document.getElementById(item.id) ||
               document.querySelector('[name="' + item.id + '"]');
    if (!el) continue;
    el.focus();
    el.value = item.value;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('blur',  {bubbles: true}));
    done.push(item.id);
  }
  return done;
}
"""


def verify_filled(page, mapping, config):
    """填表。字段值来自配置文件。

    安全：只填 mapping 里的控件。mapping 由 match_fields 产出，
    已被闸门过滤——白名单外的控件根本不会出现在这里。
    """
    payload = []
    for m in mapping:
        if m.get("kind") == "radio":
            payload.append({"id": m["target_id"], "radio": True,
                            "value": m.get("option") or ""})
            continue
        val = config.field_value(m["field"])
        if val is None:
            continue
        payload.append({"id": m["target_id"], "value": val})
    if not payload:
        return []
    return page.evaluate(FILL_JS, payload)


def dismiss_dialogs(page):
    """处理问卷星弹层：'是否继续上次回答' / '确认' / 隐私政策同意等。"""
    js = r"""
    () => {
      let clicked = [];
      const texts = ['确认', '确定', '继续', '同意', '我已阅读', '下一步'];
      // 1) 常见弹窗容器
      document.querySelectorAll(
        '.layui-layer-btn0, .layui-layer-btn1, .layer_btn .layui-layer-btn0, ' +
        '.popup-btn, .van-dialog__confirm, .next-btn, .confirm-btn').forEach((b) => {
          const t = (b.innerText || '').trim();
          if (texts.some(x => t === x || t.includes(x))) {
            b.click();
            clicked.push('btn:' + t);
          }
        });
      // 2) 文本含确认/继续的可见元素
      if (!clicked.length) {
        document.querySelectorAll('a, span, div, button, label').forEach((e) => {
          const t = (e.innerText || '').trim();
          if (t.length > 12) return;
          if (!texts.some(x => t === x)) return;
          if (e.offsetParent === null) return;
          e.click();
          clicked.push('txt:' + t);
        });
      }
      return clicked;
    }
    """
    for _ in range(6):
        try:
            hit = page.evaluate(js)
        except Exception:
            return []
        if not hit:
            return []
        log("已处理弹层: %s" % hit)
        page.wait_for_timeout(600)
    return []


def submit(page):
    """点击提交。

    实测（2026-10-10 该问卷）：按钮是 div#ctlNext.submitbtn.mainBgColor，
    **不是** a.jqbm。

    2026-10-26 红丝带晚会事故修正（顺序调换）：
    原先首选 `el.click()` 这种 JS 合成事件，产生的事件 isTrusted=false、
    没有鼠标轨迹、填完 28 毫秒就点 —— 网易易盾直接返回
    「需要安全校验，请重新提交！」，之后人工点也一直卡死。
    **现在首选 Playwright 真实鼠标（CDP 输入，isTrusted=true）+
    悬停停顿 + 随机按下延时；JS click 只作最后兜底。**
    """
    # ---- 首选：真实鼠标点击（最像人，易盾不拦） ----
    for sel in ("#ctlNext", ".submitbtn", "#jsubmit"):
        try:
            loc = page.locator(sel).first
            if loc.count() == 0 or not loc.is_visible():
                continue
            if not re.search(r"提交|交卷", (loc.inner_text() or "")):
                continue
            loc.scroll_into_view_if_needed(timeout=3000)
            box = loc.bounding_box()
            if box:
                # 先把鼠标挪到附近再落到按钮中心，制造移动轨迹
                page.mouse.move(box["x"] + box["width"] * 0.35,
                                box["y"] + box["height"] * 0.2)
                page.wait_for_timeout(random.randint(120, 260))
                page.mouse.move(box["x"] + box["width"] / 2,
                                box["y"] + box["height"] / 2,
                                steps=random.randint(6, 14))
                page.wait_for_timeout(random.randint(90, 220))
                page.mouse.click(box["x"] + box["width"] / 2,
                                 box["y"] + box["height"] / 2,
                                 delay=random.randint(45, 110))
                return "mouse:trusted(%s)" % sel
            loc.hover(timeout=3000)
            page.wait_for_timeout(random.randint(150, 300))
            loc.click(timeout=5000, delay=random.randint(45, 110))
            return "locator:trusted(%s)" % sel
        except Exception as e:
            log("真实鼠标点击 %s 失败: %s" % (sel, e))

    # ---- 兜底 1：JS click（isTrusted=false，仅在真实鼠标全失败时才用） ----
    js = r"""
    () => {
      const sel = ['#ctlNext', '.submitbtn', 'a.jqbm', 'span.jqbm', '#jsubmit',
                   '.submit_button', 'input[type="submit"]',
                   'button[type="submit"]'];
      for (const s of sel) {
        const el = document.querySelector(s);
        if (!el || el.offsetParent === null || el.disabled) continue;
        if (!/提交|交卷/.test((el.innerText || '').trim())) continue;
        el.click();
        return s;
      }
      return null;
    }
    """
    try:
        hit = page.evaluate(js)
        if hit:
            log("!! 真实鼠标失败，退化为 JS click(%s)" % hit)
            return "js:" + hit
    except Exception:
        pass

    # 兜底：按坐标点击（Playwright 的鼠标事件最接近真人）
    try:
        el = page.query_selector("#ctlNext, .submitbtn")
        if el:
            el.scroll_into_view_if_needed()
            box = el.bounding_box()
            if box:
                page.mouse.click(box["x"] + box["width"] / 2,
                                 box["y"] + box["height"] / 2)
                return "mouse:coord"
            el.click(force=True)
            return "force:click"
    except Exception as e:
        log("提交兜底失败: %s" % e)
    return None


def body_text(page):
    """安全取页面文本：跳转中document.body 可能为 null。"""
    try:
        return page.evaluate(
            "() => (document.body ? document.body.innerText : '')") or ""
    except Exception:
        return ""


def judge(page):
    """判断提交结果。返回 (状态, 详情)。

    全部关键词来自 wjx_kit 的单一来源常量，本函数不自带字面量。
    历史事故：12:52 提交成功却因词表漏「您的答卷已经提交」被误判 UNKNOWN。
    """
    # 先清掉可能挡住判定的弹层（layer 类）
    try:
        page.evaluate(r"""
        () => {
          if (!document.body) return;
          document.querySelectorAll('.layui-layer').forEach((l) => {
            const t = (l.innerText || '');
            if (/继续|确认|提示/.test(t)) return;      // 需人工分支的保留
            l.remove();
          });
        }""")
    except Exception:
        pass
    txt = body_text(page)
    if not txt.strip():
        return "UNKNOWN", "页面无内容（可能正在跳转）"

    if any(k in txt for k in K.SUCCESS_KEYWORDS):
        m = re.search(r"第\s*(\d+)\s*(?:位|名)", txt)
        return "SUCCESS", ("序号=" + m.group(1) if m
                           else "提交成功（页面未显示序号）")
    if any(k in txt for k in K.CAPTCHA_KEYWORDS):
        return "CAPTCHA", "需要人工安全验证"
    if any(k in txt for k in K.FULL_KEYWORDS):
        return "FULL", "名额已满"
    if any(k in txt for k in K.NOT_STARTED_KEYWORDS):
        return "NOT_STARTED", "问卷尚未开放"
    if any(k in txt for k in K.EXPIRED_KEYWORDS):
        return "EXPIRED", "报名已结束"
    return "UNKNOWN", (txt[:120].replace("\n", " ") or "空页面")


# ---------------- 主流程 ----------------

def read_url():
    if not os.path.exists(URLFILE):
        log("缺少 wjx_url.txt，无法继续")
        log("请把问卷链接写进 wjx_url.txt，或用 --url 参数直接传")
        sys.exit(4)
    with open(URLFILE, encoding="utf-8") as f:
        return f.read().strip()


def emit_report(result, config, url, mode, extra_notes=None, submit_status=None):
    """生成中文报告，打印 + 写文件。返回报告路径。

    submit_status 必须传真实的提交结果，否则「未确认提交」会被写成「已提交」。
    """
    notes = list(extra_notes or [])
    if config.deadline.get("开始"):
        notes.append("报名开始时间：%s%s" % (
            config.deadline.get("开始"),
            "（来源：%s）" % config.deadline.get("来源")
            if config.deadline.get("来源") else ""))
        if config.deadline.get("原文"):
            notes.append("时间原文：「%s」" % config.deadline["原文"])
    elif not config.deadline.get("开始"):
        notes.append("提示：wjx_config.json 里没填报名时间。"
                     "填了之后可以用「3_生成自动任务.bat」实现准点自动提交。")

    text = K.render_report(result, config, url, mode,
                           extra_notes=notes, submit_status=submit_status)
    print("\n" + text)
    p1, p2 = K.write_report(text, LOGS)
    log("报告已保存: %s" % p2)
    return p2


def main():
    global _log_fh
    ap = argparse.ArgumentParser(
        description="问卷星自动填表工具（默认 dry-run，绝不提交）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只检查不提交（默认模式）")
    ap.add_argument("--submit-now", action="store_true",
                    help="立刻填表并提交")
    ap.add_argument("--at", help="守时提交，格式 'YYYY-MM-DD HH:MM:SS' 或 'HH:MM:SS'")
    ap.add_argument("--demo", action="store_true",
                    help="填到提交按钮前就停，不提交")
    ap.add_argument("--force-now", action="store_true",
                    help="配合 --at：目标时间已过也强行立即提交")
    ap.add_argument("--wait-open", action="store_true",
                    help="问卷尚未开放时守候：保持浏览器热着，到点立刻"
                         "刷新拿题（本身不提交，是否提交仍由其他参数决定）")
    ap.add_argument("--max-wait", type=int, default=7200,
                    help="--wait-open 的最长守候秒数，默认 7200")
    ap.add_argument("--url", help="直接指定问卷链接")
    args = ap.parse_args()

    # ---- 关键安全默认值：不带任何参数 = 检查，不提交 ----
    want_submit = bool(args.submit_now or args.at)
    if args.demo:
        want_submit = False
    mode = ("--submit-now" if args.submit_now else
            ("--at %s" % args.at if args.at else
             ("--demo" if args.demo else "--dry-run")))
    # --dry-run 显式给了但又给了提交参数 -> 以提交为准（兼容旧 bat）
    if args.dry_run and not (args.submit_now or args.at or args.demo):
        want_submit = False

    _log_fh = open(os.path.join(
        LOGS, "run_%s.log" % now().strftime("%Y%m%d_%H%M%S")),
        "w", encoding="utf-8")

    log("模式: %s （将%s提交）" % (mode, "" if want_submit else "不"))
    if not want_submit:
        log("提示：不带 --submit-now / --at 就是检查模式，不会提交。")

    if not acquire_lock():
        ring()
        sys.exit(0)

    cleanup_profiles()
    config = K.load_config()
    run_profile = None
    try:
        keep_awake()
        url = args.url or read_url()
        url = check_domain(url)
        log("目标: %s" % url)

        # ---- 守时时间解析（拒绝过去时刻）----
        target = None
        if args.at:
            try:
                target = K._parse_dt(args.at)
                if target is None:
                    log("无法解析 --at '%s'，格式应为 "
                        "'YYYY-MM-DD HH:MM:SS' 或 'HH:MM:SS'" % args.at)
                    sys.exit(6)
                if target < now() and not args.force_now:
                    raise K.PastTimeError(target, now())
            except K.PastTimeError as ex:
                log(str(ex))
                sys.exit(6)

        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            # 每次用独立的新 profile，避免问卷星把上次未完成的填写
            # 当成草稿并弹「是否继续上次回答」——那会重置表单内容。
            run_profile = os.path.join(
                BASE, "profile_%s" % now().strftime("%Y%m%d_%H%M%S"))
            os.makedirs(run_profile, exist_ok=True)
            ctx = pw.chromium.launch_persistent_context(
                run_profile,
                channel="msedge",
                headless=False,
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',"
                "{get:()=>undefined});")
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            def _auto_dialog(d):
                log("原生弹窗自动接受: %s" % (d.message or d.type))
                try:
                    d.accept()
                except Exception:
                    pass
            page.on("dialog", _auto_dialog)

            log("打开页面...")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            log("页面标题: %s" % page.title())

            # ---- 未开放守候（--wait-open）：问卷星的服务端闸门未到时
            # 页面里没有任何题目结构，只能预热浏览器到点再扫 ----
            if args.wait_open:
                if scan(page).get("fields"):
                    log("问卷已开放，跳过守候")
                else:
                    opened, why = wait_for_open(page, url, args.max_wait)
                    if not opened:
                        log("结束守候：%s。未提交。" % why)
                        shot(page, "not_open")
                        r0 = K.decide_gate([], scan_complete=False,
                                           n_controls=0)
                        emit_report(r0, config, url, mode,
                                    extra_notes=["守候结束：%s" % why])
                        page.wait_for_timeout(8000)
                        return 0

            def rescan_and_gate(phase):
                """重新扫描 -> 过闸门。返回 (mapping, result, fields)。

                扫描到 0 个控件时（如问卷尚未开放，页面只有倒计时），
                闸门会自动拦下 —— 否则会提交一份空表单白占名额。
                """
                info_ = scan(page)
                fields_ = info_.get("fields", [])
                log("[%s] 识别到 %d 个控件" % (phase, len(fields_)))
                mapping_, items_, missing_, unused_ = match_fields(
                    fields_, config)
                result_ = K.decide_gate(items_, scan_complete=True,
                                        n_controls=len(fields_))
                result_.missing_fields = missing_
                result_.unused_fields = unused_
                return mapping_, result_, fields_

            mapping, result, fields = rescan_and_gate("初次扫描")

            log("可自动填写 %d 项，需人工 %d 项"
                % (len(result.filled), len(result.asks)))
            for it in result.filled:
                log("  [可填] %s -> %s" % (it.title, it.detail))
            for it in result.asks:
                log("  [待人工] %s：%s" % (it.title, it.reason))

            with open(FIELDMAP, "w", encoding="utf-8") as f:
                json.dump({"url": url, "fields": fields, "mapping": mapping,
                           "items": [{"status": i.status, "title": i.title,
                                      "reason": i.reason} for i in result.items],
                           "scan_complete": result.scan_complete},
                          f, ensure_ascii=False, indent=2)
            log("题目结构已存 -> fieldmap.json")

            # ========== 闸门 1：检查模式与闸门拦截 ==========
            if not want_submit or not result.ok:
                p = dump_unknown(result.items)
                shot(page, "check_result")
                ring() if result.asks else None
                emit_report(result, config, url, mode,
                            extra_notes=["未自动填写清单：%s" % p])
                log("未提交。原因：%s" % (result.reason_summary()
                                       or "当前为检查模式"))
                if want_submit and result.asks:
                    # 提交模式下被闸门拦下 -> 把窗口留给人工补填，
                    # 用户补完可以自己点提交（脚本不代劳，避免残缺答卷）
                    log("有 %d 道题需要人工处理，浏览器保留 %d 秒。"
                        % (len(result.asks), CAPTCHA_WAIT))
                    log("请在这 %d 秒内把题目补完并自己点「提交」；"
                        "补不完就让它关掉，名额不会白占。" % CAPTCHA_WAIT)
                    page.wait_for_timeout(CAPTCHA_WAIT * 1000)
                else:
                    log("浏览器保留 15 秒后关闭。")
                    page.wait_for_timeout(15000)
                return 0

            # ========== 到这里闸门已放行，可以填表 ==========
            # 关键顺序：先清弹层，再填表，最后提交。
            # 问卷星若检测到未完成的草稿会弹「是否继续上次回答」，
            # 点确认会重置已填内容，所以必须先清弹层再填。
            for attempt in range(4):
                hit = dismiss_dialogs(page)
                if not hit:
                    break
                log("填表前处理弹层: %s（第%d轮）" % (hit, attempt + 1))
                page.wait_for_timeout(800)

            done = verify_filled(page, mapping, config)
            log("已填 %d 个字段" % len(done))

            still_ok = page.evaluate(
                """(ids) => ids.every(i => {
                     const e = document.getElementById(i);
                     if (!e) return false;
                     if (e.type === 'radio' || e.type === 'checkbox') {
                       return !!e.checked;
                     }
                     return !!(e.value && e.value.length > 0); })""",
                [m["target_id"] for m in mapping])
            if not still_ok:
                log("!! 填表后回读校验失败，重新填一次")
                verify_filled(page, mapping, config)
                page.wait_for_timeout(300)

            # ---- 拟人停顿：填完立刻点提交会被易盾判为机器 ----
            # 2026-10-26 事故：填完到点击只隔 28 毫秒 -> 弹「需要安全校验」。
            # 真人从最后一个输入框移到提交按钮，至少几百毫秒到一两秒。
            _think = random.randint(1200, 2400)
            log("填表完成，停留 %.1f 秒后提交（避易盾判机器）" % (_think / 1000.0))
            page.wait_for_timeout(_think)

            if args.demo:
                log("--demo：停在提交按钮前，不提交")
                shot(page, "demo")
                emit_report(result, config, url, mode)
                page.wait_for_timeout(3000)
                return 0

            # ---- 守时 ----
            if target:
                log("守时目标 %s" % target.strftime("%H:%M:%S.%f")[:-3])
                while now() < target:
                    remain = (target - now()).total_seconds()
                    page.wait_for_timeout(min(int(remain * 1000), 5000))
                log("到达目标时间")

            def do_submit():
                """闸门 2：提交前再确认一次映射非空。"""
                if not mapping:
                    log("映射为空，拒绝提交")
                    return False
                r = submit(page)
                if r:
                    log("提交动作已触发（%s）" % r)
                    return True
                return False

            if not do_submit():
                # 页面可能尚未开放 -> 重载重试
                reloads = 0
                while reloads < 60:
                    status, detail = judge(page)
                    log("探测: %s (%s)" % (status, detail))
                    if reloads >= 60:
                        break
                    reloads += 1
                    page.reload(wait_until="domcontentloaded")
                    page.wait_for_timeout(200)
                    # 重载后重扫 + 重新过闸门
                    mapping2, result2, _ = rescan_and_gate("重载后重扫")
                    if not result2.ok:
                        log("!! 重扫后发现不会填的题，中止提交")
                        emit_report(result2, config, url, mode,
                                    extra_notes=["重载后结构变化，已中止"])
                        return 7
                    mapping = mapping2
                    for _a in range(4):
                        h = dismiss_dialogs(page)
                        if not h:
                            break
                        page.wait_for_timeout(600)
                    verify_filled(page, mapping, config)
                    if do_submit():
                        break

            # ---- 结果判定 ----
            # 页面可能在出结果前被用户关掉（看到提交成功就关窗很常见）。
            # 那不是错误：按「已触发提交但未确认」收尾，不响铃、不抛异常。
            status, detail = "UNKNOWN", ""
            try:
                status, detail = "UNKNOWN", ""
                for attempt in range(6):
                    # 第一次只等 0.8 秒：用户常在看到「提交成功」后立刻关窗，
                    # 早点判定一次，才来得及留下证据
                    page.wait_for_timeout(800 if attempt == 0 else 2500)

                    status, detail = judge(page)
                    log("第%d次判定: %s | %s" % (attempt + 1, status, detail))
                    if status in ("SUCCESS", "FULL", "EXPIRED"):
                        break
                    hit = dismiss_dialogs(page)
                    if not hit:
                        if attempt >= 2:
                            break
                        continue
                    log("提交后弹层: %s -> 重新填表并再次提交" % hit)
                    page.wait_for_timeout(600)
                    # 闸门 3：弹层重置出新题目后必须重扫 + 重新过闸门
                    mapping3, result3, _ = rescan_and_gate("弹层后重扫")
                    if not result3.ok:
                        log("!! 弹层后出现不会填的题，中止提交")
                        emit_report(result3, config, url, mode)
                        return 7
                    mapping = mapping3
                    verify_filled(page, mapping, config)
                    if not do_submit():
                        break

                if status == "SUCCESS":
                    shot(page, "SUCCESS")

                if status == "CAPTCHA":
                    log("=" * 50)
                    log("需要人工完成安全验证（网易易盾智能验证）！")
                    log("请看屏幕：Edge 窗口中央有「点击开始智能验证」，点一下即可。")
                    log("脚本会一直等，验证通过后自动重新填表并提交。")
                    log("等待上限 %d 秒…" % CAPTCHA_WAIT)
                    log("=" * 50)
                    ring(); ring(); ring()
                    shot(page, "need_your_captcha")

                    ok = False
                    for waited in range(1, CAPTCHA_WAIT + 1):
                        page.wait_for_timeout(1000)
                        passed = page.evaluate(r"""
                        () => {
                          const t = (document.body ? document.body.innerText : '') || '';
                          const kw = %s;
                          if (kw.some(k => t.includes(k))) return 'success';
                          if (!document.body) return 'gone';
                          const cap = document.querySelector(
                            '#captcha, .yidun, .nc-container, iframe[src*="captcha"], ' +
                            '.yidun_popup, [id*="captcha"]');
                          if (!cap) return 'gone';
                          return 'waiting';
                        }""" % json.dumps(list(K.SUCCESS_KEYWORDS), ensure_ascii=False))
                        if passed in ("success", "gone"):
                            log("验证已通过（%s），等待 %d 秒" % (passed, waited))
                            ok = True
                            break
                        if waited % 15 == 0:
                            log("仍在等待验证... %d/%d 秒" % (waited, CAPTCHA_WAIT))
                            shot(page, "waiting_captcha_%ds" % waited)

                    if not ok:
                        log("等待超时，未检测到验证完成。窗口保留，请手动点提交。")
                        status, detail = "CAPTCHA_TIMEOUT", "等待人工验证超时"
                    else:
                        page.wait_for_timeout(1500)
                        # 闸门 4：验证码后重提也要重新过闸门
                        mapping4, result4, _ = rescan_and_gate("验证后重扫")
                        if not result4.ok:
                            log("!! 验证后出现不会填的题，中止提交")
                            emit_report(result4, config, url, mode)
                            return 7
                        mapping = mapping4
                        verify_filled(page, mapping, config)
                        page.wait_for_timeout(300)
                        do_submit()
                        page.wait_for_timeout(6000)
                        status, detail = judge(page)
                        log("验证后提交结果: %s | %s" % (status, detail))
                        if status == "SUCCESS":
                            shot(page, "SUCCESS")
                        else:
                            shot(page, "after_captcha")

                if status in ("CAPTCHA_TIMEOUT", "CAPTCHA", "UNKNOWN"):
                    log("未自动完成提交。Edge 窗口保留 90 秒，你可手动点「提交」。")
                    for _ in range(90):
                        page.wait_for_timeout(1000)
                        s, _d = judge(page)
                        if s in ("SUCCESS", "FULL"):
                            status, detail = s, "检测到手动提交成功"
                            log("检测到你已手动提交成功！")
                            shot(page, "SUCCESS")
                            break

            except Exception as _e:
                if is_page_closed(_e):
                    status = "PAGE_CLOSED"
                    detail = "提交动作已触发，但浏览器在出结果前被关闭"
                    log("!! %s" % detail)
                else:
                    raise
            final = K.decide_gate(result.items)
            final.missing_fields = result.missing_fields
            final.unused_fields = result.unused_fields
            log("提交时间戳: %s" % now().strftime("%H:%M:%S.%f")[:-3])
            log("最终结果: %s | %s" % (status, detail))
            emit_report(final, config, url, "--submit-now",
                        extra_notes=["提交结果：%s（%s）" % (status, detail)],
                        submit_status=status)
            # 只在「需要你出手」时响：成功、以及你自己关窗（PAGE_CLOSED）都不响
            if status not in ("SUCCESS", "PAGE_CLOSED"):
                ring()
            log("日志: %s" % _log_fh.name)
            try:
                page.wait_for_timeout(1500)
            except Exception as _e2:
                if not is_page_closed(_e2):
                    raise
            try:
                ctx.close()
            except Exception:
                pass
            return 0 if status == "SUCCESS" else 8
    finally:
        release_awake()
        release_lock()
        # profile 每次约 20-25MB，跑完必须删，否则长期堆积
        if run_profile and os.path.isdir(run_profile):
            shutil.rmtree(run_profile, ignore_errors=True)
            log("已清理临时目录")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as _e:
        try:
            log("!! 未预期异常: %r" % _e)
            import traceback
            log(traceback.format_exc()[-800:])
            # 页面被关掉是用户行为，不是故障，别拿响铃吓人
            if not is_page_closed(_e):
                ring(); ring()
        except Exception:
            pass
        release_awake()
        release_lock()
        sys.exit(9)