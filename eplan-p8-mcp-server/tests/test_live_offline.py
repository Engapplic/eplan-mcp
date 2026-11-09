"""Offline tests for api/actions/live.py: the generated reflection C# must stay
well-formed, injection-free, and free of the traps that make it fail inside
EPLAN's script engine. _execute_script is replaced by a capture stub, so no
EPLAN is needed."""

import pytest

from api.actions import live


@pytest.fixture
def capture(monkeypatch):
    """Stub _execute_script; captures the generated C# instead of running it."""
    captured = {}

    def fake_execute(script, timeout=30.0):
        captured["script"] = script
        captured["timeout"] = timeout
        return {"success": True, "results": {"stubbed": True}}

    monkeypatch.setattr(live, "_execute_script", fake_execute)
    return captured


INJECTION = '"; System.Environment.Exit(0); string y = "'


def _string_literals_balanced(cs: str) -> bool:
    """After stripping escape sequences, every line must contain an even
    number of quotes - i.e. no value broke out of its string literal."""
    stripped = cs.replace("\\\\", "").replace('\\"', "")
    return all(line.count('"') % 2 == 0 for line in stripped.splitlines())


ALL_TOOLS = [
    (live.live_query_functions, {}),
    (live.live_query_pages, {}),
    (live.live_plc_io_list, {}),
    (live.live_set_function_text, {"name": "+X-K1", "text": "hi"}),
    (live.live_set_connection_designations,
     {"name": "+X-K1", "designations": ["Y11", "Y12"]}),
    (live.live_read_check_messages, {}),
]



# ---------------------------------------------------------------------------
# The CS0234 trap: DataModel/HEServices must never appear as `using` directives
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_no_datamodel_using_directive(capture, fn, kwargs):
    fn(**kwargs)
    for line in capture["script"].splitlines():
        if line.strip().startswith("using "):
            assert "Eplan.EplApi.DataModel" not in line
            assert "Eplan.EplApi.HEServices" not in line


# ---------------------------------------------------------------------------
# Assembly resolution: never Assembly.Load the native twin as the primary route
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_resolves_types_from_loaded_assemblies(capture, fn, kwargs):
    fn(**kwargs)
    cs = capture["script"]
    # Scanning the loaded set is the primary route.
    assert "AppDomain.CurrentDomain.GetAssemblies()" in cs
    # In the Assembly.Load FALLBACK list, the *Netu names must come first: on
    # 2027 the un-suffixed name is the mixed-mode native twin and loading it
    # throws BadImageFormatException. Scope the check to the candidates array,
    # since the surrounding comment mentions the un-suffixed name too.
    start = cs.index("string[] candidates")
    candidates = cs[start:cs.index("};", start)]
    assert candidates.index('"Eplan.EplApi.DataModelNetu"') < candidates.index('"Eplan.EplApi.DataModelu"')
    assert candidates.index('"Eplan.EplApi.HEServicesNetu"') < candidates.index('"Eplan.EplApi.HEServicesu"')


@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_locking_step_taken_and_disposed(capture, fn, kwargs):
    fn(**kwargs)
    cs = capture["script"]
    assert 'FindType("Eplan.EplApi.DataModel.LockingStep")' in cs
    assert "finally" in cs
    assert 'lsType.GetMethod("Dispose")' in cs


# ---------------------------------------------------------------------------
# Script-engine syntax limits
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_no_dictionary_index_initializers(capture, fn, kwargs):
    # `new Dictionary<..> { ["k"] = v }` is CS1525 in EPLAN's script engine.
    fn(**kwargs)
    assert '{ ["' not in capture["script"]
    assert "{[" not in capture["script"].replace(" ", "")


@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_result_path_placeholder_present(capture, fn, kwargs):
    fn(**kwargs)
    assert "{{RESULT_PATH}}" in capture["script"]


# ---------------------------------------------------------------------------
# limit is interpolated outside a string literal -> must be a real integer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_rejects_non_integer_limit(capture, fn, kwargs):
    result = fn(limit=INJECTION, **kwargs)
    assert result["success"] is False
    assert "limit" in result["error"].lower()
    assert "script" not in capture, "malicious limit must never reach the script"


def test_query_coerces_numeric_string_limit(capture):
    result = live.live_query_pages(limit="25")
    assert result["success"] is True
    assert "list.Count < 25" in capture["script"]


def test_query_default_limit_in_script(capture):
    live.live_query_functions()
    assert "list.Count < 100" in capture["script"]


def test_set_function_text_default_limit_is_one(capture):
    # A mistaken name must not mass-edit the project.
    live.live_set_function_text(name="+X-K1", text="hi")
    assert "details.Count >= 1" in capture["script"]


# ---------------------------------------------------------------------------
# String parameters must stay inside their literals
# ---------------------------------------------------------------------------

def test_contains_injection_stays_in_literal(capture):
    live.live_query_functions(contains=INJECTION)
    cs = capture["script"]
    # The payload survives as DATA inside the literal - that is correct. What
    # matters is that its quotes were escaped so it cannot close the literal and
    # become code: no bare `"` remains around the injected text.
    assert '\\"' in cs
    assert _string_literals_balanced(cs)
    # Every quote in the payload is escaped, so the only occurrences of a bare
    # quote are the literal's own delimiters.
    assert '"' + INJECTION not in cs


def test_name_and_text_injection_stay_in_literal(capture):
    live.live_set_function_text(name=INJECTION, text=INJECTION)
    cs = capture["script"]
    assert _string_literals_balanced(cs)


def test_backslash_path_is_escaped(capture):
    live.live_set_function_text(name=r"+X-K1", text=r"C:\Temp\A1")
    cs = capture["script"]
    # A raw C:\Temp\A1 would be CS1009 (unrecognized escape \T, \A).
    assert r"C:\\Temp\\A1" in cs
    assert _string_literals_balanced(cs)


def test_unicode_line_terminator_stays_in_literal(capture):
    ls = chr(0x2028)
    live.live_query_pages(contains=f"a{ls}b")
    assert ls not in capture["script"]


# ---------------------------------------------------------------------------
# Contract details
# ---------------------------------------------------------------------------

def test_set_function_text_requires_name(capture):
    result = live.live_set_function_text(name="", text="hi")
    assert result["success"] is False
    assert "script" not in capture


@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_timeout_is_forwarded(capture, fn, kwargs):
    fn(timeout_seconds=123.0, **kwargs)
    assert capture["timeout"] == 123.0


@pytest.mark.parametrize("fn,kwargs", ALL_TOOLS)
def test_class_names_are_unique_per_call(capture, fn, kwargs):
    fn(**kwargs)
    first = capture["script"]
    fn(**kwargs)
    second = capture["script"]
    # Same script twice would collide in EPLAN's script engine cache.
    assert first != second


def test_ambiguous_match_guard_present(capture):
    # Function.Properties and FUNC_TEXT[int] both make a plain GetProperty throw
    # AmbiguousMatchException; the generated code must use the guarded lookup.
    live.live_set_function_text(name="+X-K1", text="hi")
    cs = capture["script"]
    assert "DeclaredOnly" in cs
    assert "Type.EmptyTypes" in cs
    assert 'GetPropInfo(props.GetType(), "FUNC_TEXT")' in cs


# ---------------------------------------------------------------------------
# discovery.list_layers reaches the DataModel through the same reflection
# scaffold. Emitting `using Eplan.EplApi.DataModel;` is CS0234, which means the
# script never compiles, never writes its result file, and the tool can only
# ever time out - so guard the using-directive absence here too.
# ---------------------------------------------------------------------------

def test_list_layers_uses_reflection_not_using_directive(monkeypatch):
    from api.actions import discovery

    captured = {}

    def fake_execute(script, timeout=30.0):
        captured["script"] = script
        captured["timeout"] = timeout
        return {"success": True, "results": {"stubbed": True}}

    monkeypatch.setattr(discovery, "_execute_script", fake_execute)
    discovery.list_layers()

    cs = captured["script"]
    for line in cs.splitlines():
        if line.strip().startswith("using "):
            assert "Eplan.EplApi.DataModel" not in line
            assert "Eplan.EplApi.HEServices" not in line
    # goes through the shared reflection scaffold
    assert "AppDomain.CurrentDomain.GetAssemblies()" in cs
    assert 'FindType("Eplan.EplApi.DataModel.LockingStep")' in cs
    assert 'GetPropInfo(project.GetType(), "LayerTable")' in cs
    # a 457-layer table needs more headroom than the 30s default
    assert captured["timeout"] > 30.0


# ---------------------------------------------------------------------------
# live_set_connection_designations - connection point numbers (property 20022)
# ---------------------------------------------------------------------------

def test_conn_designations_writes_each_index_in_order(capture):
    live.live_set_connection_designations("+-TEST", ["Y11", "Y12"])
    cs = capture["script"]
    # one designation per index, in the order given
    assert 'string[] wanted = new string[] { "Y11", "Y12" };' in cs
    # and they are written by slot, not as one joined blob
    assert "int slot = i + 1;" in cs


def test_conn_designations_uses_indexed_property_lookup(capture):
    # FUNC_CONNECTIONDESIGNATION is declared both plain and [int]; the indexed
    # form is the one that addresses a single connection point. Using the
    # non-indexed lookup here would fetch the wrong declaration.
    live.live_set_connection_designations("+-TEST", ["Y11"])
    cs = capture["script"]
    assert 'GetPropInfoIdx(props.GetType(), "FUNC_CONNECTIONDESIGNATION")' in cs
    assert "new Type[] { typeof(int) }" in cs


def test_conn_designations_reads_back_each_slot(capture):
    # The reported value must come from a fresh fetch, not the local wrapper.
    live.live_set_connection_designations("+-TEST", ["Y11", "Y12"])
    cs = capture["script"]
    assert cs.count("cdIdx.GetValue(props, new object[] { slot })") >= 2


def test_conn_designations_rejects_joined_string(capture):
    # "Y11¶Y12" as one value would land entirely in index 1.
    result = live.live_set_connection_designations("+-TEST", "Y11" + live.PILCROW + "Y12")
    assert result["success"] is False
    assert "list of strings" in result["error"]
    assert "script" not in capture


def test_conn_designations_rejects_pilcrow_inside_an_element(capture):
    result = live.live_set_connection_designations("+-TEST", ["Y11" + live.PILCROW + "Y12"])
    assert result["success"] is False
    assert "pilcrow" in result["error"]
    assert "script" not in capture


@pytest.mark.parametrize("bad", [[], None, 42, {}])
def test_conn_designations_rejects_bad_container(capture, bad):
    result = live.live_set_connection_designations("+-TEST", bad)
    assert result["success"] is False
    assert "script" not in capture


def test_conn_designations_rejects_none_element(capture):
    result = live.live_set_connection_designations("+-TEST", ["Y11", None])
    assert result["success"] is False
    assert "None" in result["error"]
    assert "script" not in capture


def test_conn_designations_requires_name(capture):
    result = live.live_set_connection_designations("", ["Y11"])
    assert result["success"] is False
    assert "script" not in capture


def test_conn_designations_default_limit_is_one(capture):
    live.live_set_connection_designations("+-TEST", ["Y11"])
    assert "details.Count >= 1" in capture["script"]


