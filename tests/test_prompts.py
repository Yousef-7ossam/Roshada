"""TASK 04 — the prompt engineering system.

Prompts are prose files, so they need the checks prose does not get for free:
that they parse, that they are versioned, that shared rules actually reach every
audience, and that no instruction has crept back into a Python module.
"""
import pytest

from appointments.services.ai import prompts
from appointments.services.ai.prompts import registry
from appointments.services.ai.prompts.template import PromptError, PromptTemplate

MINIMAL = """---
name: sample
version: 1.0.0
---
Say something useful.
"""


@pytest.fixture(autouse=True)
def _pristine_library(monkeypatch):
    """No PROMPT_DIR from the developer's environment, and a clean cache."""
    monkeypatch.delenv("PROMPT_DIR", raising=False)
    registry.reload()
    yield
    monkeypatch.delenv("PROMPT_DIR", raising=False)
    registry.reload()


# ===========================================================================
# Parsing and validation
# ===========================================================================
class TestParsing:
    def test_a_minimal_prompt_parses(self):
        parsed = PromptTemplate.from_text(MINIMAL)
        assert parsed.name == "sample"
        assert parsed.version == "1.0.0"
        assert parsed.body == "Say something useful."
        assert parsed.status == "active"

    def test_frontmatter_is_required(self):
        with pytest.raises(PromptError, match="frontmatter"):
            PromptTemplate.from_text("Just a body, no metadata.")

    @pytest.mark.parametrize("missing", ["name", "version"])
    def test_name_and_version_are_required(self, missing):
        text = MINIMAL.replace(f"{missing}: ", f"x_{missing}: ")
        with pytest.raises(PromptError, match=missing):
            PromptTemplate.from_text(text)

    def test_an_empty_body_is_rejected(self):
        with pytest.raises(PromptError, match="empty"):
            PromptTemplate.from_text("---\nname: x\nversion: 1.0.0\n---\n")

    @pytest.mark.parametrize("version", ["1.0", "v1.0.0", "latest", "1.0.0-beta"])
    def test_versions_must_be_semver(self, version):
        with pytest.raises(PromptError, match="MAJOR.MINOR.PATCH"):
            PromptTemplate.from_text(MINIMAL.replace("1.0.0", version))

    def test_an_unknown_status_is_rejected(self):
        text = MINIMAL.replace("---\nname", "---\nstatus: maybe\nname")
        with pytest.raises(PromptError, match="status"):
            PromptTemplate.from_text(text)

    def test_list_values_are_parsed(self):
        text = MINIMAL.replace("version: 1.0.0",
                               "version: 1.0.0\nincludes: [a, b]\nvariables: []")
        parsed = PromptTemplate.from_text(text)
        assert parsed.includes == ("a", "b")
        assert parsed.variables == ()


# ===========================================================================
# Variables — caught at load, not at request time
# ===========================================================================
class TestVariables:
    def test_an_undeclared_variable_fails_at_load(self):
        text = MINIMAL.replace("Say something useful.", "Hello {{who}}.")
        with pytest.raises(PromptError, match="does not declare"):
            PromptTemplate.from_text(text)

    def test_a_declared_variable_renders(self):
        text = MINIMAL.replace("version: 1.0.0", "version: 1.0.0\nvariables: [who]") \
                      .replace("Say something useful.", "Hello {{who}}.")
        assert PromptTemplate.from_text(text).render(who="world") == "Hello world."

    def test_a_missing_value_raises_rather_than_leaving_a_hole(self):
        text = MINIMAL.replace("version: 1.0.0", "version: 1.0.0\nvariables: [who]") \
                      .replace("Say something useful.", "Hello {{who}}.")
        with pytest.raises(PromptError, match="no value supplied"):
            PromptTemplate.from_text(text).render()

    def test_whitespace_inside_the_braces_is_tolerated(self):
        text = MINIMAL.replace("version: 1.0.0", "version: 1.0.0\nvariables: [who]") \
                      .replace("Say something useful.", "Hello {{  who  }}.")
        assert PromptTemplate.from_text(text).render(who="world") == "Hello world."

    def test_single_braces_in_prose_are_left_alone(self):
        """Prompts legitimately contain braces — JSON examples, ranges like
        {0,1}. str.format would crash on them at request time."""
        text = MINIMAL.replace("Say something useful.",
                               'Return JSON like {"risk": 1} where risk is {0,1}.')
        assert '{"risk": 1}' in PromptTemplate.from_text(text).render()


