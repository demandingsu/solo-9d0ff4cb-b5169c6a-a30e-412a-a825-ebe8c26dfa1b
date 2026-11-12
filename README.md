# 采购公告采集 / 评标纪要脱敏 API

本服务提供两组能力：

1. **公告采集**：传入**采购公告列表页 URL** 与**项目编号**，自动抓取该页指向的同站点
   原公告与更正公告，从详情页提取发布时间、投标截止时间与取消采购信息，按发布时间合并后
   返回当前是否可投标、有效截止时间、项目状态以及全部来源链接。
2. **评标纪要脱敏预览**：传入**纪要详情页 URL**，抓取该页及其链接的同站 HTML 附表，
   只遮盖标有“身份证号”或“联系人手机号”的字段，返回保留正文与表格结构的脱敏 HTML
   和命中位置；号码被内联标签拆成多个文本节点时仍可跨节点识别并整字段遮盖。

## 业务规则

| 规则 | 实现位置 |
| --- | --- |
| 只采集列表页指向的**同站点**详情链接，外站链接、登录/导航链接自动忽略 | `app/parser.py::extract_detail_links` |
| 按**发布时间**从早到晚合并原公告与更正公告 | `app/merger.py::merge_notices` |
| 更正公告**未提及截止时间**时，沿用此前已生效的截止时间 | `app/merger.py` |
| 更正公告给出新截止时间（如延期）时，以新时间覆盖 | `app/merger.py` |
| 公告明确**取消采购** → 状态 `closed`、不可投标，不再返回有效截止时间（旧时间仅在公告明细中保留） | `app/merger.py` |
| **任一关联详情页**抓取/解析失败（404、超时、结构异常等）→ 状态 `pending` 待核实，`effective_deadline` 强制为 `null`，**绝不把旧时间当作最终期限** | `app/merger.py` + `app/collector.py` |
| 发布时间缺失、缺少原公告、全部公告均无截止时间等信息链不完整情况，同样标记 `pending` | `app/merger.py` |

### 项目状态（`status`）

- `open`：可投标，`effective_deadline` 为合并后的有效截止时间；
- `closed`：采购已取消，`can_bid=false`，无有效截止时间；
- `pending`：待核实，存在关联页面失败或信息不完整，`effective_deadline=null`；
- `not_found`：列表页未发现与该项目编号相关的公告。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 入口、路由、浏览器演示页
│   ├── collector.py   # 采集编排：列表发现 → 并发抓取 → 归属判定 → 合并
│   ├── parser.py      # HTML 解析：同站链接发现、三层字段提取、取消识别
│   ├── merger.py      # 业务合并规则（时间继承、延期覆盖、取消、待核实）
│   ├── redaction.py   # 脱敏引擎：跨节点识别、整字段遮盖、同站附表发现、预览编排
│   └── models.py      # 请求/响应 Pydantic 模型
├── sample-site/       # 本地示例站点（nginx 静态托管，完全离线）
│   ├── list.html      # 公告列表页（含外站/登录干扰链接）
│   ├── detail/        # 3 个项目的原公告/更正/取消详情页
│   └── minutes/       # 评标纪要与同站附表样例（含拆分节点号码与失败场景）
├── nginx/default.conf # 示例站点 nginx 配置（缺失页面返回真实 404）
├── tests/             # 解析/合并/脱敏单测 + 本地静态服务器端到端测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后：

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| 公告采集 API | http://localhost:8000 | Swagger 文档：http://localhost:8000/docs |
| 浏览器演示页 | http://localhost:8000/ | 填表即可发起采集 |
| 示例公告站点 | http://localhost:8080/list.html | nginx 托管的静态采购公告 |

停止：`docker compose down`。

## API 用法

### POST（推荐）

```bash
curl -X POST http://localhost:8000/api/v1/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url":"http://localhost:8080/list.html","project_code":"GSB-2026-001"}'
```

### GET（便于浏览器快速验证）

```bash
curl "http://localhost:8000/api/v1/collect?list_url=http://localhost:8080/list.html&project_code=GSB-2026-001"
```

响应关键字段：