def test_conn_designations_coerces_non_string_elements(capture):
    live.live_set_connection_designations("+-TEST", [11, 12])
    assert 'new string[] { "11", "12" };' in capture["script"]


def test_conn_designations_escapes_injection(capture):
    live.live_set_connection_designations("+-TEST", [INJECTION, "Y12"])
    cs = capture["script"]
    assert _string_literals_balanced(cs)
    assert '"' + INJECTION not in cs


# ---------------------------------------------------------------------------
# live_read_check_messages - PrjMessagesCollection (Eplan.EplApi.EServices),
# the itemized store behind the "Message management" dialog. Reached the same
# way DataModel/HEServices are: never `using`, always FindType + reflection.
# ---------------------------------------------------------------------------

def test_read_check_messages_no_eservices_using_directive(capture):
    # EServices is a third namespace in the same CS0234 family as
    # DataModel/HEServices - must never appear as a `using` directive either.
    live.live_read_check_messages()
    for line in capture["script"].splitlines():
        if line.strip().startswith("using "):
            assert "Eplan.EplApi.EServices" not in line


def test_read_check_messages_resolves_collection_by_reflection(capture):
    live.live_read_check_messages()
    cs = capture["script"]
    assert 'FindType("Eplan.EplApi.EServices.PrjMessagesCollection")' in cs
    assert 'collType.GetMethod("GetPrjMsgEnumerator")' in cs
    assert 'itType.GetProperty("CurrentProjectMessage")' in cs


def test_read_check_messages_default_window(capture):
    live.live_read_check_messages()
    cs = capture["script"]
    assert "int startIdx = 1;" in cs
    assert "int endIdx = 50;" in cs


def test_read_check_messages_start_index_and_limit_set_the_window(capture):
    live.live_read_check_messages(start_index=1940, limit=1)
    cs = capture["script"]
    assert "int startIdx = 1940;" in cs
    assert "int endIdx = 1940;" in cs


def test_read_check_messages_limit_is_capped_at_500(capture):
    live.live_read_check_messages(start_index=1, limit=999999)
    cs = capture["script"]
    assert "int endIdx = 500;" in cs


def test_read_check_messages_rejects_start_index_below_one(capture):
    result = live.live_read_check_messages(start_index=0)
    assert result["success"] is False
    assert "script" not in capture


def test_read_check_messages_contains_stays_in_literal(capture):
    live.live_read_check_messages(contains=INJECTION)
    cs = capture["script"]
    assert _string_literals_balanced(cs)
    assert '"' + INJECTION not in cs


def test_read_check_messages_default_contains_is_empty_string(capture):
    # Matches() treats an empty filter as "match everything" - confirm that is
    # what an omitted `contains` actually compiles to, not the string "None".
    live.live_read_check_messages()
    assert 'string filterText = "";' in capture["script"]


# ---------------------------------------------------------------------------
# live_plc_io_list: the Python-side shaping must not need EPLAN
# ---------------------------------------------------------------------------

def test_split_identifier_iec_parts():
    assert live._split_identifier("=ST1+CE-A1:DI a.0") == {
        "installation": "=ST1", "location": "+CE", "device": "-A1",
        "connectionPoint": "DI a.0"}
    assert live._split_identifier("=+CE-A2:0+")["installation"] == "="
    assert live._split_identifier("-X1:12") == {
        "installation": "", "location": "", "device": "-X1", "connectionPoint": "12"}


def test_split_multilingual_and_neutral_text():
    assert live._split_multilingual("es_ES@Marcha;en_US@Start;") == {"es_ES": "Marcha", "en_US": "Start"}
    assert live._split_multilingual("??_??@Presion;") == {"*": "Presion"}
    assert live._split_multilingual("") == {}


@pytest.mark.parametrize("address,dtype,cp,kind", [
    ("I0.0", "BOOL", "DI a.0", "DI"),
    ("Q0.1", "BOOL", "DQ a.1", "DO"),
    ("IW96", "WORD", "0+", "AI"),
    ("QW80", "INT", "0+", "AO"),
    ("", "", "L+", "supply"),
    ("", "", "DI a.7", "other"),
])
def test_plc_kind(address, dtype, cp, kind):
    assert live._plc_kind(address, dtype, cp) == kind


def test_shape_plc_io_groups_by_installation_and_picks_manual_over_automatic():
    raw = {
        "success": True, "matched": 2,
        "rawPoints": [
            {"name": "=ST1+CE-A1:DI a.0", "FUNC_FULLDEVICETAG": "=ST1+CE-A1",
             "FUNC_CONNECTIONDESIGNATION1": "DI a.0", "FUNC_PLCADDRESS": "I0.0",
             "FUNC_PLCDATATYPE": "BOOL", "FUNC_TEXT": "es_ES@Marcha;en_US@Start;",
             "FUNC_PLCSYMBOLICADDRESS_MANUAL": "ST1_Start",
             "FUNC_PLCSYMBOLICADDRESS_AUTOMATIC": "auto", "page": "=ST1+CE/2"},
            {"name": "=ST2+CE-A1:DI a.0", "FUNC_FULLDEVICETAG": "=ST2+CE-A1",
             "FUNC_CONNECTIONDESIGNATION1": "DI a.0", "FUNC_PLCADDRESS": "I1.0",
             "FUNC_PLCDATATYPE": "BOOL", "FUNC_TEXT": "", "page": ""},
        ],
        "rawBoxes": [
            {"name": "=ST1+CE-A1", "FUNC_FULLDEVICETAG": "=ST1+CE-A1", "FUNC_PLCCPU": "1",
             "FUNC_PLCRACK": "0", "FUNC_PLC_SUBSLOT": "1", "parts": ["SIE.X"],
             "FUNC_TEXT": "en_US@CPU;", "page": "=ST1+CE/2"},
        ],
    }
    out = live._shape_plc_io(raw)
    assert out["success"] and out["returned"] == 2 and out["matched"] == 2
    assert out["byInstallation"] == {"=ST1": 1, "=ST2": 1}
    p0 = out["points"][0]
    assert p0["installation"] == "=ST1" and p0["device"] == "-A1" and p0["kind"] == "DI"
    assert p0["symbolicAddress"] == "ST1_Start"
    assert p0["functionText"] == {"es_ES": "Marcha", "en_US": "Start"}
    m0 = out["modules"][0]
    assert m0["isCpu"] is True and m0["rack"] == "0" and m0["slot"] == "1" and m0["parts"] == ["SIE.X"]


def test_plc_io_list_filters_are_interpolated_safely(capture):
    live.live_plc_io_list(contains=INJECTION, include_unaddressed=True, limit="25")
    cs = capture["script"]
    assert _string_literals_balanced(cs)
    assert "!true) continue;" in cs
    assert "points.Count >= 25" in cs


def test_plc_io_list_returns_raw_result_when_script_failed(monkeypatch):
    monkeypatch.setattr(live, "_execute_script", lambda s, timeout=0: {"success": False, "error": "boom"})
    out = live.live_plc_io_list()
    assert out == {"success": False, "error": "boom"}


# ---------------------------------------------------------------------------
# reports_live: generated C# shape and the S029153 hint
# ---------------------------------------------------------------------------

def test_create_report_script_shape(monkeypatch):
    from api.actions import reports_live
    captured = {}
    monkeypatch.setattr(reports_live, "_execute_script",
                        lambda s, timeout=0: captured.setdefault("script", s) and {"success": True, "results": {"success": True}})
    reports_live.live_create_report("TerminalDiagram", form_name="F13_001")
    cs = captured["script"]
    assert _string_literals_balanced(cs)
    assert "GuardScratch(project, false" in cs
    assert '"TerminalDiagram"' in cs and '"F13_001"' in cs
    assert '{ block, "/1" }' in cs, "empty first-page name is refused by EPLAN (S029035)"
    assert "GenerateProject" in cs
    for line in cs.splitlines():
        if line.strip().startswith("using "):
            assert "Eplan.EplApi.DataModel" not in line and "Eplan.EplApi.HEServices" not in line


def test_create_report_without_generate_and_injection(monkeypatch):
    from api.actions import reports_live
    captured = {}
    monkeypatch.setattr(reports_live, "_execute_script",
                        lambda s, timeout=0: captured.setdefault("script", s) and {"success": True, "results": {}})
    reports_live.live_create_report(INJECTION, form_name=INJECTION, page_name=INJECTION, generate=False)
    cs = captured["script"]
    assert _string_literals_balanced(cs)
    assert "if (false)" in cs


def test_create_report_adds_hint_on_missing_form(monkeypatch):
    from api.actions import reports_live
    monkeypatch.setattr(reports_live, "_execute_script",
                        lambda s, timeout=0: {"success": False, "error": "Exception: S029153 El formulario no se ha encontrado"})
    out = reports_live.live_create_report("PartsList", form_name="F03_001")
    assert "F03_001" in out["hint"] and "Sincronizar" in out["hint"]


def test_report_types_script_is_read_only(monkeypatch):
    from api.actions import reports_live
    captured = {}
    monkeypatch.setattr(reports_live, "_execute_script",
                        lambda s, timeout=0: captured.setdefault("script", s) and {"success": True})
    reports_live.live_report_types()
    assert "GuardScratch(project," not in captured["script"], "read-only tool must not run the write guard"
    assert "PAGE_FORMULAR" in captured["script"]


# ---------------------------------------------------------------------------
# structure_live: copy pages to another installation designation
# ---------------------------------------------------------------------------

def test_copy_pages_script_shape(monkeypatch):
    from api.actions import structure_live
    captured = {}
    monkeypatch.setattr(structure_live, "_execute_script",
                        lambda s, timeout=0: captured.setdefault("script", s) and {"success": True})
    structure_live.live_copy_pages_to_structure(["+CE/2"], ["=ST2", "ST3"], keep_devices=["-A1"], location="+CE")
    cs = captured["script"]
    assert _string_literals_balanced(cs)
    assert "GuardScratch(project, false" in cs
    assert 'new string[] { "ST2", "ST3" }' in cs, "leading = must be stripped"
    assert 'string locOverride = "CE";' in cs, "leading + must be stripped"
    assert 'new string[] { "-A1" }' in cs
    assert 'Enum.Parse(nmT, "Ignore")' in cs
    assert 'GetWritable(pl.GetType(), "VisibleName")' in cs


def test_copy_pages_rejects_bad_lists():
    from api.actions import structure_live
    assert structure_live.live_copy_pages_to_structure([], ["ST2"])["success"] is False
    assert structure_live.live_copy_pages_to_structure(["+CE/2"], [])["success"] is False
    assert structure_live.live_copy_pages_to_structure("+CE/2", ["ST2"])["success"] is False


def test_copy_pages_injection_stays_in_literal(monkeypatch):
    from api.actions import structure_live
    captured = {}
    monkeypatch.setattr(structure_live, "_execute_script",
                        lambda s, timeout=0: captured.setdefault("script", s) and {"success": True})
    structure_live.live_copy_pages_to_structure([INJECTION], [INJECTION], keep_devices=[INJECTION], location=INJECTION)
    assert _string_literals_balanced(captured["script"])


