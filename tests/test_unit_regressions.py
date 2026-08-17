"""Non-Django unit regressions from the 2026-08 audit."""
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# BUG-002 — the Streamlit app imported an undeclared dependency
# ---------------------------------------------------------------------------
def _declared_requirements():
    text = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    names = set()
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if line:
            names.add(line.split(">")[0].split("<")[0].split("=")[0].strip().lower())
    return names


def test_reminders_does_not_use_an_undeclared_scheduler():
    source = (REPO_ROOT / "shared" / "reminders.py").read_text(encoding="utf-8")
    assert "import schedule" not in source
    assert "threading" not in source, "background threads cannot drive the Streamlit UI"


def test_every_third_party_frontend_import_is_declared():
    declared = _declared_requirements()
    for name in ("streamlit", "requests", "plotly", "python-dotenv"):
        assert name in declared, f"{name} is imported but missing from requirements.txt"


@pytest.mark.parametrize("package,module", [
    ("openai", "openai"),
    ("google-generativeai", "google.generativeai"),
])
def test_retired_provider_sdks_are_neither_imported_nor_declared(package, module):
    """Task 03 reaches every LLM provider over its HTTP API.

    ``google-generativeai`` is end of life and pulled Streamlit into the
    provider layer; the ``openai`` SDK was used for a single POST. Keeping
    either declared would reintroduce a dependency nothing imports.
    """
    assert package not in _declared_requirements(), \
        f"{package} is declared but no longer used"

    # Word-boundary anchored: `from .providers import openai_compat` is our own
    # adapter, not the retired SDK.
    imports = re.compile(rf"^\s*(?:import|from)\s+{re.escape(module)}\b",
                         re.MULTILINE)
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in {".venv", ".conda", "__pycache__", "node_modules"}
               for part in path.parts):
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        assert not imports.search(source), f"{path} still imports {module}"


# ---------------------------------------------------------------------------
# BUG-004 — inline comments silently disabled .gitignore patterns
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["db.sqlite3", "data_dump.json",
                                  "chat_history.jsonl", ".env"])
def test_sensitive_paths_are_actually_ignored(path):
    result = subprocess.run(["git", "check-ignore", "-q", path],
                            cwd=REPO_ROOT, capture_output=True)
    assert result.returncode == 0, f"{path} is NOT ignored by git"


