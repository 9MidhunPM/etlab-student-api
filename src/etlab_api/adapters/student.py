"""A discovered-link adapter; never act as an arbitrary upstream request proxy."""

import re
import secrets
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from email.message import Message
from hashlib import sha256
from typing import Protocol, cast
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, Tag

from etlab_api.adapters.parsing import (
    SENSITIVE_FILTER_NAME,
    FilterInputType,
    attr,
    clean_text,
    filter_input_type,
    parse_academic_page,
    safe_filter_name,
    unsafe_control_name,
)
from etlab_api.errors import EtlabError
from etlab_api.models import RESOURCES, AcademicPage, Capabilities, Capability, Download, Link


class PortalClient(Protocol):
    base_url: str

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        data: dict[str, str] | None = None,
        attachment: bool = False,
        redirect_validator: Callable[[str, str], None] | None = None,
    ) -> httpx.Response: ...

    async def close(self) -> None: ...


# Routes identify academic reads, independently of an anchor's attractive label.
# A new route must be explicitly reviewed before extending this allowlist.
READ_ACTIONS: dict[str, frozenset[str]] = {
    "profile": frozenset(
        {"profile", "viewprofile", "studentprofile", "personaldetails", "viewstudent"}
    ),
    "dashboard": frozenset({"index", "dashboard", "home"}),
    "semesters": frozenset({"semesters", "semester", "academic", "academics", "academicdetails"}),
    "subjects": frozenset(
        {"subjects", "subject", "courses", "viewsubject", "subjectdetails", "subjectlist"}
    ),
    "timetable": frozenset({"timetable", "viewtimetable", "classtimetable"}),
    "attendance": frozenset(
        {
            "attendance",
            "viewattendance",
            "attendancelist",
            "viewattendancesubject",
            "viewattendancesubjectdutyleave",
            "attendancesubject",
            "subjectattendance",
            "attendancepercentage",
        }
    ),
    "results": frozenset(
        {"results", "result", "viewresult", "examresults", "grades", "marklist", "semesterresult"}
    ),
    "assessments": frozenset(
        {
            "assessments",
            "assessment",
            "internalmarks",
            "viewinternal",
            "internalmark",
            "sessionalmarks",
            "marks",
            "exam",
            "exams",
            "viewmarks",
            "viewassessment",
        }
    ),
    "assignments": frozenset(
        {"assignments", "assignment", "viewassignment", "assignmentlist", "assignmentstatus"}
    ),
    "materials": frozenset(
        {
            "materials",
            "studymaterials",
            "studymaterial",
            "coursematerials",
            "viewmaterial",
            "viewstudymaterial",
            "material",
        }
    ),
    "notices": frozenset(
        {
            "notices",
            "notice",
            "notifications",
            "notification",
            "circulars",
            "circular",
            "viewnotice",
            "viewnotification",
            "viewcircular",
            "news",
        }
    ),
    "calendar": frozenset({"calendar", "academiccalendar", "events", "event", "viewevent"}),
    "fees": frozenset(
        {
            "fees",
            "fee",
            "feedetails",
            "dues",
            "duelist",
            "feelist",
            "viewfees",
            "viewfee",
            "feestatus",
        }
    ),
    "receipts": frozenset({"receipts", "receipt", "feereceipt", "viewreceipt", "paymenthistory"}),
    "leave": frozenset(
        {"leave", "leavestatus", "leavelist", "viewleave", "leavehistory", "leaveapplications"}
    ),
    "library": frozenset(
        {"library", "librarybooks", "issuedbooks", "bookhistory", "librarydues", "books", "book"}
    ),
    "placement": frozenset(
        {"placement", "placements", "placementdetails", "placementstatus", "viewplacement"}
    ),
    "surveys": frozenset({"surveys", "survey", "surveylist", "viewsurvey", "feedbackstatus"}),
    "documents": frozenset(
        {
            "documents",
            "document",
            "viewdocument",
            "certificates",
            "certificate",
            "academicdocuments",
        }
    ),
}
DOWNLOAD_ACTIONS = frozenset(
    {
        "download",
        "downloadfile",
        "downloadmaterial",
        "downloadstudymaterial",
        "downloadassignment",
        "downloadreceipt",
        "downloadcertificate",
        "printreceipt",
        "printcertificate",
    }
)
MAX_DISCOVERED_LINKS = 1024
FILE_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".txt",
        ".csv",
        ".png",
        ".jpg",
        ".jpeg",
        ".zip",
    }
)
NAMESPACES = frozenset(
    {"student", "ktuacademics", "academics", "academic", "library", "placement", "fees", "hostel"}
)
UNSAFE_WORDS = re.compile(
    r"(?:^|[^a-z])(?:add|create|delete|remove|edit|update|submit|save|apply|approve|reject|pay|payment|logout|reset|register|upload|send|cancel|generate)(?:$|[^a-z])",
    re.I,
)
FORBIDDEN_QUERY = frozenset(
    {
        "action",
        "cmd",
        "command",
        "execute",
        "_method",
        "redirect",
        "redirect_uri",
        "returnurl",
        "return_url",
        "url",
        "target",
    }
)