# ---------------------------------------------------------------------------
# panel_live: captions read / edit
# ---------------------------------------------------------------------------

def _capture_panel(monkeypatch):
    from api.actions import panel_live
    captured = {}
    monkeypatch.setattr(panel_live, "_execute_script",
                        lambda s, timeout=0: captured.setdefault("script", s) and {"success": True})
    return panel_live, captured


def test_panel_captions_read_is_guard_free(monkeypatch):
    panel_live, captured = _capture_panel(monkeypatch)
    panel_live.live_panel_captions("+CE/0", ref=INJECTION)
    cs = captured["script"]
    assert _string_literals_balanced(cs)
    assert "GuardScratch(project," not in cs
    assert "PropertyPlacements" in cs and "GetBoundingBox" in cs


def test_set_panel_captions_script_shape(monkeypatch):
    panel_live, captured = _capture_panel(monkeypatch)
    panel_live.live_set_panel_captions("+CE/0", "124/17/1409/0", hide=[20100, "PTIO-IN"], show=["20450"],
                                       positions={"20010": [386, 223]}, legend_position=9)
    cs = captured["script"]
    assert _string_literals_balanced(cs)
    assert "GuardScratch(project, false" in cs
    assert '"UseLocalPropertyPlacements"' in cs, "edits must switch to the local arrangement first"
    assert 'new string[] { "20100", "PTIO-IN" }' in cs
    assert 'new string[] { "20450" }' in cs
    assert 'new string[] { "20010", "386.0", "223.0" }' in cs
    assert "op_Implicit" in cs and "new object[] { 9 }" in cs
    assert '"IsDocked"' in cs


def test_set_panel_captions_rejects_bad_positions():
    from api.actions import panel_live
    assert panel_live.live_set_panel_captions("+CE/0", "-X1", positions=[1, 2])["success"] is False
    assert panel_live.live_set_panel_captions("+CE/0", "-X1", positions={"20010": [1]})["success"] is False
    assert panel_live.live_set_panel_captions("+CE/0", "-X1", legend_position="x")["success"] is False


def test_set_panel_captions_without_legend_skips_indexer(monkeypatch):
    panel_live, captured = _capture_panel(monkeypatch)
    panel_live.live_set_panel_captions("+CE/0", "-X1", hide=[INJECTION], positions={INJECTION: [0, 0]})
    cs = captured["script"]
    assert _string_literals_balanced(cs)
    assert "if (false)" in cs


# ---------------------------------------------------------------------------
# terminals_live / cables_live / project_live
# ---------------------------------------------------------------------------

def _capture(monkeypatch, module):
    captured = {}
    monkeypatch.setattr(module, "_execute_script",
                        lambda s, timeout=0: captured.setdefault("script", s) and {"success": True})
    return captured


def test_terminal_strips_read_is_guard_free(monkeypatch):
    from api.actions import terminals_live
    captured = _capture(monkeypatch, terminals_live)
    terminals_live.live_terminal_strips(contains=INJECTION)
    cs = captured["script"]
    assert _string_literals_balanced(cs)
    assert "GuardScratch(project," not in cs
    assert '"TerminalStrip"' in cs and '== "Terminal"' in cs, "PLC points must be filtered by category"


def test_set_terminal_strip_definition_script_shape(monkeypatch):
    from api.actions import terminals_live
    captured = _capture(monkeypatch, terminals_live)
    terminals_live.live_set_terminal_strip_definition("-X1", text=INJECTION, part="PXC.1", properties={"FUNC_SUPPLEMENTARYFIELD1": INJECTION})
    cs = captured["script"]
    assert _string_literals_balanced(cs)
    assert "GuardScratch(project, false" in cs
    assert '"TerminalDefText", (short)1, (short)1' in cs
    assert 'RequireMethod(tsT, "Create", new string[] { "Project", "FunctionDefinition" }' in cs
    assert "propsIn[\"FUNC_SUPPLEMENTARYFIELD1\"]" in cs


def test_set_terminal_strip_definition_placed_uses_create_page(monkeypatch):
    from api.actions import terminals_live
    captured = _capture(monkeypatch, terminals_live)
    terminals_live.live_set_terminal_strip_definition("-X1", page="+CE/2", x=300, y=200)
    cs = captured["script"]
    assert 'RequireMethod(tsT, "Create", new string[] { "Page" }' in cs
    assert "300.0, 200.0" in cs
    assert terminals_live.live_set_terminal_strip_definition("-X1", page="+CE/2")["success"] is False


def test_cables_read_is_guard_free(monkeypatch):
    from api.actions import cables_live
    captured = _capture(monkeypatch, cables_live)
    cables_live.live_cables(contains=INJECTION)
    cs = captured["script"]
    assert _string_literals_balanced(cs)
    assert "GuardScratch(project," not in cs
    assert '"CableConnections"' in cs and '"CONNECTION_DESIGNATION"' in cs


def test_place_cable_definition_script_shape(monkeypatch):
    from api.actions import cables_live
    captured = _capture(monkeypatch, cables_live)
    cables_live.live_place_cable_definition("+CE/2", "-W1", 70, 112, 220, 112, text=INJECTION, generate=False)
    cs = captured["script"]
    assert _string_literals_balanced(cs)
    assert "GuardScratch(project, false" in cs
    assert 'MakeVariantT(project, "SPECIAL", "CABDL", 0)' in cs
    assert "MakePoint(pt, 70.0, 112.0), MakePoint(pt, 220.0, 112.0)" in cs
    assert '"DESIGNATION_LOCATION"' in cs, "a bare -W1 must take the page structure"
    assert cables_live.live_place_cable_definition("+CE/2", "-W1", "a", 1, 2, 3)["success"] is False


def test_assign_cable_connections_script_shape(monkeypatch):
    from api.actions import cables_live
    captured = _capture(monkeypatch, cables_live)
    cables_live.live_assign_cable_connections("+CE/2", INJECTION, generate=False)
    cs = captured["script"]
    assert _string_literals_balanced(cs)
    assert "GuardScratch(project, false" in cs
    assert '"CableNameParts"' in cs and '"CableDefinitionLine"' in cs
    assert '"StartSymbolConnPoint"' in cs, "pin offsets are added to the device location"


def test_create_project_refuses_outside_scratch(tmp_path, monkeypatch):
    from api.actions import project_live
    tpl = tmp_path / "t.zw9"
    tpl.write_text("x")
    r = project_live.live_create_project(r"C:\Projects\Real.elk", str(tpl))
    assert r["success"] is False and "scratch" in r["error"]
    r = project_live.live_create_project(str(tmp_path / "p.elk"), str(tmp_path / "missing.zw9"))
    assert r["success"] is False and "Template not found" in r["error"]


def test_create_project_clears_target_for_the_call(tmp_path, monkeypatch):
    from api.actions import project_live, target
    monkeypatch.setattr(project_live, "SCRATCH_ROOT", str(tmp_path))
    tpl = tmp_path / "t.zw9"
    tpl.write_text("x")
    seen = {}

    def fake(script, timeout=0):
        seen["target_during"] = target.get()
        seen["script"] = script
        return {"success": True, "results": {"success": True, "project": "P"}}
    monkeypatch.setattr(project_live, "_execute_script", fake)
    target.set_target(r"D:\old.elk")
    r = project_live.live_create_project(str(tmp_path / "new" / "P.elk"), str(tpl))
    assert seen["target_during"] == ""
    assert "CreateProject" in seen["script"] and _string_literals_balanced(seen["script"])
    assert target.get().lower().endswith("p.elk")
    target.set_target("")


def test_project_templates_lists_zw9(tmp_path):
    from api.actions import project_live
    (tmp_path / "a.zw9").write_text("x")
    (tmp_path / "b.ept").write_text("x")
    (tmp_path / "c.txt").write_text("x")
    r = project_live.live_project_templates(str(tmp_path))
    assert r["success"] and r["count"] == 2
    assert {t["kind"] for t in r["templates"]} == {"basic project", "template"}


def test_assign_cable_connections_reassign_runs_after_conductors_exist(monkeypatch):
    from api.actions import cables_live, generate
    scripts = []

    def fake_exec(script, timeout=0):
        scripts.append(script)
        return {"success": True, "results": {"success": True, "handle": "h", "cable": "=+CE-W1",
                                             "crossed": [{"designation": "201", "assigned": "now"}],
                                             "reassignedFromPart": "if (true)" in script}}
    monkeypatch.setattr(cables_live, "_execute_script", fake_exec)
    monkeypatch.setattr(generate, "generate_connections", lambda **kw: {"success": True})
    monkeypatch.setattr(cables_live, "live_cables",
                        lambda **kw: {"results": {"cables": [{"handle": "h", "conductors": [{"designation": "201"}]}]}})
    r = cables_live.live_assign_cable_connections("+CE/2", "-W1", generate=True, crossing_texts=False)
    # the script carries the whole chain: updates + conductor read, reassign only with a part
    assert len(scripts) == 1 and "if (false && conductors.Count > 0)" in scripts[0]
    assert "RunAction(genCmd)" in scripts[0] and '/PAGENAME:\\"+CE/2\\"' in scripts[0] and "CableConductors(cable)" in scripts[0]
    # the fake script did not report inlineGenerate, so the wrapper fell back to its own loop + reassign script
    scripts.clear()
    r = cables_live.live_assign_cable_connections("+CE/2", "-W1", generate=True, reassign_from_part=True, crossing_texts=False, pin_colours=None)
    assert len(scripts) == 2 and "if (true && conductors.Count > 0)" in scripts[0] and "if (true)" in scripts[1]
    assert "DoReassignWires" in scripts[0] and "DoReassignWires" in scripts[1] and _string_literals_balanced(scripts[0])
    assert r["results"]["reassignedFromPart"] is True and r["results"]["connectionsGenerated"] == 2


def test_assign_cable_connections_inline_chain_skips_python_loop(monkeypatch):
    """When the script did the updates itself, no generate / read / reassign round-trips follow."""
    from api.actions import cables_live, generate
    scripts, gens, reads = [], [], []
    monkeypatch.setattr(cables_live, "_execute_script",
                        lambda s, timeout=0: scripts.append(s) or {"success": True, "results": {
                            "success": True, "handle": "h", "cable": "=+CE-W1", "crossed": [{"designation": "201", "assigned": "now"}],
                            "inlineGenerate": True, "connectionsGenerated": 3, "reassignedFromPart": True, "lockedPoints": 3,
                            "conductors": [{"designation": "201", "colour": "BN"}]}})
    monkeypatch.setattr(generate, "generate_connections", lambda **kw: gens.append(1) or {"success": True})
    monkeypatch.setattr(cables_live, "live_cables", lambda **kw: reads.append(1) or {"results": {"cables": []}})
    pins = []
    monkeypatch.setattr(cables_live, "live_set_conductor_colours",
                        lambda page, cable, **kw: pins.append(kw) or {"results": {"success": True, "skipped": "x"}})
    r = cables_live.live_assign_cable_connections("+CE/2", "-W1", reassign_from_part=True, crossing_texts=False, pin_colours="sensor", timeout_seconds=180)
    assert len(scripts) == 1 and gens == [] and reads == []
    res = r["results"]
    assert res["connectionsGenerated"] == 3 and res["conductors"][0]["colour"] == "BN" and res["lockedPoints"] == 3
    assert pins and pins[0]["timeout_seconds"] == 60.0, "the pin-colour step keeps a short leash"


