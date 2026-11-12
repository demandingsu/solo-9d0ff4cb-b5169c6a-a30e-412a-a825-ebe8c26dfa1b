"""Pydantic 数据模型：请求 / 响应 / 内部记录。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, HttpUrl


class ProjectStatus(str, Enum):
    """投标状态。

    OPEN         可投标（存在有效截止时间，且未取消）
    CLOSED       不可投标（公告明确取消采购）
    PENDING      待核实（抓取/解析失败，或无法得到有效截止时间，不能把旧时间当最终期限）
    NOT_FOUND    列表页未发现与该项目编号相关的同站点公告
    """

    OPEN = "open"
    CLOSED = "closed"
    PENDING = "pending"
    NOT_FOUND = "not_found"


class NoticeKind(str, Enum):
    ORIGINAL = "original"          # 原公告
    CORRECTION = "correction"      # 更正公告（含澄清、变更、延期）
    CANCELLATION = "cancellation"  # 取消公告（更正的一种，且明确取消采购）


class FetchRequest(BaseModel):
    """采集请求。"""

    list_url: HttpUrl = Field(..., description="采购公告列表页 URL")
    project_code: str = Field(..., min_length=1, description="项目编号，如 GSB-2026-001")
    timeout: float = Field(10.0, ge=1.0, le=60.0, description="单次请求超时时间（秒）")


class NoticeRecord(BaseModel):
    """单条公告详情的解析结果（按发布时间排序）。"""

    url: HttpUrl
    kind: NoticeKind
    title: str
    published_at: datetime | None = None
    deadline: datetime | None = Field(
        None, description="该公告声明的投标截止时间；未提及则为 None，合并时沿用已生效时间"
    )
    cancelled: bool = False
    cancellation_reason: str | None = None


class SourceLink(BaseModel):
    """来源链接（列表页 + 参与合并的详情页）。"""

    role: str  # list / original / correction / cancellation
    url: HttpUrl
    title: str | None = None


class FetchResponse(BaseModel):
    """采集响应。"""

    project_code: str
    list_url: HttpUrl
    status: ProjectStatus
    can_bid: bool = Field(..., description="当前是否可投标")
    effective_deadline: datetime | None = Field(
        None, description="有效投标截止时间；待核实/未找到时为 None，绝不返回旧时间冒充最终期限"
    )
    cancelled: bool = False
    notices: list[NoticeRecord] = Field(default_factory=list)
    sources: list[SourceLink] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    fetched_at: datetime


# --------------------------------------------------------------------------- #
# 脱敏预览
# --------------------------------------------------------------------------- #
class RedactionRequest(BaseModel):
    """脱敏预览请求：纪要详情页 URL。"""

    detail_url: HttpUrl = Field(..., description="评标纪要详情页 URL")
    timeout: float = Field(10.0, ge=1.0, le=60.0, description="单次请求超时时间（秒）")


class RedactionHit(BaseModel):
    """单个命中位置；仅描述位置，不含原始号码。"""

    field_type: str = Field(..., description="字段类型：id_card / mobile")
    label: str = Field(..., description="命中的字段标签原文，如“身份证号”")
    location: str = Field(..., description="人工可读的结构坐标，如表格行列或行内字段说明")
    context: str | None = Field(None, description="所在最近小节标题（净化后，不含长数字）")
    css_path: str = Field(..., description="字段容器的 CSS 结构坐标，便于程序化定位")


class RedactedPage(BaseModel):
    """单页脱敏结果（详情页或同站附表）。"""

    url: HttpUrl
    role: str = Field(..., description="detail / appendix")
    title: str
    redacted_html: str = Field(..., description="保留正文与表格结构的脱敏后完整 HTML")
    hits: list[RedactionHit] = Field(default_factory=list)
    hit_count: int = 0


class FailedSource(BaseModel):
    """读取或脱敏失败的来源；只有 URL、角色与固定原因码，不回显任何原值。"""

    url: HttpUrl
    role: str = Field(..., description="detail / appendix")
    reason: str = Field(
        ...,
        description="固定原因码：FETCH_ERROR / UNSUPPORTED_CONTENT / REDACTION_ERROR",
    )


class RedactionResponse(BaseModel):
    """脱敏预览响应。

    ``ok=False`` 时 ``pages`` 必为空（全有或全无），失败来源见 ``failed_sources``。
    """

    detail_url: HttpUrl
    ok: bool = Field(..., description="全部来源读取并脱敏成功时为 true")
    pages: list[RedactedPage] = Field(
        default_factory=list, description="ok=false 时恒为空列表，绝不返回半成品预览"
    )
    failed_sources: list[FailedSource] = Field(default_factory=list)
    total_hits: int = 0
    warnings: list[str] = Field(default_factory=list)
    fetched_at: datetime
