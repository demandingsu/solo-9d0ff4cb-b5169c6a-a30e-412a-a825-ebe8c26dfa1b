"""脱敏引擎单元测试（不依赖网络）。

覆盖：
* 三种页面形态（th/td 行式表格、thead 列式表格、dt/dd、行内标签）；
* 号码被内联标签拆成多个文本节点时的跨节点识别与整字段遮盖；
* 非目标字段、无标签正文号码、座机号原样保留；
* 命中位置不回显原值、残留自检、同站附表发现。
"""

from __future__ import annotations

import pytest

from app.redaction import (
    MASK,
    discover_appendix_links,
    extract_title,
    looks_like_html,
    redact_html,
)

BASE = "http://localhost:8080/minutes/m001-review.html"

# 测试中使用的固定假号码，仅用于断言“不回显”
ID18_A = "110101199003078131"
ID18_B = "310104198506122045"
ID15 = "110101880615321"
PHONE_A = "13812345678"
PHONE_B = "15998765432"
PHONE_DECOY = "13700001234"
LANDLINE = "010-88889999"


# --------------------------------------------------------------------------- #
# 行式表格：号码被内联标签拆分
# --------------------------------------------------------------------------- #
ROW_TABLE_HTML = f"""
<html><body>
<h1>测试评标纪要</h1>
<table>
  <tr><th>项目编号</th><td>GSB-2026-001</td></tr>
  <tr><th>联系人手机号</th><td><b>138</b><span>1234</span><span>5678</span></td></tr>
  <tr><th>联系电话（座机）</th><td>{LANDLINE}</td></tr>
  <tr><th>身份证号</th><td><span>110101</span><span>19900307</span><span>8131</span></td></tr>
</table>
</body></html>
"""


def test_row_table_split_nodes_whole_field_masked_and_structure_kept():
    html, hits = redact_html(ROW_TABLE_HTML, BASE)

    assert PHONE_A not in html
    assert ID18_A not in html
    assert MASK in html
    assert html.count(MASK) == 2
    # 表格结构保留
    assert "<table>" in html and "<th>" in html and "<td>" in html

    field_types = {h["field_type"] for h in hits}
    assert field_types == {"mobile", "id_card"}
    for hit in hits:
        assert "css_path" in hit and hit["css_path"]
        assert "td:nth-of-type" in hit["css_path"]


def test_non_target_fields_and_unlabeled_numbers_are_kept():
    html, _ = redact_html(ROW_TABLE_HTML, BASE)
    assert LANDLINE in html  # 座机号非目标字段
    assert "GSB-2026-001" in html  # 项目编号保留


# --------------------------------------------------------------------------- #
# 列式表格：标签在 thead，数据行按列对应
# --------------------------------------------------------------------------- #
COLUMN_TABLE_HTML = f"""
<html><body>
<h1>投标人代表信息</h1>
<table>
  <thead><tr><th>序号</th><th>投标人</th><th>身份证号</th><th>联系人手机号</th></tr></thead>
  <tbody>
    <tr><td>1</td><td>甲公司</td>
        <td><span>310104</span><span>19850612</span><span>2045</span></td>
        <td><b>150</b><b>1234</b><b>1234</b></td></tr>
    <tr><td>2</td><td>乙公司</td><td>110101880615321</td><td>13355556666</td></tr>
  </tbody>
</table>
</body></html>
"""


def test_column_header_table_masks_every_data_cell_under_labeled_columns():
    html, hits = redact_html(COLUMN_TABLE_HTML, BASE)

    assert ID18_B not in html
    assert ID15 not in html
    assert "15012341234" not in html
    assert "13355556666" not in html
    assert html.count(MASK) == 4
    # 非敏感列保留
    assert "甲公司" in html and "乙公司" in html
    assert all("列" in h["location"] for h in hits)
    assert {h["field_type"] for h in hits} == {"id_card", "mobile"}


# --------------------------------------------------------------------------- #
# dt/dd 定义列表
# --------------------------------------------------------------------------- #
DL_HTML = f"""
<html><body>
<h1>联系方式附表</h1>
<dl>
  <dt>授权代表</dt><dd>赵建国</dd>
  <dt>身份证号</dt><dd><span>110101</span><span>19900307</span><span>8131</span></dd>
  <dt>联系人手机号</dt><dd><b>186</b><b>0000</b><b>1111</b></dd>
  <dt>单位地址</dt><dd>甲市园林路 18 号</dd>
</dl>
</body></html>
"""