def test_place_cable_definition_applies_scheme_after_assign(monkeypatch):
    from api.actions import cables_live, captions_live
    calls = []
    monkeypatch.setattr(cables_live, "_execute_script",
                        lambda s, timeout=0: {"success": True, "results": {"success": True, "handle": "h1", "name": "=+CE-W1"}})
    monkeypatch.setattr(cables_live, "live_assign_cable_connections",
                        lambda *a, **kw: calls.append(("assign", kw)) or {"results": {"success": True, "crossed": [], "conductors": []}})
    monkeypatch.setattr(captions_live, "live_set_property_texts",
                        lambda page, ref, **kw: calls.append(("texts", page, ref, kw)) or {"results": {"success": True, "scheme": kw.get("scheme")}})
    r = cables_live.live_place_cable_definition("+CE/2", "-W1", 70, 112, 220, 112, part="FES.541348",
                                                scheme="Festo", show=["31004"])
    assert [c[0] for c in calls] == ["assign", "texts"]
    assert calls[0][1]["reassign_from_part"] is True
    assert calls[1][1] == "+CE/2" and calls[1][2] == "h1"
    assert calls[1][3]["scheme"] == "Festo" and calls[1][3]["show"] == ["31004"]
    assert r["results"]["propertyTexts"]["scheme"] == "Festo"
    # no scheme / show: the texts tool is not called
    calls.clear()
    cables_live.live_place_cable_definition("+CE/2", "-W1", 70, 112, 220, 112, generate=False)
    assert calls == []


def test_reassign_locks_conductors_before_doreassignwires(monkeypatch):
    from api.actions import cables_live
    scripts = []
    monkeypatch.setattr(cables_live, "_execute_script",
                        lambda s, timeout=0: scripts.append(s) or {"success": True, "results": {"success": True}})
    cables_live.live_assign_cable_connections("+CE/2", "-W1", generate=False, reassign_from_part=True)
    cs = scripts[0]
    assert _string_literals_balanced(cs)
    i_lock = cs.index('LockHard(TryRead(cc, "StartSymbolReference", null));')
    i_call = cs.index("m.Invoke(csvc, new object[] { cable, true, excl });")
    assert i_lock < i_call, "conductors and their ends are locked before DoReassignWires"
    assert 'results["lockedConductors"] = lockedConductors;' in cs


def test_decorate_cable_crossings_script_and_passthrough(monkeypatch):
    from api.actions import cables_live, captions_live
    captured = _capture(monkeypatch, cables_live)
    cables_live.live_decorate_cable_crossings("+CE/2", INJECTION)
    cs = captured["script"]
    assert _string_literals_balanced(cs)
    assert "GuardScratch(project, false" in cs
    assert 'pl.GetType().Name != "ConnectionDefinitionPoint"' in cs
    assert 'if (id != 31004 && id != 31002 && id != 20010) continue;' in cs
    assert '"CONNECTION_WIRENUMBER", "CONNECTION_WIRECROSSSECTION", "CONNECTION_WIRECROSSSECTION_UNIT"' in cs
    assert 'GetWritable(pl.GetType(), "Name").SetValue(pl, cableName, null);' in cs
    # the assignment runs the decoration once conductors exist, and not when told not to
    calls = []
    monkeypatch.setattr(cables_live, "_execute_script",
                        lambda s, timeout=0: {"success": True, "results": {"success": True, "handle": "h", "cable": "=+CE-W1",
                                                                            "crossed": [{"designation": "201", "assigned": "now"}]}})
    from api.actions import generate
    monkeypatch.setattr(generate, "generate_connections", lambda **kw: {"success": True})
    monkeypatch.setattr(cables_live, "live_cables",
                        lambda **kw: {"results": {"cables": [{"handle": "h", "conductors": [{"designation": "201"}]}]}})
    monkeypatch.setattr(cables_live, "live_decorate_cable_crossings",
                        lambda page, cable, **kw: calls.append((page, cable)) or {"results": {"success": True, "crossingPoints": []}})
    r = cables_live.live_assign_cable_connections("+CE/2", "-W1", generate=True)
    assert calls == [("+CE/2", "h")] and r["results"]["crossings"]["success"] is True
    calls.clear()
    cables_live.live_assign_cable_connections("+CE/2", "-W1", generate=True, crossing_texts=False)
    assert calls == []


def test_conductor_colours_by_pin_script(monkeypatch):
    from api.actions import cables_live
    captured = _capture(monkeypatch, cables_live)
    cables_live.live_set_conductor_colours("+CE/2", INJECTION, pin_colours={"1": "BN", "4": "BK", "3": "BU"})
    cs = captured["script"]
    assert _string_literals_balanced(cs)
    assert 'map["1"] = "BN";' in cs and 'map["4"] = "BK";' in cs and "bool auto = false;" in cs
    assert "string pin1 = PinDesignation(srA, p1o), pin2 = PinDesignation(srB, p2o);" in cs and 'SetProp(cp, "CONNECTION_WIRENUMBER", want);' in cs
    # a DCP black-box pin: the designation comes from the DCP function's name, not the symbol pin
    assert "int c = n.LastIndexOf(':');" in cs and 'PropText(props, "FUNC_CONNECTIONDESIGNATION")' in cs
    cables_live.live_set_conductor_colours("+CE/2", "-W1", pin_colours="auto")
    cs = captured["script"] if False else None  # _capture keeps the first script only
    assert cables_live.live_set_conductor_colours("+CE/2", "-W1", pin_colours="nope")["success"] is False
    assert cables_live._pin_map("sensor")[0]["3"] == "BU" and cables_live._pin_map("auto")[1] is True


def test_decorate_style_and_pin_colours_passthrough(monkeypatch):
    from api.actions import cables_live, generate
    scripts = []
    monkeypatch.setattr(cables_live, "_execute_script",
                        lambda s, timeout=0: scripts.append(s) or {"success": True, "results": {"success": True}})
    cables_live.live_decorate_cable_crossings("+CE/2", "-W1", style={"rotation": 0, "docked": True, "section": False})
    cs = scripts[-1]
    assert _string_literals_balanced(cs)
    assert "if (id == 31002 && !false)" in cs and '"IsDocked").SetValue(pp, true, null)' in cs and "if (!true)" in cs
    assert cables_live.live_decorate_cable_crossings("+CE/2", "-W1", style={"bogus": 1})["success"] is False
    calls = []
    monkeypatch.setattr(cables_live, "_execute_script",
                        lambda s, timeout=0: {"success": True, "results": {"success": True, "handle": "h", "cable": "=+CE-W1",
                                                                            "crossed": [{"designation": "201", "assigned": "now"}]}})
    monkeypatch.setattr(generate, "generate_connections", lambda **kw: {"success": True})
    monkeypatch.setattr(cables_live, "live_cables",
                        lambda **kw: {"results": {"cables": [{"handle": "h", "conductors": [{"designation": "201"}]}]}})
    monkeypatch.setattr(cables_live, "live_set_conductor_colours",
                        lambda page, cable, **kw: calls.append(("pins", kw.get("pin_colours"))) or {"results": {"success": True, "conductors": []}})
    monkeypatch.setattr(cables_live, "live_decorate_cable_crossings",
                        lambda page, cable, **kw: calls.append(("decor", kw.get("style"))) or {"results": {"success": True}})
    r = cables_live.live_assign_cable_connections("+CE/2", "-W1", generate=True, reassign_from_part=True,
                                                  crossing_style={"rotation": 0}, pin_colours={"1": "BN"})
    assert calls == [("pins", {"1": "BN"}), ("decor", {"rotation": 0})]
    assert r["results"]["pinColours"]["success"] is True
    calls.clear()
    cables_live.live_assign_cable_connections("+CE/2", "-W1", generate=True, reassign_from_part=False)
    assert calls == [("decor", None)], "no reassign from part: nothing to pair with pins"


def test_decorate_extra_points_and_hard_locks(monkeypatch):
    from api.actions import cables_live
    scripts = []
    monkeypatch.setattr(cables_live, "_execute_script",
                        lambda s, timeout=0: scripts.append(s) or {"success": True, "results": {"success": True}})
    cables_live.live_decorate_cable_crossings("+CE/2", "-W1")
    cs = scripts[-1]
    assert _string_literals_balanced(cs)
    assert 'string extraMode = "hide";' in cs and 'if (des != "") continue;' in cs
    assert "LockHard(pl);" in cs and "LockHard(cc);" in cs
    cables_live.live_decorate_cable_crossings("+CE/2", "-W1", extra_points="remove")
    assert 'string extraMode = "remove";' in scripts[-1]
    assert cables_live.live_decorate_cable_crossings("+CE/2", "-W1", extra_points="nope")["success"] is False
    # reassign locks the page's connection definition points too
    cables_live.live_assign_cable_connections("+CE/2", "-W1", generate=False, reassign_from_part=True)
    assert 'pl.GetType().Name == "ConnectionDefinitionPoint") { LockHard(pl); lockedPoints++; }' in scripts[-1]
    # the placed line gets a visible tag
    cables_live.live_place_cable_definition("+CE/2", "-W1", 70, 112, 220, 112, generate=False)
    assert 'string shown = "-W1";' in scripts[-1] and '"VisibleName").SetValue(fn, shown, null);' in scripts[-1]


def test_copy_pages_keeps_subcounter(monkeypatch):
    from api.actions import structure_live
    captured = {}
    monkeypatch.setattr(structure_live, "_execute_script",
                        lambda s, timeout=0: captured.setdefault("script", s) and {"success": True})
    structure_live.live_copy_pages_to_structure(["+CE/2.a"], ["ST2"])
    cs = captured["script"]
    assert '"PAGE_SUBCOUNTER"' in cs and 'SetProp(ppl, "PAGE_SUBCOUNTER", sub)' in cs


# ---------------------------------------------------------------------------
# Property texts (captions_live) - the general form of the panel captions
# ---------------------------------------------------------------------------

def test_property_texts_read_is_guard_free(monkeypatch):
    from api.actions import captions_live
    captured = _capture(monkeypatch, captions_live)
    captions_live.live_property_texts("=ST1+CAMPO/1", ref=INJECTION, with_schemes=True)
    cs = captured["script"]
    assert _string_literals_balanced(cs)
    assert "GuardScratch(project," not in cs
    assert "SchemeNames(pl)" in cs and '"PropertyPlacements"' in cs


def test_set_property_texts_script_order(monkeypatch):
    from api.actions import captions_live
    captured = _capture(monkeypatch, captions_live)
    captions_live.live_set_property_texts(
        "=ST1+CAMPO/1", "-X01", scheme="Arriba a la izquierda, fuera 90", hide=["22002"],
        positions={"20010": [60, 212]}, rotations={"20011": 90}, heights={"20010": 3.5},
        justifications={"20010": "BottomLeft"})
    cs = captured["script"]
    assert _string_literals_balanced(cs)
    assert "GuardScratch(project, false" in cs
    # scheme first, local switch after (selecting a scheme resets UseLocal)
    assert cs.index("SelectScheme(pl,") < cs.index('"UseLocalPropertyPlacements"')
    # degrees in, radians to EPLAN; moves last
    assert "* Math.PI / 180.0" in cs
    assert cs.index('"Rotation"') < cs.index("measure-and-correct")
    assert 'new string[] { "20010", "60.0", "212.0", "TopLeft" }' in cs
    assert 'new string[] { "20011", "90.0" }' in cs
    assert 'new string[] { "20010", "BottomLeft" }' in cs
    assert "bool anyEdit = true;" in cs


