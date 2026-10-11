"""Public response contracts preserve campus-specific academic labels."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr

RESOURCES = (
    "profile",
    "dashboard",
    "semesters",
    "subjects",
    "timetable",
    "attendance",
    "results",
    "assessments",
    "assignments",
    "materials",
    "notices",
    "calendar",
    "fees",
    "receipts",
    "leave",
    "library",
    "placement",
    "surveys",
    "documents",
)
Resource = Literal[
    "profile",
    "dashboard",
    "semesters",
    "subjects",
    "timetable",
    "attendance",
    "results",
    "assessments",
    "assignments",
    "materials",
    "notices",
    "calendar",
    "fees",
    "receipts",
    "leave",
    "library",
    "placement",
    "surveys",
    "documents",
]


class Link(BaseModel):
    id: str
    label: str
    kind: Literal["detail", "attachment"]


class Table(BaseModel):
    caption: str | None = None
    columns: list[str]
    rows: list[dict[str, str]]


class FilterOption(BaseModel):
    value: str
    label: str


class PageFilter(BaseModel):
    name: str
    label: str
    input_type: Literal["select", "text", "search", "date", "datetime-local", "number"] = "select"
    options: list[FilterOption] = Field(default_factory=list)
    current: str | None = None
    supported: bool = True


class AcademicPage(BaseModel):
    title: str
    fields: dict[str, str] = Field(default_factory=dict)
    tables: list[Table] = Field(default_factory=list)
    text: list[str] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    filters: list[PageFilter] = Field(default_factory=list)
    empty: bool = False


class Capability(BaseModel):
    resource: str
    available: bool
    status: Literal["discovered", "fixture_tested", "live_verified", "unavailable", "unverified"]
    reason: str | None = None


class Capabilities(BaseModel):
    institution: str
    adapter: str
    adapter_version: str
    features: list[Capability]
    verification: str = "unverified"


class ResponseMeta(BaseModel):
    institution: str
    fetched_at: datetime
    cache_hit: bool = False
    page: int = 1
    page_size: int = 100
    total_rows: int = 0


class AcademicResponse(BaseModel):
    data: AcademicPage
    meta: ResponseMeta


class SessionCreate(BaseModel):
    base_url: str = Field(min_length=1, max_length=255)
    username: str = Field(min_length=1, max_length=255)
    password: SecretStr = Field(min_length=1, max_length=1024)


class SessionCreated(BaseModel):
    session_token: str
    expires_at: datetime
    idle_timeout_seconds: int
    institution: str


class SessionInfo(BaseModel):
    mode: Literal["personal", "multiuser"]
    institution: str
    expires_at: datetime | None = None


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class Download(BaseModel):
    filename: str
    content_type: str
    content: bytes = Field(repr=False, exclude=True)


JsonObject = dict[str, Any]