```json
{
  "project_code": "GSB-2026-001",
  "status": "open",
  "can_bid": true,
  "effective_deadline": "2026-11-15T14:00:00",
  "cancelled": false,
  "notices": [ { "kind": "original", "published_at": "...", "deadline": "..." } ],
  "sources": [ { "role": "list", "url": "..." }, { "role": "correction", "url": "..." } ],
  "warnings": []
}
```

## 评标纪要脱敏预览 API

### 脱敏规则

| 规则 | 实现位置 |
| --- | --- |
| 只处理标有 **“身份证号”/“联系人手机号”** 的字段；无标签正文中的号码、座机号、传真号、项目编号等一律原样保留 | `app/redaction.py` |
| 号码被 `<span>/<b>/<font>` 等内联标签拆成多个文本节点时，先拼接字段内全部文本节点并归一化（去空白、连字符等）再识别，**命中则遮盖整个字段** | `app/redaction.py::redact_html` |
| 兼容行式表格（`th/td` 同行）、列式表格（标签在 `thead`）、定义列表（`dt/dd`）、行内“标签：值”四种形态 | `app/redaction.py::_scan_*` |
| 遮盖标记固定为 `[已脱敏]`，长度不随原值变化；输出保留正文、表格与文档结构 | `app/redaction.py` |
| 命中位置仅含字段类型、人工可读坐标（表格行列/定义列表/行内）、最近小节标题与 CSS 路径；**预览、位置说明、报错均不回显原始号码**，位置文本中的长数字串会被净化 | `app/redaction.py` |
| 每页脱敏后做残留自检（已识别号码不得出现在输出中），自检失败按该页脱敏失败处理 | `app/redaction.py` |
| 只抓取纪要页链接的**同站 HTML 附表（一跳，不递归）**；外站、登录/导航、页内锚点、PDF/ZIP/图片等资源自动忽略 | `app/redaction.py::discover_appendix_links` |
| **全有或全无**：任一关联页读取或脱敏失败（404/超时、非 HTML 内容、自检失败等）时不返回任何半成品 HTML，`pages` 为空并在 `failed_sources` 列出全部失败来源（仅 URL + 固定原因码） | `app/redaction.py::build_redaction_preview` |

失败原因码：`FETCH_ERROR`（抓取失败，如 404/超时）、`UNSUPPORTED_CONTENT`（非 HTML 内容）、
`REDACTION_ERROR`（脱敏处理或残留自检失败）。

### 调用方式

```bash
# POST
curl -X POST http://localhost:8000/api/v1/redact/preview \
  -H 'Content-Type: application/json' \
  -d '{"detail_url":"http://localhost:8080/minutes/m001-review.html"}'

# GET（便于浏览器快速验证）
curl "http://localhost:8000/api/v1/redact/preview?detail_url=http://localhost:8080/minutes/m001-review.html"
```

成功响应（节选）：

```json
{
  "detail_url": "http://localhost:8080/minutes/m001-review.html",
  "ok": true,
  "pages": [
    {
      "url": ".../m001-review.html",
      "role": "detail",
      "title": "市政绿化养护服务采购项目评标纪要",
      "redacted_html": "<html>...<td>[已脱敏]</td>...</html>",
      "hits": [
        {
          "field_type": "mobile",
          "label": "联系人手机号",
          "location": "第 1 个表格第 5 行第 2 列（联系人手机号字段）",
          "context": "一、评标基本信息",
          "css_path": "html > body > article > ... > td:nth-of-type(1)"
        }
      ],
      "hit_count": 8
    }
  ],
  "failed_sources": [],
  "total_hits": 16,
  "warnings": []
}
```

关联页失败时的响应（不返回任何脱敏 HTML）：

```json
{
  "ok": false,
  "pages": [],
  "failed_sources": [
    {"url": ".../m002-appendix-missing.html", "role": "appendix", "reason": "FETCH_ERROR"},
    {"url": ".../m002-notes.txt", "role": "appendix", "reason": "UNSUPPORTED_CONTENT"}
  ],
  "total_hits": 0,
  "warnings": ["存在读取或脱敏失败的来源，已取消本次预览且不返回部分结果"]
}
```

### 示例纪要与附表（`sample-site/minutes/`）