def test_set_property_texts_rejects_bad_shapes():
    from api.actions import captions_live
    assert captions_live.live_set_property_texts("p", "-X1", positions={"20010": [1]})["success"] is False
    assert captions_live.live_set_property_texts("p", "-X1", rotations=[90])["success"] is False
    assert captions_live.live_set_property_texts("p", "-X1", heights={"20010": "big"})["success"] is False


def test_panel_captions_report_rotation_height_and_suffix_match(monkeypatch):
    panel_live, captured = _capture_panel(monkeypatch)
    panel_live.live_panel_captions("+CE/0", ref="-X1")
    cs = captured["script"]
    assert 'd["rotation"]' in cs and 'd["height"]' in cs and 'd["justification"]' in cs
    assert 'n.EndsWith(wanted)' in cs, "a bare -X1 must match =ST1+CAMPO-X1 as a suffix"


def test_remove_placement_brief_mode(monkeypatch):
    from api.actions import schematic
    captured = _capture(monkeypatch, schematic)
    schematic.live_remove_placement("+CE/2", handle="1/2/3/0")
    cs = captured["script"]
    assert "true ? PageSummary(page) : ReadPage(page, 200, false, null)" in cs, "brief is the default"
    assert "LockTree(target);" in cs
    captured.clear()
    schematic.live_remove_placement("+CE/2", remove_page=True, brief=False)
    cs = captured["script"]
    assert "false ? PageSummary(page) : ReadPage(page, 500, false, null)" in cs
    # the page and everything on it are write-locked first; a stubborn object is cleared one by one
    assert "LockTree(page);" in cs and "foreach (object pl in PagePlacements(page)) if (pl != null) { LockTree(pl); onPage.Add(pl); }" in cs
    assert 'results["clearedFirst"] = cleared;' in cs and "static void LockTree(object o)" in cs


def test_assign_cable_connections_checks_name_parts_and_locks(monkeypatch):
    from api.actions import cables_live
    captured = _capture(monkeypatch, cables_live)
    cables_live.live_assign_cable_connections("+CE/2", "-W1", generate=False)
    # the connection's own definition points are hard-locked before the cable is written into it
    assert 'object cdps0 = TryRead(cn, "ConnectionDefPoints", null);' in captured["script"] and "LockHard(q0)" in captured["script"]
    cs = captured["script"]
    assert _string_literals_balanced(cs)
    assert "static void LockAny(object o)" in cs
    assert 'PropText(cnp, "FUNC_CODE")' in cs and "&& named)" in cs, \
        "a connection linked to the line but without the cable's name parts is NOT 'already'"
    assert "LockAny(sr1); LockAny(sr2);" in cs, "pre-existing connections need their ends locked on retry"


def test_place_cable_removes_orphan_points_before_the_line(monkeypatch):
    """Crossing points of a deleted line keep a new line from linking: the
    placing tool removes them (and updates once) BEFORE Cable.Create."""
    from api.actions import cables_live, generate
    scripts, gens = [], []
    answers = [
        {"success": True, "results": {"success": True, "removed": 1, "orphanPoints": [{"handle": "p1", "name": "=+", "x": 80.0, "y": 110.0, "removed": True}]}},
        {"success": True, "results": {"success": True, "handle": "h", "name": "=+CE-W1"}},
    ]
    monkeypatch.setattr(cables_live, "_execute_script", lambda s, timeout=0: scripts.append(s) or answers[len(scripts) - 1])
    monkeypatch.setattr(generate, "generate_connections", lambda **kw: gens.append(kw) or {"success": True})
    monkeypatch.setattr(cables_live, "live_assign_cable_connections",
                        lambda page, cable, **kw: {"results": {"success": True, "crossed": [], "conductors": []}})
    r = cables_live.live_place_cable_definition("+CE/2", "-W1", 70, 110, 220, 110, part="FES.541348")
    orphan, create = scripts
    assert _string_literals_balanced(orphan) and "LiveCableOrphans_" in orphan
    assert "double sx1 = 70.0, sy1 = 110.0, sx2 = 220.0, sy2 = 110.0;" in orphan
    assert 'bool orphan = line == null && (kind == "Cable" || (nameless && des == ""));' in orphan
    assert "if (true)" in orphan and 'pl.GetType().GetMethod("Remove", Type.EmptyTypes).Invoke(pl, null)' in orphan
    assert "LiveCableDef_" in create and scripts.index(create) == 1
    assert gens == [{"project_name": None, "page_name": "+CE/2"}]
    assert r["results"]["orphanPoints"][0]["handle"] == "p1"
    # generate=False places the bare line and looks at nothing
    scripts.clear(); gens.clear(); answers[0] = answers[1]
    cables_live.live_place_cable_definition("+CE/2", "-W1", 70, 110, 220, 110, generate=False)
    assert len(scripts) == 1 and "LiveCableDef_" in scripts[0] and gens == []


def test_assign_replaces_a_stale_line_when_nothing_links(monkeypatch):
    """Three updates and no conductor: remove the orphan points, the line,
    and place it again on the same wires with its part and visible name."""
    from api.actions import cables_live, generate, schematic
    assign = {"success": True, "results": {"success": True, "handle": "h", "cable": "=+CE-W1",
                                             "crossed": [{"designation": "201", "assigned": "now"}],
                                             "cableStart": [70.0, 110.0], "cableEnd": [220.0, 110.0],
                                             "cableVisibleName": "-W1", "cableText": "", "cableParts": ["FES.541348"],
                                             "cableSymbol": "CABDL", "cableVariant": 0}}
    orphan = {"success": True, "results": {"success": True, "removed": 3, "orphanPoints": [{"handle": "p"}] * 3}}
    monkeypatch.setattr(cables_live, "_execute_script",
                        lambda s, timeout=0: orphan if "LiveCableOrphans_" in s else assign)
    gens = []
    monkeypatch.setattr(generate, "generate_connections", lambda **kw: gens.append(kw) or {"success": True})
    monkeypatch.setattr(cables_live, "live_cables", lambda **kw: {"results": {"cables": [{"handle": "h", "conductors": []}]}})
    recreated, scripts = [], []
    real_exec = cables_live._execute_script
    def exec_spy(s, timeout=0):
        scripts.append(s)
        if "LiveCableRecreate_" in s:
            recreated.append(s)
            return {"success": True, "results": {"success": True, "newHandle": "h2", "newName": "=ST1+CAMPO-W1", "removedPoints": 2}}
        return real_exec(s, timeout=timeout)
    monkeypatch.setattr(cables_live, "_execute_script", exec_spy)
    calls = []
    orig_assign = cables_live.live_assign_cable_connections
    def assign_spy(page, cable, **kw):
        calls.append((page, cable, kw))
        if kw.get("replaced_line"):
            return {"success": True, "results": {"success": True, "handle": cable, "conductors": [{"designation": "201"}]}}
        return orig_assign(page, cable, **kw)
    monkeypatch.setattr(cables_live, "live_assign_cable_connections", assign_spy)
    r = assign_spy("+CE/2", "-W1", reassign_from_part=True, pin_colours="sensor",
                   crossing_style={"rotation": 0}, extra_points="remove")
    res = r["results"]
    assert res["replacedLine"] is True and res["previousHandle"] == "h" and len(res["orphanPoints"]) == 3 and res["removedPoints"] == 2
    assert len(recreated) == 1 and len(gens) == 4   # three updates, then one after the points went
    rc = recreated[0]
    assert _string_literals_balanced(rc)
    assert 'Handle(pl) == "h"' in rc and "WindowMacro.Create" in rc and 'if ((PropText(pl, "Name") ?? "") != cableName) continue;' in rc
    assert 'cable.GetType().GetMethod("Remove", Type.EmptyTypes).Invoke(cable, null);' in rc and 'Enum.Parse(wp[5].ParameterType, "Absolute")' in rc
    page, h, kw = calls[-1]
    assert (page, h) == ("+CE/2", "h2") and kw["replaced_line"] is True and kw["reassign_from_part"] is True
    assert kw["pin_colours"] == "sensor" and kw["crossing_style"] == {"rotation": 0} and kw["extra_points"] == "remove"
    # the clone's own assignment never replaces again
    recreated.clear()
    r = orig_assign("+CE/2", "-W1", replaced_line=True)
    assert recreated == [] and r["results"]["conductors"] == []


def test_assign_updates_once_when_the_line_crosses_nothing_yet(monkeypatch):
    """Wires drawn in this session have no Connection objects until the
    first update: the assignment updates once and looks again."""
    from api.actions import cables_live, generate
    runs = []
    def fake(s, timeout=0):
        runs.append(s)
        crossed = [] if len(runs) == 1 else [{"designation": "1", "assigned": "now"}]
        return {"success": True, "results": {"success": True, "handle": "h", "cable": "=+CE-W1", "crossed": crossed}}
    monkeypatch.setattr(cables_live, "_execute_script", fake)
    gens = []
    monkeypatch.setattr(generate, "generate_connections", lambda **kw: gens.append(1) or {"success": True})
    monkeypatch.setattr(cables_live, "live_cables",
                        lambda **kw: {"results": {"cables": [{"handle": "h", "conductors": [{"designation": "1"}]}]}})
    r = cables_live.live_assign_cable_connections("+CE/2", "-W1", crossing_texts=False)
    assert r["results"]["generatedFirst"] is True and len(runs) == 2 and len(gens) == 2
    assert r["results"]["conductors"] == [{"designation": "1"}]
    # generate=False never updates
    runs.clear(); gens.clear()
    cables_live.live_assign_cable_connections("+CE/2", "-W1", generate=False)
    assert len(runs) == 1 and gens == []