# ===========================================================================
# Composition and reusability
# ===========================================================================
class TestComposition:
    def test_fragments_are_prepended_in_order(self):
        rendered = prompts.render("medical_assistant", context_block="X")
        style = rendered.index("Response style")
        safety = rendered.index("Safety rules")
        body = rendered.index("You are Roshada's medical assistant")
        assert style < safety < body

    def test_including_an_unknown_fragment_is_an_error(self):
        text = MINIMAL.replace("version: 1.0.0", "version: 1.0.0\nincludes: [nope]")
        parsed = PromptTemplate.from_text(text)
        with pytest.raises(PromptError, match="unknown fragment"):
            parsed.render(fragments={})

    def test_the_safety_rules_exist_in_exactly_one_place(self):
        """Written once, reused everywhere — so patient and clinician wording
        cannot drift apart."""
        fragment = prompts.get("safety")
        for name in ("medical_assistant", "doctor_copilot"):
            rendered = prompts.render(name, context_block="X")
            assert fragment.body in rendered

    def test_the_context_block_is_substituted_not_left_as_a_placeholder(self):
        rendered = prompts.render("medical_assistant", context_block="Profile: age 41")
        assert "Profile: age 41" in rendered
        assert "{{" not in rendered


# ===========================================================================
# The shipped library
# ===========================================================================
class TestLibrary:
    def test_every_required_purpose_is_represented(self):
        """The six categories the brief asked the library to be organised by."""
        for purpose in ("medical_assistant", "patient_copilot", "doctor_copilot",
                        "rag", "agents", "safety"):
            assert prompts.by_purpose(purpose), f"no prompt for purpose '{purpose}'"

    def test_every_prompt_in_the_library_loads(self):
        assert len(prompts.names()) >= 6
        for name in prompts.names():
            assert prompts.get(name).version

    def test_every_model_facing_prompt_carries_the_safety_rules(self):
        """The one rule that must not be forgettable when adding a prompt."""
        for name in prompts.names():
            prompt = prompts.get(name)
            if prompt.purpose == "safety" and prompt.name == "safety":
                continue
            assert "safety" in prompt.includes, \
                f"{name} does not include the safety fragment"

    def test_every_prompt_documents_itself(self):
        for name in prompts.names():
            assert prompts.get(name).description, f"{name} has no description"

    def test_planned_prompts_are_marked_as_such(self):
        """``rag_answer`` left this set when the RAG pipeline landed — it is
        rendered by ``knowledge.rag.service`` now. The rest stay
        registered-but-unbuilt."""
        planned = {n for n in prompts.names() if prompts.get(n).status == "planned"}
        assert planned == {"patient_copilot", "agent_tool_use", "safety_review"}

    def test_the_rag_prompt_is_active_and_actually_rendered(self):
        import pathlib

        assert prompts.get("rag_answer").status == "active"
        # "active" has to mean something: real code renders it.
        repo = pathlib.Path(prompts.registry.LIBRARY_DIR).parents[4]
        source = (repo / "knowledge" / "rag" / "service.py").read_text(
            encoding="utf-8")
        assert '"rag_answer"' in source

    def test_no_running_code_renders_a_planned_prompt(self):
        """Registered so the category exists — but nothing calls them until the
        task that owns them lands.

        Scanned across every app, not only the AI service package: a planned
        prompt rendered from another module would be just as wrong.
        """
        import pathlib

        repo = pathlib.Path(prompts.registry.LIBRARY_DIR).parents[4]
        planned = [n for n in prompts.names() if prompts.get(n).status == "planned"]
        for app in ("appointments", "knowledge", "accounts", "records",
                    "comms", "radiology", "pharmacy", "shared"):
            for path in (repo / app).rglob("*.py"):
                source = path.read_text(encoding="utf-8")
                for name in planned:
                    assert f'"{name}"' not in source and f"'{name}'" not in source, \
                        f"{path} references the planned prompt '{name}'"

    def test_active_prompts_are_the_ones_the_roles_map_to(self):
        for name in prompts.ROLE_PROMPTS.values():
            assert prompts.get(name).status == "active"

    def test_no_credential_or_model_id_is_baked_into_a_prompt(self):
        for name in prompts.names() + prompts.fragment_names():
            body = prompts.get(name).body.lower()
            for leak in ("sk-", "api_key", "gpt-4", "gemini-2", "groq"):
                assert leak not in body, f"{name} hardcodes '{leak}'"


