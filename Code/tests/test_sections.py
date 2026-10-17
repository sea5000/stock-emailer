"""The section registry: the store, validation, and what the sandbox refuses."""

import pytest

import lots_db
import sections


def test_default_store_shape():
    store = sections.default_store()
    assert list(store["order"]) == sections.SECTION_KEYS
    assert set(store["sections"]) == set(sections.SECTION_KEYS)
    for cfg in store["sections"].values():
        assert cfg["enabled"] is True
        assert cfg["html"]
        assert "text" not in cfg, "text is one shared template, not per section"
    assert store["text"] == sections.DEFAULT_TEXT


def test_every_section_has_a_default_template():
    for key in sections.SECTION_KEYS:
        assert sections.DEFAULT_HTML[key].strip(), f"{key} has an empty default"
        assert sections.SECTION_LABELS[key]
        assert sections.SECTION_DESCRIPTIONS[key]


def test_store_round_trip(sandbox):
    store = sections.load_store(lots_db)
    store["sections"]["footer"]["html"] = "<p>EDITED</p>"
    store["sections"]["footer"]["enabled"] = False
    store["text"] = "custom {{ net_gl }}"
    store["order"] = list(reversed(store["order"]))
    sections.save_store(lots_db, store)

    back = sections.load_store(lots_db)
    assert back["sections"]["footer"]["html"] == "<p>EDITED</p>"
    assert back["sections"]["footer"]["enabled"] is False
    assert back["text"] == "custom {{ net_gl }}"
    assert back["order"] == list(reversed(sections.SECTION_KEYS))


def test_reset_one_section_leaves_the_rest(sandbox):
    store = sections.load_store(lots_db)
    store["sections"]["footer"]["html"] = "<p>EDITED</p>"
    store["sections"]["verdict"]["enabled"] = False
    store["text"] = "custom"
    sections.save_store(lots_db, store)

    sections.reset_section(lots_db, "footer")
    back = sections.load_store(lots_db)
    assert back["sections"]["footer"]["html"] == sections.DEFAULT_HTML["footer"]
    assert back["sections"]["footer"]["enabled"] is True
    assert back["sections"]["verdict"]["enabled"] is False, "sibling must survive"
    assert back["text"] == "custom", "reset_section must not touch the text template"


def test_reset_text_and_all(sandbox):
    store = sections.load_store(lots_db)
    store["sections"]["footer"]["html"] = "<p>EDITED</p>"
    store["text"] = "custom"
    sections.save_store(lots_db, store)

    sections.reset_text(lots_db)
    assert sections.load_store(lots_db)["text"] == sections.DEFAULT_TEXT

    store = sections.load_store(lots_db)
    store["sections"]["footer"]["html"] = "<p>EDITED AGAIN</p>"
    sections.save_store(lots_db, store)
    sections.reset_all(lots_db)
    back = sections.load_store(lots_db)
    assert back == sections.default_store()


def test_unknown_section_from_a_future_release_is_ignored(sandbox):
    lots_db.set_setting(
        sections.SETTING_KEY,
        {
            "sections": {"from_the_future": {"html": "x"}, "footer": {"html": "<p>F</p>"}},
            "order": ["footer", "from_the_future"],
            "text": "t",
        },
    )
    store = sections.load_store(lots_db)
    assert "from_the_future" not in store["sections"]
    assert store["sections"]["footer"]["html"] == "<p>F</p>"
    # A section missing from the order must be put back, never lost.
    assert sorted(store["order"]) == sorted(sections.SECTION_KEYS)
    assert len(store["order"]) == len(set(store["order"]))


def test_validate_catches_bad_syntax(sandbox):
    store = sections.default_store()
    assert sections.validate(store) == []

    store["sections"]["footer"]["html"] = "{% for x in %}"
    problems = sections.validate(store)
    assert len(problems) == 1
    assert "Footer" in problems[0]

    store = sections.default_store()
    store["text"] = "{% if %}"
    problems = sections.validate(store)
    assert len(problems) == 1
    assert "Plain text" in problems[0]


def test_validate_only_enabled_can_skip_switched_off_sections():
    store = sections.default_store()
    store["sections"]["footer"]["html"] = "{% for x in %}"
    store["sections"]["footer"]["enabled"] = False
    assert sections.validate(store), "the editor should still flag it"
    assert sections.validate(store, only_enabled=True) == [], "a send should not care"


def test_validate_ignores_a_blank_template():
    store = sections.default_store()
    store["sections"]["footer"]["html"] = "   "
    assert sections.validate(store) == []


def test_a_broken_section_does_not_blank_the_email(fake_context):
    store = sections.default_store()
    store["sections"]["holdings"]["html"] = "{% for x in %}"
    html = sections.render_html(fake_context, store)
    assert "Portfolio Value" in html
    assert len(html) > 2000


def test_sandbox_blocks_attribute_traversal(fake_context):
    store = sections.default_store()
    store["sections"]["header"]["html"] = "{{ ''.__class__.__mro__ }}"
    out = sections.render_html(fake_context, store)
    assert "__mro__" not in out


@pytest.mark.parametrize(
    "template",
    [
        "{{ config }}",
        "{{ self._TemplateReference__context }}",
        "{{ cycler.__init__.__globals__ }}",
    ],
)
def test_sandbox_blocks_known_escapes(fake_context, template):
    store = sections.default_store()
    store["sections"]["header"]["html"] = template
    out = sections.render_html(fake_context, store)
    assert "globals" not in out
    assert "Config" not in out


def test_disabling_a_section_removes_its_html(fake_context):
    store = sections.default_store()
    full = sections.render_html(fake_context, store)
    store["sections"]["verdict"]["enabled"] = False
    without = sections.render_html(fake_context, store)
    assert "Do nothing" in full
    assert "Do nothing" not in without
    assert len(without) < len(full)


def test_disabling_a_section_removes_its_text_line(fake_context):
    store = sections.default_store()
    assert "VERDICT:" in sections.render_text(fake_context, store)
    store["sections"]["verdict"]["enabled"] = False
    assert "VERDICT:" not in sections.render_text(fake_context, store)


def test_section_order_is_respected(fake_context):
    store = sections.default_store()
    normal = sections.render_html(fake_context, store)
    reversed_store = dict(store)
    reversed_store["order"] = list(reversed(store["order"]))
    flipped = sections.render_html(fake_context, reversed_store)
    assert flipped != normal
    assert len(flipped) == len(normal)


def test_render_text_survives_a_broken_text_template(fake_context):
    store = sections.default_store()
    store["text"] = "{% if %}"
    assert sections.render_text(fake_context, store) == ""


def test_text_template_exposes_the_headline_numbers(fake_context):
    text = sections.render_text(fake_context, sections.default_store())
    assert "Portfolio Checkup" in text
    assert "$41,234.56" in text
    assert "$24,800.26" in text
    assert "$16,434.30" in text
    assert "66.27%" in text
    assert "VERDICT:" in text
    assert "\r\n" in text, "email plain text uses CRLF"
