"""脱敏引擎与预览编排。

只处理标有“身份证号”或“联系人手机号”的字段，其余正文、表格结构原样保留。

关键设计：

* 号码可能被 ``<span>/<b>/<font>`` 等内联标签拆成多个文本节点，因此先在
  字段容器内取全部文本节点拼接、归一化（去空白与常见分隔符）后再识别，
  命中则对**整个字段**遮盖，而不是只遮盖匹配片段；
* 识别仅在“带标签的字段”内进行，正文里未打标签的手机号、座机号一律不动；
* 命中位置只记录字段类型、结构坐标与所在小节标题，任何响应（含报错）
  都不会回显原始号码；
* 每页脱敏后做残留自检，失败抛 :class:`RedactionError`，由编排层汇总为
  失败来源，且不返回任何半成品 HTML。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

from app.parser import same_site

# 固定遮盖标记：不随原值长度变化，避免从标记长度反推号码位数
MASK = "[已脱敏]"

# 仅这两个标签会触发脱敏（冒号由匹配逻辑处理，这里不带标点）
FIELD_LABELS: dict[str, str] = {
    "身份证号": "id_card",
    "联系人手机号": "mobile",
}

# 18 位（末位可为 X）或 15 位身份证号；两侧边界防止从更长数字串中截取
_ID_RE = re.compile(
    r"(?<![0-9Xx])\d{17}[0-9Xx](?![0-9Xx])|(?<!\d)\d{15}(?!\d)"
)
# 中国大陆 11 位手机号
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# 兜底：标签字段内若为 11 位以上纯数字/字母 X 串（被异常分隔打散），同样遮盖
_DENSE_RE = re.compile(r"^[0-9xX]{11,}$")
# 位置说明里的长数字串一律抹掉，双重保险防止回显
_DIGIT_RUN_RE = re.compile(r"\d{6,}")
# 归一化时去掉的视觉分隔符（空白、连字符、下划线等）
_SEP_RE = re.compile(r"[\s\-—–_]+")
_WS_RE = re.compile(r"\s+")

_INLINE_CONTAINERS = {"p", "li", "td", "th", "dd", "dt", "div", "span", "label", "b", "strong", "em", "i"}
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")

# 附表发现：导航类路径（登录/注册/首页等）；不拦截 contact，联系方式附表常用该词
_SKIP_PATH_RE = re.compile(r"(login|logout|register|index|home|about|search)", re.I)
# 附表发现：明确的非 HTML 二进制/资源后缀直接跳过；
# 文本类（txt/csv 等）不做后缀拦截，交给内容类型判定以暴露不支持的来源
_SKIP_EXTENSIONS = {
    "pdf", "zip", "rar", "7z", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "png", "jpg", "jpeg", "gif", "svg", "mp3", "mp4", "avi",
}
_MAX_APPENDIX = 20
_MAX_CONCURRENCY = 5
_USER_AGENT = "TenderRedactor/1.0 (+local procurement minutes preview API)"


class RedactionError(RuntimeError):
    """脱敏处理失败（如自检发现残留）。消息中不得包含原始号码。"""


@dataclass
class _Task:
    """一个待判定/遮盖的标签字段。"""

    label: str
    field_type: str
    mode: str  # table / dl / inline
    container: Tag
    location: str
    # inline 模式：“标签：”结束的边界文本节点及其截断位置
    boundary: NavigableString | None = None
    boundary_split: int = 0


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # pragma: no cover - 取决于运行环境
        return BeautifulSoup(html, "html.parser")


def _clean(text: str | None) -> str:
    return _WS_RE.sub(" ", text).strip() if text else ""


def _normalize_value(text: str) -> str:
    """跨节点拼接后的归一化：去空白与视觉分隔符。"""
    return _SEP_RE.sub("", text or "")


def _detect_sensitive(normalized: str) -> tuple[bool, list[str]]:
    """在归一化文本上识别身份证号/手机号，返回（是否敏感, 命中的原值）。

    原值仅用于脱敏后的残留自检，不会进入任何对外响应。
    """
    found = [m.group(0) for m in _ID_RE.finditer(normalized)]
    found.extend(m.group(0) for m in _PHONE_RE.finditer(normalized))
    if not found and _DENSE_RE.match(normalized):
        return True, []
    return bool(found), found


def _sanitize_context(text: str) -> str:
    """位置说明文本净化：长数字串替换为占位符并截断长度。"""
    cleaned = _clean(text)
    cleaned = _DIGIT_RUN_RE.sub("[数字]", cleaned)
    return cleaned[:40]


def _css_path(tag: Tag) -> str:
    """生成稳定的 CSS 结构坐标，如 ``article > table:nth-of-type(1) > tr...``。"""
    parts: list[str] = []
    cur: Tag | None = tag
    while isinstance(cur, Tag) and cur.name and cur.name != "[document]":
        nth = 1
        sibling = cur.find_previous_sibling(cur.name)
        while sibling is not None:
            nth += 1
            sibling = sibling.find_previous_sibling(cur.name)
        parts.append(f"{cur.name}:nth-of-type({nth})")
        cur = cur.parent
    return " > ".join(reversed(parts))


def _nearest_heading(tag: Tag) -> str | None:
    heading = tag.find_previous(_HEADING_TAGS)
    if heading is None:
        return None
    return _sanitize_context(heading.get_text(" "))


# --------------------------------------------------------------------------- #
# 字段发现：三种页面形态
# --------------------------------------------------------------------------- #
def _scan_tables(soup: BeautifulSoup) -> list[_Task]:
    """表格形态，兼容两种布局：

    * 行式：``<th>标签</th><td>值</td>`` 同行排列；
    * 列式：标签位于 ``thead`` 或全 ``th`` 表头行，数据行按列对应。
    """
    tasks: list[_Task] = []
    for table_idx, table in enumerate(soup.find_all("table"), start=1):
        column_labels: dict[int, str] = {}
        for row_idx, tr in enumerate(table.find_all("tr", recursive=True), start=1):
            cells = tr.find_all(["th", "td"], recursive=False)
            is_header_row = tr.find_parent("thead") is not None or (
                cells and all(cell.name == "th" for cell in cells)
            )
            if is_header_row:
                for col_idx, cell in enumerate(cells, start=1):
                    label = _clean(cell.get_text(" ")).rstrip("：: ")
                    if label in FIELD_LABELS:
                        column_labels[col_idx] = label
                continue
            for col_idx, cell in enumerate(cells, start=1):
                label = _clean(cell.get_text(" ")).rstrip("：: ")
                if label in FIELD_LABELS and col_idx < len(cells):
                    value_cell = cells[col_idx]
                    tasks.append(
                        _Task(
                            label=label,
                            field_type=FIELD_LABELS[label],
                            mode="table",
                            container=value_cell,
                            location=(
                                f"第 {table_idx} 个表格第 {row_idx} 行第 {col_idx + 1} 列"
                                f"（{label}字段）"
                            ),
                        )
                    )
                elif col_idx in column_labels and cell.name == "td":
                    label = column_labels[col_idx]
                    tasks.append(
                        _Task(
                            label=label,
                            field_type=FIELD_LABELS[label],
                            mode="table",
                            container=cell,
                            location=(
                                f"第 {table_idx} 个表格第 {row_idx} 行“{label}”列"
                                f"（表头第 {col_idx} 列）"
                            ),
                        )
                    )
    return tasks


def _scan_definition_lists(soup: BeautifulSoup) -> list[_Task]:
    """``<dt>标签</dt><dd>值</dd>`` 定义列表形态。"""
    tasks: list[_Task] = []
    for dl_idx, dl in enumerate(soup.find_all("dl"), start=1):
        terms = [
            child
            for child in dl.children
            if isinstance(child, Tag) and child.name in ("dt", "dd")
        ]
        for i, dt in enumerate(terms):
            if dt.name != "dt":
                continue
            label = _clean(dt.get_text(" ")).rstrip("：: ")
            if label not in FIELD_LABELS:
                continue
            if i + 1 < len(terms) and terms[i + 1].name == "dd":
                tasks.append(
                    _Task(
                        label=label,
                        field_type=FIELD_LABELS[label],
                        mode="dl",
                        container=terms[i + 1],
                        location=f"第 {dl_idx} 个定义列表的“{label}”字段",
                    )
                )
    return tasks


def _find_inline_boundary(container: Tag, label: str) -> tuple[NavigableString | None, int]:
    """在容器内定位“标签：”的结束位置，兼容标签文字本身被内联标签拆分的情况。

    容器内可能先出现不带冒号的同名词（如“联系人手机号如有变更请报备……
    正式登记 联系人手机号：159…”），因此按文本节点顺序滑动匹配标签，
    只有标签**紧邻**（允许空白）出现且其后紧跟冒号的位置才是字段边界。

    返回（边界文本节点, 冒号后一位的截断下标）；找不到返回（None, 0）。
    """
    # 保留空白节点：冒号可能单独位于标签之间的空白文本节点中
    strings = [s for s in container.find_all(string=True, recursive=True)]
    idx = 0
    while idx < len(strings):
        node = strings[idx]
        text = str(node)

        # 同一节点内可能多次出现标签（如先出现不带冒号的同名词），
        # 只接受“标签之后到冒号之间仅有空白”的那一次出现
        for match in re.finditer(re.escape(label), text):
            colon = re.search(r"[:：]", text[match.end():])
            if colon is None:
                continue
            between = text[match.end(): match.end() + colon.start()]
            if _WS_RE.sub("", between) == "":
                return node, match.end() + colon.end()

        stripped = text.strip()
        # 标签从本节点开始、可能跨多个节点；拼完后，跳过空白节点，
        # 下一个含内容的节点必须以冒号开头
        if label.startswith(stripped):
            assembled = stripped
            j = idx + 1
            while j < len(strings) and label.startswith(assembled) and assembled != label:
                assembled += strings[j].strip()
                j += 1
            if assembled == label:
                k = j
                while k < len(strings) and not strings[k].strip():
                    k += 1
                if k < len(strings):
                    next_colon = re.match(r"\s*[:：]", strings[k])
                    if next_colon is not None:
                        return strings[k], next_colon.end()
        idx += 1
    return None, 0


def _scan_inline(soup: BeautifulSoup) -> list[_Task]:
    """``<p>联系人手机号：<span>138</span>...</p>`` 行内“标签：值”形态。"""
    tasks: list[_Task] = []
    # 标签文字本身可能被内联标签拆开，get_text 拼接后标签内部带空白，
    # 因此在每个字符之间允许出现任意空白
    label_alternatives = "|".join(
        r"\s*".join(re.escape(ch) for ch in label) for label in FIELD_LABELS
    )
    label_pattern = re.compile(rf"({label_alternatives})\s*[:：]")
    for element in soup.find_all(list(_INLINE_CONTAINERS)):
        # 跳过表格/定义列表容器，避免把整块结构当成行内字段重复处理
        if element.find(["table", "dl"]):
            continue
        text = _clean(element.get_text(" "))
        match = label_pattern.search(text)
        if not match:
            continue
        label = _WS_RE.sub("", match.group(1))
        boundary, split = _find_inline_boundary(element, label)
        if boundary is None:  # 结构异常，定位不到边界时不处理，交给自检/人工
            continue
        tasks.append(
            _Task(
                label=label,
                field_type=FIELD_LABELS[label],
                mode="inline",
                container=element,
                boundary=boundary,
                boundary_split=split,
                location=f"正文行内“{label}”字段（{element.name} 元素）",
            )
        )
    return tasks


def _dedupe(tasks: list[_Task]) -> list[_Task]:
    seen: set[tuple[str, int]] = set()
    unique: list[_Task] = []
    for task in tasks:
        key = (task.mode, id(task.container))
        if key in seen:
            continue
        seen.add(key)
        unique.append(task)
    return unique


# --------------------------------------------------------------------------- #
# 遮盖动作
# --------------------------------------------------------------------------- #
def _mask_structural(task: _Task) -> None:
    """表格/定义列表：整个值单元格替换为遮盖标记，外壳标签保留。"""
    task.container.clear()
    task.container.append(MASK)


_SENTENCE_END_RE = re.compile(r"[。；;！？!?，,、]")


def _mask_inline(task: _Task) -> None:
    """行内字段：保留“标签：”前缀，遮盖其后号码（跨内联标签），

    并保留号码之后以句末标点起始的后续正文。
    """
    container = task.container
    boundary = task.boundary
    assert boundary is not None

    # 1) 边界之后的文本节点按顺序处理：号码部分摘除，遇到句末标点则保留剩余正文
    strings = container.find_all(string=True, recursive=True)
    after: list[NavigableString] = []
    seen = False
    for node in strings:
        if seen:
            after.append(node)
        elif node is boundary:
            seen = True

    trailing: str | None = None
    for node in after:
        if trailing is not None:
            break
        end_match = _SENTENCE_END_RE.search(node)
        if end_match is not None:
            trailing = node[end_match.start():]
        node.extract()

    # 2) 截断边界节点，只保留到冒号
    boundary.replace_with(boundary[: task.boundary_split])

    # 3) 清理变空的内联标签，自底向上避免父标签提前失效
    for tag in list(container.find_all(True, recursive=True)):
        if not tag.contents:
            tag.decompose()

    # 4) 追加固定遮盖标记与（若有）后续正文
    container.append(MASK)
    if trailing:
        container.append(trailing)


def _inline_value_text(task: _Task) -> str:
    """取行内字段冒号之后的值文本（跨内联标签拼接），不含标签本身。"""
    boundary = task.boundary
    assert boundary is not None
    parts = [boundary[task.boundary_split:]]
    strings = task.container.find_all(string=True, recursive=True)
    seen = False
    for node in strings:
        if seen:
            parts.append(str(node))
        elif node is boundary:
            seen = True
    return " ".join(parts)


def redact_html(html: str, url: str) -> tuple[str, list[dict]]:
    """对单页 HTML 执行脱敏，返回（脱敏后 HTML, 命中位置列表）。

    命中位置不含任何原始号码；处理后自检发现残留则抛 :class:`RedactionError`。
    """
    soup = _soup(html)
    tasks = _dedupe(_scan_tables(soup) + _scan_definition_lists(soup) + _scan_inline(soup))

    hits: list[dict] = []
    detected_values: list[str] = []

    for task in tasks:
        if task.mode == "inline":
            # 行内字段只检测冒号之后的“值”，标签文字不参与号码识别
            value_text = _inline_value_text(task)
        else:
            value_text = task.container.get_text(" ")
        normalized = _normalize_value(value_text)
        if not normalized:
            continue
        is_sensitive, raw_values = _detect_sensitive(normalized)
        if not is_sensitive:
            continue

        if task.mode == "inline":
            _mask_inline(task)
        else:
            _mask_structural(task)

        detected_values.extend(raw_values)
        hits.append(
            {
                "field_type": task.field_type,
                "label": task.label,
                "location": task.location,
                "context": _nearest_heading(task.container),
                "css_path": _css_path(task.container),
            }
        )

    output = str(soup)

    # 残留自检：任何已识别号码都不得出现在输出中
    for value in detected_values:
        if value in output:
            raise RedactionError(
                f"脱敏后自检发现敏感信息残留，已拒绝输出（{url}）"
            )
    # 遮盖标记数量应与命中数一致
    if output.count(MASK) < len(hits):
        raise RedactionError(f"脱敏遮盖标记缺失，已拒绝输出（{url}）")

    return output, hits


def extract_title(html: str) -> str:
    soup = _soup(html)
    h1 = soup.find("h1")
    title_node = soup.find("title")
    title = _clean(h1.get_text(" ") if h1 else (title_node.get_text(" ") if title_node else ""))
    return _sanitize_context(title) or "未命名页面"


def looks_like_html(content_type: str, body: str) -> bool:
    if "html" in (content_type or "").lower():
        return True
    return body.lstrip()[:200].lower().startswith(("<!doctype html", "<html"))


# --------------------------------------------------------------------------- #
# 同站附表链接发现
# --------------------------------------------------------------------------- #
def discover_appendix_links(html: str, base_url: str) -> list[str]:
    """发现纪要页指向的同站 HTML 附表链接（一跳，不递归）。

    外站链接、登录/导航链接、非 HTML 资源（pdf/zip/图片等）、自引用均忽略，
    结果去重并保持页面顺序，上限 :data:`_MAX_APPENDIX` 个。
    """
    soup = _soup(html)
    seen: set[str] = set()
    links: list[str] = []
    base_path = urlparse(base_url).path.rstrip("/")

    for anchor in soup.find_all("a", href=True):
        absolute = urljoin(base_url, anchor["href"].strip())
        if not absolute.startswith(("http://", "https://")):
            continue
        if not same_site(absolute, base_url):
            continue
        parsed = urlparse(absolute)
        if _SKIP_PATH_RE.search(parsed.path):
            continue
        last_segment = parsed.path.rsplit("/", 1)[-1]
        extension = last_segment.rsplit(".", 1)[-1].lower() if "." in last_segment else ""
        if extension in _SKIP_EXTENSIONS:
            continue
        if parsed.path.rstrip("/") == base_path:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        links.append(absolute)
        if len(links) >= _MAX_APPENDIX:
            break
    return links


# --------------------------------------------------------------------------- #
# 预览编排：抓取详情页 + 同站附表，全成功才输出
# --------------------------------------------------------------------------- #
def _failure(url: str, role: str, reason: str) -> dict:
    return {"url": url, "role": role, "reason": reason}


async def _fetch_html(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url)
    response.raise_for_status()
    if not looks_like_html(response.headers.get("content-type", ""), response.text):
        raise RedactionError("unsupported-content")
    return response.text


async def build_redaction_preview(detail_url: str, timeout: float) -> dict:
    """编排脱敏预览。

    任一来源读取或脱敏失败时返回 ``ok=False`` 且 ``pages`` 为空，
    并在 ``failed_sources`` 中列出全部失败来源（仅 URL + 固定原因码）。
    """
    from datetime import datetime, timezone

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
    ) as client:
        # 1) 抓取纪要详情页
        try:
            detail_html = await _fetch_html(client, detail_url)
        except RedactionError:
            return _failed_response(
                detail_url,
                [_failure(detail_url, "detail", "UNSUPPORTED_CONTENT")],
            )
        except httpx.HTTPError:
            return _failed_response(
                detail_url,
                [_failure(detail_url, "detail", "FETCH_ERROR")],
            )

        # 2) 先脱敏详情页，尽早失败、避免无谓抓取
        try:
            detail_redacted, detail_hits = redact_html(detail_html, detail_url)
        except RedactionError:
            return _failed_response(
                detail_url,
                [_failure(detail_url, "detail", "REDACTION_ERROR")],
            )

        # 3) 从原始详情页发现同站附表
        appendix_urls = discover_appendix_links(detail_html, detail_url)

        # 4) 并发抓取附表
        semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

        async def fetch_one(url: str) -> tuple[str, str]:
            async with semaphore:
                return url, await _fetch_html(client, url)

        fetched: list[tuple[str, str]] = []
        failures: list[dict] = []
        results = await asyncio.gather(
            *(fetch_one(u) for u in appendix_urls), return_exceptions=True
        )
        for url, outcome in zip(appendix_urls, results):
            if isinstance(outcome, RedactionError):
                failures.append(_failure(url, "appendix", "UNSUPPORTED_CONTENT"))
            elif isinstance(outcome, httpx.HTTPError):
                failures.append(_failure(url, "appendix", "FETCH_ERROR"))
            elif isinstance(outcome, BaseException):
                failures.append(_failure(url, "appendix", "FETCH_ERROR"))
            else:
                fetched.append(outcome)

        if failures:
            return _failed_response(detail_url, failures)

        # 5) 全部抓取成功后再逐页脱敏
        pages = [
            {
                "url": detail_url,
                "role": "detail",
                "title": extract_title(detail_html),
                "redacted_html": detail_redacted,
                "hits": detail_hits,
                "hit_count": len(detail_hits),
            }
        ]
        for url, html in fetched:
            try:
                redacted, hits = redact_html(html, url)
            except RedactionError:
                return _failed_response(
                    detail_url,
                    [_failure(url, "appendix", "REDACTION_ERROR")],
                )
            pages.append(
                {
                    "url": url,
                    "role": "appendix",
                    "title": extract_title(html),
                    "redacted_html": redacted,
                    "hits": hits,
                    "hit_count": len(hits),
                }
            )

    total_hits = sum(page["hit_count"] for page in pages)
    return {
        "detail_url": detail_url,
        "ok": True,
        "pages": pages,
        "failed_sources": [],
        "total_hits": total_hits,
        "warnings": [],
        "fetched_at": datetime.now(timezone.utc),
    }


def _failed_response(detail_url: str, failures: list[dict]) -> dict:
    from datetime import datetime, timezone

    return {
        "detail_url": detail_url,
        "ok": False,
        # 全有或全无：失败时绝不返回任何半成品脱敏 HTML
        "pages": [],
        "failed_sources": failures,
        "total_hits": 0,
        "warnings": ["存在读取或脱敏失败的来源，已取消本次预览且不返回部分结果"],
        "fetched_at": datetime.now(timezone.utc),
    }