@dataclass(frozen=True)
class DiscoveredLink:
    url: str
    resource: str
    kind: str


@dataclass(frozen=True)
class ReadForm:
    action: str
    options: Mapping[str, frozenset[str]]
    inputs: Mapping[str, "ReadInput"]
    hidden: Mapping[str, str]
    defaults: Mapping[str, str]


@dataclass(frozen=True)
class ReadInput:
    input_type: FilterInputType
    max_length: int
    minimum: str
    maximum: str


class StudentAdapter:
    """Read authenticated student pages using only reviewed, discovered destinations."""

    def __init__(self, client: PortalClient) -> None:
        self.client = client
        self.base_url = str(client.base_url).rstrip("/") + "/"
        self._routes: dict[str, str] = {}
        self._links: OrderedDict[str, DiscoveredLink] = OrderedDict()
        self._link_ids: dict[tuple[str, str, str], str] = {}
        self._authenticated = False
        self._identity: str | None = None

    def _same_origin(self, value: str, page_url: str) -> str | None:
        if not value or value.startswith("#"):
            return None
        if any(character in value for character in ("\\", "\r", "\n", "\x00")):
            return None
        try:
            resolved = urlsplit(urljoin(page_url, value))
            base = urlsplit(self.base_url)
            if resolved.username or resolved.password:
                return None
            matches_origin = (
                resolved.scheme,
                resolved.hostname,
                resolved.port or (443 if resolved.scheme == "https" else 80),
            ) == (
                base.scheme,
                base.hostname,
                base.port or (443 if base.scheme == "https" else 80),
            )
        except ValueError:
            return None
        if not matches_origin:
            return None
        if any(
            key.lower() in FORBIDDEN_QUERY or unsafe_control_name(key)
            for key, _ in parse_qsl(resolved.query)
        ):
            return None
        return urlunsplit((resolved.scheme, resolved.netloc, resolved.path, resolved.query, ""))

    @staticmethod
    def _is_login_url(url: str) -> bool:
        return bool(
            re.fullmatch(
                r"/(?:index\.php/)?(?:(?:user|site|student)/login/?)?", urlsplit(url).path, re.I
            )
        )

    def _login_redirect(self, url: str, method: str) -> None:
        safe = self._same_origin(url, self.base_url)
        if safe and (
            (method == "GET" and (self._is_login_url(safe) or self._resource(safe) is not None))
            or (method == "POST" and self._is_login_url(safe))
        ):
            return
        raise EtlabError(
            "operation_denied", "The portal redirected outside reviewed login/read operations.", 405
        )

    def _read_redirect(self, url: str, method: str) -> None:
        safe = self._same_origin(url, self.base_url)
        if (
            method == "GET"
            and safe
            and (self._is_login_url(safe) or self._resource(safe) is not None)
        ):
            return
        raise EtlabError(
            "operation_denied",
            "The portal redirected outside reviewed student read operations.",
            405,
        )

    def _attachment_redirect(self, url: str, method: str) -> None:
        safe = self._same_origin(url, self.base_url)
        if method == "GET" and (
            (safe and self._is_login_url(safe)) or self._attachment_url(url, self.base_url)
        ):
            return
        raise EtlabError(
            "operation_denied", "The portal redirected outside reviewed attachment reads.", 405
        )

    def _action(self, url: str) -> str | None:
        path = urlsplit(url).path
        for _ in range(3):
            decoded = unquote(path)
            if path == decoded:
                break
            path = decoded
        segments = [segment.lower() for segment in path.strip("/").split("/") if segment]
        if not segments or any(segment in {".", ".."} for segment in segments):
            return None
        if segments[0] == "index.php":
            segments = segments[1:]
        if not segments or segments[0] not in NAMESPACES:
            return None
        if segments[0] != "student" and len(segments) > 1 and segments[1] == "student":
            segments = segments[1:]
        if len(segments) < 2:
            return None
        # Extra path parts are opaque identifiers, not further controllers/actions.
        if len(segments) > 3 or (
            len(segments) == 3 and not re.fullmatch(r"[a-z0-9_.-]+", segments[2])
        ):
            return None
        return segments[1].replace("-", "").replace("_", "")

    def _resource(self, url: str) -> str | None:
        action = self._action(url)
        return next(
            (resource for resource, actions in READ_ACTIONS.items() if action in actions), None
        )

    def _attachment(self, url: str) -> bool:
        action = self._action(url)
        if action in DOWNLOAD_ACTIONS:
            return True
        path = unquote(urlsplit(url).path).lower()
        if not path.startswith(
            ("/uploads/", "/upload/", "/files/", "/documents/", "/attachments/")
        ):
            return False
        return any(
            path.endswith(extension) for extension in FILE_EXTENSIONS
        ) and ".." not in path.split("/")

    def _discover(self, html: str, page_url: str) -> None:
        soup = BeautifulSoup(html, "html.parser")
        for anchor in soup.select("a[href]"):
            url = self._same_origin(attr(anchor, "href"), page_url)
            if url and (resource := self._resource(url)):
                self._routes.setdefault(resource, url)

    @staticmethod
    def _login_form(soup: BeautifulSoup) -> Tag | None:
        password = soup.find("input", attrs={"type": re.compile(r"^password$", re.I)})
        return password.find_parent("form") if isinstance(password, Tag) else None

    @staticmethod
    def _login_submit_control(form: Tag) -> Tag | None:
        submitters = [
            element
            for element in form.find_all(["button", "input"])
            if attr(element, "type", "submit" if element.name == "button" else "text").lower()
            == "submit"
            and not element.has_attr("disabled")
            and element.find_parent("fieldset", attrs={"disabled": True}) is None
        ]
        if len(submitters) <= 1:
            return submitters[0] if submitters else None
        login_submitters = [
            element
            for element in submitters
            if re.search(
                r"\b(?:login|log\s+in|sign\s+in)\b",
                clean_text(element.get_text(" ", strip=True)) or attr(element, "value"),
                re.I,
            )
        ]
        if len(login_submitters) != 1:
            raise EtlabError(
                "unsupported_layout", "The portal login submit control is ambiguous.", 502
            )
        return login_submitters[0]

    @staticmethod
    def _challenge(soup: BeautifulSoup) -> bool:
        return bool(
            soup.select(
                ".g-recaptcha, .h-captcha, input[name*=captcha], input[name*=otp], input[name*=totp], input[autocomplete=one-time-code]"
            )
        ) or bool(
            re.search(
                r"(?:complete the captcha|two.factor authentication|verification code|enter.*one.time password)",
                clean_text(soup.get_text(" ", strip=True)),
                re.I,
            )
        )

    def _check_response(self, response: httpx.Response, *, authentication: bool = False) -> str:
        if response.status_code in {401, 403}:
            raise EtlabError("upstream_denied", "The portal denied access to this page.", 403)
        if response.status_code >= 400:
            raise EtlabError("upstream_unavailable", "The portal could not provide this page.", 502)
        content_type = response.headers.get("content-type", "text/html").lower()
        if "html" not in content_type:
            raise EtlabError(
                "unsupported_layout", "The portal returned an unsupported page format.", 502
            )
        soup = BeautifulSoup(response.text, "html.parser")
        if self._challenge(soup):
            raise EtlabError(
                "authentication_challenge",
                "The portal requires a CAPTCHA or interactive verification.",
                409,
            )
        if not authentication and self._login_form(soup) is not None:
            self._authenticated = False
            raise EtlabError("auth_expired", "The portal session expired; authenticate again.", 401)
        text = clean_text(soup.get_text(" ", strip=True)).lower()
        if any(
            marker in text
            for marker in (
                "you are not authorized",
                "access denied",
                "permission denied",
                "you do not have permission",
            )
        ):
            raise EtlabError("upstream_denied", "The portal denied access to this page.", 403)
        return response.text

    async def login(self, username: str, password: str) -> None:
        self._authenticated = False
        response = await self.client.fetch(self.base_url, redirect_validator=self._login_redirect)
        html = self._check_response(response, authentication=True)
        soup = BeautifulSoup(html, "html.parser")
        form = self._login_form(soup)
        page_url = str(response.url)
        if form is None:
            # Only follow a visible, same-origin login link; never guess a credential endpoint.
            for anchor in soup.select("a[href]"):
                link = self._same_origin(attr(anchor, "href"), page_url)
                if link and re.fullmatch(
                    r"/(?:index\.php/)?(?:user|site|student)/login/?", urlsplit(link).path, re.I
                ):
                    response = await self.client.fetch(
                        link, redirect_validator=self._login_redirect
                    )
                    html = self._check_response(response, authentication=True)
                    soup = BeautifulSoup(html, "html.parser")
                    form = self._login_form(soup)
                    page_url = str(response.url)
                    break
        if form is None:
            raise EtlabError(
                "unsupported_layout", "A supported portal login form was not found.", 502
            )
        action = self._same_origin(attr(form, "action") or page_url, page_url)
        if action is None or not re.fullmatch(
            r"/(?:index\.php/)?(?:(?:user|site|student)/login/?)?", urlsplit(action).path, re.I
        ):
            raise EtlabError(
                "unsupported_layout", "The portal login destination is not supported.", 502
            )
        if attr(form, "method", "get").lower() != "post":
            raise EtlabError("unsupported_layout", "The portal login form must use POST.", 502)
        password_input = form.find("input", attrs={"type": re.compile(r"^password$", re.I)})
        user_inputs = [
            element
            for element in form.find_all("input")
            if attr(element, "type", "text").lower() in {"text", "email"} and attr(element, "name")
        ]
        username_input = next(
            (
                element
                for element in user_inputs
                if re.search(r"user|login|email", attr(element, "name"), re.I)
            ),
            user_inputs[0] if user_inputs else None,
        )
        if (
            not isinstance(password_input, Tag)
            or not attr(password_input, "name")
            or username_input is None
        ):
            raise EtlabError(
                "unsupported_layout", "The portal credential fields could not be identified.", 502
            )
        data = {
            attr(element, "name"): attr(element, "value")
            for element in form.select("input[type=hidden][name]")
            if not element.has_attr("disabled")
        }
        data[attr(username_input, "name")] = username
        data[attr(password_input, "name")] = password
        submitter = self._login_submit_control(form)
        if submitter is not None:
            # HTML buttons have an empty successful-control value when no value is set.
            submit_name = attr(submitter, "name")
            if submit_name:
                if submit_name in {attr(username_input, "name"), attr(password_input, "name")}:
                    raise EtlabError(
                        "unsupported_layout",
                        "The portal submit control conflicts with a credential field.",
                        502,
                    )
                submit_value = attr(submitter, "value")
                if submit_name in data and data[submit_name] != submit_value:
                    raise EtlabError(
                        "unsupported_layout",
                        "The portal submit control conflicts with a hidden field.",
                        502,
                    )
                data[submit_name] = submit_value
            # An observed submitter may override the form; reject unsupported overrides.
            effective_action = self._same_origin(attr(submitter, "formaction") or action, page_url)
            effective_method = attr(submitter, "formmethod") or "post"
            if (
                effective_action is None
                or not self._is_login_url(effective_action)
                or effective_method.lower() != "post"
            ):
                raise EtlabError(
                    "unsupported_layout",
                    "The portal login submit destination is not supported.",
                    502,
                )
            action = effective_action
        result = await self.client.fetch(
            action, method="POST", data=data, redirect_validator=self._login_redirect
        )
        html = self._check_response(result, authentication=True)
        result_soup = BeautifulSoup(html, "html.parser")
        if self._login_form(result_soup) is not None:
            raise EtlabError(
                "invalid_credentials", "The portal did not accept the supplied credentials.", 401
            )
        self._routes.clear()
        self._discover(html, str(result.url))
        has_logout = any(
            "logout" in attr(anchor, "href").lower() for anchor in result_soup.select("a[href]")
        )
        if not self._routes and not has_logout:
            raise EtlabError(
                "unsupported_layout",
                "The portal did not return a recognizable student session.",
                502,
            )
        identity = sha256(username.encode("utf-8")).hexdigest()
        if self._identity != identity:
            self._links.clear()
            self._link_ids.clear()
        self._identity = identity
        self._authenticated = True
        landing_resource = self._resource(str(result.url))
        if landing_resource:
            self._routes.setdefault(landing_resource, str(result.url))

    def _ensure_session(self) -> None:
        if not self._authenticated:
            raise EtlabError(
                "auth_expired", "Authenticate with the portal before reading student data.", 401
            )

    async def capabilities(self) -> Capabilities:
        self._ensure_session()
        return Capabilities(
            institution=self.base_url.rstrip("/"),
            adapter="generic-student-html",
            adapter_version="0.1.0",
            verification="fixture_tested",
            features=[
                Capability(
                    resource=resource,
                    available=resource in self._routes,
                    status="discovered" if resource in self._routes else "unavailable",
                    reason=None
                    if resource in self._routes
                    else "No reviewed student read route was discovered in this session.",
                )
                for resource in RESOURCES
            ],
        )

    def _register_link(self, resource: str, page_url: str, href: str, label: str) -> Link | None:
        url = self._same_origin(href, page_url)
        attachment_url = self._attachment_url(href, page_url)
        discovered_resource = self._resource(url) if url else None
        if attachment_url:
            kind = "attachment"
            url = attachment_url
        elif discovered_resource == resource:
            kind = "detail"
        else:
            return None
        assert url is not None
        key = (resource, url, kind)
        token = self._link_ids.get(key)
        if token is None:
            token = secrets.token_urlsafe(18)
            self._link_ids[key] = token
            self._links[token] = DiscoveredLink(url=url, resource=resource, kind=kind)
            if len(self._links) > MAX_DISCOVERED_LINKS:
                _, evicted = self._links.popitem(last=False)
                self._link_ids.pop((evicted.resource, evicted.url, evicted.kind), None)
        else:
            self._links.move_to_end(token)
        return Link(id=token, label=label, kind=kind)

    def _attachment_url(self, href: str, page_url: str) -> str | None:
        same_origin = self._same_origin(href, page_url)
        if same_origin:
            return same_origin if self._attachment(same_origin) else None
        # External file hosts require the operator's explicit policy validator.
        validator = getattr(self.client, "validate_attachment_url", None)
        if (
            not callable(validator)
            or not href
            or href.startswith("#")
            or any(character in href for character in ("\\", "\r", "\n", "\x00"))
        ):
            return None
        try:
            candidate = urljoin(page_url, href)
            parsed = urlsplit(candidate)
            if parsed.scheme != "https" or parsed.username or parsed.password:
                return None
            path = unquote(parsed.path).lower()
            if ".." in path.split("/") or not any(
                path.endswith(extension) for extension in FILE_EXTENSIONS
            ):
                return None
            if any(
                key.lower() in FORBIDDEN_QUERY or unsafe_control_name(key)
                for key, _ in parse_qsl(parsed.query)
            ):
                return None
            return cast(Callable[[str], str], validator)(
                urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
            )
        except (EtlabError, ValueError):
            return None

    def _read_form(self, form: Tag, page_url: str, resource: str) -> ReadForm | None:
        if attr(form, "method", "get").lower() != "get":
            return None
        action = self._same_origin(attr(form, "action") or page_url, page_url)
        if action is None or self._resource(action) != resource:
            return None
        # Hidden CSRF fields are safe to preserve, but do not authorize mutation controls.
        for element in form.select("input[name], button[name], select[name], textarea[name]"):
            name = attr(element, "name")
            if unsafe_control_name(name) or name.lower() in FORBIDDEN_QUERY:
                return None
            if element.name != "input" or attr(element, "type", "text").lower() != "hidden":
                if (
                    SENSITIVE_FILTER_NAME.search(name)
                    or attr(element, "type").lower() == "password"
                ):
                    return None
        options = {
            attr(select, "name"): frozenset(
                attr(option, "value", option.get_text())
                for option in select.find_all("option")
                if not option.has_attr("disabled")
            )
            for select in form.select("select[name]")
            if safe_filter_name(attr(select, "name")) and not select.has_attr("disabled")
        }
        inputs: dict[str, ReadInput] = {}
        defaults: dict[str, str] = {}
        for select in form.select("select[name]"):
            name = attr(select, "name")
            if name not in options:
                continue
            selected = select.find("option", attrs={"selected": True}) or select.find("option")
            if isinstance(selected, Tag):
                defaults[name] = attr(selected, "value", selected.get_text())
        for element in form.select("input[name]"):
            kind = filter_input_type(element)
            if kind is None:
                continue
            name = attr(element, "name")
            try:
                max_length = max(0, min(512, int(attr(element, "maxlength", "512"))))
            except ValueError:
                max_length = 512
            inputs[name] = ReadInput(
                input_type=kind,
                max_length=max_length,
                minimum=attr(element, "min"),
                maximum=attr(element, "max"),
            )
            defaults[name] = attr(element, "value")
        hidden = {
            attr(element, "name"): attr(element, "value")
            for element in form.select("input[type=hidden][name]")
        }
        return ReadForm(
            action=action, options=options, inputs=inputs, hidden=hidden, defaults=defaults
        )

    @staticmethod
    def _valid_filter(form: ReadForm, name: str, value: str) -> bool:
        if name in form.options:
            return value in form.options[name]
        control = form.inputs.get(name)
        if (
            control is None
            or len(value) > control.max_length
            or re.search(r"[\x00-\x1f\x7f]", value)
        ):
            return False
        if value == "":
            return True
        try:
            if control.input_type == "date":
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    return False
                date.fromisoformat(value)
                return not (
                    (control.minimum and value < control.minimum)
                    or (control.maximum and value > control.maximum)
                )
            if control.input_type == "datetime-local":
                if not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?", value
                ):
                    return False
                datetime.fromisoformat(value)
                return not (
                    (control.minimum and value < control.minimum)
                    or (control.maximum and value > control.maximum)
                )
            if control.input_type == "number":
                if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value):
                    return False
                number = Decimal(value)
                return number.is_finite() and not (
                    (control.minimum and number < Decimal(control.minimum))
                    or (control.maximum and number > Decimal(control.maximum))
                )
        except (ValueError, InvalidOperation):
            return False
        return True

    def _parse(self, html: str, page_url: str, resource: str) -> AcademicPage:
        page = parse_academic_page(
            html,
            register_link=lambda href, label: self._register_link(resource, page_url, href, label),
            safe_form=lambda form: self._read_form(form, page_url, resource) is not None,
        )
        # A very large page may exceed the session registry; advertise only retained IDs.
        page.links = [link for link in page.links if link.id in self._links]
        if not (
            page.fields or page.tables or page.text or page.links or page.filters or page.empty
        ):
            raise EtlabError(
                "unsupported_layout", "The portal page has no recognizable academic content.", 502
            )
        return page

    async def read(
        self,
        resource: str,
        filters: dict[str, str] | None = None,
        detail_id: str | None = None,
    ) -> AcademicPage:
        self._ensure_session()
        if resource not in RESOURCES:
            raise EtlabError("unsupported_resource", "This student resource is not supported.", 404)
        if detail_id is not None:
            link = self._links.get(detail_id)
            if link is None or link.resource != resource or link.kind != "detail":
                raise EtlabError(
                    "not_found",
                    "The requested detail link does not belong to this resource and session.",
                    404,
                )
            url = link.url
            self._links.move_to_end(detail_id)
        elif resource in self._routes:
            url = self._routes[resource]
        else:
            raise EtlabError(
                "unsupported_resource",
                "This resource was not discovered for this student account.",
                404,
            )
        response = await self.client.fetch(url, redirect_validator=self._read_redirect)
        html = self._check_response(response)
        page_url = str(response.url)
        if filters:
            soup = BeautifulSoup(html, "html.parser")
            forms = [
                read_form
                for form in soup.find_all("form")
                if (read_form := self._read_form(form, page_url, resource)) is not None
            ]
            matched = next(
                (
                    form
                    for form in forms
                    if set(filters).issubset(set(form.options) | set(form.inputs))
                ),
                None,
            )
            if matched is None or any(
                not self._valid_filter(matched, name, value) for name, value in filters.items()
            ):
                raise EtlabError(
                    "invalid_filter",
                    "Filters must use a discovered safe form, offered options, and valid input values.",
                    422,
                )
            params = dict(parse_qsl(urlsplit(matched.action).query, keep_blank_values=True))
            params.update(matched.hidden)
            params.update(matched.defaults)
            params.update(filters)
            parsed = urlsplit(matched.action)
            filtered_url = urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, urlencode(params), "")
            )
            response = await self.client.fetch(filtered_url, redirect_validator=self._read_redirect)
            html = self._check_response(response)
            page_url = str(response.url)
        self._discover(html, page_url)
        return self._parse(html, page_url, resource)

    async def download(self, attachment_id: str) -> Download:
        self._ensure_session()
        link = self._links.get(attachment_id)
        if link is None or link.kind != "attachment":
            raise EtlabError(
                "not_found", "The attachment does not belong to this student session.", 404
            )
        self._links.move_to_end(attachment_id)
        response = await self.client.fetch(
            link.url, attachment=True, redirect_validator=self._attachment_redirect
        )
        if response.status_code in {401, 403}:
            raise EtlabError("upstream_denied", "The portal denied access to this attachment.", 403)
        if response.status_code >= 400:
            raise EtlabError(
                "upstream_unavailable", "The portal could not provide this attachment.", 502
            )
        content_type = (
            response.headers.get("content-type", "application/octet-stream")
            .split(";", 1)[0]
            .strip()
        )
        if content_type.lower() in {"text/html", "application/xhtml+xml"}:
            self._check_response(response)
            raise EtlabError(
                "unsupported_layout", "The portal returned a page instead of an attachment.", 502
            )
        disposition = Message()
        disposition["content-disposition"] = response.headers.get("content-disposition", "")
        filename = (
            disposition.get_filename()
            or unquote(urlsplit(link.url).path.rsplit("/", 1)[-1])
            or "attachment"
        )
        filename = re.sub(r"[\\/\x00-\x1f\x7f]", "_", filename)[:255]
        return Download(filename=filename, content_type=content_type, content=response.content)

    async def close(self) -> None:
        self._authenticated = False
        self._routes.clear()
        self._links.clear()
        self._link_ids.clear()
        self._identity = None
        await self.client.close()
