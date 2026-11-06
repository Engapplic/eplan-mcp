"""
Property texts of ANY placement (schematic boxes, devices, terminals, panel
items): read them with their boxes, pick a property arrangement scheme by
name, hide / show, move, rotate, resize and justify them one by one.

The panel-only tools in panel_live.py (legend positions) stay; these are the
general ones a dense I/O page needs: EPLAN draws a black box's device tag and
function text at its top-left, so with a 48 mm pitch they land on the
neighbour. Here the tag goes above the box, the function text turns vertical
and the texts of a cable or a Y are hidden.

Learned live on 2026.0.3 (CAP_TEST scratch, =TST+CAMPO/1, EX600-DXPD macro):

- `PropertyPlacement.Rotation` is in RADIANS and wraps modulo 2*pi (writing
  90 reads back 2.035). The tools take DEGREES.
- `Height` (mm), `Justification` (JustificationType: TopLeft, TopCenter,
  TopRight, MiddleLeft, MiddleCenter, MiddleRight, BottomLeft, BottomCenter,
  BottomRight, BaseLineLeft, ... FromLayer) and `IsVisible` are plain
  members; `IsDocked = false` frees a docked text so it can be moved alone.
- `SymbolReference.PropertyPlacementsSchemas` lists the arrangements
  (`All` -> schemes with `Name`, `Selected` read/write). Selecting one puts
  the placement back on a SHARED arrangement (UseLocalPropertyPlacements
  becomes false), so the scheme is applied FIRST and the local switch after.
- Every per-text edit must come after `UseLocalPropertyPlacements = true`,
  otherwise the shared arrangement moves for every placement using it
  (panel_live.py learned that the hard way).

Writes are scratch-guarded like every schematic writer (see schematic.py).
"""

import uuid

from ._base import cs_escape
from .scripted import _execute_script
from . import live, schematic
from .panel_live import _HELPERS_PANEL
from .schematic import _err, _guard_prelude
from .schematic_model import cs_bool, cs_text, SchematicValueError


_HELPERS_CAPTIONS = r'''
    static void LockQuiet(object o)
    {
        if (o == null) return;
        try { MethodInfo lk = o.GetType().GetMethod("LockObject", Type.EmptyTypes); if (lk != null) lk.Invoke(o, null); } catch { }
    }

    static List<string> SchemeNames(object pl)
    {
        List<string> names = new List<string>();
        object sch = TryRead(pl, "PropertyPlacementsSchemas", null);
        if (sch == null) return names;
        MethodInfo all = sch.GetType().GetMethod("get_All");
        if (all == null) return names;
        object lst = all.Invoke(sch, null);
        if (lst == null) return names;
        foreach (object o in (IEnumerable)lst) if (o != null) names.Add(PropText(o, "Name") ?? "");
        return names;
    }

    static string SelectedScheme(object pl)
    {
        try
        {
            object sch = TryRead(pl, "PropertyPlacementsSchemas", null);
            if (sch == null) return null;
            object sel = sch.GetType().GetMethod("get_Selected").Invoke(sch, null);
            return sel == null ? null : PropText(sel, "Name");
        }
        catch { return null; }
    }

    // Pick a property arrangement by its (display) name; exact first, then
    // case-insensitive prefix, so "Arriba a la izquierda, fuera 90" finds the
    // scheme whose name ends with the degree sign.
    static string SelectScheme(object pl, string wanted)
    {
        object sch = TryRead(pl, "PropertyPlacementsSchemas", null);
        if (sch == null) throw new Exception("Placement has no PropertyPlacementsSchemas; it cannot take an arrangement scheme.");
        object lst = sch.GetType().GetMethod("get_All").Invoke(sch, null);
        object hit = null, prefix = null; int prefixHits = 0;
        List<string> names = new List<string>();
        foreach (object o in (IEnumerable)lst)
        {
            if (o == null) continue;
            string n = PropText(o, "Name") ?? "";
            names.Add(n);
            if (n == wanted) { hit = o; break; }
            if (n.StartsWith(wanted, StringComparison.OrdinalIgnoreCase)) { prefix = o; prefixHits++; }
        }
        if (hit == null && prefixHits == 1) hit = prefix;
        if (hit == null)
            throw new Exception("No property arrangement scheme named '" + wanted + "'" +
                (prefixHits > 1 ? " (prefix matches " + prefixHits + " schemes)" : "") +
                ". Available: " + string.Join(" | ", names.ToArray()));
        sch.GetType().GetMethod("set_Selected").Invoke(sch, new object[] { hit });
        return PropText(hit, "Name");
    }
'''


