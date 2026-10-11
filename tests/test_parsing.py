from pathlib import Path

from bs4 import BeautifulSoup

from etlab_api.adapters.parsing import parse_academic_page, parse_table

FIXTURES = Path(__file__).parent / "fixtures"


def test_profile_preserves_labels_and_never_exports_script() -> None:
    page = parse_academic_page((FIXTURES / "profile.html").read_text())
    assert page.title == "Student profile"
    assert page.fields == {
        "Student name": "Example Student",
        "Admission number": "EX-0001",
        "Branch": "Computer Science",
        "Batch": "2025–2029",
        "University registration": "EXAMPLE001",
    }
    assert "not-exported" not in page.model_dump_json()


def test_duplicate_definition_labels_and_existing_suffix_preserve_all_values() -> None:
    page = parse_academic_page("""<main><h1>Academic record</h1><dl>
    <dt>Semester</dt><dd>I</dd>
    <dt>Semester (2)</dt><dd>Legacy semester label</dd>
    <dt>Semester</dt><dd>II</dd>
    <dt>Semester</dt><dd>III</dd>
    </dl></main>""")
    assert page.fields == {
        "Semester": "I",
        "Semester (2)": "Legacy semester label",
        "Semester (3)": "II",
        "Semester (4)": "III",
    }


def test_profile_table_and_readonly_duplicate_labels_never_replace_other_values() -> None:
    page = parse_academic_page("""<main><h1>Profile history</h1>
    <dl><dt>Branch</dt><dd>Computer Science</dd></dl>
    <table class="detail-view">
    <tr><th>Branch</th><td>Electronics</td></tr>
    <tr><th>Branch (2)</th><td>Legacy branch label</td></tr>
    <tr><th>Branch</th><td>Civil Engineering</td></tr>
    </table><label for="branch">Branch</label>
    <input id="branch" name="branch" value="Mechanical Engineering" readonly>
    </main>""")
    assert page.fields == {
        "Branch": "Computer Science",
        "Branch (2)": "Electronics",
        "Branch (2) (2)": "Legacy branch label",
        "Branch (3)": "Civil Engineering",
        "Branch (4)": "Mechanical Engineering",
    }


def test_rowspan_and_colspan_preserve_academic_values() -> None:
    page = parse_academic_page((FIXTURES / "attendance.html").read_text())
    table = page.tables[0]
    assert table.caption == "Attendance summary"
    assert table.columns == ["Subject", "Classes / Attended", "Classes / Conducted", "Percentage"]
    assert table.rows[0] == {
        "Subject": "CS201 — Algorithms",
        "Classes / Attended": "24",
        "Classes / Conducted": "30",
        "Percentage": "80.00%",
    }
    assert table.rows[1]["Percentage"] == "96.875%"
    assert page.filters[0].current == "2"
    assert page.filters[0].name == "StudentAttendance[semester]"
    assert not page.filters[0].supported


def test_body_rowspan_and_duplicate_headers_do_not_overwrite_cells() -> None:
    soup = BeautifulSoup(
        """<table><tr><th>Mark</th><th>Mark</th><th>Term</th></tr>
    <tr><td rowspan="2">A+</td><td>96</td><td>I</td></tr>
    <tr><td>97</td><td>II</td></tr></table>""",
        "html.parser",
    )
    assert soup.table is not None
    table = parse_table(soup.table)
    assert table.columns == ["Mark", "Mark (2)", "Term"]
    assert table.rows[1] == {"Mark": "A+", "Mark (2)": "97", "Term": "II"}


def test_generated_column_suffix_cannot_collide_with_a_literal_header() -> None:
    soup = BeautifulSoup(
        """<table><tr><th>Mark</th><th>Mark</th><th>Mark (2)</th></tr>
    <tr><td>A+</td><td>96</td><td>97</td></tr></table>""",
        "html.parser",
    )
    assert soup.table is not None
    table = parse_table(soup.table)
    assert len(set(table.columns)) == 3
    assert len(table.rows[0]) == 3
    assert list(table.rows[0].values()) == ["A+", "96", "97"]


def test_explicit_empty_state_is_not_missing_or_denied() -> None:
    page = parse_academic_page((FIXTURES / "empty.html").read_text())
    assert page.empty
    assert page.tables[0].columns == ["Title", "Date"]


def test_post_and_mutating_forms_are_not_implicitly_supported() -> None:
    page = parse_academic_page("""<main><h1>Survey</h1><form method="post" action="/student/submit">
    <label for="answer">Answer</label><select id="answer" name="answer"><option value="1">Yes</option></select>
    <input type="hidden" name="csrf" value="not-in-output"></form></main>""")
    assert not page.filters[0].supported
    assert "not-in-output" not in page.model_dump_json()


def test_plain_text_notices_and_headerless_tables_are_readable() -> None:
    page = parse_academic_page(
        "<main><h1>Notice</h1><div>Exam registration opens on Monday.</div></main>"
    )
    assert page.text == ["Exam registration opens on Monday."]
    table_page = parse_academic_page(
        "<main><h1>Marks</h1><table><tr><td>CS201</td><td>A+</td></tr></table></main>"
    )
    assert table_page.tables[0].rows == [{"Column 1": "CS201", "Column 2": "A+"}]


def test_discovered_input_filters_have_types_current_values_and_no_hidden_secrets() -> None:
    page = parse_academic_page((FIXTURES / "date_filters.html").read_text())
    assert [item.input_type for item in page.filters] == [
        "date",
        "date",
        "search",
        "text",
        "number",
        "datetime-local",
    ]
    assert page.filters[0].name == "Attendance[from]"
    assert page.filters[0].current == "2026-09-01"
    assert all(item.options == [] for item in page.filters)
    assert "synthetic-csrf-value" not in page.model_dump_json()


def test_credentials_actions_and_readonly_tokens_are_not_public_filters_or_fields() -> None:
    page = parse_academic_page("""<main><h1>Filter</h1><form method="get" action="/student/attendance">
    <input name="username" value="private-user"><input name="password" type="password" value="private-password">
    <input name="action" value="delete"><input name="csrf_token" value="private-token" readonly>
    <select name="command"><option value="delete">Delete</option></select>
    <input name="search" value="Algorithms"></form></main>""")
    assert [item.name for item in page.filters] == ["search"]
    assert not page.fields
    assert "private-" not in page.model_dump_json()