def test_set_property_texts_batch_items_one_script(monkeypatch):
    """Many placements in ONE script; each item scoped, failures per item;
    the compact result keeps only the touched captions."""
    from api.actions import captions_live
    scripts = []
    monkeypatch.setattr(captions_live, "_execute_script",
                        lambda s, timeout=0: scripts.append(s) or {"success": True, "results": {
                            "project": "P", "count": 2, "failed": 1, "success": False,
                            "items": [{"ref": "-X01", "success": True, "handle": "h1"},
                                      {"ref": "-B3", "success": False, "error": "No placement matches ref -B3 on the page."}]}})
    r = captions_live.live_set_property_texts("+CAMPO/1", items=[
        {"ref": "-X01", "positions": {"20010": [60, 212]}, "hide": ["22002"]},
        {"ref": "-B3", "rotations": {"20011": 90}, "scheme": "Arriba a la izquierda, fuera 90"},
    ])
    assert len(scripts) == 1
    cs = scripts[0]
    assert _string_literals_balanced(cs)
    assert cs.count('ir["ref"] = ') == 2 and cs.count("catch (Exception ex) { ir[\"success\"] = false;") == 2
    assert 'FindPanelItems(page, "-X01")' in cs and 'FindPanelItems(page, "-B3")' in cs
    assert 'new string[] { "20010", "60.0", "212.0", "TopLeft" }' in cs and 'new string[] { "20011", "90.0" }' in cs
    # the scheme and the local switch keep their order inside each item
    assert cs.index('SelectScheme(pl, "Arriba') < cs.rindex('"UseLocalPropertyPlacements"')
    # compact by default: no before list, after = touched captions only
    assert 'if (!true) ir["before"]' in cs and 'TouchedCaptions(pl, new string[] { "22002", "20010" })' in cs
    assert "static List<Dictionary<string, object>> TouchedCaptions(" in cs
    assert r["results"]["failed"] == 1 and r["results"]["items"][1]["success"] is False
    # brief=False keeps the full before / after lists
    scripts.clear()
    captions_live.live_set_property_texts("+CAMPO/1", items=[{"ref": "-X01", "hide": ["22002"]}], brief=False)
    assert 'if (!false) ir["before"]' in scripts[0] and "false ? TouchedCaptions" in scripts[0]
    # shapes
    assert captions_live.live_set_property_texts("p", items=[])["success"] is False
    assert captions_live.live_set_property_texts("p", items=[{"positions": {}}])["success"] is False
    assert captions_live.live_set_property_texts("p", "-X1", items=[{"ref": "-X2"}])["success"] is False
    assert captions_live.live_set_property_texts("p", items=[{"ref": "-X2", "bogus": 1}])["success"] is False
    assert captions_live.live_set_property_texts("p")["success"] is False


def test_set_property_texts_single_ref_keeps_flat_shape(monkeypatch):
    from api.actions import captions_live
    monkeypatch.setattr(captions_live, "_execute_script",
                        lambda s, timeout=0: {"success": True, "results": {
                            "project": "P", "count": 1, "failed": 0, "success": True,
                            "items": [{"ref": "-X01", "success": True, "handle": "h1", "name": "=+CE-X01", "after": [], "moves": []}]}})
    r = captions_live.live_set_property_texts("+CE/2", "-X01", hide=["22002"])
    assert r["success"] is True and r["results"]["handle"] == "h1" and "items" not in r["results"] and "ref" not in r["results"]
    monkeypatch.setattr(captions_live, "_execute_script",
                        lambda s, timeout=0: {"success": True, "results": {
                            "project": "P", "count": 1, "failed": 1, "success": False,
                            "items": [{"ref": "-X01", "success": False, "error": "No placement matches ref -X01 on the page."}]}})
    r = captions_live.live_set_property_texts("+CE/2", "-X01", hide=["22002"])
    assert r["success"] is False and "No placement" in r["error"]


def test_batch_compacts_edit_op_echoes():
    from api.actions import ops
    full = {"clrType": "ConnectionDefinitionPoint", "handle": "h", "name": "=+CE-W1", "location": {"x": 1, "y": 2},
            "wireNumber": "BN", "boundingBox": [{"x": 0, "y": 0}, {"x": 2, "y": 2}], "symbol": {"library": "SPECIAL"},
            "absentMembers": ["Name"], "pins": [1]}
    out = {"ops": [{"op": "update", "placed": dict(full)}, {"op": "place", "placed": dict(full)},
                   {"op": "remove", "placed": dict(full), "page_after": {"placementCount": 5, "placements": [1, 2, 3, 4, 5]}}]}
    ops._compact_ops(out)
    assert out["ops"][0]["placed"] == {"clrType": "ConnectionDefinitionPoint", "handle": "h", "name": "=+CE-W1", "location": {"x": 1, "y": 2}, "wireNumber": "BN"}
    assert out["ops"][1]["placed"] == full, "a place op keeps its pins and box"
    assert out["ops"][2]["page_after"] == {"placementCount": 5}


def test_tool_wrapper_returns_compact_json():
    import json, server
    wrapped = server._json_wrapper(lambda: {"a": [1, 2], "b": {"x": 1.5}})
    assert wrapped() == '{"a":[1,2],"b":{"x":1.5}}'
    assert json.loads(wrapped()) == {"a": [1, 2], "b": {"x": 1.5}}


def test_copy_region_script_and_chain(monkeypatch):
    """Region -> temp window macro -> insert -> rename maps; then one
    connection update and the cable chain on every copied cable."""
    from api.actions import copy_live, generate, cables_live
    scripts = []
    monkeypatch.setattr(copy_live, "_execute_script",
                        lambda s, timeout=0: scripts.append(s) or {"success": True, "results": {
                            "success": True, "inserted": 9, "cables": ["c1", "c2"], "renamed": 5, "copied": []}})
    gens, assigns = [], []
    monkeypatch.setattr(generate, "generate_connections", lambda **kw: gens.append(kw) or {"success": True})
    monkeypatch.setattr(cables_live, "live_assign_cable_connections",
                        lambda page, h, **kw: assigns.append((page, h, kw)) or {"results": {"success": True, "handle": h, "conductors": [{"designation": "1", "colour": "BN"}]}})
    r = copy_live.live_copy_region("=ST1+CAMPO/10", 100, 40, 160, 200, x=170, y=40,
                                   rename={"=ST1": "=ST3", "-MB01": "-MB50"}, text_replace={"Cilindro 1": "Cilindro 3"},
                                   pin_colours="sensor")
    cs = scripts[0]
    assert _string_literals_balanced(cs)
    assert 'FindPage(project, "=ST1+CAMPO/10")' in cs and cs.count('FindPage(project, "=ST1+CAMPO/10")') == 2, "same page by default"
    assert "double rx1 = 100.0, ry1 = 40.0, rx2 = 160.0, ry2 = 200.0;" in cs and "bool inside = false" in cs
    assert 'Enum.Parse(cp[1].ParameterType, "MultiLine")' in cs and "WindowMacro.Create" in cs and 'm.Name != "WindowMacro"' in cs
    assert "MakePoint(wp[4].ParameterType, 170.0, 40.0)" in cs
    assert 'new string[] { "=ST1", "=ST3" }, new string[] { "-MB01", "-MB50" }' in cs
    assert 'new string[] { "Cilindro 1", "Cilindro 3" }' in cs
    assert "static string Apply(string s, string[][] map)" in cs and "LockHard(o)" in cs
    # free texts are MultiLangStrings: read with GetAsString, written with SetAsString
    assert 'TryRead(o, "Contents", null)' in cs and 'GetMethod("GetAsString", Type.EmptyTypes)' in cs and 'GetMethod("SetAsString", new Type[] { typeof(string) })' in cs
    res = r["results"]
    assert 'else if (tn == "LocationBox")' in cs, "structure boxes are renamed too"
    assert "picked" not in res and "copied" not in res and res["failed"] == [] and res["pickedCount"] == 0, "brief by default"
    assert gens == [{"project_name": None, "page_name": "=ST1+CAMPO/10"}]
    assert [a[1] for a in assigns] == ["c1", "c2"] and assigns[0][2]["reassign_from_part"] is True and assigns[0][2]["pin_colours"] == "sensor"
    assert res["cableResults"][0]["conductors"] == [("1", "BN")] and r["success"] is True
    # another page, same spot is fine; the same page at the same spot is not
    scripts.clear()
    copy_live.live_copy_region("+CE/2", 0, 0, 10, 10, to_page="+CE/3")
    assert 'FindPage(project, "+CE/3")' in scripts[0] and "MakePoint(wp[4].ParameterType, 0.0, 0.0)" in scripts[0]
    assert copy_live.live_copy_region("+CE/2", 0, 0, 10, 10)["success"] is False
    assert copy_live.live_copy_region("+CE/2", 0, 0, 10, 10, x=50, y=0, rename=["a"])["success"] is False
    # touch mode and corner order
    scripts.clear()
    copy_live.live_copy_region("+CE/2", 160, 200, 100, 40, x=170, y=40, touch=True, cables=False)
    assert "double rx1 = 100.0, ry1 = 40.0, rx2 = 160.0, ry2 = 200.0;" in scripts[0] and "bool inside = true" in scripts[0]


def test_set_property_texts_anchor_corner(monkeypatch):
    from api.actions import captions_live
    captured = _capture(monkeypatch, captions_live)
    captions_live.live_set_property_texts("+CE/2", "-B1", positions={"20011": [60, 212, "BottomLeft"], "20010": [10, 20]},
                                          anchors={"20010": "Center"})
    cs = captured["script"]
    assert 'new string[] { "20011", "60.0", "212.0", "BottomLeft" }' in cs and 'new string[] { "20010", "10.0", "20.0", "Center" }' in cs
    assert "cur = AnchorOf(b, w[3]);" in cs and "now = AnchorOf(b, w[3]);" in cs and 'case "BottomLeft": return new double[] { b[0], b[1] };' in cs
    assert captions_live.live_set_property_texts("+CE/2", "-B1", positions={"20011": [1, 2, "Middle"]})["success"] is False
    assert captions_live.live_set_property_texts("+CE/2", "-B1", positions={"20011": [1, 2]}, anchors=["x"])["success"] is False
    # items carry anchors too
    captured.clear()
    captions_live.live_set_property_texts("+CE/2", items=[{"ref": "-B1", "positions": {"20011": [1, 2]}, "anchors": {"20011": "TopRight"}}])
    assert 'new string[] { "20011", "1.0", "2.0", "TopRight" }' in captured["script"]


def test_copy_region_brief_false_keeps_lists(monkeypatch):
    from api.actions import copy_live, generate
    monkeypatch.setattr(copy_live, "_execute_script",
                        lambda s, timeout=0: {"success": True, "results": {"success": True, "inserted": 2, "cables": [],
                                                                            "picked": [{"handle": "a"}], "copied": [{"handle": "b", "error": "x"}, {"handle": "c"}]}})
    monkeypatch.setattr(generate, "generate_connections", lambda **kw: {"success": True})
    r = copy_live.live_copy_region("+CE/2", 0, 0, 10, 10, x=50, y=0, brief=False)["results"]
    assert r["pickedCount"] == 1 and len(r["copied"]) == 2 and r["failed"] == [{"handle": "b", "error": "x"}]
    r = copy_live.live_copy_region("+CE/2", 0, 0, 10, 10, x=50, y=0)["results"]
    assert "copied" not in r and r["failed"] == [{"handle": "b", "error": "x"}]


