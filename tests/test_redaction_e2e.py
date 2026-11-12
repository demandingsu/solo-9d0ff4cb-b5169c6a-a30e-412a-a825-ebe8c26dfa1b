"""端到端测试：本地静态服务器 + FastAPI，验证脱敏预览完整链路（离线可跑）。"""

from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample-site"

# 样例页中的全部敏感号码（仅用于断言不回显）
SECRETS = [
    "13812345678",      # m001 纪要基本信息表，拆分
    "15998765432",      # m001 行内字段，拆分
    "110101199003078131",  # m001/附表中的 18 位身份证
    "18600001111",      # m001 投标人表/附表
    "310104198506122045",
    "15012341234",
    "110101880615321",  # 15 位身份证
    "13355556666",
    "13900002222",      # m002
    "440106199201093327",
    "13711113333",
]
# 无字段标签/非目标字段中的号码，必须原样保留
KEEPERS = ["13700001234", "010-88889999"]
# 仅出现在附表中的非目标号码
APPENDIX_KEEPERS = ["010-66667777"]


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def static_server():
    handler = functools.partial(_QuietHandler, directory=str(SAMPLE_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _preview(client, url):
    resp = client.post(
        "/api/v1/redact/preview",
        json={"detail_url": url, "timeout": 5},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_preview_redacts_detail_and_both_appendices(client, static_server):
    data = _preview(client, f"{static_server}/minutes/m001-review.html")

    assert data["ok"] is True
    assert data["failed_sources"] == []
    roles = [p["role"] for p in data["pages"]]
    assert roles == ["detail", "appendix", "appendix"]
    assert data["total_hits"] >= 8

    # 每页都带脱敏 HTML 与命中位置
    for page in data["pages"]:
        assert page["redacted_html"]
        assert "[已脱敏]" in page["redacted_html"]
        assert page["hit_count"] == len(page["hits"])
        for hit in page["hits"]:
            assert hit["field_type"] in {"id_card", "mobile"}
            assert hit["location"] and hit["css_path"]


def test_no_original_secret_value_echoed_anywhere(client, static_server):
    raw = client.post(
        "/api/v1/redact/preview",
        json={"detail_url": f"{static_server}/minutes/m001-review.html", "timeout": 5},
    ).text
    for secret in SECRETS:
        assert secret not in raw
    # 拆分后的片段也不应残留在任何页面 HTML 中（整字段已被遮盖）
    data = _preview(client, f"{static_server}/minutes/m001-review.html")
    for page in data["pages"]:
        html = page["redacted_html"]
        assert "110101" not in html
        assert "19900307" not in html


def test_unlabeled_numbers_and_non_target_fields_are_preserved(client, static_server):
    data = _preview(client, f"{static_server}/minutes/m001-review.html")
    detail_html = data["pages"][0]["redacted_html"]
    for keeper in KEEPERS:
        assert keeper in detail_html
    # 正文与表格结构保留
    assert "<table>" in detail_html and "<h1>" in detail_html
    assert "评标结论" in detail_html

    # 附表里的传真等非目标字段同样保留
    all_html = "".join(p["redacted_html"] for p in data["pages"])
    for keeper in APPENDIX_KEEPERS:
        assert keeper in all_html
    # 附表正文结构保留
    appendix = next(p for p in data["pages"] if p["role"] == "appendix")
    assert "<dl>" in "".join(p["redacted_html"] for p in data["pages"])
    assert appendix["title"]


def test_external_nav_anchor_and_pdf_links_are_not_fetched(client, static_server):
    data = _preview(client, f"{static_server}/minutes/m001-review.html")
    all_urls = [p["url"] for p in data["pages"]]
    assert not any("example.gov" in u for u in all_urls)
    assert not any("login" in u for u in all_urls)
    assert not any(u.endswith(".pdf") for u in all_urls)
    # 重复附表链接去重：详情 + 两个附表，没有第三张附表
    assert len(data["pages"]) == 3


def test_any_failed_appendix_returns_no_partial_preview_and_lists_sources(client, static_server):
    data = _preview(client, f"{static_server}/minutes/m002-review.html")

    assert data["ok"] is False
    # 全有或全无：即使详情页本身可脱敏，也不返回任何页面 HTML
    assert data["pages"] == []
    assert data["total_hits"] == 0
    reasons = {(f["url"].rsplit("/", 1)[-1], f["reason"]) for f in data["failed_sources"]}
    assert ("m002-appendix-missing.html", "FETCH_ERROR") in reasons
    assert ("m002-notes.txt", "UNSUPPORTED_CONTENT") in reasons
    # 报错同样不回显原值（详情页里的号码不得出现在响应中）
    import json

    serialized = json.dumps(data, ensure_ascii=False)
    for secret in ("13900002222", "440106199201093327", "13711113333"):
        assert secret not in serialized
    assert any("失败" in w for w in data["warnings"])


def test_unreachable_detail_url_is_failed_response(client):
    data = _preview(client, "http://127.0.0.1:1/no-server-minutes.html")
    assert data["ok"] is False
    assert data["pages"] == []
    assert len(data["failed_sources"]) == 1
    assert data["failed_sources"][0]["role"] == "detail"
    assert data["failed_sources"][0]["reason"] == "FETCH_ERROR"


def test_get_endpoint_works_the_same(client, static_server):
    resp = client.get(
        "/api/v1/redact/preview",
        params={"detail_url": f"{static_server}/minutes/m001-review.html"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert len(data["pages"]) == 3
