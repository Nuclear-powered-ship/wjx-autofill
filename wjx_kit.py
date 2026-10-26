"""
问卷星自动填表工具 —— 纯函数层。

设计约束（重要）：
  本模块**不得import playwright**，否则单元测试会被拖进浏览器依赖。
  只放"纯逻辑"：配置加载、闸门决策、报告渲染、域名校验、时间解析。
  碰 page对象的一律留在 wjx_signup.py。

核心不变式：
  1) 守恒定律：扫描发现的题目数 == 报告里列出的题目数。少一条就是 bug。
  2) 闸门只看ASK 状态，**不看 required**（wjx 的必答标记不可靠）。
  3) 任何 ASK 存在=> 禁止提交。绝不猜、绝不提交残缺答卷。
"""
import json
import os
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "wjx_config.json")

# ---------------------------------------------------------------- 域名

ALLOWED_DOMAINS = ("wjx.cn", "wjx.com")


class DomainError(Exception):
    pass


def check_domain(url, allow_test=False):
    """校验 URL 是否属于问卷星域名。

    注意点号边界：host == d or host.endswith("." + d)
    不能用 host.endswith(d)——那会让 evilwjx.cn 通过。

    allow_test=True 时放行 file:// / localhost，供本地 fixture 测试。
    正常运行时一律拒绝非问卷星域名。
    """
    if not url or not str(url).strip():
        raise DomainError("链接为空")
    url = str(url).strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if not host:
        # 无 host：可能是 file:/// 或相对路径
        if allow_test and os.environ.get("WJX_TEST") == "1":
            if url.startswith("file://") or url.startswith("http://127.0.0.1") \
               or url.startswith("http://localhost"):
                return url
        raise DomainError("无法解析域名: %s" % url)

    # 测试态旁路：仅当显式开启 WJX_TEST 且 host 是本地回环
    if os.environ.get("WJX_TEST") == "1" and host in ("127.0.0.1", "localhost"):
        return url

    for d in ALLOWED_DOMAINS:
        if host == d or host.endswith("." + d):
            return url

    raise DomainError(
        "域名不在白名单内：%s\n仅允许 %s（防误操作到其他网站）"
        % (host, " / ".join(ALLOWED_DOMAINS)))


def check_domain_or_exit(url, allow_test=False):
    """给主流程用：不合规就打印+ 退出，返回规范化URL。"""
    try:
        return check_domain(url, allow_test=allow_test)
    except DomainError as e:
        print("[拒绝执行] %s" % e)
        raise SystemExit(3)


# ---------------------------------------------------------------- 配置