def test_strand_layout_matches_the_nebu_macro():
    from api.actions import strand_live
    rows = strand_live.strand_layout([24, 32, 40], 136, 100, 28, ["BN", "BK", "BU"])
    got = {(k, x, y): (sym, v, d) for k, sym, v, x, y, d in rows}
    assert got[("connector", 24.0, 136.0)] == ("COST", 0, "BN") and got[("connector", 24.0, 100.0)] == ("COST", 6, "BN")
    assert got[("connector", 32.0, 136.0)] == ("TST", 4, "BK") and got[("connector", 32.0, 100.0)] == ("TST", 2, "BK")
    assert got[("connector", 40.0, 136.0)] == ("COST", 4, "BU") and got[("connector", 40.0, 100.0)] == ("COST", 2, "BU")
    assert got[("inter", 28.0, 136.0)] == ("TS", 4, "") and got[("inter", 28.0, 100.0)] == ("TS", 2, "")
    assert len(rows) == 8
    # the rows are told apart by height, not by which was called join
    flipped = {(k, x, y): (sym, v, d) for k, sym, v, x, y, d in strand_live.strand_layout([24, 32, 40], 100, 136, 28, None)}
    assert flipped[("connector", 24.0, 136.0)] == ("COST", 0, "") and flipped[("connector", 24.0, 100.0)] == ("COST", 6, "")
    import pytest
    with pytest.raises(strand_live.SchematicValueError):
        strand_live.strand_layout([24, 32, 40], 100, 100, 28, None)
    with pytest.raises(strand_live.SchematicValueError):
        strand_live.strand_layout([24, 32], 1, 2, 32, None)
    with pytest.raises(strand_live.SchematicValueError):
        strand_live.strand_layout([24, 32, 40], 1, 2, 28, ["BN"])


def test_place_strand_script(monkeypatch):
    from api.actions import strand_live
    captured = _capture(monkeypatch, strand_live)
    strand_live.live_place_strand("+CAMPO/1", [24, 32, 40], 136, 100, 28, designations=["BN", "BK", "BU"])
    cs = captured["script"]
    assert _string_literals_balanced(cs)
    assert 'new string[] { "connector", "COST", "0", "24.0", "136.0", "BN" }' in cs
    assert 'new string[] { "inter", "TS", "2", "28.0", "100.0", "" }' in cs
    assert '"Eplan.EplApi.DataModel.StrandInterConnector" : "Eplan.EplApi.DataModel.StrandConnector"' in cs
    assert 'RequireMethod(t, "Create", new string[] { "Page", "SymbolVariant" }, false)' in cs
    assert 'SetProp(props, "STRANDCONNECTOR_DESIGNATION", it[5]);' in cs and "if (true)" in cs and "if (id == 19072)" in cs
    captured.clear()
    strand_live.live_place_strand("+CAMPO/1", [24, 32, 40], 136, 100, 28, hide_designation=False)
    assert "if (false)" in captured["script"]
    assert strand_live.live_place_strand("+CAMPO/1", "24,32", 136, 100, 28)["success"] is False


def test_clean_cable_points_script(monkeypatch):
    from api.actions import cables_live, generate
    scripts = []
    monkeypatch.setattr(cables_live, "_execute_script",
                        lambda s, timeout=0: scripts.append(s) or {"success": True, "results": {"success": True, "removed": 2, "points": []}})
    gens = []
    monkeypatch.setattr(generate, "generate_connections", lambda **kw: gens.append(kw) or {"success": True})
    r = cables_live.live_clean_cable_points("+CAMPO/1", 60, 200, 20, 80, cable="=ST1+CAMPO-W90.1")
    cs = scripts[0]
    assert _string_literals_balanced(cs)
    assert "double rx1 = 20.0, ry1 = 80.0, rx2 = 60.0, ry2 = 200.0; string cableName = \"=ST1+CAMPO-W90.1\";" in cs
    assert 'if (des != "") continue;' in cs and '(cableName != "" && name == cableName)' in cs
    assert gens == [{"project_name": None, "page_name": "+CAMPO/1"}] and r["results"]["connectionsGenerated"] is True


def test_pin_colours_script_never_returns_early(monkeypatch):
    # an early `return;` in the body skipped the result file: the caller
    # waited the whole timeout (P26009 =MOV+CAMPO/13, 2026-11-09)
    from api.actions import cables_live
    scripts = []
    monkeypatch.setattr(cables_live, "_execute_script",
                        lambda s, timeout=0: scripts.append(s) or {"success": True, "results": {"success": True}})
    cables_live.live_set_conductor_colours("+CE/2", "-W1", pin_colours="auto")
    cs = scripts[-1]
    assert _string_literals_balanced(cs)
    body = cs[cs.index("bool auto = true;"):cs.index("string json = Newtonsoft")]
    assert "return;" not in body
    assert "skip = true; break;" in body and "if (!skip) foreach (object cc in conds)" in body
    # the connection's own definition points are locked before the colour write
    assert 'object dps0 = TryRead(cc, "ConnectionDefPoints", null);' in body
    assert "foreach (object pl in pageCdps)" in body


def test_reassign_reloads_empty_part_copy_and_fills_cable_fields(monkeypatch):
    from api.actions import cables_live
    scripts = []
    monkeypatch.setattr(cables_live, "_execute_script",
                        lambda s, timeout=0: scripts.append(s) or {"success": True, "results": {"success": True}})
    cables_live.live_assign_cable_connections("+CE/2", "-W1", generate=True, reassign_from_part=True)
    cs = scripts[-1]
    assert _string_literals_balanced(cs)
    assert 'GetMethod("LoadFromMasterdata", Type.EmptyTypes)' in cs and 'results["partReloaded"] = reloaded;' in cs
    assert '"DoReassignWires after reload: "' in cs
    assert 'new string[] { "FUNC_CABLE_CROSSSECTION_UNIT", "ARTICLE_WIRECROSSSECTION_UNIT" }' in cs
    assert 'results["cableFieldsFromPart"] = filled;' in cs


def test_place_cable_visible_name_relative_to_page(monkeypatch):
    from api.actions import cables_live
    scripts = []
    monkeypatch.setattr(cables_live, "_execute_script",
                        lambda s, timeout=0: scripts.append(s) or {"success": True, "results": {"success": True}})
    cables_live.live_place_cable_definition("=MOV+CAMPO/13", "=ST2+CAMPO-W17", 71, 84, 97, 84, generate=False)
    cs = scripts[-1]
    assert _string_literals_balanced(cs)
    assert 'string shown = "=ST2+CAMPO-W17";' in cs
    assert 'if (sPlant != null && sPlant != plant2) v += "=" + sPlant;' in cs
    assert '"VisibleName").SetValue(fn, shown, null); results["visibleName"] = shown;' in cs


def test_decorate_extra_points_anywhere_along_the_wire(monkeypatch):
    from api.actions import cables_live
    scripts = []
    monkeypatch.setattr(cables_live, "_execute_script",
                        lambda s, timeout=0: scripts.append(s) or {"success": True, "results": {"success": True}})
    cables_live.live_decorate_cable_crossings("+CE/2", "-W1", extra_points="remove")
    cs = scripts[-1]
    assert _string_literals_balanced(cs)
    assert "off > 6.0" not in cs
    assert "cond.Add(new object[] { horizontal ? x1 : y1, cc, Math.Min(a1, a2), Math.Max(a1, a2) });" in cs
    assert "along >= (double)c[2] - 0.05 && along <= (double)c[3] + 0.05" in cs


def test_assign_reads_again_before_replacing_the_line(monkeypatch):
    # the in-script conductor read right after the update can lag: a fresh
    # read that shows every wire linked means no clone
    from api.actions import cables_live, generate
    monkeypatch.setattr(cables_live, "_execute_script",
                        lambda s, timeout=0: {"success": True, "results": {
                            "success": True, "handle": "h", "cable": "=+CE-W1", "inlineGenerate": True, "connectionsGenerated": 3,
                            "conductors": [], "cableStart": [70, 110], "cableEnd": [220, 110],
                            "crossed": [{"assigned": "now"}, {"assigned": "now"}]}})
    monkeypatch.setattr(generate, "generate_connections", lambda **kw: {"success": True})
    monkeypatch.setattr(cables_live, "live_cables",
                        lambda **kw: {"results": {"cables": [{"handle": "h", "conductors": [{"designation": "1"}, {"designation": "2"}]}]}})
    called = []
    monkeypatch.setattr(cables_live, "_orphan_points", lambda *a, **k: called.append("orphans") or {"success": True, "orphanPoints": []})
    monkeypatch.setattr(cables_live, "_recreate_line", lambda *a, **k: called.append("recreate") or {"success": False})
    r = cables_live.live_assign_cable_connections("+CE/2", "-W1", generate=True, reassign_from_part=False, crossing_texts=False)
    res = r["results"]
    assert called == [] and res.get("freshRead") is True and len(res["conductors"]) == 2 and "replacedLine" not in res


def test_window_macro_inserts_lock_the_whole_page_first(monkeypatch):
    # Insert.WindowMacro writes into objects already on the page (P26009
    # =MOV+CAMPO/13: LockingException on a pin of an earlier column)
    from api.actions import ops, copy_live, cables_live
    scripts = []
    fake = lambda s, timeout=0, **k: scripts.append(s) or {"success": True, "results": {"success": True}}
    cs = ops._OPS_HELPERS
    assert "static int LockPageAll(object page)" in cs and "LockPageAll(page);" in cs
    assert cs.index("LockPageAll(page);") < cs.index("if (symbolMacro)")
    monkeypatch.setattr(copy_live, "_execute_script", fake)
    copy_live.live_copy_region("+CE/2", 0, 0, 10, 10, x=50, y=0)
    cs = scripts[-1]
    assert _string_literals_balanced(cs)
    assert "foreach (object pl0 in PagePlacements(dst)) if (pl0 != null) { LockTree(pl0); lockedOnPage++; }" in cs
    assert cs.index("LockTree(dst);") < cs.index("wmi.Invoke(ins")
    monkeypatch.setattr(cables_live, "_execute_script", fake)
    cables_live._recreate_line("+CE/2", "1/2/3/0", False, 60)
    cs = scripts[-1]
    assert _string_literals_balanced(cs)
    assert "foreach (object pl0 in PagePlacements(page)) if (pl0 != null) LockTree(pl0);" in cs
    assert cs.index("LockTree(page);\n            foreach (object pl0") < cs.index("wm6.Invoke(ins")


def test_black_box_pins_follow_their_box(monkeypatch):
    # a DCP pin keeps only ":n" as its name: the box supplies the DT and the
    # pin follows every retag; a pin with a code of its own is nested and
    # shown twice ("-B17-B17:1", P26009 /13). Measured 2026-11-09.
    from api.actions import ops, copy_live
    h = ops._OPS_HELPERS
    assert "static bool PinFollowBox(object o)" in h and "static List<object> PinsOfBox(object page, object box, HashSet<string> limitTo)" in h
    assert 'GetWritable(o.GetType(), "Name").SetValue(o, want, null);' in h
    macro = h[h.index("static void OpMacro"):h.index("static void OpUpdate")]
    assert "foreach (object pn in PinsOfBox(page, o, own)) { try { PinFollowBox(pn); } catch { } following.Add(Handle(pn)); }" in macro
    assert 'if (following.Contains(Handle(o))) { JObject a2 = (JObject)a.DeepClone(); a2.Remove("tag");' in macro
    upd = h[h.index("static void OpUpdate"):h.index("static void OpMove")]
    assert upd.index("PinsFollow(page, fn, null, r);") < upd.index("Finish(project, page, fn, a, r);")
    assert 'if (B(a, "pinFollow", false)) r["pinFollow"] = PinFollowBox(fn);' in upd
    sbox = h[h.index("static void OpStructureBox"):h.index("static void OpMacro")]
    assert "foreach (object pn in PinsOfBox(page, o, null)) { try { if (PinFollowBox(pn)) followed++; } catch { } followers.Add(Handle(pn)); }" in sbox
    assert "if (followers.Contains(Handle(o))) continue;" in sbox
    scripts = []
    monkeypatch.setattr(copy_live, "_execute_script",
                        lambda s, timeout=0, **k: scripts.append(s) or {"success": True, "results": {"success": True}})
    copy_live.live_copy_region("+CE/2", 0, 0, 10, 10, x=50, y=0, rename={"=ST2": "=ST3"})
    cs = scripts[-1]
    assert _string_literals_balanced(cs)
    assert 'GetWritable(o.GetType(), "Name").SetValue(o, pn.Substring(c), null); pinsFollow++;' in cs
    assert cs.index('results["pinsFollow"] = pinsFollow;') < cs.index('if (nn != n && nestedCode == "")')
    assert 'if (pinFollowers.Contains(Handle(o))) { nestedCode = "pin"; d["follows"] = true; }' in cs