_ITEM_DICT = '''
                Dictionary<string, object> d = new Dictionary<string, object>();
                d["handle"] = Handle(pl); d["name"] = PropText(pl, "Name") ?? ""; d["clrType"] = pl.GetType().Name;
                object ul = TryRead(pl, "UseLocalPropertyPlacements", null); d["useLocal"] = ul == null ? (object)null : (bool)ul;
                d["scheme"] = SelectedScheme(pl);
                object lo = TryRead(pl, "Location", null); if (lo != null) { Dictionary<string, object> ld = PtDict(lo); d["location"] = new double[] { Convert.ToDouble(ld["x"]), Convert.ToDouble(ld["y"]) }; }
'''


def live_property_texts(page: str, ref: str = None, only_visible: bool = False,
                        with_schemes: bool = False, timeout_seconds: float = 120.0) -> dict:
    """
    The property texts a placement displays (device tag 20010, function text
    20011, part number 22002, ...) with their boxes in page mm, on ANY page
    type. Read-only.

    This is the general form of eplan_live_panel_captions: a schematic black
    box, a terminal, a PLC box or a panel item - whatever shows texts.

    Args:
        page: Page name ("=ST1+CAMPO/1", "+CE/0").
        ref: One placement: its handle ("227/17/1463/0"), its full tag
            ("=ST1+CAMPO-X01") or the bare "-X01" (matched as a suffix when
            no placement carries the bare tag). A tag shared by several
            placements returns all of them. Omit for every placement on the
            page that has texts.
        only_visible: Leave hidden texts out.
        with_schemes: Also list the property arrangement schemes the
            placement can take (names for eplan_live_set_property_texts'
            `scheme`).
        timeout_seconds: Default 120.

    Returns:
        {"success", "page", "items": [{"handle", "name", "clrType",
        "useLocal", "scheme" (selected arrangement), "location",
        "schemes"? [...], "captions": [{"index", "id", "text", "visible",
        "docked", "rotation" (degrees), "height" (mm), "justification",
        "location" (relative to the placement), "box" [x0, y0, x1, y1]
        (page mm; only for visible, non-empty texts)}]}]}
    """
    try:
        page_cs = cs_escape(cs_text(page, "page"))
        ref_cs = cs_escape(cs_text(ref, "ref")) if ref else ""
    except SchematicValueError as exc:
        return _err(exc)
    body = '''
            object page = FindPage(project, "''' + page_cs + '''");
            List<Dictionary<string, object>> items = new List<Dictionary<string, object>>();
            foreach (object pl in FindPanelItems(page, "''' + ref_cs + '''"))
            {
                if (GetReadable(pl.GetType(), "PropertyPlacements") == null) continue;
                List<Dictionary<string, object>> caps = CaptionBoxes(pl);
                if (''' + cs_bool(only_visible) + ''') { List<Dictionary<string, object>> vis = new List<Dictionary<string, object>>(); foreach (Dictionary<string, object> c in caps) if ((bool)c["visible"]) vis.Add(c); caps = vis; }
                if ("''' + ref_cs + '''" == "" && caps.Count == 0) continue;
''' + _ITEM_DICT + '''
                if (''' + cs_bool(with_schemes) + ''') d["schemes"] = SchemeNames(pl);
                d["captions"] = caps;
                items.Add(d);
            }
            if ("''' + ref_cs + '''" != "" && items.Count == 0) throw new Exception("No placement matches ref ''' + ref_cs + ''' on page ''' + page_cs + '''.");
            results["items"] = items; results["page"] = "''' + page_cs + '''"; results["success"] = true;
'''
    return _execute_script(
        live._script("LivePropertyTexts_" + uuid.uuid4().hex[:6], body,
                     extra_helpers=schematic._HELPERS_SCHEMATIC + _HELPERS_PANEL + _HELPERS_CAPTIONS),
        timeout=timeout_seconds,
    )