# ===========================================================================
# Versioning
# ===========================================================================
class TestVersioning:
    def test_a_prompt_is_identified_by_name_and_version(self):
        assert prompts.get("medical_assistant").id == \
            f"medical_assistant@{prompts.get('medical_assistant').version}"

    def test_the_rendered_prompt_reports_which_prompt_it_was(self):
        _, prompt_id = prompts.system_prompt("patient", "X")
        assert prompt_id.startswith("medical_assistant@")

    def test_each_role_gets_its_own_prompt(self):
        patient_text, patient_id = prompts.system_prompt("patient", "X")
        doctor_text, doctor_id = prompts.system_prompt("doctor", "X")
        assert patient_id != doctor_id
        assert patient_text != doctor_text
        assert "clinical assistant" in doctor_text.lower()

    def test_an_unknown_role_falls_back_rather_than_crashing(self):
        _, prompt_id = prompts.system_prompt("wizard", "X")
        assert prompt_id.startswith(prompts.DEFAULT_PROMPT)

    def test_the_catalogue_lists_every_prompt_with_its_version(self):
        catalogue = prompts.catalogue()
        assert {entry["name"] for entry in catalogue} == set(prompts.names())
        for entry in catalogue:
            assert entry["version"] and entry["purpose"]


# ===========================================================================
# Configuration — changing prompts without touching code
# ===========================================================================
class TestOverrides:
    def test_an_operator_can_replace_a_prompt_without_changing_code(
            self, monkeypatch, tmp_path):
        (tmp_path / "medical_assistant.md").write_text(
            "---\nname: medical_assistant\nversion: 9.9.9\n"
            "purpose: medical_assistant\ndescription: Operator override.\n"
            "includes: [safety]\nvariables: [context_block]\n---\n"
            "CUSTOM DEPLOYMENT PERSONA.\n\n{{context_block}}\n", encoding="utf-8")

        monkeypatch.setenv("PROMPT_DIR", str(tmp_path))
        registry.reload()

        text, prompt_id = prompts.system_prompt("patient", "Profile: age 41")
        assert "CUSTOM DEPLOYMENT PERSONA." in text
        assert prompt_id == "medical_assistant@9.9.9"
        # The shared safety rules still apply to the operator's own prompt.
        assert "never diagnose or prescribe" in text.lower()

    def test_an_operator_can_add_a_new_prompt(self, monkeypatch, tmp_path):
        (tmp_path / "triage.md").write_text(
            "---\nname: triage\nversion: 1.0.0\npurpose: medical_assistant\n"
            "description: Local triage prompt.\nincludes: [safety]\n---\n"
            "Sort the following by urgency.\n", encoding="utf-8")
        monkeypatch.setenv("PROMPT_DIR", str(tmp_path))
        registry.reload()
        assert "triage" in prompts.names()

    def test_an_operator_can_override_a_shared_fragment(self, monkeypatch, tmp_path):
        fragments = tmp_path / "_fragments"
        fragments.mkdir()
        (fragments / "safety.md").write_text(
            "---\nname: safety\nversion: 2.0.0\npurpose: safety\n"
            "description: Stricter local rules.\n---\n"
            "LOCAL SAFETY POLICY APPLIES.\n", encoding="utf-8")
        monkeypatch.setenv("PROMPT_DIR", str(tmp_path))
        registry.reload()

        text, _ = prompts.system_prompt("patient", "X")
        assert "LOCAL SAFETY POLICY APPLIES." in text

    def test_a_malformed_override_fails_loudly_rather_than_being_ignored(
            self, monkeypatch, tmp_path):
        """Silently reverting to the built-in would discard an operator's own
        safety wording without telling anyone."""
        (tmp_path / "broken.md").write_text("no frontmatter here", encoding="utf-8")
        monkeypatch.setenv("PROMPT_DIR", str(tmp_path))
        with pytest.raises(PromptError):
            registry.reload()

    def test_a_missing_prompt_dir_is_an_error_not_a_silent_no_op(
            self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROMPT_DIR", str(tmp_path / "does-not-exist"))
        with pytest.raises(PromptError, match="not a directory"):
            registry.reload()

    def test_without_prompt_dir_the_shipped_library_is_used(self, monkeypatch):
        monkeypatch.delenv("PROMPT_DIR", raising=False)
        registry.reload()
        assert prompts.override_dir() is None
        assert prompts.get("medical_assistant").version != "9.9.9"


# ===========================================================================
# Nothing scattered
# ===========================================================================
class TestNoScatteredPrompts:
    #: Phrases that only ever belong in a model instruction.
    INSTRUCTION_MARKERS = (
        "you are roshada", "you are a medical assistant",
        "safety rules you must", "respond in the user's language",
        "your tasks:",
    )

    def test_no_python_module_contains_prompt_text(self):
        import pathlib

        import appointments
        repo = pathlib.Path(appointments.__file__).resolve().parent.parent

        offenders = []
        for path in repo.rglob("*.py"):
            if any(part in {".venv", ".conda", "__pycache__", "tests"}
                   for part in path.parts):
                continue
            lowered = path.read_text(encoding="utf-8", errors="ignore").lower()
            for marker in self.INSTRUCTION_MARKERS:
                if marker in lowered:
                    offenders.append(f"{path.name}: {marker!r}")
        assert not offenders, "prompt text found outside the library: " + str(offenders)

    def test_the_safety_rules_are_no_longer_a_python_constant(self):
        from shared import safety
        assert not hasattr(safety, "SAFETY_RULES"), \
            "SAFETY_RULES moved to the prompt library"

    def test_shared_safety_still_owns_the_non_prompt_parts(self):
        """The deterministic checks stay: they must work with no model at all."""
        from shared import safety
        assert safety.detect_emergency("I have chest pain") == "chest pain"
        assert "123" in safety.EMERGENCY_NOTICE


# ===========================================================================
# Packaging — prompts are data files, so they can be lost in ways code cannot
# ===========================================================================
class TestPackaging:
    def test_the_prompt_library_survives_the_docker_build(self):
        """`.dockerignore` excludes `*.md`. Without an explicit negation the
        image ships with no prompts at all, and every answer degrades."""
        import pathlib

        import appointments
        repo = pathlib.Path(appointments.__file__).resolve().parent.parent
        rules = (repo / ".dockerignore").read_text(encoding="utf-8").splitlines()
        negations = [line.strip() for line in rules if line.strip().startswith("!")]

        assert any("prompts/library/*.md" in rule for rule in negations), \
            "the prompt library is excluded from the Docker image by *.md"
        assert any("_fragments/*.md" in rule for rule in negations), \
            "shared prompt fragments are excluded from the Docker image"

        # The negations must come after the *.md rule, or Docker ignores them.
        md_index = next(i for i, line in enumerate(rules) if line.strip() == "*.md")
        negation_index = next(i for i, line in enumerate(rules)
                              if "prompts/library/*.md" in line)
        assert negation_index > md_index, \
            "the negation must follow the *.md rule to take effect"

    def test_every_library_file_is_markdown(self):
        """Discovery globs *.md — a prompt saved as .txt would silently vanish."""
        for path in registry.LIBRARY_DIR.rglob("*"):
            if path.is_file():
                assert path.suffix == ".md", f"{path.name} will not be discovered"