def test_gitignore_has_no_inline_comments():
    for i, line in enumerate((REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            assert "#" not in stripped, (
                f".gitignore line {i} has a trailing comment, which becomes part "
                f"of the pattern and disables it: {line!r}")


# ---------------------------------------------------------------------------
# BUG-003 — a real credential lived in the committed .env.example
# ---------------------------------------------------------------------------
def test_env_example_contains_no_real_looking_secret():
    import re
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if not value or not any(t in key.upper() for t in ("KEY", "SECRET", "PASSWORD", "TOKEN")):
            continue
        assert not re.fullmatch(r"sk-[A-Za-z0-9_\-]{20,}", value), (
            f"{key.strip()} looks like a live API key")
        assert not re.fullmatch(r"AIza[A-Za-z0-9_\-]{30,}", value), (
            f"{key.strip()} looks like a live Google API key")


# ---------------------------------------------------------------------------
# PostgreSQL is the only backend.
#
# These three tests replace the previous DB_ENGINE guards. Those existed because
# SQLite was a *silent fallback*: omitting DB_ENGINE meant the app quietly used
# a local file even when DB_HOST pointed at a real server, so the tests checked
# that the selector was present in .env.example and docker-compose.
#
# There is no selector and no fallback any more, so asserting the old mechanism
# is present would assert a regression. What must hold now is the opposite: that
# nothing can route the application at SQLite.
# ---------------------------------------------------------------------------
def test_postgres_is_the_only_configured_backend():
    from django.conf import settings

    engines = {db["ENGINE"] for db in settings.DATABASES.values()}
    assert engines == {"django.db.backends.postgresql"}, (
        f"expected PostgreSQL only, found {engines}")


def test_settings_contain_no_sqlite_backend():
    """A fallback cannot be reintroduced by an environment variable if the
    backend string does not appear in the settings at all."""
    text = (REPO_ROOT / "config" / "settings.py").read_text(encoding="utf-8")
    assert "sqlite3" not in text
    assert "DB_ENGINE" not in text, (
        "DB_ENGINE selected between SQLite and PostgreSQL; it must not return")


def test_the_suite_itself_runs_on_postgres():
    """The tests used to force SQLite, so they never exercised the engine the
    application actually runs on."""
    from django.db import connection

    assert connection.vendor == "postgresql"


def test_env_example_documents_postgres_credentials():
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT"):
        assert key in text, f"{key} is not documented for operators"
    assert "DB_ENGINE" not in text


def test_compose_configures_postgres_for_the_api_service():
    text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "postgres" in text
    assert "DB_HOST=db" in text
    assert "DB_ENGINE" not in text


# ---------------------------------------------------------------------------
# P0-1 — chat history must never go back to a shared flat file
#
# (This supersedes the old BUG-025 tests, which asserted the JSONL store used an
# absolute path and tz-aware timestamps. That store is gone: history now lives
# in the database, keyed to a user, so both concerns are structural.)
# ---------------------------------------------------------------------------
def test_shared_chat_history_file_store_is_gone():
    assert not (REPO_ROOT / "chat_history_store.py").exists(), (
        "the shared-file chat store exposed every user's medical questions to "
        "every other user and must not come back")


def test_frontend_history_module_goes_through_the_api():
    """Checks executable code, not comments — the docstring may still describe
    the old file-based store it replaced."""
    import ast
    source = (REPO_ROOT / "shared" / "history.py").read_text(encoding="utf-8")
    assert "api_request" in source

    tree = ast.parse(source)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "open" not in called, "history must not touch the local filesystem"


# ---------------------------------------------------------------------------
# Sidebar navigation config — guards the routing contract
# ---------------------------------------------------------------------------
def _section_items(var_name):
    """[(section_title, [(label, icon)])] for a *_NAV_SECTIONS assignment.

    Parsed from source rather than imported: importing streamlit_app.py would
    execute the whole app.
    """
    import ast
    tree = ast.parse((REPO_ROOT / "streamlit_app.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == var_name):
            out = []
            for section in node.value.elts:
                title = section.elts[0].value
                items = [(i.elts[0].value, i.elts[1].value) for i in section.elts[1].elts]
                out.append((title, items))
            return out
    raise AssertionError(f"{var_name} not found")


#: One per role. The router keys pages by label and Streamlit keys the sidebar
#: buttons by label too, so a duplicate inside a portal silently shadows a page.
NAV_VARS = ["PATIENT_NAV_SECTIONS", "DOCTOR_NAV_SECTIONS",
            "LABORATORY_NAV_SECTIONS", "RADIOLOGY_NAV_SECTIONS",
            "PHARMACY_NAV_SECTIONS", "ADMIN_NAV_SECTIONS"]


def test_there_is_one_navigation_per_role():
    """Six roles, six portals — a role with no nav falls back to the patient
    sidebar, which would be the wrong pages rather than an obvious failure."""
    from accounts import roles
    assert len(NAV_VARS) == len(roles.ALL_ROLES)
    for var in NAV_VARS:
        assert _section_items(var), f"{var} is missing or empty"


@pytest.mark.parametrize("var", NAV_VARS)
def test_nav_sections_are_well_formed(var):
    sections = _section_items(var)
    assert sections, f"{var} is empty"
    labels = [label for _t, items in sections for label, _i in items]
    assert len(labels) == len(set(labels)), (
        f"{var} has duplicate labels; the router keys pages by label")
    assert labels[0] == "Dashboard", "the first item must be the default landing page"
    for title, items in sections:
        assert title and items, f"empty section {title!r} in {var}"


@pytest.mark.parametrize("var", ["LABORATORY_NAV_SECTIONS",
                                 "RADIOLOGY_NAV_SECTIONS",
                                 "PHARMACY_NAV_SECTIONS",
                                 "ADMIN_NAV_SECTIONS"])
def test_provider_and_admin_portals_do_not_offer_the_ai_assistant(var):
    """The API refuses these roles the assistant, so offering the page would
    only produce a 403. The two must not drift apart."""
    labels = {label for _t, items in _section_items(var) for label, _i in items}
    assert not labels & {"AI Assistant", "AI Copilot"}, (
        f"{var} offers an AI page its role is not authorized to use")


def test_patient_nav_keeps_every_documented_destination():
    """All pre-redesign destinations must still be reachable."""
    labels = {label for _t, items in _section_items("PATIENT_NAV_SECTIONS")
              for label, _i in items}
    for required in ("Dashboard", "Find Doctors", "Book Appointment", "My Appointments",
                     "AI Assistant", "Medical Records",
                     "Prescriptions", "Notifications", "Profile", "Settings"):
        assert required in labels, f"navigation lost '{required}'"


def test_the_pharmacy_portal_offers_its_real_pages():
    """The pharmacy portal was placeholders until the Pharmacy module landed."""
    labels = {label for _t, items in _section_items("PHARMACY_NAV_SECTIONS")
              for label, _i in items}
    for required in ("Dashboard", "Inventory", "Medication Requests",
                     "Prescriptions", "Notifications", "Profile"):
        assert required in labels, f"the pharmacy portal is missing '{required}'"


def test_the_doctor_portal_can_reach_prescribing():
    labels = {label for _t, items in _section_items("DOCTOR_NAV_SECTIONS")
              for label, _i in items}
    assert "Prescriptions" in labels


def test_no_portal_still_advertises_the_pharmacy_module_as_unbuilt():
    """Placeholder copy that outlives its feature reads as a broken promise."""
    source = (REPO_ROOT / "streamlit_app.py").read_text(encoding="utf-8")
    for stale in ("arrives with the Pharmacy system",
                  "Roshada does not issue prescriptions yet",
                  "Prescription tracking isn't available yet"):
        assert stale not in source, f"stale placeholder copy: {stale!r}"


def test_no_portal_still_advertises_messaging_as_unbuilt():
    source = (REPO_ROOT / "streamlit_app.py").read_text(encoding="utf-8")
    for stale in ("Secure messaging with your doctor is a later task",
                  "Secure messaging between doctors and patients is a later task"):
        assert stale not in source, f"stale placeholder copy: {stale!r}"


def test_both_clinical_portals_reach_messaging():
    """Messaging is patient<->doctor, so exactly those two portals need it."""
    for var in ("PATIENT_NAV_SECTIONS", "DOCTOR_NAV_SECTIONS"):
        labels = {label for _t, items in _section_items(var)
                  for label, _i in items}
        assert "Messages" in labels, f"{var} lost messaging"


def test_every_portal_has_a_notification_centre():
    """Every role receives notifications, so every portal must show them."""
    for var in NAV_VARS:
        labels = {label for _t, items in _section_items(var)
                  for label, _i in items}
        if var == "ADMIN_NAV_SECTIONS":
            continue   # the admin portal is platform administration, not a inbox
        assert "Notifications" in labels, f"{var} has no notification centre"


def test_both_clinical_portals_reach_the_medical_record():
    """Patients and doctors are the two roles the record is built for."""
    for var in ("PATIENT_NAV_SECTIONS", "DOCTOR_NAV_SECTIONS"):
        labels = {label for _t, items in _section_items(var)
                  for label, _i in items}
        assert "Medical Records" in labels, f"{var} lost the medical record"


def test_no_facility_portal_offers_the_medical_record():
    """A lab, centre or pharmacy has no route to a patient's whole history."""
    for var in ("LABORATORY_NAV_SECTIONS", "RADIOLOGY_NAV_SECTIONS",
                "PHARMACY_NAV_SECTIONS"):
        labels = {label for _t, items in _section_items(var)
                  for label, _i in items}
        assert not labels & {"Medical Records", "Patient Records"}, (
            f"{var} offers a medical record its role cannot open")


def test_only_the_admin_portal_offers_the_knowledge_base():
    """The backend refuses every other role, so offering the pages elsewhere
    would only produce a 403."""
    admin = {label for _t, items in _section_items("ADMIN_NAV_SECTIONS")
             for label, _i in items}
    for required in ("Sources", "Documents", "Retrieval", "Index Status"):
        assert required in admin, f"the admin portal is missing '{required}'"

    for var in NAV_VARS:
        if var == "ADMIN_NAV_SECTIONS":
            continue
        labels = {label for _t, items in _section_items(var)
                  for label, _i in items}
        assert not labels & {"Sources", "Index Status", "Retrieval"}, (
            f"{var} offers Knowledge Base pages its role cannot use")


def test_nav_icons_are_material_symbol_names():
    """Icons are rendered as :material/<name>: — one family, no extra dependency."""
    import re
    for var in NAV_VARS:
        for _title, items in _section_items(var):
            for label, icon in items:
                assert re.fullmatch(r"[a-z0-9_]+", icon), (
                    f"{label!r} has icon {icon!r}, which is not a Material Symbol name")


def test_sidebar_no_longer_depends_on_the_iframe_component():
    source = (REPO_ROOT / "streamlit_app.py").read_text(encoding="utf-8")
    assert "option_menu" not in source, (
        "the iframe menu cannot render section headers, tooltips or focus states")


def test_disclaimer_has_no_markdown_that_cannot_render():
    """theme.page_header HTML-escapes its subtitle, so markdown emphasis in the
    medical disclaimer showed up as literal asterisks on the page."""
    from shared.ui import DISCLAIMER
    assert "**" not in DISCLAIMER
    assert "<" not in DISCLAIMER


def test_no_module_writes_a_global_chat_history_file():
    """No top-level module may read or write the old shared history file."""
    import ast
    for path in REPO_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        literals = {node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        assert not any("chat_history.jsonl" in s for s in literals), (
            f"{path.name} still references the shared history file")