class Config(object):
    """wjx_config.json 的内存表示。"""

    def __init__(self, raw):
        self.raw = raw or {}
        self.fields = self.raw.get("字段", {}) or {}
        self.deadline = self.raw.get("报名时间", {}) or {}
        self.allow_skip_optional = bool(self.raw.get("允许跳过非必答题", False))
        self.show_plaintext = bool(self.raw.get("报告显示明文", False))

    # -- 字段相关--
    def field_value(self, name):
        """取字段值。支持三种条目：

        1) {"值": "..."}            -> 直接用（最高优先，忽略「组合」）
        2) {"组合": "{班级}"}        -> 用各基础字段的值渲染模板
        3) 未配 / 值为空且无组合     -> None

        组合模板渲染失败（引用的字段不存在或无值）时返回 None，
        由上层 match_fields 走「配置里还没填值」的 ASK 拦下 —— 绝不猜。
        """
        f = self.fields.get(name)
        if not f:
            return None
        v = f.get("值")
        if v is not None and str(v).strip() != "":
            return str(v).strip()
        tpl = f.get("组合")
        if tpl is not None and str(tpl).strip() != "":
            return self._render_compose(str(tpl))
        return None

    def _base_value(self, name):
        """只取「值」，不取「组合」。用于组合模板渲染，天然防递归
        （组合引用组合会造成无限递归，这里一刀切断）。"""
        f = self.fields.get(name)
        if not f:
            return None
        v = f.get("值")
        if v is None or str(v).strip() == "":
            return None
        return str(v).strip()

    def _render_compose(self, tpl):
        """把 "{班级}" / "{专业} {班级}" 渲染成字符串。

        占位符必须是【基础字段名】（不是别名），且该字段「值」非空；
        任一占位符解析失败 -> 返回 None（绝不产出半成品 / 半截值）。
        """
        def repl(m):
            fname = m.group(1).strip()
            val = self._base_value(fname)
            return ("\x00" + fname) if val is None else val

        out = re.sub(r"\{([^{}]+)\}", repl, tpl)
        if "\x00" in out:
            return None
        return out.strip()

    def configured_fields(self):
        """返回 {字段名: 值}，只含已配置的。"""
        out = {}
        for name in self.fields:
            v = self.field_value(name)
            if v is not None:
                out[name] = v
        return out

    def all_field_names(self):
        return list(self.fields.keys())

    def aliases(self):
        """所有别名 -> 字段名 的映射表。"""
        m = {}
        for name, spec in self.fields.items():
            for a in (spec.get("别名") or [name]):
                m[a] = name
        return m

    def field_by_alias(self, text):
        """从一段题干文本里找出对应的字段名。

        命中多个 => 返回 (None, [候选...]) 表示歧义，交人工判断。

        单字别名保护：像年级配"级"这种单字别名，会在"班级"里误命中
        （"班级"含"级"）。所以别名长度 < 2 的必须**整词相等**才算命中，
        长度 >= 2 的才允许子串匹配。

        最长别名优先消歧：若命中的短别名被【别的字段】的更长别名包含
        （如"专业班级"行同时命中"专业""班级"和"专业班级"），
        则剔除被覆盖的短别名，只留最精确的长别名。
        注意：同一字段内部的长短别名（如"专业"与"所学专业"）不互相剔除。
        """
        if not text:
            return None, []
        tokens = re.split(r"[^\u4e00-\u9fffA-Za-z0-9]+", text)
        raw = []  # [(别名, 字段名), ...]
        for alias, name in self.aliases().items():
            if not alias:
                continue
            if len(alias) < 2:
                # 短别名：整词相等才命中
                if alias in tokens:
                    raw.append((alias, name))
            elif alias in text:
                raw.append((alias, name))

        # 最长别名优先：短别名若被【别的字段】的更长别名包含，则剔除
        keep = []
        for a_i, f_i in raw:
            shadowed = any(
                f_j != f_i and a_i in a_j and len(a_j) > len(a_i)
                for a_j, f_j in raw
            )
            if not shadowed:
                keep.append((a_i, f_i))

        hits = sorted(set(f for _, f in keep))
        if len(hits) == 1:
            return hits[0], []
        if len(hits) > 1:
            return None, hits
        return None, []

    def field_spec(self, name):
        return self.fields.get(name, {}) or {}

    # -- 报名时间 --
    def start_dt(self):
        return _parse_dt(self.deadline.get("开始"))

    def end_dt(self):
        return _parse_dt(self.deadline.get("截止"))


def load_config(path=CONFIG_PATH):
    """读配置文件。不存在时返回带空字段的 Config，不抛异常。"""
    if not os.path.exists(path):
        print("[提示] 未找到 %s，使用空配置" % os.path.basename(path))
        return Config({})
    with open(path, "r", encoding="utf-8") as f:
        return Config(json.load(f))


def save_config(cfg, path=CONFIG_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg.raw, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 时间解析

CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
          "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
          "日": 7, "天": 7}


# 单一来源：所有判定点必须引用这里的常量，**禁止各写一份**。
# 历史事故：2026-10-10 12:52 提交其实成功了（页面显示「您的答卷已经提交」），
# 但成功词表里漏了这句话，连续三次误判成 UNKNOWN。教训：词表只能有一份。
SUCCESS_KEYWORDS = (
    "提交成功",
    "您已完成",
    "已完成该问卷",
    "您的答卷已经提交",   # <= 12:52 事故的那句，绝不能删
    "感谢您的参与",
    "您的回答已",
    "您的回答已被记录",
)

