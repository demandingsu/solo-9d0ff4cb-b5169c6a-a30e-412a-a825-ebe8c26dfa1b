"""FastAPI 入口：公告采集 API + 评标纪要脱敏预览 API。

端点：
* GET  /                          演示页（可直接在浏览器发起采集/脱敏预览）
* POST /api/v1/collect            传入列表页 URL 与项目编号，返回合并后的投标状态
* GET  /api/v1/collect            同上的查询参数形式，便于浏览器/curl 快速演示
* POST /api/v1/redact/preview     传入纪要详情 URL，返回同站页面的脱敏 HTML 与命中位置
* GET  /api/v1/redact/preview     同上的查询参数形式
* GET  /healthz                   健康检查
"""

from __future__ import annotations

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

from app.collector import collect
from app.models import FetchRequest, FetchResponse, RedactionRequest, RedactionResponse
from app.redaction import build_redaction_preview

app = FastAPI(
    title="采购公告采集与纪要脱敏 API",
    description=(
        "抓取同站点原公告与更正公告，按发布时间合并，输出投标状态与有效截止时间；"
        "并支持对评标纪要及同站附表中的身份证号、联系人手机号做脱敏预览。"
    ),
    version="1.1.0",
)


_DEMO_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>采购公告采集 / 纪要脱敏 API 演示</title>
<style>
body { font-family: system-ui, "PingFang SC", "Microsoft YaHei", sans-serif; margin: 2rem; max-width: 960px; }
input { width: 32rem; max-width: 80vw; padding: .4rem; }
button { padding: .45rem 1.2rem; cursor: pointer; }
pre { background: #f4f4f6; padding: 1rem; border-radius: 8px; overflow:auto; max-height: 60vh; }
.hint { color: #666; font-size: .9rem; }
section { border: 1px solid #e3e6ea; border-radius: 10px; padding: 1.2rem 1.4rem; margin-bottom: 1.6rem; }
h2 { margin-top: 0; }
</style>
</head>
<body>
<h1>采购公告采集 / 纪要脱敏 API 演示</h1>
<p class="hint">示例站点由 docker compose 中的 nginx 提供，默认地址 <code>http://localhost:8080/list.html</code></p>

<section>
  <h2>一、公告采集与合并</h2>
  <form id="collect-form">
    <p><label>列表页 URL <input id="list_url" value="http://localhost:8080/list.html"></label></p>
    <p><label>项目编号 <input id="project_code" value="GSB-2026-001"></label></p>
    <button type="submit">采集并合并</button>
  </form>
  <pre id="collect-out">响应将显示在这里……</pre>
</section>

<section>
  <h2>二、评标纪要脱敏预览</h2>
  <p class="hint">仅遮盖标有“身份证号”或“联系人手机号”的字段；号码被内联标签拆分时仍按整字段遮盖。</p>
  <form id="redact-form">
    <p><label>纪要详情 URL <input id="detail_url" value="http://localhost:8080/minutes/m001-review.html"></label></p>
    <button type="submit">生成脱敏预览</button>
  </form>
  <pre id="redact-out">响应将显示在这里……</pre>
</section>

<script>
async function postJson(formId, outId, url, payload) {
  const out = document.getElementById(outId);
  out.textContent = '请求中……';
  try {
    const resp = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    out.textContent = JSON.stringify(await resp.json(), null, 2);
  } catch (err) {
    out.textContent = '请求失败：' + err;
  }
}

document.getElementById('collect-form').addEventListener('submit', (e) => {
  e.preventDefault();
  postJson('collect-form', 'collect-out', '/api/v1/collect', {
    list_url: document.getElementById('list_url').value,
    project_code: document.getElementById('project_code').value
  });
});

document.getElementById('redact-form').addEventListener('submit', (e) => {
  e.preventDefault();
  postJson('redact-form', 'redact-out', '/api/v1/redact/preview', {
    detail_url: document.getElementById('detail_url').value
  });
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def demo_page() -> str:
    return _DEMO_PAGE


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/collect", response_model=FetchResponse, tags=["collect"])
async def collect_post(req: FetchRequest) -> FetchResponse:
    return await collect(req)


@app.get("/api/v1/collect", response_model=FetchResponse, tags=["collect"])
async def collect_get(
    list_url: str = Query(..., description="采购公告列表页 URL"),
    project_code: str = Query(..., description="项目编号"),
    timeout: float = Query(10.0, ge=1.0, le=60.0),
) -> FetchResponse:
    return await collect(FetchRequest(list_url=list_url, project_code=project_code, timeout=timeout))


@app.post("/api/v1/redact/preview", response_model=RedactionResponse, tags=["redaction"])
async def redact_preview_post(req: RedactionRequest) -> RedactionResponse:
    result = await build_redaction_preview(str(req.detail_url), req.timeout)
    return RedactionResponse(**result)


@app.get("/api/v1/redact/preview", response_model=RedactionResponse, tags=["redaction"])
async def redact_preview_get(
    detail_url: str = Query(..., description="评标纪要详情页 URL"),
    timeout: float = Query(10.0, ge=1.0, le=60.0),
) -> RedactionResponse:
    result = await build_redaction_preview(detail_url, timeout)
    return RedactionResponse(**result)
