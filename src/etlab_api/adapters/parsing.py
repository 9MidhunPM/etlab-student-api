"""Conservative HTML extraction that preserves portal labels and displayed values."""

import re
from collections.abc import Callable
from typing import Literal, cast
from urllib.parse import unquote

from bs4 import BeautifulSoup, Tag

from etlab_api.models import AcademicPage, FilterOption, Link, PageFilter, Table

LinkRegistrar = Callable[[str, str], Link | None]
FormValidator = Callable[[Tag], bool]
FilterInputType = Literal["text", "search", "date", "datetime-local", "number"]
SENSITIVE_FILTER_NAME = re.compile(
    r"password|passwd|csrf|token|secret|credential|(?:^|[\W_])(?:username|email|session|auth)(?:$|[\W_])",
    re.I,
)
UNSAFE_FILTER_NAME = re.compile(
    r"(?:^|[\W_])(?:action|cmd|command|execute|_method|redirect|redirect_uri|returnurl|return_url|url|target|add|create|delete|remove|edit|update|submit|save|apply|approve|reject|pay|payment|logout|reset|register|upload|send|cancel|generate)(?:$|[\W_])",
    re.I,
)
UNSAFE_CONTROL_TOKENS = frozenset(
    {
        "action",
        "cmd",
        "command",
        "execute",
        "method",
        "redirect",
        "returnurl",
        "url",
        "target",
        "add",
        "create",
        "delete",
        "remove",
        "edit",
        "update",
        "submit",
        "save",
        "apply",
        "approve",
        "reject",
        "pay",
        "payment",
        "logout",
        "reset",
        "register",
        "upload",
        "send",
        "cancel",
        "generate",
    }
)


def unsafe_control_name(name: str) -> bool:
    """Treat bracketed, percent-encoded and camelCase form/query names uniformly."""
    for _ in range(5):
        decoded = unquote(name)
        if decoded == name:
            break
        name = decoded
    split_camel = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    split_camel = re.sub(r"([A-Z])([A-Z][a-z])", r"\1 \2", split_camel)
    tokens = set(re.findall(r"[a-z0-9]+", split_camel.lower()))
    return bool(tokens & UNSAFE_CONTROL_TOKENS) or bool(UNSAFE_FILTER_NAME.search(name))


def safe_filter_name(name: str) -> bool:
    return bool(name) and not (SENSITIVE_FILTER_NAME.search(name) or unsafe_control_name(name))


def filter_input_type(element: Tag) -> FilterInputType | None:
    kind = attr(element, "type", "text").lower()
    if (
        kind not in {"text", "search", "date", "datetime-local", "number"}
        or not safe_filter_name(attr(element, "name"))
        or element.has_attr("readonly")
        or element.has_attr("disabled")
        or attr(element, "autocomplete").lower()
        in {"username", "email", "current-password", "new-password", "one-time-code"}
    ):
        return None
    return cast(FilterInputType, kind)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def attr(tag: Tag, name: str, default: str = "") -> str:
    value = tag.get(name, default)
    return " ".join(str(part) for part in value) if isinstance(value, list) else str(value)


def _cell_text(cell: Tag) -> str:
    """Keep line breaks meaningful without leaking script contents."""
    clone = BeautifulSoup(str(cell), "html.parser")
    for node in clone.select("script, style"):
        node.decompose()
    return clean_text(clone.get_text(" ", strip=True))


def _grid(table: Tag) -> tuple[list[list[str]], list[Tag]]:
    rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
    pending: dict[int, tuple[int, str]] = {}
    output: list[list[str]] = []
    for row in rows:
        filled: dict[int, str] = {column: value for column, (_, value) in pending.items()}
        next_pending = {
            column: (remaining - 1, value)
            for column, (remaining, value) in pending.items()
            if remaining > 1
        }
        column = 0
        for cell in row.find_all(["td", "th"], recursive=False):
            while column in filled:
                column += 1
            try:
                colspan = max(1, min(100, int(attr(cell, "colspan", "1"))))
                rowspan = max(1, min(1000, int(attr(cell, "rowspan", "1"))))
            except ValueError:
                colspan = rowspan = 1
            value = _cell_text(cell)
            for offset in range(colspan):
                filled[column + offset] = value
                if rowspan > 1:
                    next_pending[column + offset] = (rowspan - 1, value)
            column += colspan
        pending = next_pending
        width = max(filled, default=-1) + 1
        output.append([filled.get(index, "") for index in range(width)])
    width = max((len(row) for row in output), default=0)
    return [row + [""] * (width - len(row)) for row in output], rows


def parse_table(table: Tag) -> Table:
    grid, source_rows = _grid(table)
    if not grid:
        return Table(columns=[], rows=[])
    width = len(grid[0])
    head_count = 0
    for row in source_rows:
        if row.find_parent("thead") is not None or (
            row.find("th", recursive=False) is not None and row.find("td", recursive=False) is None
        ):
            head_count += 1
        else:
            break
    columns: list[str] = []
    used: dict[str, int] = {}
    occupied: set[str] = set()
    for column in range(width):
        parts: list[str] = []
        for header in grid[:head_count]:
            if header[column] and header[column] not in parts:
                parts.append(header[column])
        label = " / ".join(parts) or f"Column {column + 1}"
        used[label] = used.get(label, 0) + 1
        candidate = label if used[label] == 1 else f"{label} ({used[label]})"
        while candidate in occupied:
            used[label] += 1
            candidate = f"{label} ({used[label]})"
        occupied.add(candidate)
        columns.append(candidate)
    caption = table.find("caption", recursive=False)
    records = [dict(zip(columns, row, strict=True)) for row in grid[head_count:]]
    return Table(
        caption=clean_text(caption.get_text(" ", strip=True)) if caption else None,
        columns=columns,
        rows=records,
    )