def test_definition_list_mask_and_keep_other_terms():
    html, hits = redact_html(DL_HTML, BASE)
    assert ID18_A not in html
    assert "18600001111" not in html
    assert html.count(MASK) == 2
    assert "赵建国" in html and "甲市园林路 18 号" in html
    assert "定义列表" in hits[0]["location"]


# --------------------------------------------------------------------------- #
# 行内“标签：值”
# --------------------------------------------------------------------------- #
INLINE_HTML = f"""
<html><body>
<h1>登记信息</h1>
<p>采购代理机构现场联系人信息登记如下：
  <strong>联系人手机号</strong>：<span>159</span><span>9876</span><span>5432</span>，
  请保持通讯畅通。</p>
<p>如对评标流程有疑问，可在工作时间拨打监督电话 {PHONE_DECOY} 反映情况。</p>
</body></html>
"""


def test_inline_field_keeps_label_masks_number_and_preserves_following_prose():
    html, hits = redact_html(INLINE_HTML, BASE)

    assert PHONE_B not in html
    assert "联系人手机号" in html  # 标签文字保留
    assert "：" in html
    assert MASK in html
    assert "请保持通讯畅通" in html  # 号码之后的正文保留
    assert len(hits) == 1 and hits[0]["field_type"] == "mobile"


def test_unlabeled_phone_in_prose_is_not_masked():
    html, hits = redact_html(INLINE_HTML, BASE)
    assert PHONE_DECOY in html  # 无字段标签，不在处理范围
    assert all(h["field_type"] != "id_card" for h in hits)


def test_inline_label_split_across_tags_is_recognized():
    html_text = (
        "<html><body><p>登记的<span>联系</span><span>人手机号</span>："
        "<span>133</span><span>5555</span><span>6666</span></p></body></html>"
    )
    html, hits = redact_html(html_text, BASE)
    assert "13355556666" not in html
    # 被拆分的标签文字仍各就各位保留
    assert "<span>联系</span>" in html and "<span>人手机号</span>" in html
    assert MASK in html
    assert len(hits) == 1


def test_earlier_label_without_colon_is_not_mistaken_for_field():
    # 先出现不带冒号的同名词，真正的“标签：值”在后：只遮盖后者
    html_text = (
        "<html><body><p>说明：联系人手机号如有变更请报备。"
        "正式登记 联系人手机号：<span>159</span>9876<span>5432</span>，谢谢。</p></body></html>"
    )
    html, hits = redact_html(html_text, BASE)
    assert PHONE_B not in html
    assert "联系人手机号如有变更请报备" in html
    assert MASK in html and "谢谢" in html
    assert len(hits) == 1


def test_label_mentioned_without_any_colon_produces_no_hit():
    html_text = "<html><body><p>联系人手机号如有变更请报备，本页没有字段。</p></body></html>"
    html, hits = redact_html(html_text, BASE)
    assert hits == []
    assert MASK not in html


# --------------------------------------------------------------------------- #
# 分隔符与兜底识别
# --------------------------------------------------------------------------- #
def test_separator_normalization_handles_hyphenated_number():
    html_text = (
        "<html><body><table><tr><th>联系人手机号</th>"
        "<td><span>133</span>-<span>5555</span>-<span>6666</span></td></tr></table></body></html>"
    )
    html, hits = redact_html(html_text, BASE)
    assert "13355556666" not in html
    assert "133" not in html
    assert MASK in html and len(hits) == 1


def test_dense_non_standard_digit_string_still_whole_field_masked():
    # 12 位异常数字串：虽不匹配手机号/身份证正则，但标签字段内的长数字串兜底遮盖
    html_text = (
        '<html><body><p>联系人手机号：<span>1234</span><span>5678</span><span>9012</span></p></body></html>'
    )
    html, hits = redact_html(html_text, BASE)
    assert "123456789012" not in html
    assert MASK in html and len(hits) == 1


def test_labeled_field_without_number_is_untouched():
    html_text = "<html><body><table><tr><th>联系人手机号</th><td>待补充</td></tr></table></body></html>"
    html, hits = redact_html(html_text, BASE)
    assert "待补充" in html
    assert hits == []
    assert MASK not in html