def live_set_property_texts(page: str, ref: str, scheme: str = None, hide: list = None,
                            show: list = None, positions: dict = None, rotations: dict = None,
                            heights: dict = None, justifications: dict = None,
                            allow_real_project: bool = False,
                            timeout_seconds: float = 300.0) -> dict:
    """
    Arrange the property texts of ONE placement: pick a scheme, hide / show,
    move, rotate, resize, justify. WRITES - scratch-only by default.

    Order of work: scheme (puts the placement on that shared arrangement),
    then the switch to a LOCAL arrangement (UseLocalPropertyPlacements, so
    the per-text edits touch only this placement and survive closing the
    project), then show, hide, heights, justifications, rotations, and the
    moves last (a move is measured on the box, which the other edits change).
    A move is given as the wanted TOP-LEFT of the text box in page mm and
    reached by measure-and-correct (undock, move, re-measure; a few rounds).

    Typical dense-I/O use: {"positions": {"20010": [x, y_above_box]},
    "rotations": {"20011": 90}, "hide": ["22002"]} on each black box, and
    {"hide": ["20011"]} on the cable and the Y.

    Args:
        page: Page name.
        ref: The placement's handle, its full tag, or the bare "-X01"
            (suffix match). Must resolve to exactly one placement; several
            -> error listing the handles (use eplan_live_property_texts).
        scheme: Name of a property arrangement scheme (exact, or a unique
            case-insensitive prefix: "Arriba a la izquierda, fuera 90").
            eplan_live_property_texts(with_schemes=True) lists them.
        hide, show: Texts to hide / show, each a property id (20011,
            "20011") or the exact displayed text ("-X01").
        positions: {selector: [x, y]} - wanted top-left of the text box.
        rotations: {selector: degrees} - 0 horizontal, 90 reads upwards.
        heights: {selector: mm} - text height.
        justifications: {selector: name} - a JustificationType name
            ("TopLeft", "MiddleCenter", "BottomLeft", ...; the error lists
            them).
        allow_real_project: Must be True to write outside the scratch root.
        timeout_seconds: Default 300.

    Returns:
        {"success", "handle", "name", "useLocal", "scheme", "before": [captions],
        "after": [captions], "moves": [{"selector", "target", "reached",
        "rounds", "final"}], "log": [...]}
    """
    try:
        page_cs = cs_escape(cs_text(page, "page"))
        ref_cs = cs_escape(cs_text(ref, "ref"))
        scheme_cs = cs_escape(cs_text(scheme, "scheme")) if scheme else ""
        hide_cs = [cs_escape(cs_text(str(h), "hide[]")) for h in (hide or [])]
        show_cs = [cs_escape(cs_text(str(h), "show[]")) for h in (show or [])]
        pos_rows = []
        if positions is not None and not isinstance(positions, dict):
            raise SchematicValueError("positions must be {selector: [x, y]}.")
        for sel, xy in (positions or {}).items():
            if not isinstance(xy, (list, tuple)) or len(xy) != 2:
                raise SchematicValueError("positions[%r] must be [x, y]." % (sel,))
            pos_rows.append((cs_escape(cs_text(str(sel), "positions key")), float(xy[0]), float(xy[1])))

        def num_rows(d, what):
            if d is not None and not isinstance(d, dict):
                raise SchematicValueError("%s must be {selector: number}." % what)
            return [(cs_escape(cs_text(str(k), what + " key")), float(v)) for k, v in (d or {}).items()]
        rot_rows = num_rows(rotations, "rotations")
        h_rows = num_rows(heights, "heights")
        if justifications is not None and not isinstance(justifications, dict):
            raise SchematicValueError("justifications must be {selector: name}.")
        j_rows = [(cs_escape(cs_text(str(k), "justifications key")), cs_escape(cs_text(str(v), "justifications value")))
                  for k, v in (justifications or {}).items()]
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc if isinstance(exc, SchematicValueError) else SchematicValueError(str(exc)))

    def arr(items):
        return "new string[] { " + ", ".join('"%s"' % i for i in items) + " }"

    def table2(rows):
        return ", ".join('new string[] { "%s", "%r" }' % (s, v) for s, v in rows)
    pos_table = ", ".join('new string[] { "%s", "%r", "%r" }' % (s, x, y) for s, x, y in pos_rows)
    j_table = ", ".join('new string[] { "%s", "%s" }' % (s, v) for s, v in j_rows)
    any_edit = bool(hide_cs or show_cs or pos_rows or rot_rows or h_rows or j_rows)

    body = _guard_prelude(allow_real_project) + '''
            object page = FindPage(project, "''' + page_cs + '''");
            LockQuiet(page);
            List<object> hits = FindPanelItems(page, "''' + ref_cs + '''");
            if (hits.Count == 0) throw new Exception("No placement matches ref ''' + ref_cs + ''' on the page.");
            if (hits.Count > 1)
            {
                List<string> hs = new List<string>(); foreach (object h in hits) hs.Add(Handle(h) + " " + (PropText(h, "Name") ?? ""));
                throw new Exception("ref ''' + ref_cs + ''' matches " + hits.Count + " placements; use a handle: " + string.Join(", ", hs.ToArray()));
            }
            object pl = hits[0];
            if (GetReadable(pl.GetType(), "PropertyPlacements") == null) throw new Exception("A " + pl.GetType().Name + " has no property texts.");
            List<string> log = new List<string>();
            LockQuiet(pl);
            results["handle"] = Handle(pl); results["name"] = PropText(pl, "Name") ?? "";
            results["before"] = CaptionBoxes(pl);

            // 1. arrangement scheme (shared) - before the local switch, which it resets
            if ("''' + scheme_cs + '''" != "") { string chosen = SelectScheme(pl, "''' + scheme_cs + '''"); log.Add("scheme -> " + chosen); }
            results["scheme"] = SelectedScheme(pl);

            // 2. local arrangement, always, before any per-text edit
            PropertyInfo ul = GetWritable(pl.GetType(), "UseLocalPropertyPlacements");
            if (ul == null) throw new Exception("Placement has no UseLocalPropertyPlacements; texts cannot be edited safely. " + MemberList(pl.GetType(), false));
            bool anyEdit = ''' + cs_bool(any_edit) + ''';
            if (anyEdit && !(bool)ul.GetValue(pl, null))
            {
                try { ul.SetValue(pl, true, null); }
                catch (Exception e1) { MethodInfo sw = MethodByShape(pl.GetType(), "SwitchLocalPropertyPlacements", new string[] { }, false); if (sw == null) throw new Exception("UseLocalPropertyPlacements=true failed: " + Flatten(e1)); sw.Invoke(pl, null); }
                log.Add("UseLocalPropertyPlacements -> " + ul.GetValue(pl, null));
            }
            results["useLocal"] = (bool)ul.GetValue(pl, null);

            // 3. show / hide
            foreach (string sel in ''' + arr(show_cs) + ''')
            { object pp = FindCaption(pl, sel); if (pp == null) { log.Add("show: no text '" + sel + "'"); continue; } LockQuiet(pp); GetWritable(pp.GetType(), "IsVisible").SetValue(pp, true, null); log.Add("shown '" + sel + "'"); }
            foreach (string sel in ''' + arr(hide_cs) + ''')
            { object pp = FindCaption(pl, sel); if (pp == null) { log.Add("hide: no text '" + sel + "'"); continue; } LockQuiet(pp); GetWritable(pp.GetType(), "IsVisible").SetValue(pp, false, null); log.Add("hidden '" + sel + "'"); }

            // 4. heights (mm)
            foreach (string[] w in new string[][] { ''' + table2(h_rows) + ''' })
            {
                object pp = FindCaption(pl, w[0]); if (pp == null) { log.Add("height: no text '" + w[0] + "'"); continue; }
                LockQuiet(pp);
                double h = double.Parse(w[1], System.Globalization.CultureInfo.InvariantCulture);
                GetWritable(pp.GetType(), "Height").SetValue(pp, h, null);
                log.Add("height '" + w[0] + "' = " + TryRead(pp, "Height", null));
            }

            // 5. justifications (JustificationType names)
            foreach (string[] w in new string[][] { ''' + j_table + ''' })
            {
                object pp = FindCaption(pl, w[0]); if (pp == null) { log.Add("justification: no text '" + w[0] + "'"); continue; }
                LockQuiet(pp);
                PropertyInfo jp = GetWritable(pp.GetType(), "Justification");
                object jv;
                try { jv = Enum.Parse(jp.PropertyType, w[1], true); }
                catch (Exception) { throw new Exception("Unknown justification '" + w[1] + "'. Valid: " + string.Join(", ", Enum.GetNames(jp.PropertyType))); }
                jp.SetValue(pp, jv, null);
                log.Add("justification '" + w[0] + "' = " + TryRead(pp, "Justification", null));
            }

            // 6. rotations: degrees in, radians to EPLAN
            foreach (string[] w in new string[][] { ''' + table2(rot_rows) + ''' })
            {
                object pp = FindCaption(pl, w[0]); if (pp == null) { log.Add("rotation: no text '" + w[0] + "'"); continue; }
                LockQuiet(pp);
                try { GetWritable(pp.GetType(), "IsDocked").SetValue(pp, false, null); } catch { }
                double deg = double.Parse(w[1], System.Globalization.CultureInfo.InvariantCulture);
                double rad = (((deg % 360.0) + 360.0) % 360.0) * Math.PI / 180.0;
                GetWritable(pp.GetType(), "Rotation").SetValue(pp, rad, null);
                log.Add("rotation '" + w[0] + "' = " + deg + " deg");
            }

            // 7. moves: measure-and-correct on the box's top-left corner
            List<Dictionary<string, object>> moves = new List<Dictionary<string, object>>();
            string[][] wanted = new string[][] { ''' + pos_table + ''' };
            foreach (string[] w in wanted)
            {
                Dictionary<string, object> mv = new Dictionary<string, object>();
                mv["selector"] = w[0];
                double tx = double.Parse(w[1], System.Globalization.CultureInfo.InvariantCulture), ty = double.Parse(w[2], System.Globalization.CultureInfo.InvariantCulture);
                mv["target"] = new double[] { tx, ty };
                object pp = FindCaption(pl, w[0]);
                if (pp == null) { mv["error"] = "no text '" + w[0] + "'"; moves.Add(mv); continue; }
                LockQuiet(pp);
                try { GetWritable(pp.GetType(), "IsDocked").SetValue(pp, false, null); } catch { }
                try { GetWritable(pp.GetType(), "IsVisible").SetValue(pp, true, null); } catch { }
                PropertyInfo locPi = GetWritable(pp.GetType(), "Location");
                double sx = 1, sy = 1; int rounds = 0; bool reached = false; double[] cur = null;
                for (int it = 0; it < 6; it++)
                {
                    cur = null;
                    foreach (Dictionary<string, object> c in CaptionBoxes(pl)) if (CaptionMatches(c, w[0]) && c.ContainsKey("box")) { double[] b = (double[])c["box"]; cur = new double[] { b[0], b[3] }; break; }
                    if (cur == null) { mv["error"] = "text has no box (empty text?)"; break; }
                    double dx = tx - cur[0], dy = ty - cur[1];
                    if (Math.Abs(dx) <= 0.3 && Math.Abs(dy) <= 0.3) { reached = true; break; }
                    Dictionary<string, object> ld = PtDict(locPi.GetValue(pp, null));
                    locPi.SetValue(pp, MakePoint(locPi.PropertyType, Convert.ToDouble(ld["x"]) + dx * sx, Convert.ToDouble(ld["y"]) + dy * sy), null);
                    rounds++;
                    double[] now = null;
                    foreach (Dictionary<string, object> c in CaptionBoxes(pl)) if (CaptionMatches(c, w[0]) && c.ContainsKey("box")) { double[] b = (double[])c["box"]; now = new double[] { b[0], b[3] }; break; }
                    if (now == null) break;
                    if (Math.Abs(dx) > 0.3 && (now[0] - cur[0]) * dx < 0) sx = -sx;
                    if (Math.Abs(dy) > 0.3 && (now[1] - cur[1]) * dy < 0) sy = -sy;
                }
                mv["reached"] = reached; mv["rounds"] = rounds; if (cur != null) mv["final"] = cur;
                moves.Add(mv);
            }
            results["moves"] = moves;
            results["after"] = CaptionBoxes(pl);
            results["log"] = log;
            results["success"] = true;
'''
    return _execute_script(
        live._script("LiveSetPropertyTexts_" + uuid.uuid4().hex[:6], body,
                     extra_helpers=schematic._HELPERS_SCHEMATIC + _HELPERS_PANEL + _HELPERS_CAPTIONS),
        timeout=timeout_seconds,
    )