def _label_for(root: Tag, element: Tag) -> str:
    element_id = attr(element, "id")
    label = root.find("label", attrs={"for": element_id}) if element_id else None
    if not isinstance(label, Tag):
        label = element.find_parent("label")
    return (
        clean_text(label.get_text(" ", strip=True))
        if isinstance(label, Tag)
        else attr(element, "name")
    )


def _add_field(fields: dict[str, str], label: str, value: str) -> None:
    """Disambiguate repeated labels without overwriting any displayed value."""
    candidate = label
    suffix = 2
    while candidate in fields:
        candidate = f"{label} ({suffix})"
        suffix += 1
    fields[candidate] = value


def parse_academic_page(
    html: str,
    *,
    register_link: LinkRegistrar | None = None,
    safe_form: FormValidator | None = None,
) -> AcademicPage:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("#content, main, .main-content, .content-wrapper, .page-content")
    if root is None:
        root = soup.body or soup
    for node in root.select("script, style, nav, aside, header, footer, .sidebar, .breadcrumb"):
        node.decompose()

    heading = root.find(["h1", "h2"])
    page_title = soup.title
    title = (
        clean_text(heading.get_text(" ", strip=True))
        if heading
        else (clean_text(page_title.get_text(" ", strip=True)) if page_title else "Student data")
    )
    fields: dict[str, str] = {}
    for definition_label in root.find_all("dt"):
        definition_value = definition_label.find_next_sibling("dd")
        if definition_value:
            key = clean_text(definition_label.get_text(" ", strip=True)).rstrip(":")
            if key:
                _add_field(fields, key, clean_text(definition_value.get_text(" ", strip=True)))
    for table in root.find_all("table"):
        if table.find_parent("table") is not None:
            continue
        # Yii detail-view profile tables use label/value rows rather than column headers.
        detail_view = "detail-view" in attr(table, "class")
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"], recursive=False)
            if len(cells) == 2 and (
                detail_view or (cells[0].name == "th" and cells[1].name == "td")
            ):
                key = _cell_text(cells[0]).rstrip(":")
                if key:
                    _add_field(fields, key, _cell_text(cells[1]))
    for element in root.select(
        "input[readonly], input[disabled], textarea[readonly], textarea[disabled]"
    ):
        if attr(element, "type").lower() in {"password", "hidden"}:
            continue
        if SENSITIVE_FILTER_NAME.search(attr(element, "name")):
            continue
        input_label = _label_for(root, element).rstrip(":")
        if input_label:
            input_value = (
                attr(element, "value")
                if element.name == "input"
                else clean_text(element.get_text(" ", strip=True))
            )
            _add_field(fields, input_label, input_value)

    tables = [
        parse_table(table) for table in root.find_all("table") if table.find_parent("table") is None
    ]
    filters: list[PageFilter] = []
    for element in root.select("select[name]"):
        if not safe_filter_name(attr(element, "name")) or element.has_attr("disabled"):
            continue
        form = element.find_parent("form")
        options = [
            FilterOption(
                value=attr(option, "value", option.get_text()),
                label=clean_text(option.get_text(" ", strip=True)),
            )
            for option in element.find_all("option")
            if not option.has_attr("disabled")
        ]
        selected = element.find("option", attrs={"selected": True})
        current = (
            attr(selected, "value", selected.get_text())
            if isinstance(selected, Tag)
            else (options[0].value if options else None)
        )
        filters.append(
            PageFilter(
                name=attr(element, "name"),
                label=_label_for(root, element),
                options=options,
                current=current,
                supported=isinstance(form, Tag) and safe_form is not None and safe_form(form),
            )
        )
    for element in root.select("input[name]"):
        input_type = filter_input_type(element)
        if input_type is None:
            continue
        form = element.find_parent("form")
        if not isinstance(form, Tag):
            continue
        filters.append(
            PageFilter(
                name=attr(element, "name"),
                label=_label_for(root, element),
                input_type=input_type,
                current=attr(element, "value"),
                supported=safe_form is not None and safe_form(form),
            )
        )
    links: list[Link] = []
    seen_links: set[str] = set()
    if register_link:
        for anchor in root.select("a[href]"):
            link = register_link(
                attr(anchor, "href"),
                clean_text(anchor.get_text(" ", strip=True)) or attr(anchor, "title") or "Open",
            )
            if link and link.id not in seen_links:
                links.append(link)
                seen_links.add(link.id)
    text: list[str] = []
    for element in root.find_all(["p", "li", "h3", "h4", "pre"]):
        if element.find_parent(["table", "form", "nav", "aside"]) is not None:
            continue
        value = clean_text(element.get_text(" ", strip=True))
        if value and value not in text:
            text.append(value)
    # A text-only portal notice can be a div rather than semantic paragraphs.
    if not tables and not fields and not text:
        clone = BeautifulSoup(str(root), "html.parser")
        for element in clone.select("form, h1, h2, a, button"):
            element.decompose()
        value = clean_text(clone.get_text(" ", strip=True))
        if value:
            text.append(value)
    marker = clean_text(root.get_text(" ", strip=True)).lower()
    empty = any(
        phrase in marker
        for phrase in (
            "no records found",
            "no results found",
            "no data available",
            "no items found",
            "nothing to display",
        )
    )
    return AcademicPage(
        title=title,
        fields=fields,
        tables=tables,
        text=text,
        links=links,
        filters=filters,
        empty=empty,
    )