CAPTCHA_KEYWORDS = (
    "滑动验证", "验证码", "安全验证", "智能验证", "请完成安全校验",
)

FULL_KEYWORDS = (
    "已满", "停止答题", "报名人数已满", "已超过人数限制", "名额已满",
)

NOT_STARTED_KEYWORDS = (
    "未开始", "未到填写时间", "尚未开始", "报名未开始", "问卷未开放",
)

EXPIRED_KEYWORDS = (
    "已结束", "已过期", "报名已结束", "问卷已过期", "填写已结束",
)

# DOM 噪音词，出现在页脚/隐藏 div 里会造成误判
NOISE_KEYWORDS = ("隐私政策", "问卷星提供技术支持", "举报", "抽奖规则")


def _cn_to_int(s):
    """把 '十二' / '十' / '九' 这类中文数字转成 int（支持 1-31 范围）。"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    if s in CN_NUM:
        return CN_NUM[s]
    if s.startswith("十") and len(s) == 2 and s[1] in CN_NUM:
        return 10 + CN_NUM[s[1]]
    if len(s) == 2 and s[0] in CN_NUM and s[1] == "十":
        return CN_NUM[s[0]] * 10
    if len(s) == 3 and s[1] == "十":
        return CN_NUM.get(s[0], 0) * 10 + CN_NUM.get(s[2], 0)
    return None


def _parse_dt(v):
    """解析 '2026-10-10 12:10:00' / '2026/10/10 12:10' 等。失败返回 None。"""
    if not v:
        return None
    s = str(v).strip().replace("/", "-").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%d", "%m-%d %H:%M", "%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            if "%Y" not in fmt:
                dt = dt.replace(year=datetime.now().year)
            return dt
        except ValueError:
            continue
    return None


def parse_deadline_from_text(text, today=None):
    """从活动通知文本里提取报名起止时间。

    识别这类写法：
      「报名时间：10月10日（周六）12:10:00 12:20:00」
      「报名：10-10 12:00 - 20:00」
      「报名时间：2026年10月10日12:00 至 10月11日20:00」
    顺序：先整段匹配 → 找不到再用钉钉/截图手工填。

    返回 (start_str, end_str, 原文片段)；找不到返回 (None, None, None)。
    """
    if not text:
        return None, None, None
    t = re.sub(r"\s+", "", str(text))

    # 模式 A：10月10日12:10:00 至 10月11日12:10:00 / 10-10 12:00 至 20:00
    #注意分隔符后可能只跟「时刻」（20:00），不一定跟完整日期
    m = re.search(
        r"(\d{1,2})[月\-/](\d{1,2})[日号]?[^\d]{0,12}?"
        r"(\d{1,2}):(\d{2})(?::(\d{2}))?"
        r"(?:[至到\-~—]{1,3}"
        r"(?:(\d{1,2})[月\-/](\d{1,2})[日号]?[^\d]{0,12}?)?"
        r"(\d{1,2}):(\d{2})(?::(\d{2}))?)?", t)
    if m:
        mo, dy, h1, mi1, s1 = (m.group(i) for i in range(1, 6))
        s1 = s1 or "00"
        year = datetime.now().year
        try:
            start = datetime(year, int(mo), int(dy), int(h1), int(mi1),
                              int(s1))
        except ValueError:
            return None, None, None
        end = None
        # 分组重排：6=截止月 7=截止日 8=截止时 9=截止分 10=截止秒
        if m.group(8):
            e_h, e_mi, e_s = m.group(8), m.group(9), m.group(10) or "00"
            e_mo, e_dy = m.group(6), m.group(7)
            e_mo = e_mo or mo          # 省略日期则沿用开始日
            e_dy = e_dy or dy
            try:
                end = datetime(year, int(e_mo), int(e_dy), int(e_h),
                               int(e_mi), int(e_s))
            except ValueError:
                end = None
        else:
            end = start + timedelta(hours=4)
        return (start.strftime("%Y-%m-%d %H:%M:%S"),
                end.strftime("%Y-%m-%d %H:%M:%S"),
                m.group(0))

    # 模式 B：中文数字月份 —— 「十月十日十二点」
    m = re.search(r"([一二三四五六七八九十]+)月([一二三四五六七八九十]+)[日号]"
                  r"[^\d]{0,8}([零一二三四五六七八九十]+)[点时]"
                  r"([零一二三四五六七八九十]*)[分]?", t)
    if m:
        mo = _cn_to_int(m.group(1))
        dy = _cn_to_int(m.group(2))
        h = _cn_to_int(m.group(3))
        mi = _cn_to_int(m.group(4)) or 0
        if mo and dy and h:
            year = datetime.now().year
            try:
                start = datetime(year, mo, dy, h, mi)
                return (start.strftime("%Y-%m-%d %H:%M:%S"),
                        (start + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
                        m.group(0))
            except ValueError:
                return None, None, None
    return None, None, None


def resolve_start(config, force_now=False, now=None):
    """决定何时提交。返回 datetime。

    规则：
      - 配置里有报名开始时间 -> 用它
      - 没有 + force_now=True -> 现在就提交
      - 没有 + force_now=False -> 返回 None（调用方应拒绝提交）

    传入了一个**已经过去**的时间 => 抛 PastTimeError，
    绝不静默立即提交（当前 wjx_signup.py 的旧行为就是这个坑）。
    """
    now = now or datetime.now()
    if force_now:
        return now
    dt = config.start_dt()
    if dt is None:
        return None
    if dt < now:
        raise PastTimeError(dt, now)
    return dt


class PastTimeError(Exception):
    def __init__(self, target, now):
        self.target = target
        self.now = now
        super(PastTimeError, self).__init__(
            "目标时间 %s 已经过去了（当前 %s）。\n"
            "如确实要立即提交，请显式加 --force-now；"
            "不会静默替你提交。"
            % (target.strftime("%Y-%m-%d %H:%M:%S"),
               now.strftime("%Y-%m-%d %H:%M:%S")))


# ---------------------------------------------------------------- 闸门

# 处置三态
AUTO = "AUTO"   # 能可靠自动填
ASK = "ASK"     # 停下问用户，**阻塞提交**
SKIP = "SKIP"   # 明确跳过（如纯说明文字），但必须进报告


class Item(object):
    """报告里的一行。"""

    def __init__(self, status, title, reason="", next_step="",
                 detail="", qtype="", required=False, controls=None):
        self.status = status
        self.title = title
        self.reason = reason
        self.next_step = next_step
        self.detail = detail
        self.qtype = qtype
        self.required = required
        self.controls = controls or []


class GateResult(object):
    """闸门决策结果。"""

    def __init__(self, ok, items, filled, asks, skips, scan_complete=True,
                 missing_fields=None, unused_fields=None):
        self.ok = ok
        self.items = items
        self.filled = filled
        self.asks = asks
        self.skips = skips
        self.scan_complete = scan_complete
        self.missing_fields = missing_fields or []
        self.unused_fields = unused_fields or []

    def reason_summary(self):
        if not self.ok:
            if not self.scan_complete:
                return "页面结构扫描不完整，无法确认是否漏题，已停下"
            if self.asks:
                return "有 %d 道题脚本不会填，按安全规则停下等你处理" % len(self.asks)
        return ""


def decide_gate(items, scan_complete=True, n_controls=None):
    """闸门决策：**纯函数，输入 items，输出能否提交。**

    items: [Item]
    n_controls: 扫描到的控件总数（可选，用于空页面兜底）

    规则：
      - 任何 ASK 存在 -> 不可提交
      - scan_complete=False -> 不可提交（不许拿"没扫到"当"不存在"）
      - **扫到 0 个控件 -> 不可提交**。问卷未开放时页面只有倒计时，
        此时若放行，会提交一份空表单白占名额。这是真实踩过的场景：
        2026-10-10 22:11 侦察「10.12职面未来求职分享会」，问卷 12:00 才开放，
        页面显示"距离问卷开始还有 1 天"，扫描到 0 个控件。
      - 全部 AUTO/SKIP -> 可提交
    注意：不看 required（wjx 的必答标记不可靠），只看状态。
    """
    filled = [i for i in items if i.status == AUTO]
    asks = [i for i in items if i.status == ASK]
    skips = [i for i in items if i.status == SKIP]

    # 空页面兜底：一个控件都没扫到 -> 一律不提交
    empty_page = (n_controls is not None and n_controls == 0)
    if empty_page and not asks:
        blocker = Item(
            ASK, "（页面上一道题都没有）",
            reason="扫描到 0 个可填控件。可能是问卷尚未开放、"
                   "或页面结构是工具还不认识的类型",
            next_step="若是未开放，等到报名开始时间再跑；"
                      "若已开放，把截图发我，说明需要适配新结构")
        asks = [blocker]
        items = list(items) + [blocker]

    ok = (not asks) and scan_complete
    return GateResult(ok, items, filled, asks, skips, scan_complete)


# ---------------------------------------------------------------- 报告

BAR = "=" * 60


def mask_value(v, show=False):
    """打码：既能让用户验证填对了，又不泄露明文。"""
    if v is None:
        return "（未配置）"
    s = str(v)
    if show or len(s) <= 2:
        return s
    head = s[:2] if len(s) > 3 else s[:1]
    return "%s%s（%d 字）" % (head, "*" * max(1, len(s) - 2), len(s))


def render_report(result, config, url, mode, ts=None,
                  extra_notes=None, artifacts=None, submit_status=None):
    """生成中文报告文本。

    submit_status：真实提交结果（SUCCESS / UNKNOWN / CAPTCHA_TIMEOUT /
    PAGE_CLOSED ...）。2026-10-26 红丝带晚会事故的教训：本函数原先只看
    闸门 result，不看提交状态，于是 15:00 那次「三次 UNKNOWN + 窗口被关」
    被报告写成「✅ 已提交」，用户差点以为报名成功。
    **提交模式下，只有 submit_status == SUCCESS 才允许写「已提交」。**
    None 表示调用方没提供（兼容旧调用），按已提交处理以保持向后兼容。
    """
    ts = ts or datetime.now()
    L = []
    a = L.append

    a(BAR)
    a("问卷星自动填表报告")
    a("  时间：%s" % ts.strftime("%Y-%m-%d %H:%M:%S"))
    a("  链接：%s" % url)
    a("  模式：%s" % mode)
    a(BAR)
    a("")

    # ---- 一、结论 ----
    a("【一、结论】")
    if not result.scan_complete:
        a("  ⛔ 未提交。页面结构扫描不完整，无法确认是否漏题。")
        a("     可能是问卷用了工具还没见过的题型/结构。")
        a("     下一步：把截图发我，或看 logs/ 下fieldmap.json 里的原始结构。")
    elif result.asks:
        a("  ⛔ 未提交。发现 %d 道脚本不会填的题，按安全规则停下等你处理。"
          % len(result.asks))
        a("     （绝不会猜答案、绝不提交残缺答卷）")
    elif not result.ok:
        a("  ⛔ 未提交。原因见下方清单。")
    elif mode.startswith("--dry-run"):
        a("  ✅ 检查通过，%d 项可自动填写。未提交（检查模式）。" % len(result.filled))
        a("     下一步：双击「submit.bat」正式提交。")
    elif submit_status is not None and submit_status != "SUCCESS":
        # 提交动作发过了，但没有拿到成功回执 —— 不能报「已提交」
        a("  ⚠️ 未确认提交！提交动作已发出，但没有拿到「答卷已提交」回执。")
        a("     实际结果：%s" % submit_status)
        a("     （%d 项已填写，表单项本身没问题，是提交环节没走完）"
          % len(result.filled))
        a("     下一步：重开问卷看是否显示「您已提交过」；没显示就立刻重报，"
          "别假设已经报上。")
    else:
        a("  ✅ 已提交，%d 项已填写。" % len(result.filled))
    a("")

    # ---- 二、已填写 ----
    a("【二、已自动填写】（共 %d 项）" % len(result.filled))
    if result.filled:
        for it in result.filled:
            tag = "（必答）" if it.required else ""
            a("  ✅ %-22s %s%s" % (it.title[:22], it.detail, tag))
    else:
        a("  （无）")
    a("")

    # ---- 三、未填写 ----
    a("【三、未填写】（共 %d 项）—— 这些需要你出手" % len(result.asks))
    if result.asks:
        for i, it in enumerate(result.asks, 1):
            a("  ⛔ %s" % (it.title or "（无标题）"))
            if it.qtype:
                a("     题型：%s%s" % (it.qtype, "（必答）" if it.required else ""))
            if it.reason:
                a("     原因：%s" % it.reason)
            if it.next_step:
                a("     下一步：%s" % it.next_step)
            a("")
    else:
        a("  （无，全部题目都能自动填写）")
        a("")

    # ---- 四、已跳过 ----
    a("【四、已跳过（无需填写）】")
    if result.skips:
        for it in result.skips:
            a("  ℹ️  %s" % it.title[:40])
    else:
        a("  （无）")
    a("")

    # ---- 五、配置情况 ----
    a("【五、你的信息配置】")
    if result.missing_fields:
        for f in result.missing_fields:
            a("  ⛔ 「%s」页面上有这道题，但配置里还没填值。" % f)
            a("     下一步：打开 wjx_config.json，找到 \"%s\"，把 \"值\" 填上。" % f)
        a("")
    if result.unused_fields:
        a("  ℹ️  配置了但本问卷没问：" + "、".join(result.unused_fields))
        a("     （不是错误，无需处理）")
    a("  ℹ️  报告中身份信息默认打码。想看明文：")
    a("     wjx_config.json 里设 \"报告显示明文\": true")
    a("")

    # ---- 六、文件 ----
    a("【六、相关文件】")
    if artifacts:
        for label, p in artifacts:
            a("  %s：%s" % (label, p))
    else:
        a("  题目结构：fieldmap.json")
        a("  详细日志：logs\\run_<时间戳>.log")
    a("")

    if extra_notes:
        a("【补充说明】")
        for n in extra_notes:
            a("  %s" % n)
        a("")

    a(BAR)
    if result.ok and mode.startswith("--dry-run"):
        a(" 一句话：检查通过，双击「submit.bat」提交。")
    elif result.ok and submit_status is not None and submit_status != "SUCCESS":
        a(" 一句话：没确认提交成功（%s），别当已报名，去页面上看一眼。" % submit_status)
    elif result.ok:
        a(" 一句话：已提交，截图见logs 目录。")
    else:
        a(" 一句话：改完 wjx_config.json 后，重跑「check.bat」。")
    a(BAR)
    return "\n".join(L)


def write_report(text, logs_dir):
    """写报告到 logs/report_<时间戳>.txt，同时保留 report_latest.txt。

    文件名**必须用ASCII**：cmd / 任务计划程序 / 用户在资源管理器里双击时，
    非 ASCII 文件名在部分 Windows 配置下会出问题（实测过）。
    """
    os.makedirs(logs_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    p1 = os.path.join(logs_dir, "report_%s.txt" % ts)
    p2 = os.path.join(logs_dir, "report_latest.txt")
    for p in (p1, p2):
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    return p1, p2