def test_fifteen_digit_legacy_id_is_masked():
    html_text = f"<html><body><table><tr><th>身份证号</th><td>{ID15}</td></tr></table></body></html>"
    html, hits = redact_html(html_text, BASE)
    assert ID15 not in html
    assert MASK in html and hits[0]["field_type"] == "id_card"


def test_short_number_not_mistaken_for_phone():
    html_text = "<html><body><table><tr><th>联系人手机号</th><td>分机号 8021</td></tr></table></body></html>"
    html, hits = redact_html(html_text, BASE)
    assert "分机号 8021" in html
    assert hits == []


# --------------------------------------------------------------------------- #
# 命中位置不回显原值
# --------------------------------------------------------------------------- #
def test_hit_locations_never_contain_original_values():
    html, hits = redact_html(ROW_TABLE_HTML, BASE)
    import json

    serialized = json.dumps(hits, ensure_ascii=False)
    for secret in (PHONE_A, ID18_A, "138", "5678"):
        assert secret not in serialized


def test_hit_context_uses_nearest_heading_and_sanitizes_digits():
    html_text = f"""
    <html><body>
    <h2>二、投标人代表信息 编号9988776655</h2>
    <table><tr><th>身份证号</th><td>{ID15}</td></tr></table>
    </body></html>
    """
    _, hits = redact_html(html_text, BASE)
    assert len(hits) == 1
    assert hits[0]["context"] is not None
    assert "9988776655" not in hits[0]["context"]
    assert "[数字]" in hits[0]["context"]


# --------------------------------------------------------------------------- #
# 标题提取与 HTML 判定
# --------------------------------------------------------------------------- #
def test_extract_title_prefers_h1_and_sanitizes():
    html_text = "<html><head><title>旧标题</title></head><body><h1>评标纪要 13812345678</h1></body></html>"
    title = extract_title(html_text)
    assert "评标纪要" in title
    assert PHONE_A not in title


def test_looks_like_html():
    assert looks_like_html("text/html; charset=utf-8", "<html></html>")
    assert looks_like_html("", "<!doctype html><html>")
    assert not looks_like_html("text/plain", "纯文本内容")
    assert not looks_like_html("application/pdf", "%PDF-1.4")


# --------------------------------------------------------------------------- #
# 同站附表链接发现
# --------------------------------------------------------------------------- #
LINKS_HTML = """
<html><body>
  <a href="/login.html">登录</a>
  <a href="https://example.gov/other-city/m.html">外站附表</a>
  <a href="#section-1">页内锚点</a>
  <a href="/minutes/m001-appendix-opening.html">附表一</a>
  <a href="m001-appendix-contacts.html">附表二</a>
  <a href="/minutes/m001-appendix-opening.html">附表一重复</a>
  <a href="/minutes/assets/bid-opening.pdf">PDF 资料</a>
  <a href="/minutes/m001-review.html">当前页自身</a>
</body></html>
"""


def test_discover_appendix_links_filters_dedupes_and_orders():
    links = discover_appendix_links(LINKS_HTML, BASE)
    assert links == [
        "http://localhost:8080/minutes/m001-appendix-opening.html",
        "http://localhost:8080/minutes/m001-appendix-contacts.html",
    ]


def test_discover_appendix_links_keeps_text_resource_for_content_check():
    html_text = '<html><body><a href="/minutes/assets/m002-notes.txt">备注</a></body></html>'
    links = discover_appendix_links(html_text, BASE)
    assert links == ["http://localhost:8080/minutes/assets/m002-notes.txt"]


# --------------------------------------------------------------------------- #
# 无命中与结构保留
# --------------------------------------------------------------------------- #
def test_page_without_any_labeled_field_is_returned_unchanged_without_hits():
    html_text = "<html><body><h1>普通页面</h1><p>正文 13700001234 保留。</p></body></html>"
    html, hits = redact_html(html_text, BASE)
    assert PHONE_DECOY in html
    assert hits == []
    assert MASK not in html


def test_full_document_structure_is_preserved():
    html_text = (
        "<!doctype html><html><head><title>完整文档</title></head>"
        f"<body><table><tr><th>身份证号</th><td>{ID15}</td></tr></table></body></html>"
    )
    html, _ = redact_html(html_text, BASE)
    assert "<html" in html and "</html>" in html
    assert "<head>" in html or "<head " in html
    assert "<title>完整文档</title>" in html
    assert "<table>" in html
