"""Guard: the MCP tool schema must agree with the terminal the humans use.

``api/mcp.py`` exposes the same models as the browser terminal through a
second front door, and it pays for that with two hand-maintained mirrors:

* ``PARAMS`` mirrors the ``MODELS`` registry in ``public/assets/terminal.js``
  (ids, bounds, defaults, enum choices). If they drift, an AI client can
  compute something the UI forbids — or be refused something the UI allows.
* ``_HEADLINE`` names the one result key that is "the answer" for each model.
  If that key is wrong the endpoint cannot report a headline at all, and if it
  disagrees with ``SCEN_HEADLINE`` (terminal.js) or ``_HEADLINE_PICK``
  (``api/premium.py``) the browser and an AI client report different headline
  numbers for the same run.

Neither mirror has a compiler to keep it honest, so these tests do it: the JS
registry is parsed out of the file text (no JS engine, no ``eval``) and every
headline key is checked against the model's real ``calculate()`` output.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import api.mcp as mcp
import api.premium as premium

ROOT = Path(__file__).resolve().parents[1]
TERMINAL_JS = ROOT / "public" / "assets" / "terminal.js"

#: FF3 is deliberately absent from api/mcp.py (pandas does not fit the
#: serverless bundle budget). It is the one mnemonic the two registries are
#: allowed to disagree about — and test_ff3_is_explicitly_omitted below
#: asserts that the omission is declared rather than accidental.
OMITTED_FROM_MCP = {"FF3"}

SYNC_HINT = ("api/mcp.py's PARAMS is a hand-maintained mirror of the MODELS "
             "registry in public/assets/terminal.js — update whichever one is "
             "wrong so the sliders and the tool schema agree")


# --------------------------------------------------------------------------- #
# A minimal reader for the JS object/array literals in terminal.js.
#
# The registry is plain data — object literals with bare keys, strings,
# numbers, booleans and arrays — so a ~50-line recursive-descent reader covers
# it exactly. Deliberately not a JS engine and deliberately not eval(): this
# test must not execute anything from a file it is auditing.
# --------------------------------------------------------------------------- #
_SKIP = re.compile(r"(?:\s+|//[^\n]*|/\*.*?\*/)*", re.S)
_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_LITERAL = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|true|false|null")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}


class _JsReader:
    def __init__(self, text: str, pos: int) -> None:
        self.t, self.i = text, pos

    def _skip(self) -> None:
        self.i = _SKIP.match(self.t, self.i).end()

    def value(self):
        self._skip()
        char = self.t[self.i]
        if char == "{":
            return self._object()
        if char == "[":
            return self._array()
        if char in "\"'":
            return self._string()
        return self._literal()

    def _object(self) -> dict:
        self.i += 1
        out: dict = {}
        while True:
            self._skip()
            if self.t[self.i] == "}":
                self.i += 1
                return out
            key = self._key()            # read the key before the value: Python
            out[key] = self._after_colon()   # evaluates an assignment's RHS first
            self._skip()
            if self.t[self.i] == ",":
                self.i += 1

    def _array(self) -> list:
        self.i += 1
        out: list = []
        while True:
            self._skip()
            if self.t[self.i] == "]":
                self.i += 1
                return out
            out.append(self.value())
            self._skip()
            if self.t[self.i] == ",":
                self.i += 1

    def _key(self) -> str:
        self._skip()
        if self.t[self.i] in "\"'":
            return self._string()
        match = _IDENT.match(self.t, self.i)
        assert match, f"expected an object key at offset {self.i}"
        self.i = match.end()
        return match.group(0)

    def _after_colon(self):
        self._skip()
        assert self.t[self.i] == ":", f"expected ':' at offset {self.i}"
        self.i += 1
        return self.value()

    def _string(self) -> str:
        quote = self.t[self.i]
        self.i += 1
        buf: list[str] = []
        while self.t[self.i] != quote:
            char = self.t[self.i]
            if char == "\\":
                self.i += 1
                buf.append(_ESCAPES.get(self.t[self.i], self.t[self.i]))
            else:
                buf.append(char)
            self.i += 1
        self.i += 1
        return "".join(buf)

    def _literal(self):
        match = _LITERAL.match(self.t, self.i)
        assert match, f"unparseable value at offset {self.i}: {self.t[self.i:self.i + 30]!r}"
        self.i = match.end()
        raw = match.group(0)
        if raw in ("true", "false"):
            return raw == "true"
        if raw == "null":
            return None
        return float(raw) if ("." in raw or "e" in raw or "E" in raw) else int(raw)


def _js_literal(text: str, anchor: str):
    """Parse the single JS literal that follows ``anchor`` in ``text``."""
    match = re.search(anchor, text)
    assert match, f"could not locate {anchor!r} in {TERMINAL_JS}"
    return _JsReader(text, match.end()).value()


@pytest.fixture(scope="module")
def js_text() -> str:
    return TERMINAL_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js_models(js_text: str) -> dict[str, dict]:
    """``{mnemonic: model-entry}`` from terminal.js's MODELS registry."""
    models = _js_literal(js_text, r"const\s+MODELS\s*=\s*")
    assert isinstance(models, list) and len(models) >= 11, \
        f"parsed {type(models).__name__} with unexpected size from MODELS"
    by_mnemonic = {m["mn"]: m for m in models}
    assert len(by_mnemonic) == len(models), "duplicate mnemonics in MODELS"
    return by_mnemonic


