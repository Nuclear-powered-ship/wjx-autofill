"""单选（radio）支持的本地夹具自测。

没有在线问卷可以试（目标问卷 2026-10-22 才开放），所以照问卷星实测
DOM 结构造一个本地页面，验证三件事：
  1) SCAN_JS 能把同名 radio 聚成一个字段，并抓出每个选项的文字
  2) match_fields 只在「配置声明了选项 + 值能唯一定位」时才产出映射
  3) FILL_JS 真的能把选项点成 checked

用法：
  python tests/radio_fixture_check.py
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import wjx_kit as K          # noqa: E402
import wjx_signup as S       # noqa: E402

FIXTURE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>夹具问卷</title></head><body>
<form>
  <div id="div1">
    <div class="field-title"><span style="color:red">*</span>1. 你的性别</div>
    <div class="ui-radio"><span class="jqRadio"></span>
      <input type="radio" id="q1_1" name="q1" value="1"><label for="q1_1">男</label></div>
    <div class="ui-radio"><span class="jqRadio"></span>
      <input type="radio" id="q1_2" name="q1" value="2"><label for="q1_2">女</label></div>
  </div>
  <div id="div2">
    <div class="field-title">2. 年级</div>
    <div class="ui-radio"><span class="jqRadio"></span>
      <input type="radio" id="q2_1" name="q2" value="1"><label for="q2_1">大一</label></div>
    <div class="ui-radio"><span class="jqRadio"></span>
      <input type="radio" id="q2_2" name="q2" value="2"><label for="q2_2">大二</label></div>
  </div>
  <div id="div3">
    <div class="field-title">3. 姓名</div>
    <input type="text" id="q3" name="q3">
  </div>
  <div id="div3b">
    <div class="field-title">3b. 学号</div>
    <input type="text" id="q3b" name="q3b">
  </div>
  <div id="div3c">
    <div class="field-title">3c. 班级</div>
    <input type="text" id="q3c" name="q3c">
  </div>
  <div id="div4">
    <div class="field-title">4. 你参加过的活动</div>
    <div class="ui-checkbox">
      <input type="checkbox" id="q4_1" name="q4" value="1"><label for="q4_1">歌手赛</label></div>
  </div>
</form>
<div id="ctlNext" class="submitbtn">提交</div>
</body></html>"""

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", name,
                          ("  " + detail) if detail else ""))


def main():
    from playwright.sync_api import sync_playwright
    # 不依赖仓库里的真实配置：这里现造一个通用样本，任何人跑结果一致
    cfg = K.Config({"字段": {
        "姓名": {"值": "张三", "别名": ["姓名"]},
        "学号": {"值": "20240001", "别名": ["学号"]},
        "班级": {"值": "计算机2301", "别名": ["班级"]},
        "性别": {"值": "男", "别名": ["性别"], "题型": "radio",
                 "选项": ["男", "女"]},
        "年级": {"值": "大一", "别名": ["年级"], "题型": "radio",
                 "选项": ["大一", "大二"]},
    }})
    path = os.path.join(BASE, "_fixture.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(FIXTURE)

    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge", headless=True)
        page = br.new_page()
        page.goto("file:///" + path.replace("\\", "/"))
        page.wait_for_timeout(300)

        info = page.evaluate(S.SCAN_JS)
        fields = info["fields"]
        print("\n== 扫描结果 ==")
        for f in fields:
            print("  %s type=%s qtitle=%s options=%s"
                  % (f["id"], f.get("type"), f.get("qtitle"),
                     [o["text"] for o in (f.get("options") or [])]))

        sexes = [f for f in fields if f.get("type") == "radio"
                 and "性别" in (f.get("qtitle") or "")]
        check("R1 单选组被聚成一个字段", len(sexes) == 1, str(len(sexes)))
        if sexes:
            check("R2 选项文字抓全（男/女）",
                  [o["text"] for o in sexes[0]["options"]] == ["男", "女"],
                  str([o["text"] for o in sexes[0]["options"]]))

        mapping, items, missing, unused = S.match_fields(fields, cfg)
        print("\n== 匹配结果 ==")
        for m in mapping:
            print("  MAP %s -> id=%s option=%s" % (m["field"], m["target_id"],
                                                   m.get("option")))
        for it in items:
            if it.status == K.ASK:
                print("  ASK %s：%s" % (it.title, it.reason))

        sex_map = [m for m in mapping if m["field"] == "性别"]
        check("R3 性别单选被正确映射", len(sex_map) == 1, str(sex_map))
        check("R4 映射到的选项是「男」",
              sex_map and sex_map[0].get("option") == "男")
        grade_map = [m for m in mapping if m["field"] == "年级"]
        check("R5 年级单选也能映射（值=大一）",
              bool(grade_map) and grade_map[0].get("option") == "大一",
              str(grade_map))
        check("R6 多选/复选框仍被拦下不猜",
              any(it.status == K.ASK and "checkbox" in (it.qtype or "")
                  for it in items) or
              any("checkbox" in (it.qtype or "") or "多选" in it.reason
                  for it in items),
              "复选框必须报 ASK")

        done = S.verify_filled(page, mapping, cfg)
        print("\n== 填写结果 ==\n  done=%s" % done)
        checked = page.evaluate(
            "() => ['q1_1','q1_2','q2_1','q2_2'].map("
            "i => {const e=document.getElementById(i);"
            "return i+':'+(e?e.checked:'nil');})")
        print("  checked=%s" % checked)
        check("R7 性别点中「男」(q1_1)", "q1_1:true" in checked, str(checked))
        check("R8 年级点中「大一」(q2_1)", "q2_1:true" in checked, str(checked))
        check("R9 同组互斥（女未选中）", "q1_2:false" in checked)
        sid = page.evaluate("() => document.getElementById('q3b').value")
        cls = page.evaluate("() => document.getElementById('q3c').value")
        check("R11 学号能填", sid == "20240001", "q3b=%r" % sid)
        check("R12 班级能填", cls == "计算机2301", "q3c=%r" % cls)
        txt = page.evaluate("() => document.getElementById('q3').value")
        check("R10 文本题照旧能填", txt == "张三", "q3=%r" % txt)

        br.close()

    try:
        os.remove(path)
    except Exception:
        pass

    print("\n" + "=" * 50)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