def test_remove_op_locks_the_group_chain():
    # removing one object of an inserted macro threw LockingException on
    # the macro's group (P26009 =MOV+CAMPO/13): the groups above are locked
    from api.actions import ops
    h = ops._OPS_HELPERS
    assert "static void LockWithGroups(object o)" in h
    assert 'object g = TryRead(t, "Group", null);' in h.split("static void LockWithGroups(object o)")[1].split("static void RemoveObject")[0]
    assert "foreach (object o in targets) { LockWithGroups(o); RemoveObject(o); }" in h
    assert "try { LockWithGroups(o); } catch { }" in h


def test_assign_reports_and_drops_unplaced_conductors(monkeypatch):
    # a conductor whose connection is no longer placed is stale (P26009 /13
    # kept the old B17 -> A30.4 conductors after a Y box was wired in)
    from api.actions import cables_live, generate
    scripts = []
    monkeypatch.setattr(cables_live, "_execute_script",
                        lambda s, timeout=0: scripts.append(s) or {"success": True, "results": {
                            "success": True, "handle": "h", "cable": "=+CE-W1", "inlineGenerate": True, "connectionsGenerated": 2,
                            "conductors": [{"handle": "1/18/1/0", "placed": True, "source": "a", "target": "b"},
                                           {"handle": "1/18/2/0", "placed": False, "source": "c", "target": "old"}],
                            "crossed": [{"assigned": "now"}]}})
    monkeypatch.setattr(generate, "generate_connections", lambda **kw: {"success": True})
    monkeypatch.setattr(cables_live, "live_cables",
                        lambda **kw: {"results": {"cables": [{"handle": "h", "conductors": [{"handle": "1/18/1/0", "placed": True}]}]}})
    dropped = []
    real_drop = cables_live._drop_connections
    monkeypatch.setattr(cables_live, "_drop_connections",
                        lambda page, handles, allow, timeout: dropped.append(handles) or {"success": True, "removed": len(handles)})
    r = cables_live.live_assign_cable_connections("+CE/2", "-W1", generate=True, reassign_from_part=False, crossing_texts=False)
    res = r["results"]
    assert res["unplacedConductors"] == [{"handle": "1/18/2/0", "source": "c", "target": "old", "colour": None}]
    assert dropped == [] and "droppedConductors" not in res and len(res["conductors"]) == 2
    r = cables_live.live_assign_cable_connections("+CE/2", "-W1", generate=True, reassign_from_part=False, crossing_texts=False,
                                                  drop_unplaced=True)
    res = r["results"]
    assert dropped == [["1/18/2/0"]] and res["droppedConductors"] == 1 and len(res["conductors"]) == 1
    # the drop script itself: only unplaced connections of the given handles go
    scripts.clear()
    real_drop("+CE/2", ["1/18/2/0"], False, 60)
    cs = scripts[-1]
    assert _string_literals_balanced(cs)
    assert 'new string[] { "1/18/2/0" }' in cs and 'errors.Add(Handle(cn) + ": still placed, not removed"); continue;' in cs
    # the conductor readers carry handle + placed
    assert 'cd["handle"] = Handle(c);' in cables_live._CABLE_DICT and 'cd["placed"] = (bool)ip;' in cables_live._HELPERS_CABLE


def test_assign_rebuilds_connections_when_a_pin_feeds_two_conductors(monkeypatch):
    # a stale logical connection after rewiring: B17:1 -> A30.4:1 next to
    # B17:1 -> X17:1 (P26009 /13); only a rebuild_all drops it
    from api.actions import cables_live, generate
    monkeypatch.setattr(cables_live, "_execute_script",
                        lambda s, timeout=0: {"success": True, "results": {
                            "success": True, "handle": "h", "cable": "=+CE-W1", "inlineGenerate": True, "connectionsGenerated": 2,
                            "conductors": [{"handle": "1", "placed": True, "source": "=B17:1", "target": "=A30:1"},
                                           {"handle": "2", "placed": True, "source": "=B17:1", "target": "=X17:1"}],
                            "crossed": [{"assigned": "now"}]}})
    calls = []
    monkeypatch.setattr(generate, "generate_connections", lambda **kw: calls.append(kw) or {"success": True})
    monkeypatch.setattr(cables_live, "live_cables",
                        lambda **kw: {"results": {"cables": [{"handle": "h", "conductors": [{"handle": "2", "placed": True, "source": "=B17:1", "target": "=X17:1"}]}]}})
    r = cables_live.live_assign_cable_connections("+CE/2", "-W1", generate=True, reassign_from_part=False, crossing_texts=False)
    res = r["results"]
    assert res["rebuiltFor"] == ["=B17:1"] and any(kw.get("rebuild_all") for kw in calls) and len(res["conductors"]) == 1
    assert res["connectionsGenerated"] == 3


def test_page_description_on_create_and_setter(monkeypatch):
    # the page description is PagePropertyList.PAGE_NOMINATIOMN (11011),
    # written after Page.Create since it is not name-forming
    from api.actions import schematic, live
    scripts = []
    monkeypatch.setattr(schematic, "_execute_script",
                        lambda s, timeout=0: scripts.append(s) or {"success": True, "results": {"success": True, "page": "+CE/9"}})
    schematic.live_create_page(location="CE", counter=9, description="es_ES@Entradas digitales;en_US@Digital inputs;")
    cs = scripts[-1]
    assert _string_literals_balanced(cs)
    assert 'SetProp(pprops, "PAGE_NOMINATIOMN", "es_ES@Entradas digitales;en_US@Digital inputs;");' in cs
    assert cs.index('Call(create, page, new object[] { project, docType, ppl });') < cs.index('"PAGE_NOMINATIOMN"')
    schematic.live_create_page(location="CE", counter=9)
    assert "if (null != null)" in scripts[-1]
    r = schematic.live_set_page_description("+CE/9", "Digital inputs")
    cs = scripts[-1]
    assert _string_literals_balanced(cs) and 'SetProp(pprops, "PAGE_NOMINATIOMN", "Digital inputs");' in cs
    assert 'results["before"] = PropText(pprops, "PAGE_NOMINATIOMN");' in cs
    monkeypatch.setattr(live, "_execute_script",
                        lambda s, timeout=0: scripts.append(s) or {"success": True, "results": {"success": True}})
    live.live_query_pages()
    assert 'PropText(pp0, "PAGE_NOMINATIOMN")' in scripts[-1]


def test_update_parts_replace_remove_and_nested_follow(monkeypatch):
    from api.actions import ops, copy_live
    h = ops._OPS_HELPERS
    assert "static void SetParts(object fn, JArray wanted, Dictionary<string, object> r)" in h
    assert 'MethodInfo rm = fn.GetType().GetMethod("RemoveArticleReference");' in h
    assert 'if (Has(a, "parts")) SetParts(fn, (JArray)a["parts"], r);' in h
    assert 'if (Has(a, "removePart")) RemoveParts(fn, a["removePart"], r);' in h
    assert 'if (Has(a, "tag") && fn.GetType().Name == "BoxedDevice" && B(a, "nestedFollow", true)) PinsFollow(page, fn, null, r);' in h
    # no NameParts assignment on freshly inserted pins (that closed EPLAN)
    assert "ClearOwnCode" not in h and "NestedFollow(" not in h
    scripts = []
    monkeypatch.setattr(copy_live, "_execute_script",
                        lambda s, timeout=0, **k: scripts.append(s) or {"success": True, "results": {"success": True}})
    copy_live.live_copy_region("+CE/2", 0, 0, 10, 10, x=50, y=0, rename={"=ST2": "=ST3"})
    cs = scripts[-1]
    assert _string_literals_balanced(cs)
    assert "ownCodeCleared" not in cs


def test_pin_follow_keeps_full_designation_and_covers_terminals():
    from api.actions import ops, cables_live
    h = ops._OPS_HELPERS
    assert "static bool IsBoxPinType(object o)" in h and 'return tn == "Function" || tn == "Terminal";' in h
    body = h[h.index("static bool PinFollowBox(object o)"):h.index("static List<object> PinsOfBox")]
    assert "int c = n.IndexOf(':');" in body and "LastIndexOf" not in body
    assert "if (pl == null || Handle(pl) == me || !IsBoxPinType(pl)) continue;" in h
    # house style is the default for the crossing texts
    assert cables_live.CROSSING_STYLE == {"rotation": 0.0, "height": 1.8, "docked": True, "section": False, "section_height": 1.3}


def test_update_op_swaps_the_symbol_in_place():
    from api.actions import ops
    h = ops._OPS_HELPERS
    upd = h[h.index("static void OpUpdate"):h.index("static void OpMove")]
    assert 'if (Has(a, "symbol") && a["symbol"].Type == JTokenType.Object)' in upd
    assert 'MethodByShape(fn.GetType(), "SetSymbolVariant", new string[] { "SymbolVariant", "Boolean" }, false);' in upd
    assert 'B(sy, "useLogicalDefinition", false)' in upd and 'r["symbolBefore"]' in upd


def test_set_connection_layers_script(monkeypatch):
    # the colour of an autoconnect line is its layer: cable conductors on
    # 311, plain wires on 510, written on the definition points (FIJ/10)
    from api.actions import cables_live, generate
    scripts = []
    monkeypatch.setattr(cables_live, "_execute_script",
                        lambda s, timeout=0: scripts.append(s) or {"success": True, "results": {"success": True, "changed": [{"handle": "1"}]}})
    gens = []
    monkeypatch.setattr(generate, "generate_connections", lambda **kw: gens.append(kw) or {"success": True})
    r = cables_live.live_set_connection_layers("+CE/2")
    cs = scripts[-1]
    assert _string_literals_balanced(cs)
    assert 'string cableLayer = "311", indLayer = "510";' in cs
    assert 'string want = kind == "Cable" ? cableLayer : indLayer;' in cs
    assert 'SetProp(TryRead(dp, "Properties", null), "CONNECTION_ACL_LAYER", want);' in cs
    assert "if (pts.Count == 0) { noPoint.Add(d); continue; }" in cs
    assert r["results"]["connectionsGenerated"] is True and gens[0]["page_name"] == "+CE/2"
    assert cables_live.live_set_connection_layers("+CE/2", cable_layer="x")["success"] is False