@pytest.fixture(scope="module")
def js_scen_headline(js_text: str) -> dict[str, dict]:
    return _js_literal(js_text, r"const\s+SCEN_HEADLINE\s*=\s*")


def _js_choices(param: dict):
    """Enum choices of a terminal.js param (``select`` or ``toggle``), if any."""
    for key in ("select", "toggle"):
        if key in param:
            return list(param[key]), key
    return None, None


def _same_number(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return a == b


def _shared() -> list[str]:
    return sorted(set(mcp.PARAMS) - OMITTED_FROM_MCP)


# --------------------------------------------------------------------------- #
# (a) Registry drift: PARAMS vs terminal.js MODELS
# --------------------------------------------------------------------------- #
def test_mnemonics_match_except_the_declared_omission(js_models):
    js_only = set(js_models) - set(mcp.PARAMS) - OMITTED_FROM_MCP
    py_only = set(mcp.PARAMS) - set(js_models)
    assert not js_only, f"in terminal.js but not in api/mcp.py PARAMS: {sorted(js_only)} — {SYNC_HINT}"
    assert not py_only, f"in api/mcp.py PARAMS but not in terminal.js: {sorted(py_only)} — {SYNC_HINT}"


def test_ff3_is_explicitly_omitted_with_a_reason(js_models):
    assert "FF3" in js_models, "terminal.js no longer ships FF3; revisit this exemption"
    assert "FF3" not in mcp.PARAMS and "FF3" not in mcp.BUILDERS, \
        "FF3 is now exposed over MCP — drop it from OMITTED_FROM_MCP here and from api.mcp.OMITTED"
    assert "FF3" in mcp.OMITTED, "FF3 is missing from api/mcp.py's PARAMS but is not declared in OMITTED"
    name, reason = mcp.OMITTED["FF3"]
    assert name.strip(), "OMITTED['FF3'] has no display name"
    assert reason.strip(), "OMITTED['FF3'] has no reason — a caller must be told where to run it instead"


@pytest.mark.parametrize("mnemonic", _shared())
def test_parameter_ids_match(js_models, mnemonic):
    js_ids = [p["id"] for p in js_models[mnemonic]["params"]]
    py_ids = [s["id"] for s in mcp.PARAMS[mnemonic]]
    assert set(js_ids) == set(py_ids), (
        f"{mnemonic}: parameter ids differ — only in terminal.js: "
        f"{sorted(set(js_ids) - set(py_ids))}; only in api/mcp.py: "
        f"{sorted(set(py_ids) - set(js_ids))}. {SYNC_HINT}")


@pytest.mark.parametrize("mnemonic", _shared())
def test_bounds_defaults_and_choices_match(js_models, mnemonic):
    js_params = {p["id"]: p for p in js_models[mnemonic]["params"]}
    for spec in mcp.PARAMS[mnemonic]:
        pid = spec["id"]
        js = js_params.get(pid)
        assert js is not None, f"{mnemonic}.{pid}: absent from terminal.js. {SYNC_HINT}"

        choices, js_key = _js_choices(js)
        if spec["kind"] == "enum":
            assert choices is not None, (
                f"{mnemonic}.{pid}: api/mcp.py declares an enum {spec['choices']} but "
                f"terminal.js has no select/toggle list. {SYNC_HINT}")
            assert list(spec["choices"]) == choices, (
                f"{mnemonic}.{pid}: enum choices differ — api/mcp.py has "
                f"{list(spec['choices'])}, terminal.js {js_key} has {choices}. {SYNC_HINT}")
        else:
            assert choices is None, (
                f"{mnemonic}.{pid}: terminal.js declares a {js_key} list {choices} but "
                f"api/mcp.py has a {spec['kind']} parameter. {SYNC_HINT}")
            assert _same_number(spec["min"], js["min"]), (
                f"{mnemonic}.{pid}: min differs — api/mcp.py has {spec['min']}, "
                f"terminal.js has {js['min']}. {SYNC_HINT}")
            assert _same_number(spec["max"], js["max"]), (
                f"{mnemonic}.{pid}: max differs — api/mcp.py has {spec['max']}, "
                f"terminal.js has {js['max']}. {SYNC_HINT}")
            assert bool(js.get("int")) == (spec["kind"] == "int"), (
                f"{mnemonic}.{pid}: integer-ness differs — api/mcp.py kind is "
                f"{spec['kind']!r}, terminal.js int flag is {js.get('int')}. {SYNC_HINT}")

        assert _same_number(spec["def"], js["def"]), (
            f"{mnemonic}.{pid}: default differs — api/mcp.py has {spec['def']!r}, "
            f"terminal.js has {js['def']!r}. {SYNC_HINT}")


@pytest.mark.parametrize("mnemonic", _shared())
def test_every_default_is_inside_its_own_bounds(mnemonic):
    """A default outside [min, max] would make the zero-argument call — the
    most likely call an AI client makes — fail validation immediately."""
    values, defaulted = mcp._validate(mnemonic, {})
    assert defaulted == [s["id"] for s in mcp.PARAMS[mnemonic]], \
        f"{mnemonic}: _validate({{}}) did not report every parameter as defaulted"
    assert set(values) == {s["id"] for s in mcp.PARAMS[mnemonic]}


# --------------------------------------------------------------------------- #
# (b) Headline-key drift: _HEADLINE vs what calculate() actually returns
# --------------------------------------------------------------------------- #
def test_headline_covers_exactly_the_exposed_models():
    assert set(mcp._HEADLINE) == set(mcp.BUILDERS), (
        "_HEADLINE and BUILDERS disagree — only in _HEADLINE: "
        f"{sorted(set(mcp._HEADLINE) - set(mcp.BUILDERS))}; only in BUILDERS: "
        f"{sorted(set(mcp.BUILDERS) - set(mcp._HEADLINE))}")


@pytest.mark.parametrize("mnemonic", sorted(mcp.BUILDERS))
def test_headline_key_exists_in_the_real_result(mnemonic):
    """Build each model on its own defaults and confirm the headline key is
    really there. A wrong key does not crash — it silently degrades every
    result for that model to "(no headline: ...)", which is exactly the kind
    of quiet failure nobody notices in review."""
    values, _ = mcp._validate(mnemonic, {})
    results = mcp.BUILDERS[mnemonic](values).calculate()
    key, unit = mcp._HEADLINE[mnemonic]
    assert key in results, (
        f"{mnemonic}: _HEADLINE names {key!r} but calculate() returned keys "
        f"{sorted(results)}. Pick the key that is actually the answer.")
    assert unit in ("$", "%", "x"), f"{mnemonic}: unknown headline unit {unit!r}"

    headline = mcp._headline(mnemonic, mcp._clean(results))
    assert "no headline" not in headline, \
        f"{mnemonic}: headline rendered as {headline!r} despite the key being present"


@pytest.mark.parametrize("mnemonic", sorted(set(mcp._HEADLINE) - mcp.PREMIUM))
def test_free_headline_keys_match_the_browser_terminal(js_scen_headline, mnemonic):
    assert mnemonic in js_scen_headline, \
        f"{mnemonic} is missing from SCEN_HEADLINE in terminal.js"
    js_key = js_scen_headline[mnemonic]["key"]
    assert mcp._HEADLINE[mnemonic][0] == js_key, (
        f"{mnemonic}: headline key differs — api/mcp.py reports "
        f"{mcp._HEADLINE[mnemonic][0]!r}, the browser terminal reports {js_key!r}. "
        "The same model run must not produce two different headline numbers.")


@pytest.mark.parametrize("mnemonic", sorted(mcp.PREMIUM))
def test_premium_headline_keys_match_api_premium(mnemonic):
    assert mnemonic in premium._HEADLINE_PICK, \
        f"{mnemonic} is missing from _HEADLINE_PICK in api/premium.py"
    assert mcp._HEADLINE[mnemonic] == premium._HEADLINE_PICK[mnemonic], (
        f"{mnemonic}: headline differs — api/mcp.py has {mcp._HEADLINE[mnemonic]}, "
        f"api/premium.py has {premium._HEADLINE_PICK[mnemonic]}.")


# --------------------------------------------------------------------------- #
# Tool-list shape
# --------------------------------------------------------------------------- #
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def test_tool_names_are_unique_and_well_formed():
    names = [t["name"] for t in mcp._tools()]
    assert len(names) == len(set(names)), f"duplicate tool names: {sorted(names)}"
    for name in names:
        assert _TOOL_NAME.match(name), f"tool name {name!r} is not ^[A-Za-z0-9_.-]{{1,128}}$"


def test_every_tool_has_a_description():
    for tool in mcp._tools():
        assert tool.get("description", "").strip(), f"{tool['name']} has no description"


def test_tool_input_schemas_expose_exactly_their_model_parameters():
    for tool in mcp._tools():
        schema = tool["inputSchema"]
        assert schema["type"] == "object", f"{tool['name']}: inputSchema is not an object"
        props = schema.get("properties", {})
        mnemonic = mcp.TOOL_TO_MNEMONIC.get(tool["name"])
        if mnemonic is None:                       # the finmodels_list_models tool
            assert not props, f"{tool['name']} takes no arguments but declares {sorted(props)}"
            continue
        expected = {s["id"] for s in mcp.PARAMS[mnemonic]}
        assert set(props) == expected, (
            f"{tool['name']} ({mnemonic}): inputSchema properties differ from PARAMS — "
            f"missing {sorted(expected - set(props))}, extra {sorted(set(props) - expected)}")
        for pid, node in props.items():
            assert node.get("description", "").strip(), \
                f"{tool['name']}.{pid} has no description"