| 文件 | 场景 |
| --- | --- |
| `m001-review.html` | 评标纪要详情：表格/行内两种字段形态，号码被多个内联标签拆分；含无标签手机号、座机号等**不应被遮盖**的干扰项；链接两个同站附表，另含外站、登录、锚点、PDF、重复链接 |
| `m001-appendix-opening.html` | 附表一：列式表格，拆分节点号码，含被忽略的 PDF 链接 |
| `m001-appendix-contacts.html` | 附表二：`dt/dd` 定义列表与跨标签行内字段，含应保留的单位地址与传真号 |
| `m002-review.html` | 第二个纪要：关联一个已下线附表（nginx 返回真实 404）和一个纯文本附表，演示**全有或全无**的失败响应 |
| `assets/m002-notes.txt` | 非 HTML 关联资源，触发 `UNSUPPORTED_CONTENT` 失败原因 |



## 示例站点内置的三个演示项目

| 项目编号 | 场景 | 预期结果 |
| --- | --- | --- |
| `GSB-2026-001` | 原公告 → 澄清更正（**未提截止时间，沿用原时间**）→ 延期更正（**新时间覆盖**） | `open`，有效截止时间 `2026-11-15 14:00` |
| `GSB-2026-002` | 原公告 → 取消公告（预算收回） | `closed`，不可投标，无有效截止时间 |
| `GSB-2026-003` | 原公告 → 更正页**已下线（404）** | `pending` 待核实，`effective_deadline=null`，来源中列出失败链接 |

另外列表页还包含：外站链接、登录导航链接、其他项目公告（`GSB-2026-099`）、
无关项目的坏链接（`GSB-2025-888`），用于验证同站过滤、项目归属判定与失败隔离。

## 详情页解析策略（三层回退）

1. **结构化标记**：`data-field="published-at|deadline|cancelled|..."` 元素或
   `<meta property="article:published_time">`；
2. **标签匹配**：`发布时间 / 投标截止时间 / 响应文件递交截止时间 / 取消原因` 等
   中文标签，兼容 `th/td`、`dt/dd`、`li/p` 等布局，支持
   `2026-11-10 09:30`、`2026年11月15日 14时00分` 等常见写法；
3. **正文正则兜底**：在含“截止/递交/提交/受理”等时限语义的块级元素中识别日期，
   并排除“发布时间”句、资质业绩说明中的历史日期（如“近三年（2023年1月1日起）”）。

取消采购识别支持 `data-field="cancelled"` 标记、取消原因标签以及
“本项目取消采购 / 终止本次招标”等显式句式。

> 接入真实采购平台时，若页面结构不同，优先在详情页模板中补 `data-field` 标记，
> 或在 `app/parser.py` 顶部的标签/关键词元组中登记该平台的字段叫法。

## 本地开发与测试（不使用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 终端 A：托管示例公告页
cd sample-site && python -m http.server 8080
# 终端 B：启动 API
uvicorn app.main:app --reload --port 8000

# 测试（自带静态服务器，无需外部依赖）
pytest -q
```

## 设计说明

- 列表页抓取失败直接返回 `502`；详情页失败不影响整体响应，而是按业务规则沉淀为
  `pending` + 失败来源链接，便于投标专员人工核实。
- 关联归属判定结合详情页项目编号与列表行上下文（含编号列），
  即使详情页下线（只能看到列表行）也能正确判定失败是否与目标项目相关；
  无关项目的坏链接不会污染查询结果。
- 详情页并发抓取（上限 5），统一超时、自定义 UA、跟随重定向。
- 脱敏预览的失败处理与采集接口刻意不同：公开前的脱敏必须保证“全有或全无”，
  因此任一关联页读取/脱敏失败都返回 `ok=false` 且不带任何 HTML，避免半成品外泄；
  所有对外文本（命中位置、标题、告警、错误码）都经过数字串净化，原始号码只在
  内存中用于识别与残留自检，不进入日志之外的任何响应字段。
- 脱敏识别范围严格限定在两个标签字段内，避免对正文做全局正则替换而误伤
  公告中的其他数字内容；标签字段内再做跨节点归一化识别，兼顾安全与保真。
