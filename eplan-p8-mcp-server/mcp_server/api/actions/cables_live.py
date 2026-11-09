"""
Cables: what cables the project has and which connections they carry, and a
writer that places a CABLE DEFINITION LINE across connections, the way
"Insertar > Definición de cables" does.

Learned live on 2026.0.3 (scratch copy of TEST-S7-1200):

- A cable is an `EObjects.Cable` function (function category Cable 1/1,
  "Definición de cables"). Placed, it is the SPECIAL/CABDL line; the
  connections the line crosses become its conductors (`CableConnections`)
  once the connections are generated. A cable without a part has as many
  conductors as lines it crosses; with a part, the part's connection
  templates (MDCableTemplatePosition) give the conductors their colours and
  cross-sections, and "Asignar conexiones" matches them.
- `Cable.Create(Page, SymbolVariant, PointD start, PointD end)` places the
  line; Name, part and FUNC_TEXT are written afterwards like any function.
- The GUI assigns the crossed connections while the line is drawn; a line
  placed by the API gets NO conductors from "generate connections" alone,
  however often it runs (verified: 0 after rebuild-all, after opening the
  page, after nudging the line). What works is what the GUI does under the
  hood: write the cable's `NameParts` into each crossed Connection's
  `CableNameParts` (the connection then answers CONNECTION_IS_CABLE=1) and
  regenerate connections once - the cable's `CableConnections` then lists
  them and each connection's `CableDefinitionLine` is the cable. Which
  connections cross is decided geometrically here: a connection runs
  between its two symbol references' pins (reference Location + pin
  offset), and it crosses when that segment straddles the line's y inside
  the line's x range (or the transposed test for a vertical line).
- The workstation licence answers EditingOfCables=true, Cabling=false:
  cable functions and diagrams are allowed, 3D routing is not.
"""

import os
import uuid

from ._base import cs_escape
from .scripted import _execute_script
from . import live, schematic
from .schematic import _err, _guard_prelude
from .schematic_model import cs_text, SchematicValueError
from .terminals_live import _HELPERS_TERM


_CABLE_DICT = '''
                Dictionary<string, object> d = new Dictionary<string, object>();
                d["handle"] = Handle(fn);
                d["name"] = PropText(fn, "Name") ?? "";
                object fprops = TryRead(fn, "Properties", null);
                d["text"] = fprops == null ? "" : (PropText(fprops, "FUNC_TEXT") ?? "");
                object pg = TryRead(fn, "Page", null);
                d["page"] = pg == null ? null : PropText(pg, "Name");
                List<string> parts = new List<string>();
                object ars = TryRead(fn, "ArticleReferences", null);
                if (ars != null) foreach (object ar in (IEnumerable)ars) parts.Add(PropText(ar, "PartNr") ?? "");
                d["parts"] = parts;
                List<Dictionary<string, object>> conductors = new List<Dictionary<string, object>>();
                object conns = TryRead(fn, "CableConnections", null);
                if (conns != null) foreach (object c in (IEnumerable)conns)
                {
                    if (c == null) continue;
                    Dictionary<string, object> cd = new Dictionary<string, object>();
                    cd["name"] = PropText(c, "Name") ?? "";
                    object cp = TryRead(c, "Properties", null);
                    if (cp != null)
                    {
                        cd["designation"] = PropText(cp, "CONNECTION_DESIGNATION") ?? "";
                        cd["colour"] = PropText(cp, "CONNECTION_WIRENUMBER") ?? "";
                        cd["crossSection"] = PropText(cp, "CONNECTION_WIRECROSSSECTION_WITH_UNIT") ?? "";
                    }
                    object s1 = TryRead(c, "StartSymbolReference", null), s2 = TryRead(c, "EndSymbolReference", null);
                    cd["source"] = s1 == null ? "" : (PropText(s1, "Name") ?? "");
                    cd["target"] = s2 == null ? "" : (PropText(s2, "Name") ?? "");
                    object cpg = TryRead(c, "Page", null);
                    cd["page"] = cpg == null ? null : PropText(cpg, "Name");
                    conductors.Add(cd);
                }
                d["conductors"] = conductors;
'''


def live_cables(contains: str = None, timeout_seconds: float = 120.0) -> dict:
    """
    The project's cables with their conductors. Read-only.

    Lists every cable function (placed definition lines and unplaced cables
    alike) with its part(s), function text, page and the connections it
    carries: conductor designation, colour, cross-section, source and target
    device. A cable whose conductor list is empty crosses no generated
    connection yet: run eplan_generate_connections, or move the line.

    Args:
        contains: Only cables whose name contains this ("-W1", "+CE").
        timeout_seconds: Default 120.

    Returns:
        {"success", "cables": [{"handle", "name", "text", "page", "parts",
        "conductors": [{"designation" (wire number), "colour", "crossSection",
        "source", "target", "page"}]}], "count"}.
    """
    try:
        contains_cs = cs_escape(cs_text(contains, "contains")) if contains else ""
    except SchematicValueError as exc:
        return _err(exc)

    body = '''
            string contains = "''' + contains_cs + '''";
            List<Dictionary<string, object>> cables = new List<Dictionary<string, object>>();
            foreach (object fn in AllFunctions(project))
            {
                if (fn == null || fn.GetType().Name != "Cable") continue;
                string name = PropText(fn, "Name") ?? "";
                if (contains != "" && name.IndexOf(contains, StringComparison.OrdinalIgnoreCase) < 0) continue;
''' + _CABLE_DICT + '''
                cables.Add(d);
            }
            results["cables"] = cables;
            results["count"] = cables.Count;
            results["success"] = true;
'''
    return _execute_script(
        live._script("LiveCables_" + uuid.uuid4().hex[:6], body,
                     extra_helpers=schematic._HELPERS_SCHEMATIC + _HELPERS_TERM + _HELPERS_CABLE),
        timeout=timeout_seconds,
    )


_HELPERS_CABLE = r'''
    // Run an EPLAN action from inside the script (CommandLineInterpreter),
    // so a connection update does not cost a round-trip of its own: the
    // cable chain went from ~14 scripts to ~5 (2026-11-08).
    static void RunAction(string cmd)
    {
        Type t = FindType("Eplan.EplApi.ApplicationFramework.CommandLineInterpreter");
        if (t == null) throw new Exception("CommandLineInterpreter is not loaded in this script host.");
        object cli = Activator.CreateInstance(t);
        MethodInfo m = null;
        foreach (MethodInfo mi in t.GetMethods())
            if (mi.Name == "Execute" && mi.GetParameters().Length == 1 && mi.GetParameters()[0].ParameterType == typeof(string)) { m = mi; break; }
        if (m == null) throw new Exception("CommandLineInterpreter.Execute(string) not found.");
        try { m.Invoke(cli, new object[] { cmd }); }
        catch (TargetInvocationException tie) { throw new Exception("Action failed: " + cmd + " <- " + Flatten(tie.InnerException ?? tie)); }
    }

    static List<Dictionary<string, object>> CableConductors(object fn)
    {
        List<Dictionary<string, object>> conductors = new List<Dictionary<string, object>>();
        object conns = TryRead(fn, "CableConnections", null);
        if (conns != null) foreach (object c in (IEnumerable)conns)
        {
            if (c == null) continue;
            Dictionary<string, object> cd = new Dictionary<string, object>();
            cd["name"] = PropText(c, "Name") ?? "";
            object cp = TryRead(c, "Properties", null);
            if (cp != null)
            {
                cd["designation"] = PropText(cp, "CONNECTION_DESIGNATION") ?? "";
                cd["colour"] = PropText(cp, "CONNECTION_WIRENUMBER") ?? "";
                cd["crossSection"] = PropText(cp, "CONNECTION_WIRECROSSSECTION_WITH_UNIT") ?? "";
            }
            object s1 = TryRead(c, "StartSymbolReference", null), s2 = TryRead(c, "EndSymbolReference", null);
            cd["source"] = s1 == null ? "" : (PropText(s1, "Name") ?? "");
            cd["target"] = s2 == null ? "" : (PropText(s2, "Name") ?? "");
            object cpg = TryRead(c, "Page", null);
            cd["page"] = cpg == null ? null : PropText(cpg, "Name");
            conductors.Add(cd);
        }
        return conductors;
    }

    static void LockAny(object o)
    {
        if (o == null) return;
        try { MethodInfo lk = o.GetType().GetMethod("LockObject", Type.EmptyTypes); if (lk != null) lk.Invoke(o, null); } catch { }
    }

    // LockObject plus SmartLock: a connection definition point of a real
    // project refused CONNECTION_WIRENUMBER (object not locked for write
    // access) after LockObject alone (P26009, 2026-11-07).
    static void LockHard(object o)
    {
        if (o == null) return;
        LockAny(o);
        try { MethodInfo sm = o.GetType().GetMethod("SmartLock", Type.EmptyTypes); if (sm != null) sm.Invoke(o, null); } catch { }
    }

    // The designation of the pin a conductor lands on. A black box drawn
    // as SPECIAL/DC + DCP has every symbol pin named "1": the real
    // designation is the DCP function's own (=+-B3:4 gives 4), then the
    // symbol pin's.
    static string PinDesignation(object sr, object pin)
    {
        string n = sr == null ? "" : (PropText(sr, "Name") ?? "");
        int c = n.LastIndexOf(':');
        if (c >= 0 && c < n.Length - 1) return n.Substring(c + 1);
        object props = sr == null ? null : TryRead(sr, "Properties", null);
        string fcd = props == null ? null : PropText(props, "FUNC_CONNECTIONDESIGNATION");
        if (!string.IsNullOrEmpty(fcd)) { int nl = fcd.IndexOf('\n'); return nl > 0 ? fcd.Substring(0, nl) : fcd; }
        return pin == null ? "" : (PropText(pin, "Designation") ?? "");
    }
'''


# Crossing points left behind by a DELETED cable line. Drawing a cable line
# in the GUI (and live_decorate_cable_crossings) puts a zero-size connection
# definition point on every crossed wire, right on the line. Delete the line
# and those points stay: the wire keeps KindOfWire = Cable with no
# CableDefinitionLine, and a NEW line placed over it never links (verified
# in scratch 2026-11-07: three generates, zero conductors, even after the
# name parts were rewritten; removing those points and regenerating turned
# the wires back into IndividualConnection and the next line linked at
# once). EPLAN links a line to its wires only in the first connection
# update after the line is created, so the points must go BEFORE the line
# is placed - a line already placed over them has to be re-placed.
_ORPHAN_BODY = '''
            object page = FindPage(project, "PAGENAME");
            double sx1 = SX1, sy1 = SY1, sx2 = SX2, sy2 = SY2;
            bool horiz = Math.Abs(sx2 - sx1) >= Math.Abs(sy2 - sy1);
            double lo = horiz ? Math.Min(sx1, sx2) : Math.Min(sy1, sy2), hi = horiz ? Math.Max(sx1, sx2) : Math.Max(sy1, sy2);
            double axis = horiz ? (sy1 + sy2) / 2.0 : (sx1 + sx2) / 2.0;
            List<object> snap = new List<object>();
            foreach (object q0 in (IEnumerable)PagePlacements(page)) snap.Add(q0);
            List<Dictionary<string, object>> found = new List<Dictionary<string, object>>();
            List<string> errors = new List<string>();
            int removed = 0;
            foreach (object pl in snap)
            {
                if (pl == null || pl.GetType().Name != "ConnectionDefinitionPoint") continue;
                object loc = TryRead(pl, "Location", null); if (loc == null) continue;
                double x = Convert.ToDouble(loc.GetType().GetProperty("X").GetValue(loc, null)), y = Convert.ToDouble(loc.GetType().GetProperty("Y").GetValue(loc, null));
                double along = horiz ? x : y, across = horiz ? y : x;
                if (Math.Abs(across - axis) > 0.3 || along < lo - 0.3 || along > hi + 0.3) continue;
                string kind = Convert.ToString(TryRead(pl, "KindOfWire", null));
                object line = TryRead(pl, "CableDefinitionLine", null);
                string name = PropText(pl, "Name") ?? "";
                object pp = TryRead(pl, "Properties", null);
                string des = pp == null ? "" : (PropText(pp, "CONNECTION_DESIGNATION") ?? "");
                bool nameless = name == "" || name.EndsWith("+") || name.EndsWith("=");
                // orphan: a cable-kind point with no line, or a nameless
                // point with no wire number sitting exactly on the line
                bool orphan = line == null && (kind == "Cable" || (nameless && des == ""));
                if (!orphan) continue;
                Dictionary<string, object> d = new Dictionary<string, object>();
                d["handle"] = Handle(pl); d["name"] = name; d["kind"] = kind; d["x"] = x; d["y"] = y;
                if (DOREMOVE)
                {
                    try { LockAny(page); LockHard(pl); pl.GetType().GetMethod("Remove", Type.EmptyTypes).Invoke(pl, null); d["removed"] = true; removed++; }
                    catch (Exception e) { d["error"] = Flatten(e); errors.Add(Flatten(e)); }
                }
                found.Add(d);
            }
            results["orphanPoints"] = found;
            results["removed"] = removed;
            results["errors"] = errors;
            results["success"] = errors.Count == 0;
'''


# A line that linked nothing after three updates is stale for EPLAN: only a
# NEW line links in its first update. Re-creating it by Cable.Create lost
# the full name (the page structure took over), the part and the text
# arrangement (P26009 -W90.1, 2026-11-09). A one-object window macro keeps
# all three: write the line to a temp .ema, remove it, insert the macro at
# the same point. Its own leftover points (same name, no wire number) go
# too - they are what kept the wires in Cable kind in the first place.
_RECREATE_BODY = '''
            object page = FindPage(project, "PAGENAME");
            object cable = null;
            foreach (object pl in PagePlacements(page))
                if (pl != null && pl.GetType().Name == "Cable" && Handle(pl) == "HANDLE") { cable = pl; break; }
            if (cable == null) throw new Exception("No cable line with handle HANDLE on page PAGENAME.");
            string cableName = PropText(cable, "Name") ?? "";
            object loc = TryRead(cable, "Location", null);
            double lx = Convert.ToDouble(loc.GetType().GetProperty("X").GetValue(loc, null)), ly = Convert.ToDouble(loc.GetType().GetProperty("Y").GetValue(loc, null));
            LockAny(page);
            // 1. leftover points of this very cable anywhere on the page (no wire number)
            int removedPoints = 0;
            List<object> snap = new List<object>();
            foreach (object q0 in (IEnumerable)PagePlacements(page)) snap.Add(q0);
            foreach (object pl in snap)
            {
                if (pl == null || pl.GetType().Name != "ConnectionDefinitionPoint") continue;
                if ((PropText(pl, "Name") ?? "") != cableName) continue;
                object pp = TryRead(pl, "Properties", null);
                if (pp != null && (PropText(pp, "CONNECTION_DESIGNATION") ?? "") != "") continue;
                try { LockHard(pl); pl.GetType().GetMethod("Remove", Type.EmptyTypes).Invoke(pl, null); removedPoints++; } catch { }
            }
            results["removedPoints"] = removedPoints;
            // 2. the line as a one-object macro
            Type wmT = FindType("Eplan.EplApi.DataModel.MasterData.WindowMacro");
            object wm = Activator.CreateInstance(wmT);
            MethodInfo create = null;
            foreach (MethodInfo m in wmT.GetMethods())
            {
                ParameterInfo[] ps = m.GetParameters();
                if (m.Name == "Create" && ps.Length == 6 && ps[0].ParameterType == typeof(string) && ps[3].ParameterType.IsArray && ps[3].ParameterType.GetElementType().Name == "Placement") create = m;
            }
            if (create == null) throw new Exception("WindowMacro.Create(string, RepresentationType, int, Placement[], MultiLangString, PointD) not found.");
            ParameterInfo[] cp = create.GetParameters();
            object rep = Enum.Parse(cp[1].ParameterType, "MultiLine");
            Array arr = Array.CreateInstance(cp[3].ParameterType.GetElementType(), 1); arr.SetValue(cable, 0);
            string macroFile = @"MACROFILE";
            try { create.Invoke(wm, new object[] { macroFile, rep, 0, arr, Activator.CreateInstance(cp[4].ParameterType), MakePoint(cp[5].ParameterType, lx, ly) }); }
            catch (TargetInvocationException tie) { throw new Exception("WindowMacro.Create failed: " + Flatten(tie.InnerException ?? tie)); }
            // 3. out with the old line
            LockHard(cable);
            cable.GetType().GetMethod("Remove", Type.EmptyTypes).Invoke(cable, null);
            // 4. in with the clone, same point, names kept
            Type insType = FindType("Eplan.EplApi.HEServices.Insert");
            object ins = Activator.CreateInstance(insType);
            MethodInfo wm6 = null;
            foreach (MethodInfo m in insType.GetMethods())
            {
                ParameterInfo[] ps = m.GetParameters();
                if (m.Name == "WindowMacro" && ps.Length == 6 && ps[0].ParameterType == typeof(string)) wm6 = m;
            }
            if (wm6 == null) throw new Exception("Insert.WindowMacro(string, RepresentationType, int, Page, PointD, MoveKind) not found.");
            ParameterInfo[] wp = wm6.GetParameters();
            object[] objs;
            try { objs = (object[])wm6.Invoke(ins, new object[] { macroFile, rep, 0, page, MakePoint(wp[4].ParameterType, lx, ly), Enum.Parse(wp[5].ParameterType, "Absolute") }); }
            catch (TargetInvocationException tie) { throw new Exception("Insert.WindowMacro failed: " + Flatten(tie.InnerException ?? tie)); }
            string newHandle = null;
            if (objs != null) foreach (object o in objs) if (o != null && o.GetType().Name == "Cable") { newHandle = Handle(o); results["newName"] = PropText(o, "Name"); }
            if (newHandle == null) throw new Exception("The clone inserted no cable line.");
            results["newHandle"] = newHandle; results["success"] = true;
'''


def _recreate_line(page: str, handle: str, allow_real_project: bool, timeout_seconds: float) -> dict:
    # Clone the line through a one-object window macro (see _RECREATE_BODY).
    macro_dir = os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or ".", "eplan_mcp_macros")
    os.makedirs(macro_dir, exist_ok=True)
    macro_file = os.path.join(macro_dir, "line_%s.ema" % uuid.uuid4().hex[:8])
    body = (_guard_prelude(allow_real_project)
            + _RECREATE_BODY.replace("PAGENAME", cs_escape(page)).replace("HANDLE", cs_escape(handle))
            .replace("MACROFILE", macro_file.replace('"', '""')))
    r = _execute_script(
        live._script("LiveCableRecreate_" + uuid.uuid4().hex[:6], body,
                     extra_helpers=schematic._HELPERS_SCHEMATIC + _HELPERS_TERM + _HELPERS_CABLE),
        timeout=timeout_seconds,
    )
    try:
        if os.path.exists(macro_file):
            os.remove(macro_file)
    except OSError:
        pass
    res = r.get("results") if isinstance(r.get("results"), dict) else r
    return res if isinstance(res, dict) else {"success": False, "error": str(res)}


def _orphan_points(page: str, coords, remove: bool, allow_real_project: bool,
                   timeout_seconds: float) -> dict:
    # Find (and with remove=True delete) the crossing points a deleted cable
    # line left on the wires along the segment coords=(x1, y1, x2, y2).
    body = (_guard_prelude(allow_real_project)
            + _ORPHAN_BODY.replace("PAGENAME", cs_escape(page))
            .replace("SX1", repr(float(coords[0]))).replace("SY1", repr(float(coords[1])))
            .replace("SX2", repr(float(coords[2]))).replace("SY2", repr(float(coords[3])))
            .replace("DOREMOVE", "true" if remove else "false"))
    r = _execute_script(
        live._script("LiveCableOrphans_" + uuid.uuid4().hex[:6], body,
                     extra_helpers=schematic._HELPERS_SCHEMATIC + _HELPERS_TERM + _HELPERS_CABLE),
        timeout=timeout_seconds,
    )
    res = r.get("results") if isinstance(r.get("results"), dict) else r
    return res if isinstance(res, dict) else {"success": False, "error": str(res)}


_ASSIGN_BODY = '''
            object page = FindPage(project, "PAGENAME");
            object cable = null;
            string wantedCable = "CABLENAME";
            foreach (object pl in PagePlacements(page))
            {
                if (pl == null || pl.GetType().Name != "Cable") continue;
                string n = PropText(pl, "Name") ?? "";
                if (n == wantedCable || Handle(pl) == wantedCable || (n.Length > 0 && n.EndsWith(wantedCable) && wantedCable.StartsWith("-"))) { cable = pl; break; }
            }
            if (cable == null) throw new Exception("No cable definition line '" + wantedCable + "' on page PAGENAME.");
            object bb = cable.GetType().GetMethod("GetBoundingBox", Type.EmptyTypes).Invoke(cable, null);
            double cx1 = 1e9, cx2 = -1e9, cy1 = 1e9, cy2 = -1e9;
            foreach (object p in (IEnumerable)bb)
            {
                double px = Convert.ToDouble(p.GetType().GetProperty("X").GetValue(p, null)), py = Convert.ToDouble(p.GetType().GetProperty("Y").GetValue(p, null));
                cx1 = Math.Min(cx1, px); cx2 = Math.Max(cx2, px); cy1 = Math.Min(cy1, py); cy2 = Math.Max(cy2, py);
            }
            bool horizontal = (cx2 - cx1) >= (cy2 - cy1);
            double cmid = horizontal ? (cy1 + cy2) / 2.0 : (cx1 + cx2) / 2.0;
            LockAny(page); LockAny(cable);
            object cableParts = TryRead(cable, "NameParts", null);
            string wantCode = cableParts == null ? "" : (PropText(cableParts, "FUNC_CODE") ?? "");
            string wantCnt = cableParts == null ? "" : (PropText(cableParts, "FUNC_COUNTER") ?? "");
            Type finderType = FindType("Eplan.EplApi.DataModel.DMObjectsFinder");
            object finder = Activator.CreateInstance(finderType, new object[] { project });
            Type cfT = FindType("Eplan.EplApi.DataModel.ConnectionsFilter");
            object cf = Activator.CreateInstance(cfT);
            MethodInfo gc = finderType.GetMethod("GetConnections", new Type[] { cfT });
            List<Dictionary<string, object>> assigned = new List<Dictionary<string, object>>();
            List<string> errors = new List<string>();
            foreach (object cn in (IEnumerable)gc.Invoke(finder, new object[] { cf }))
            {
                if (cn == null) continue;
                object pg = TryRead(cn, "Page", null);
                if (pg == null || PropText(pg, "Name") != "PAGENAME") continue;
                object sr1 = TryRead(cn, "StartSymbolReference", null), sr2 = TryRead(cn, "EndSymbolReference", null);
                if (sr1 == null || sr2 == null) continue;
                object l1 = TryRead(sr1, "Location", null), l2 = TryRead(sr2, "Location", null);
                if (l1 == null || l2 == null) continue;
                double x1 = Convert.ToDouble(l1.GetType().GetProperty("X").GetValue(l1, null)), y1 = Convert.ToDouble(l1.GetType().GetProperty("Y").GetValue(l1, null));
                double x2 = Convert.ToDouble(l2.GetType().GetProperty("X").GetValue(l2, null)), y2 = Convert.ToDouble(l2.GetType().GetProperty("Y").GetValue(l2, null));
                object sp = TryRead(cn, "StartSymbolConnPoint", null), ep = TryRead(cn, "EndSymbolConnPoint", null);
                object pl1 = sp == null ? null : TryRead(sp, "Location", null), pl2 = ep == null ? null : TryRead(ep, "Location", null);
                if (pl1 != null) { x1 += Convert.ToDouble(pl1.GetType().GetProperty("X").GetValue(pl1, null)); y1 += Convert.ToDouble(pl1.GetType().GetProperty("Y").GetValue(pl1, null)); }
                if (pl2 != null) { x2 += Convert.ToDouble(pl2.GetType().GetProperty("X").GetValue(pl2, null)); y2 += Convert.ToDouble(pl2.GetType().GetProperty("Y").GetValue(pl2, null)); }
                bool crosses;
                if (horizontal)
                    crosses = Math.Abs(x1 - x2) < 0.01 && x1 >= cx1 && x1 <= cx2 && Math.Min(y1, y2) < cmid && Math.Max(y1, y2) > cmid;
                else
                    crosses = Math.Abs(y1 - y2) < 0.01 && y1 >= cy1 && y1 <= cy2 && Math.Min(x1, x2) < cmid && Math.Max(x1, x2) > cmid;
                if (!crosses) continue;
                object cp = TryRead(cn, "Properties", null);
                Dictionary<string, object> d = new Dictionary<string, object>();
                d["designation"] = cp == null ? "" : (PropText(cp, "CONNECTION_DESIGNATION") ?? "");
                d["source"] = PropText(sr1, "Name"); d["target"] = PropText(sr2, "Name");
                // "already" only when the connection is linked to THIS line
                // AND carries the cable's name parts: EPLAN links a connection
                // to a line it touches without writing the cable into it, and
                // that half-state showed up as 0 conductors on the cable list.
                object already = TryRead(cn, "CableDefinitionLine", null);
                object cnp = TryRead(cn, "CableNameParts", null);
                string haveCode = cnp == null ? "" : (PropText(cnp, "FUNC_CODE") ?? "");
                string haveCnt = cnp == null ? "" : (PropText(cnp, "FUNC_COUNTER") ?? "");
                bool named = haveCode != "" && haveCode == wantCode && haveCnt == wantCnt;
                if (already != null && Handle(already) == Handle(cable) && named) { d["assigned"] = "already"; assigned.Add(d); continue; }
                d["linkedBefore"] = already != null && Handle(already) == Handle(cable);
                string lastErr = null;
                for (int attempt = 0; attempt < 3 && lastErr == null && !d.ContainsKey("assigned"); attempt++)
                {
                    try
                    {
                        // LockObject is declared on StorableObject; GetMethod walks
                        // the base types (MethodByShape does not), and a connection
                        // that existed before the line needs its two ends locked too.
                        LockAny(cn);
                        // Writing the cable into a connection also writes its
                        // connection definition points: on a page copied as a
                        // window macro they came unlocked (LockingException on a
                        // 124/59/... object, P26009 =MOV+CAMPO/11, 2026-11-09).
                        try { object cdps0 = TryRead(cn, "ConnectionDefPoints", null); if (cdps0 != null) foreach (object q0 in (IEnumerable)cdps0) if (q0 != null) LockHard(q0); } catch { }
                        if (attempt >= 1) { LockAny(sr1); LockAny(sr2); try { MethodInfo sm = cn.GetType().GetMethod("SmartLock", Type.EmptyTypes); if (sm != null) sm.Invoke(cn, null); } catch { } }
                        GetWritable(cn.GetType(), "CableNameParts").SetValue(cn, cableParts, null);
                        d["assigned"] = "now";
                        if (attempt > 0) d["lockRetries"] = attempt;
                    }
                    catch (Exception e)
                    {
                        string msg = Flatten(e);
                        if (attempt < 2 && msg.IndexOf("lock", StringComparison.OrdinalIgnoreCase) >= 0) continue;
                        lastErr = msg;
                    }
                }
                if (lastErr != null) { d["assigned"] = "failed"; errors.Add((string)d["designation"] + ": " + lastErr); }
                assigned.Add(d);
            }
            results["cable"] = PropText(cable, "Name");
            results["handle"] = Handle(cable);
            results["orientation"] = horizontal ? "horizontal" : "vertical";
            try
            {
                // the line as it was drawn (start = Location, end = the far
                // corner of the bounding box pulled in by the pen width), so
                // the caller can re-place it on the same wires
                object cl = TryRead(cable, "Location", null);
                double lx = Convert.ToDouble(cl.GetType().GetProperty("X").GetValue(cl, null)), ly = Convert.ToDouble(cl.GetType().GetProperty("Y").GetValue(cl, null));
                double pen = horizontal ? (cy2 - cy1) / 2.0 : (cx2 - cx1) / 2.0;
                double ex = horizontal ? (Math.Abs(lx - (cx1 + pen)) < Math.Abs(lx - (cx2 - pen)) ? cx2 - pen : cx1 + pen) : lx;
                double ey = horizontal ? ly : (Math.Abs(ly - (cy1 + pen)) < Math.Abs(ly - (cy2 - pen)) ? cy2 - pen : cy1 + pen);
                results["cableStart"] = new double[] { lx, ly }; results["cableEnd"] = new double[] { ex, ey };
                results["cableVisibleName"] = PropText(cable, "VisibleName") ?? "";
                object cprops = TryRead(cable, "Properties", null);
                results["cableText"] = cprops == null ? "" : (PropText(cprops, "FUNC_TEXT") ?? "");
                List<string> cparts = new List<string>();
                object cars = TryRead(cable, "ArticleReferences", null);
                if (cars != null) foreach (object ar in (IEnumerable)cars) cparts.Add(PropText(ar, "PartNr") ?? "");
                results["cableParts"] = cparts;
                object sv = TryRead(cable, "SymbolVariant", null); object sy = sv == null ? null : TryRead(sv, "Symbol", null);
                results["cableSymbol"] = sy == null ? null : PropText(sy, "Name");
                results["cableVariant"] = sv == null ? null : TryRead(sv, "VariantNr", null);
            }
            catch (Exception ge) { results["geometryError"] = Flatten(ge); }
            results["crossed"] = assigned;
            results["errors"] = errors;
            results["success"] = errors.Count == 0;
'''


_REASSIGN_INNER = '''
                // "Asignar conexiones": the part's wire templates hand their
                // colour / cross-section to the conductors (EPL.DEFAULT-CABLE
                // turned BU/BU/BU into BK/BN/GY). Verified 2026-11-03.
                Type csT = FindType("Eplan.EplApi.HEServices.CableService");
                object csvc = Activator.CreateInstance(csT);
                Type apT = FindType("Eplan.EplApi.DataModel.AnyPropertyId");
                Array excl = Array.CreateInstance(apT, 0);
                MethodInfo m = null;
                foreach (MethodInfo mi in csT.GetMethods())
                    if (mi.Name == "DoReassignWires" && mi.GetParameters().Length == 3 && mi.GetParameters()[0].ParameterType.Name == "Function") m = mi;
                if (m == null) throw new Exception("CableService.DoReassignWires(Function, bool, AnyPropertyId[]) not found.");
                // DoReassignWires writes into every conductor connection and
                // its two ends: without the write locks it threw LockingException
                // (object not locked for write access) on P26009 (2026-11-06).
                LockAny(cable);
                try { object pg0 = TryRead(cable, "Page", null); if (pg0 != null) LockAny(pg0); } catch { }
                int lockedConductors = 0, lockedPoints = 0;
                // DoReassignWires writes the conductor data into the
                // connection definition points on the wires (verified in
                // scratch): lock those too.
                try
                {
                    foreach (object pl in PagePlacements(page))
                        if (pl != null && pl.GetType().Name == "ConnectionDefinitionPoint") { LockHard(pl); lockedPoints++; }
                }
                catch (Exception lpe) { errors.Add("lock points: " + Flatten(lpe)); }
                results["lockedPoints"] = lockedPoints;
                try
                {
                    object ccs = TryRead(cable, "CableConnections", null);
                    if (ccs != null) foreach (object cc in (IEnumerable)ccs)
                    {
                        if (cc == null) continue;
                        LockHard(cc);
                        LockHard(TryRead(cc, "StartSymbolReference", null));
                        LockHard(TryRead(cc, "EndSymbolReference", null));
                        lockedConductors++;
                    }
                }
                catch (Exception le) { errors.Add("lock conductors: " + Flatten(le)); }
                results["lockedConductors"] = lockedConductors;
                try { m.Invoke(csvc, new object[] { cable, true, excl }); results["reassignedFromPart"] = true; }
                catch (Exception e) { results["reassignedFromPart"] = false; errors.Add("DoReassignWires: " + Flatten(e)); results["success"] = false; }
                // A project part copy without master data (an Article created
                // empty, or copied into the project before the parts database
                // had its conductor templates: P26009 FES.8003618, 2026-11-09)
                // hands out nothing: every conductor stays colourless. Reload
                // the copy from the parts database and reassign once more.
                if (results.ContainsKey("reassignedFromPart") && (bool)results["reassignedFromPart"])
                {
                    bool anyColour = false;
                    object ccs2 = TryRead(cable, "CableConnections", null);
                    if (ccs2 != null) foreach (object cc in (IEnumerable)ccs2)
                    {
                        object p2 = cc == null ? null : TryRead(cc, "Properties", null);
                        if (p2 != null && !string.IsNullOrEmpty(PropText(p2, "CONNECTION_WIRENUMBER"))) { anyColour = true; break; }
                    }
                    object ars2 = TryRead(cable, "ArticleReferences", null);
                    if (!anyColour && ars2 != null)
                    {
                        int reloaded = 0;
                        foreach (object ar in (IEnumerable)ars2)
                        {
                            object art = ar == null ? null : TryRead(ar, "Article", null);
                            if (art == null) continue;
                            try { LockHard(art); object ok = art.GetType().GetMethod("LoadFromMasterdata", Type.EmptyTypes).Invoke(art, null); if (ok is bool && (bool)ok) reloaded++; }
                            catch (Exception re) { errors.Add("LoadFromMasterdata: " + Flatten(re)); }
                        }
                        results["partReloaded"] = reloaded;
                        if (reloaded > 0)
                        {
                            try { m.Invoke(csvc, new object[] { cable, true, excl }); }
                            catch (Exception e2) { errors.Add("DoReassignWires after reload: " + Flatten(e2)); }
                        }
                    }
                }
                // The cable's own conductor count / cross-section / unit
                // (20052 / 20053 / unit) come from the part when still empty:
                // what the GUI fills in on part assignment.
                try
                {
                    object cprops = TryRead(cable, "Properties", null);
                    object ars3 = TryRead(cable, "ArticleReferences", null);
                    object art3 = null;
                    if (ars3 != null) foreach (object ar in (IEnumerable)ars3) if (ar != null && art3 == null) art3 = TryRead(ar, "Article", null);
                    object aprops = art3 == null ? null : TryRead(art3, "Properties", null);
                    if (cprops != null && aprops != null)
                    {
                        List<string> filled = new List<string>();
                        string[][] pairs = new string[][] {
                            new string[] { "FUNC_CABLEWIRECOUNT", "ARTICLE_CABLEWIRECOUNT" },
                            new string[] { "FUNC_CABLEWIRECROSSSECTION", "ARTICLE_CABLEWIRECROSSSECTION" },
                            new string[] { "FUNC_CABLE_CROSSSECTION_UNIT", "ARTICLE_WIRECROSSSECTION_UNIT" } };
                        foreach (string[] pr in pairs)
                        {
                            string have = PropText(cprops, pr[0]) ?? "", v = PropText(aprops, pr[1]) ?? "";
                            if (have != "" || v == "") continue;
                            try { SetProp(cprops, pr[0], v); filled.Add(pr[0]); } catch (Exception fe) { results[pr[0] + "_error"] = Flatten(fe); }
                        }
                        if (filled.Count > 0) results["cableFieldsFromPart"] = filled;
                    }
                }
                catch (Exception cfe) { results["cableFieldsError"] = Flatten(cfe); }
'''


_REASSIGN_TAIL = '''
            if (REASSIGN)
            {
''' + _REASSIGN_INNER + '''            }
'''


# One script for the whole chain after the assignment: connection update(s)
# and the conductor read, then - with a part - the reassign and one more
# update. The caller used to run each as its own round-trip (3 updates, 3
# reads, 1 reassign, 1 update, 1 read: ~9 scripts at 0.4-0.7 s each).
# Measured on 2026.0.3: the first update after the assignment does not yet
# list the conductors, the second does - hence the loop of up to three.
_INLINE_TAIL = '''
            if (GENERATE && errors.Count == 0)
            {
                int wanted = 0;
                foreach (Dictionary<string, object> d in assigned) { string a0 = Convert.ToString(d["assigned"]); if (a0 == "now" || a0 == "already") wanted++; }
                string projPath = PropText(project, "ProjectFullName") ?? "";
                string genCmd = "generate /TYPE:CONNECTIONS /PROJECTNAME:\\"" + projPath + "\\" /PAGENAME:\\"PAGEVALUE\\"";
                int runs = 0;
                List<Dictionary<string, object>> conductors = CableConductors(cable);
                try
                {
                    while (runs < 3) { RunAction(genCmd); runs++; conductors = CableConductors(cable); if (conductors.Count >= wanted) break; }
                    results["inlineGenerate"] = true;
                    if (REASSIGN && conductors.Count > 0)
                    {
''' + _REASSIGN_INNER + '''
                        RunAction(genCmd); runs++; conductors = CableConductors(cable);
                    }
                    results["connectionsGenerated"] = runs;
                    results["conductors"] = conductors;
                }
                catch (Exception ge) { results["inlineGenerateError"] = Flatten(ge); results.Remove("inlineGenerate"); }
            }
'''



_DECORATE_BODY = """
            // The crossing points: EPLAN puts a ConnectionDefinitionPoint of
            // zero size, named "=+", on the line at every crossed wire. A
            // manufacturer macro (Festo NEBU, 2026-11-06) names them after
            // the cable, gives them the conductor's colour / cross-section
            // and shows those two texts rotated 90 deg at the crossing. The
            // same is done here.
            object page = FindPage(project, "PAGENAME");
            object cable = null;
            foreach (object pl in PagePlacements(page))
            {
                if (pl == null || pl.GetType().Name != "Cable") continue;
                string cn = PropText(pl, "Name") ?? "";
                if (Handle(pl) == "CABLENAME" || cn == "CABLENAME" || ("CABLENAME".StartsWith("-") && cn.EndsWith("CABLENAME") && cn.Length > "CABLENAME".Length)) { cable = pl; break; }
            }
            if (cable == null) throw new Exception("No cable definition line '" + "CABLENAME" + "' on page " + "PAGENAME" + ".");
            LockAny(page); LockAny(cable);
            string cableName = PropText(cable, "Name") ?? "";
            double cx1 = double.MaxValue, cx2 = double.MinValue, cy1 = double.MaxValue, cy2 = double.MinValue;
            foreach (object p in (IEnumerable)cable.GetType().GetMethod("GetBoundingBox", Type.EmptyTypes).Invoke(cable, null))
            {
                double px = Convert.ToDouble(p.GetType().GetProperty("X").GetValue(p, null)), py = Convert.ToDouble(p.GetType().GetProperty("Y").GetValue(p, null));
                cx1 = Math.Min(cx1, px); cx2 = Math.Max(cx2, px); cy1 = Math.Min(cy1, py); cy2 = Math.Max(cy2, py);
            }
            bool horizontal = (cx2 - cx1) >= (cy2 - cy1);
            double cmid = horizontal ? (cy1 + cy2) / 2.0 : (cx1 + cx2) / 2.0;
            // conductor -> where its wire runs (x for a horizontal line)
            List<object[]> cond = new List<object[]>();
            object ccs = TryRead(cable, "CableConnections", null);
            if (ccs != null) foreach (object cc in (IEnumerable)ccs)
            {
                if (cc == null) continue;
                object sr1 = TryRead(cc, "StartSymbolReference", null);
                object l1 = sr1 == null ? null : TryRead(sr1, "Location", null);
                if (l1 == null) continue;
                double x1 = Convert.ToDouble(l1.GetType().GetProperty("X").GetValue(l1, null)), y1 = Convert.ToDouble(l1.GetType().GetProperty("Y").GetValue(l1, null));
                object sp = TryRead(cc, "StartSymbolConnPoint", null);
                object pl1 = sp == null ? null : TryRead(sp, "Location", null);
                if (pl1 != null) { x1 += Convert.ToDouble(pl1.GetType().GetProperty("X").GetValue(pl1, null)); y1 += Convert.ToDouble(pl1.GetType().GetProperty("Y").GetValue(pl1, null)); }
                // the other end too: the wire's extent along its axis
                object sr2e = TryRead(cc, "EndSymbolReference", null);
                object l2e = sr2e == null ? null : TryRead(sr2e, "Location", null);
                double x2e = x1, y2e = y1;
                if (l2e != null)
                {
                    x2e = Convert.ToDouble(l2e.GetType().GetProperty("X").GetValue(l2e, null)); y2e = Convert.ToDouble(l2e.GetType().GetProperty("Y").GetValue(l2e, null));
                    object ep2 = TryRead(cc, "EndSymbolConnPoint", null);
                    object pl2e = ep2 == null ? null : TryRead(ep2, "Location", null);
                    if (pl2e != null) { x2e += Convert.ToDouble(pl2e.GetType().GetProperty("X").GetValue(pl2e, null)); y2e += Convert.ToDouble(pl2e.GetType().GetProperty("Y").GetValue(pl2e, null)); }
                }
                double a1 = horizontal ? y1 : x1, a2 = horizontal ? y2e : x2e;
                cond.Add(new object[] { horizontal ? x1 : y1, cc, Math.Min(a1, a2), Math.Max(a1, a2) });
            }
            List<Dictionary<string, object>> points = new List<Dictionary<string, object>>();
            List<string> errors = new List<string>();
            foreach (object pl in PagePlacements(page))
            {
                if (pl == null || pl.GetType().Name != "ConnectionDefinitionPoint") continue;
                object loc = TryRead(pl, "Location", null);
                if (loc == null) continue;
                double lx = Convert.ToDouble(loc.GetType().GetProperty("X").GetValue(loc, null)), ly = Convert.ToDouble(loc.GetType().GetProperty("Y").GetValue(loc, null));
                bool onLine = horizontal ? (Math.Abs(ly - cmid) < 0.05 && lx >= cx1 - 0.05 && lx <= cx2 + 0.05)
                                         : (Math.Abs(lx - cmid) < 0.05 && ly >= cy1 - 0.05 && ly <= cy2 + 0.05);
                if (!onLine) continue;
                string nm = PropText(pl, "Name") ?? "";
                if (nm != "" && nm != "=+" && nm != cableName) continue;   // someone else's point
                Dictionary<string, object> d = new Dictionary<string, object>();
                d["handle"] = Handle(pl); d["at"] = horizontal ? lx : ly;
                try
                {
                    LockHard(pl);
                    object cc = null;
                    foreach (object[] c in cond) if (Math.Abs((double)c[0] - (horizontal ? lx : ly)) < 0.05) { cc = c[1]; break; }
                    if (cc != null) { LockHard(cc); LockHard(TryRead(cc, "StartSymbolReference", null)); LockHard(TryRead(cc, "EndSymbolReference", null)); }
                    if (nm != cableName) { try { GetWritable(pl.GetType(), "Name").SetValue(pl, cableName, null); d["named"] = true; } catch (Exception ne) { d["nameError"] = Flatten(ne); } }
                    object props = TryRead(pl, "Properties", null);
                    if (cc != null && props != null)
                    {
                        object cp = TryRead(cc, "Properties", null);
                        foreach (string pn in new string[] { "CONNECTION_WIRENUMBER", "CONNECTION_WIRECROSSSECTION", "CONNECTION_WIRECROSSSECTION_UNIT" })
                        {
                            string v = cp == null ? null : PropText(cp, pn);
                            if (string.IsNullOrEmpty(v)) continue;
                            string perr = null;
                            for (int attempt = 0; attempt < 2; attempt++)
                            {
                                try { SetProp(props, pn, v); d[pn] = v; perr = null; break; }
                                catch (Exception pe) { perr = Flatten(pe); LockHard(pl); }
                            }
                            if (perr != null) d[pn + "_error"] = perr;
                        }
                    }
                    else d["conductor"] = "none";
                    // texts: colour (31004) and cross-section (31002) rotated 90 deg
                    PropertyInfo ul = GetWritable(pl.GetType(), "UseLocalPropertyPlacements");
                    if (ul != null && !(bool)ul.GetValue(pl, null)) { try { ul.SetValue(pl, true, null); } catch { } }
                    object pps = TryRead(pl, "PropertyPlacements", null);
                    int shown = 0;
                    if (pps != null) foreach (object pp in (IEnumerable)pps)
                    {
                        if (pp == null) continue;
                        object dp = TryRead(pp, "DisplayedProperty", null);
                        int id = dp == null ? -1 : Convert.ToInt32(TryRead(dp, "AsInt", null) ?? -1);
                        if (id != 31004 && id != 31002 && id != 20010) continue;
                        LockAny(pp);
                        if (id == 20010) { try { GetWritable(pp.GetType(), "IsVisible").SetValue(pp, false, null); } catch { } continue; }
                        if (id == 31002 && !STYLE_SECTION) { try { GetWritable(pp.GetType(), "IsVisible").SetValue(pp, false, null); } catch { } continue; }
                        try { GetWritable(pp.GetType(), "IsVisible").SetValue(pp, true, null); } catch { }
                        try { GetWritable(pp.GetType(), "IsDocked").SetValue(pp, STYLE_DOCKED, null); } catch { }
                        try { GetWritable(pp.GetType(), "Height").SetValue(pp, id == 31004 ? STYLE_HEIGHT : STYLE_SECTION_HEIGHT, null); } catch { }
                        if (!STYLE_DOCKED)
                        {
                            try { PropertyInfo jp = GetWritable(pp.GetType(), "Justification"); jp.SetValue(pp, Enum.Parse(jp.PropertyType, id == 31004 ? "BottomLeft" : "TopLeft", true), null); } catch { }
                            try { GetWritable(pp.GetType(), "Rotation").SetValue(pp, STYLE_ROT_RAD, null); } catch { }
                            try { PropertyInfo lp = GetWritable(pp.GetType(), "Location"); lp.SetValue(pp, MakePoint(lp.PropertyType, 0.0, id == 31004 ? 1.5 : 0.5), null); } catch { }
                        }
                        shown++;
                    }
                    d["texts"] = shown;
                }
                catch (Exception e) { d["error"] = Flatten(e); errors.Add(Flatten(e)); }
                points.Add(d);
            }
            // "Asignar conexiones" leaves a SECOND connection definition
            // point a few mm off the line on every crossed wire that had
            // none (48 red bars on P26009). Its data is already on the
            // crossing point, so it is hidden (default), removed or kept.
            string extraMode = "EXTRA_MODE";
            List<Dictionary<string, object>> extras = new List<Dictionary<string, object>>();
            if (extraMode != "keep")
            {
                foreach (object pl in PagePlacements(page))
                {
                    if (pl == null || pl.GetType().Name != "ConnectionDefinitionPoint") continue;
                    object loc = TryRead(pl, "Location", null);
                    if (loc == null) continue;
                    double lx = Convert.ToDouble(loc.GetType().GetProperty("X").GetValue(loc, null)), ly = Convert.ToDouble(loc.GetType().GetProperty("Y").GetValue(loc, null));
                    double off = horizontal ? Math.Abs(ly - cmid) : Math.Abs(lx - cmid);
                    if (off < 0.05) continue;                                  // on the line itself
                    // anywhere along a crossed wire: EPLAN parked the second
                    // point 3 mm under the sensor pins, 60 mm off the line
                    // (P26009 =MOV+CAMPO/13, 2026-11-09)
                    double along = horizontal ? ly : lx;
                    bool onWire = false;
                    foreach (object[] c in cond) if (Math.Abs((double)c[0] - (horizontal ? lx : ly)) < 0.05 && along >= (double)c[2] - 0.05 && along <= (double)c[3] + 0.05) { onWire = true; break; }
                    if (!onWire) continue;
                    string nm = PropText(pl, "Name") ?? "";
                    if (nm != "" && nm != "=+" && nm != cableName) continue;
                    object pp0 = TryRead(pl, "Properties", null);
                    string des = pp0 == null ? "" : (PropText(pp0, "CONNECTION_DESIGNATION") ?? "");
                    if (des != "") continue;                                   // a real wire-number label: keep
                    Dictionary<string, object> e = new Dictionary<string, object>();
                    e["handle"] = Handle(pl); e["at"] = new double[] { lx, ly };
                    try
                    {
                        LockHard(pl);
                        if (extraMode == "remove") { pl.GetType().GetMethod("Remove", Type.EmptyTypes).Invoke(pl, null); e["action"] = "removed"; }
                        else
                        {
                            int hidden = 0;
                            object pps2 = TryRead(pl, "PropertyPlacements", null);
                            if (pps2 != null) foreach (object pp in (IEnumerable)pps2)
                            {
                                if (pp == null) continue;
                                try { LockAny(pp); GetWritable(pp.GetType(), "IsVisible").SetValue(pp, false, null); hidden++; } catch { }
                            }
                            e["action"] = "hidden"; e["texts"] = hidden;
                        }
                    }
                    catch (Exception ex) { e["error"] = Flatten(ex); errors.Add(Flatten(ex)); }
                    extras.Add(e);
                }
            }
            results["extraPoints"] = extras;
            results["cable"] = cableName; results["handle"] = Handle(cable);
            results["crossingPoints"] = points;
            results["errors"] = errors; results["success"] = errors.Count == 0;
"""


# M8 / M12 sensor connector pins (IEC 61076-2-101 / -104): what a NEBU or a
# D-M9 lead carries on each pin. Used when `pin_colours` is "sensor" or
# "auto".
SENSOR_PIN_COLOURS = {"1": "BN", "2": "WH", "3": "BU", "4": "BK", "5": "GY"}

_PINFIX_BODY = """
            // "Asignar conexiones" hands the part's wire templates to the
            // conductors in template order, not by pin: a NEBU-M8G3 (1 BN,
            // 4 BK, 3 BU) crossed left to right as pins 1, 3, 4 came out
            // BN, BK, BU (P26009, 2026-11-06). The templates carry no pin,
            // so the pairing is a map pin -> colour applied to every
            // conductor whose end pin is in the map.
            object page = FindPage(project, "PAGENAME");
            object cable = null;
            foreach (object pl in PagePlacements(page))
            {
                if (pl == null || pl.GetType().Name != "Cable") continue;
                string cn = PropText(pl, "Name") ?? "";
                if (Handle(pl) == "CABLENAME" || cn == "CABLENAME" || ("CABLENAME".StartsWith("-") && cn.EndsWith("CABLENAME") && cn.Length > "CABLENAME".Length)) { cable = pl; break; }
            }
            if (cable == null) throw new Exception("No cable definition line '" + "CABLENAME" + "' on page " + "PAGENAME" + ".");
            LockAny(page); LockAny(cable);
            Dictionary<string, string> map = new Dictionary<string, string>();
MAPROWS
            bool auto = AUTO;
            List<Dictionary<string, object>> rows = new List<Dictionary<string, object>>();
            List<string> errors = new List<string>();
            object ccs = TryRead(cable, "CableConnections", null);
            List<object> conds = new List<object>();
            if (ccs != null) foreach (object cc in (IEnumerable)ccs) if (cc != null) conds.Add(cc);
            // NEVER `return` from the body: the result file is written after
            // it, and a return left the caller waiting for the full timeout
            // (P26009 =MOV+CAMPO/13 "no result file appeared within 60s",
            // 2026-11-09).
            bool skip = false;
            if (auto)
            {
                // only when every conductor already carries a sensor-lead
                // colour (the part IS a sensor lead); anything else is left alone
                HashSet<string> allowed = new HashSet<string>(map.Values);
                foreach (object cc in conds)
                {
                    object cp0 = TryRead(cc, "Properties", null);
                    string col = cp0 == null ? "" : (PropText(cp0, "CONNECTION_WIRENUMBER") ?? "");
                    if (col == "" || !allowed.Contains(col)) { results["skipped"] = "conductor colour '" + col + "' is not a sensor-lead colour; pass pin_colours explicitly"; skip = true; break; }
                }
            }
            // the page's connection definition points, read once
            List<object> pageCdps = new List<object>();
            foreach (object q in (IEnumerable)PagePlacements(page)) if (q != null && q.GetType().Name == "ConnectionDefinitionPoint") pageCdps.Add(q);
            if (!skip) foreach (object cc in conds)
            {
                Dictionary<string, object> d = new Dictionary<string, object>();
                try
                {
                    object cp = TryRead(cc, "Properties", null);
                    d["designation"] = cp == null ? "" : (PropText(cp, "CONNECTION_DESIGNATION") ?? "");
                    d["before"] = cp == null ? "" : (PropText(cp, "CONNECTION_WIRENUMBER") ?? "");
                    object sp = TryRead(cc, "StartSymbolConnPoint", null), ep = TryRead(cc, "EndSymbolConnPoint", null);
                    // the pin designation lives on StartPin / EndPin (what live_read_connections reports)
                    object p1o = TryRead(cc, "StartPin", null), p2o = TryRead(cc, "EndPin", null);
                    object srA = TryRead(cc, "StartSymbolReference", null), srB = TryRead(cc, "EndSymbolReference", null);
                    string pin1 = PinDesignation(srA, p1o), pin2 = PinDesignation(srB, p2o);
                    d["pins"] = new string[] { pin1, pin2 };
                    // The pin that counts is the DEVICE's (the sensor's M8
                    // 1/3/4), not the terminal's (a terminal has pins 1/2
                    // too): prefer the end that is not a Terminal.
                    object sr1 = srA, sr2 = srB;
                    bool t1 = sr1 != null && sr1.GetType().Name == "Terminal", t2 = sr2 != null && sr2.GetType().Name == "Terminal";
                    string[] order = (t1 && !t2) ? new string[] { pin2, pin1 } : new string[] { pin1, pin2 };
                    string want = null, pin = null;
                    foreach (string cand in order) if (want == null && map.ContainsKey(cand)) { want = map[cand]; pin = cand; }
                    if (want == null) { d["result"] = "no pin in map"; rows.Add(d); continue; }
                    d["pin"] = pin; d["colour"] = want; d["pinSide"] = pin == pin1 ? "start" : "end";
                    if ((string)d["before"] == want) { d["result"] = "already"; rows.Add(d); continue; }
                    LockHard(cc); LockHard(sr1); LockHard(sr2);
                    // the colour write goes through to the connection's own
                    // definition points: lock them first (pin 1 of P26009
                    // /13 threw LockingException on its crossing point)
                    object dps0 = TryRead(cc, "ConnectionDefPoints", null);
                    if (dps0 != null) foreach (object q in (IEnumerable)dps0) if (q != null) LockHard(q);
                    int written = 0;
                    try { SetProp(cp, "CONNECTION_WIRENUMBER", want); written++; } catch (Exception e1) { d["connectionError"] = Flatten(e1); }
                    // the connection definition points on this wire (the
                    // rotulo and the line crossing) define the colour too
                    object l1 = sr1 == null ? null : TryRead(sr1, "Location", null), l2 = sr2 == null ? null : TryRead(sr2, "Location", null);
                    if (l1 != null && l2 != null)
                    {
                        double x1 = Convert.ToDouble(l1.GetType().GetProperty("X").GetValue(l1, null)), y1 = Convert.ToDouble(l1.GetType().GetProperty("Y").GetValue(l1, null));
                        double x2 = Convert.ToDouble(l2.GetType().GetProperty("X").GetValue(l2, null)), y2 = Convert.ToDouble(l2.GetType().GetProperty("Y").GetValue(l2, null));
                        object pl1 = sp == null ? null : TryRead(sp, "Location", null), pl2 = ep == null ? null : TryRead(ep, "Location", null);
                        if (pl1 != null) { x1 += Convert.ToDouble(pl1.GetType().GetProperty("X").GetValue(pl1, null)); y1 += Convert.ToDouble(pl1.GetType().GetProperty("Y").GetValue(pl1, null)); }
                        if (pl2 != null) { x2 += Convert.ToDouble(pl2.GetType().GetProperty("X").GetValue(pl2, null)); y2 += Convert.ToDouble(pl2.GetType().GetProperty("Y").GetValue(pl2, null)); }
                        int cdps = 0;
                        foreach (object pl in pageCdps)
                        {
                            object loc = TryRead(pl, "Location", null);
                            if (loc == null) continue;
                            double lx = Convert.ToDouble(loc.GetType().GetProperty("X").GetValue(loc, null)), ly = Convert.ToDouble(loc.GetType().GetProperty("Y").GetValue(loc, null));
                            bool on = Math.Abs(x1 - x2) < 0.01
                                ? (Math.Abs(lx - x1) < 0.05 && ly >= Math.Min(y1, y2) - 0.05 && ly <= Math.Max(y1, y2) + 0.05)
                                : (Math.Abs(y1 - y2) < 0.01 && Math.Abs(ly - y1) < 0.05 && lx >= Math.Min(x1, x2) - 0.05 && lx <= Math.Max(x1, x2) + 0.05);
                            if (!on) continue;
                            try { LockHard(pl); SetProp(TryRead(pl, "Properties", null), "CONNECTION_WIRENUMBER", want); cdps++; } catch (Exception e2) { d["cdpError"] = Flatten(e2); }
                        }
                        d["pointsWritten"] = cdps;
                    }
                    d["result"] = written > 0 ? "set" : "failed";
                    if (written == 0) errors.Add((string)d["designation"] + ": " + d["connectionError"]);
                }
                catch (Exception e) { d["error"] = Flatten(e); errors.Add(Flatten(e)); }
                rows.Add(d);
            }
            results["cable"] = PropText(cable, "Name"); results["handle"] = Handle(cable);
            results["conductors"] = rows; results["errors"] = errors; results["success"] = errors.Count == 0;
"""


def _pin_map(pin_colours):
    """{pin: colour} from the tool argument; None when nothing applies."""
    if pin_colours is None or pin_colours is False:
        return None, False
    if isinstance(pin_colours, str):
        if pin_colours.lower() in ("sensor", "m8", "m12"):
            return dict(SENSOR_PIN_COLOURS), False
        if pin_colours.lower() == "auto":
            return dict(SENSOR_PIN_COLOURS), True
        raise SchematicValueError("pin_colours must be 'auto', 'sensor' or {pin: colour}.")
    if isinstance(pin_colours, dict) and pin_colours:
        return {str(k): str(v) for k, v in pin_colours.items()}, False
    raise SchematicValueError("pin_colours must be 'auto', 'sensor' or {pin: colour}.")


def live_set_conductor_colours(page: str, cable: str, pin_colours="sensor",
                               allow_real_project: bool = False,
                               timeout_seconds: float = 60.0) -> dict:
    """
    Give each conductor of a cable the colour of the PIN it lands on. WRITES.

    "Asignar conexiones" hands the part's wire templates to the conductors
    in template order, not by pin (a Festo NEBU-M8G3 crossed as pins 1, 3, 4
    came out BN, BK, BU instead of BN, BU, BK). The templates carry no pin,
    so this applies a map {pin designation: colour} to every conductor one
    of whose ends is that pin, writing CONNECTION_WIRENUMBER on the
    connection and on the connection definition points along the wire.

    Args:
        page: Page name.
        cable: The cable's full name, short tag or handle.
        pin_colours: "sensor" (M8 / M12 sensor leads: 1 BN, 2 WH, 3 BU,
            4 BK, 5 GY), "auto" (the same map, applied only when every
            conductor already carries one of those colours, i.e. the part
            is a sensor lead) or an explicit {"1": "BN", "4": "BK", ...}.
        allow_real_project: Must be True to write outside the scratch root.

    Returns:
        {"success", "cable", "handle", "conductors": [{"designation",
         "pins", "pin", "colour", "before", "result": "set"|"already"|
         "no pin in map", "pointsWritten"}], "skipped"?, "errors"}
    """
    try:
        page = cs_text(page, "page")
        cable = cs_text(cable, "cable")
        pmap, auto = _pin_map(pin_colours)
        if pmap is None:
            raise SchematicValueError("pin_colours is required.")
        rows = "".join('            map["%s"] = "%s";\
' % (cs_escape(cs_text(k, "pin")), cs_escape(cs_text(v, "colour")))
                       for k, v in pmap.items())
    except SchematicValueError as exc:
        return _err(exc)
    body = (_guard_prelude(allow_real_project)
            + _PINFIX_BODY.replace("PAGENAME", cs_escape(page)).replace("CABLENAME", cs_escape(cable))
              .replace("MAPROWS\
", rows).replace("AUTO", "true" if auto else "false"))
    return _execute_script(
        live._script("LiveCablePins_" + uuid.uuid4().hex[:6], body,
                     extra_helpers=schematic._HELPERS_SCHEMATIC + _HELPERS_TERM + _HELPERS_CABLE),
        timeout=timeout_seconds,
    )


CROSSING_STYLE = {"rotation": 90.0, "height": 1.8, "docked": False, "section": True, "section_height": 1.3}


def live_decorate_cable_crossings(page: str, cable: str, style: dict = None,
                                  extra_points: str = "hide",
                                  allow_real_project: bool = False,
                                  timeout_seconds: float = 180.0) -> dict:
    """
    Show the conductor colour and cross-section at every crossing of a
    cable definition line, the way a manufacturer macro does. WRITES.

    EPLAN leaves a zero-size connection definition point named "=+" on the
    line at each crossed wire. This names those points after the cable,
    copies each conductor's colour / cross-section into them (after
    reassign_from_part they come from the cable part), and turns on their
    colour (31004) and cross-section (31002) texts rotated 90 degrees at the
    crossing - "BN" above the line, "0,25" below (the point has no
    "with unit" text to show). live_place_cable_definition and
    live_assign_cable_connections run this by default (crossing_texts).

    Args:
        page: Page name.
        cable: The cable's full name, short tag or handle.
        style: How the crossing texts look, any of {"rotation": 90 (degrees;
            0 = horizontal), "height": 1.8 (mm, colour), "docked": False
            (True = attached to the point by the scheme, no rotation /
            offset), "section": True (False hides the cross-section),
            "section_height": 1.3}. Defaults = the Festo macro look.
        extra_points: What to do with the second connection definition
            point "Asignar conexiones" leaves a few mm off the line on each
            crossed wire that had none (its data is already on the crossing
            point): "hide" (default: its texts invisible), "remove" or
            "keep". A point that carries a wire number is never touched.
        allow_real_project: Must be True to write outside the scratch root.

    Returns:
        {"success", "cable", "handle", "crossingPoints": [{"handle", "at",
         "named", "CONNECTION_WIRENUMBER", ..., "texts"}], "extraPoints":
         [{"handle", "at", "action"}], "errors"}
    """
    try:
        page = cs_text(page, "page")
        cable = cs_text(cable, "cable")
        st = dict(CROSSING_STYLE)
        if style is not None:
            if not isinstance(style, dict):
                raise SchematicValueError("style must be a dict.")
            unknown = set(style) - set(CROSSING_STYLE)
            if unknown:
                raise SchematicValueError("style: unknown keys %s; valid: %s" % (sorted(unknown), sorted(CROSSING_STYLE)))
            st.update(style)
        rot = float(st["rotation"]); height = float(st["height"]); sh = float(st["section_height"])
        if extra_points not in ("hide", "remove", "keep"):
            raise SchematicValueError("extra_points must be 'hide', 'remove' or 'keep'.")
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc if isinstance(exc, SchematicValueError) else SchematicValueError(str(exc)))
    rad = ((rot % 360.0) + 360.0) % 360.0 * 3.141592653589793 / 180.0
    body = (_guard_prelude(allow_real_project)
            + _DECORATE_BODY.replace("PAGENAME", cs_escape(page)).replace("CABLENAME", cs_escape(cable))
              .replace("STYLE_SECTION_HEIGHT", repr(sh)).replace("STYLE_SECTION", "true" if st["section"] else "false")
              .replace("STYLE_DOCKED", "true" if st["docked"] else "false")
              .replace("STYLE_HEIGHT", repr(height)).replace("STYLE_ROT_RAD", repr(rad))
              .replace("EXTRA_MODE", extra_points))
    return _execute_script(
        live._script("LiveCableDecor_" + uuid.uuid4().hex[:6], body,
                     extra_helpers=schematic._HELPERS_SCHEMATIC + _HELPERS_TERM + _HELPERS_CABLE),
        timeout=timeout_seconds,
    )


def live_assign_cable_connections(page: str, cable: str, generate: bool = True,
                                  reassign_from_part: bool = False,
                                  crossing_texts: bool = True,
                                  crossing_style: dict = None, pin_colours="auto",
                                  extra_points: str = "hide",
                                  allow_real_project: bool = False,
                                  timeout_seconds: float = 180.0, replaced_line: bool = False) -> dict:
    """
    Make the connections a cable definition line crosses its conductors.
    WRITES - scratch-only by default.

    A line placed by the API (or moved afterwards) crosses wires that EPLAN
    does not assign to it on its own; this does what drawing the line in the
    GUI does: every connection on the page whose run straddles the line gets
    the cable's name, and after one connection update the cable lists them
    as conductors. Safe to repeat: already-assigned wires are reported as
    "already".

    Args:
        page: Page name, as live_query_pages reports it.
        cable: The cable's full name ("=+CE-W1"), short tag ("-W1") or
            handle.
        generate: Regenerate the page's connections afterwards (default
            True) so the cable's conductor list is current.
        reassign_from_part: Also run "Asignar conexiones" so the cable
            part's wire templates give the conductors their colour and
            cross-section (default False; the placing tool passes True when
            a part is attached). Without a part it changes nothing. The
            conductors and their two ends are write-locked first (it threw
            "object not locked for write access" without).
        crossing_texts: After generating, name the crossing points after
            the cable and show colour / cross-section at each crossing
            (live_decorate_cable_crossings; default True).
        crossing_style: Look of those texts, see live_decorate_cable_crossings
            (e.g. {"rotation": 0, "docked": True, "section": False}).
        pin_colours: After reassign_from_part, pair colours with PINS
            instead of template order (live_set_conductor_colours):
            "auto" (default: the M8/M12 sensor-lead map, only when the
            part gave sensor-lead colours), "sensor", a {pin: colour}
            dict, or None to leave the template order.
        extra_points: "hide" (default), "remove" or "keep" the second
            connection definition points that "Asignar conexiones" leaves
            off the line (see live_decorate_cable_crossings).
        allow_real_project: Must be True to write outside the scratch root.
        timeout_seconds: Default 180.

    Returns:
        {"success", "cable", "handle", "orientation", "crossed": [{
        "designation", "source", "target", "assigned": "now"|"already"|
        "failed"}], "errors", "conductors" (after generate), "crossings"
        (the decoration result)}. When the wires still carried the crossing
        points of a DELETED line (a wire stays "Cable" kind with no line and
        a new line never links to it), those points are removed and the line
        is placed again on the same wires: "replacedLine": True,
        "orphanPoints", "previousHandle" (the old handle is gone).
    """
    try:
        page = cs_text(page, "page")
        cable = cs_text(cable, "cable")
    except SchematicValueError as exc:
        return _err(exc)
    def _run(reassign, inline=False):
        tail = (_INLINE_TAIL.replace("GENERATE", "true") if inline else _REASSIGN_TAIL)
        body = (_guard_prelude(allow_real_project)
                + _ASSIGN_BODY.replace("PAGENAME", cs_escape(page)).replace("CABLENAME", cs_escape(cable))
                + tail.replace("REASSIGN", "true" if reassign else "false").replace("PAGEVALUE", cs_escape(page)))
        return _execute_script(
            live._script("LiveCableAssign_" + uuid.uuid4().hex[:6], body,
                         extra_helpers=schematic._HELPERS_SCHEMATIC + _HELPERS_TERM + _HELPERS_CABLE),
            timeout=timeout_seconds,
        )

    r = _run(reassign_from_part, inline=generate)
    res = r.get("results") if isinstance(r.get("results"), dict) else r
    if generate and isinstance(res, dict) and res.get("success") and not res.get("crossed"):
        # Wires drawn in this session have no Connection objects until the
        # first connection update, so a line over freshly placed devices
        # crosses "nothing": update once and look again.
        from .generate import generate_connections as _gen0
        from . import target as _t0
        _gen0(project_name=_t0.get() or None, page_name=page)
        r = _run(reassign_from_part, inline=True)
        res = r.get("results") if isinstance(r.get("results"), dict) else r
        if isinstance(res, dict):
            res["generatedFirst"] = True
    if generate and isinstance(res, dict) and res.get("success"):
        from .generate import generate_connections
        from . import target as _target
        # PROJECTNAME is passed explicitly: without it the action runs on
        # whatever project is current in the GUI, not on the target.
        wanted = len([c for c in res.get("crossed") or [] if c.get("assigned") in ("now", "already")])

        def _read():
            after = live_cables(contains=res.get("cable") or cable, timeout_seconds=timeout_seconds)
            after_res = after.get("results") if isinstance(after.get("results"), dict) else after
            for c in (after_res or {}).get("cables") or []:
                if c.get("handle") == res.get("handle"):
                    return c.get("conductors") or []
            return []

        # Measured on 2026.0.3: the first connection update after the
        # assignment does not yet list the conductors, the second does. So
        # update, read, and update once more while the count is short.
        runs = 0
        conductors = []
        inline = bool(res.get("inlineGenerate"))
        if inline:
            # the script already updated, read and (with a part) reassigned
            runs = int(res.get("connectionsGenerated") or 0)
            conductors = res.get("conductors") or []
        while not inline and runs < 3:
            g = generate_connections(project_name=_target.get() or None, page_name=page)
            runs += 1
            if not g.get("success"):
                res["generateError"] = g.get("error") or g.get("eplanMessages")
                break
            conductors = _read()
            if len(conductors) >= wanted:
                break
        if wanted and len(conductors) < wanted and not replaced_line and inline:
            # The in-script read right after the connection update can lag
            # behind: one fresh read before treating the line as stale.
            conductors = _read()
            res["conductors"] = conductors
            res["freshRead"] = True
        if wanted and len(conductors) < wanted and not replaced_line:
            # Some wires still linked to nothing (a moved line leaves its old
            # points on the wires it no longer crosses, and those wires keep
            # the Cable kind): same cure as for none at all.
            # Nothing linked although wires were assigned: the usual cause
            # is the crossing points of a DELETED line still sitting on the
            # wires (see _ORPHAN_BODY). A line already placed over them is
            # stale for EPLAN, so remove the points, then the line, and
            # place it again on the same wires.
            seg = (res.get("cableStart") or []) + (res.get("cableEnd") or [])
            if len(seg) == 4:
                orp = _orphan_points(page, seg, True, allow_real_project, timeout_seconds)
                pts = orp.get("orphanPoints") or []
                if orp.get("success", True):
                    # Even with no orphan point found the line is stale by
                    # now (three updates linked nothing): one re-place is
                    # cheap, and replaced_line keeps it from looping.
                    if orp.get("removed"):
                        generate_connections(project_name=_target.get() or None, page_name=page)
                    rc = _recreate_line(page, res.get("handle"), allow_real_project, timeout_seconds)
                    if rc.get("success") and rc.get("newHandle"):
                        again = live_assign_cable_connections(
                            page, rc["newHandle"], generate=True, reassign_from_part=reassign_from_part,
                            crossing_texts=crossing_texts, crossing_style=crossing_style,
                            pin_colours=pin_colours, extra_points=extra_points,
                            allow_real_project=allow_real_project, timeout_seconds=timeout_seconds,
                            replaced_line=True)
                        again_res = again.get("results") if isinstance(again.get("results"), dict) else again
                        if isinstance(again_res, dict):
                            again_res["replacedLine"] = True
                            again_res["orphanPoints"] = pts
                            again_res["removedPoints"] = rc.get("removedPoints")
                            again_res["previousHandle"] = res.get("handle")
                        return again
                    res["orphanPoints"] = pts
                    res["replaceError"] = rc.get("error") or rc.get("errors")
                else:
                    res["orphanPoints"] = pts
                    res["orphanError"] = orp.get("error") or orp.get("errors")
        if reassign_from_part and conductors and not inline:
            # The templates can only be handed to conductors that exist, so
            # this runs after the connection update, then one more update.
            r2 = _run(True)
            res2 = r2.get("results") if isinstance(r2.get("results"), dict) else r2
            res["reassignedFromPart"] = bool(isinstance(res2, dict) and res2.get("reassignedFromPart"))
            if isinstance(res2, dict):
                for k in ("lockedPoints", "lockedConductors", "partReloaded", "cableFieldsFromPart"):
                    if k in res2:
                        res[k] = res2[k]
            if isinstance(res2, dict) and not res2.get("success"):
                res["reassignError"] = res2.get("error") or res2.get("errors")
            g = generate_connections(project_name=_target.get() or None, page_name=page)
            runs += 1
            if g.get("success"):
                conductors = _read()
        res["connectionsGenerated"] = runs
        res["conductors"] = conductors
        if pin_colours and conductors and reassign_from_part:
            pr = live_set_conductor_colours(page, res.get("handle") or cable, pin_colours=pin_colours,
                                            allow_real_project=allow_real_project,
                                            timeout_seconds=min(timeout_seconds, 60.0))
            p_res = pr.get("results") if isinstance(pr.get("results"), dict) else pr
            res["pinColours"] = p_res
            if isinstance(p_res, dict) and not p_res.get("success"):
                res["pinColoursError"] = p_res.get("error") or p_res.get("errors")
            elif isinstance(p_res, dict) and not p_res.get("skipped"):
                conductors = _read()
                res["conductors"] = conductors
        if crossing_texts and conductors:
            dr = live_decorate_cable_crossings(page, res.get("handle") or cable, style=crossing_style,
                                               extra_points=extra_points,
                                               allow_real_project=allow_real_project,
                                               timeout_seconds=timeout_seconds)
            d_res = dr.get("results") if isinstance(dr.get("results"), dict) else dr
            res["crossings"] = d_res
            if isinstance(d_res, dict) and not d_res.get("success"):
                res["crossingsError"] = d_res.get("error") or d_res.get("errors")
    return r


def live_place_cable_definition(page: str, name: str, x1: float, y1: float,
                                x2: float, y2: float, part: str = None,
                                text: str = None, properties: dict = None,
                                symbol: str = "CABDL", variant: int = 0,
                                generate: bool = True, scheme: str = None,
                                show: list = None, crossing_texts: bool = True,
                                crossing_style: dict = None, pin_colours="auto",
                                extra_points: str = "hide",
                                allow_real_project: bool = False,
                                timeout_seconds: float = 180.0,
                                reassign_from_part: bool = None, replaced_line: bool = False) -> dict:
    """
    Place a cable definition line across connections and name it. WRITES -
    scratch-only by default.

    Draw the line (x1,y1)-(x2,y2) so it crosses the connection lines that
    belong to the cable - a horizontal line under the terminal strip across
    the vertical wires to the field devices is the usual case. Keep it clear
    of the connection definition points (the wire-number symbols): cross the
    bare wire. Every crossed connection becomes a conductor of the cable;
    with a cable part attached EPLAN takes colours and cross-sections from
    the part's connection templates. The conductors are read back, so an
    empty list means the line crossed nothing (wrong y, or the wires are
    not generated connections yet).

    Args:
        page: Page name, as live_query_pages reports it.
        name: Cable tag, e.g. "-W1" or "=+CE-W1".
        x1, y1, x2, y2: Line ends in mm.
        part: Cable part number (e.g. an ÖLFLEX from the parts database).
        text: Function text ("es_ES@...;en_US@...;" for both languages).
        properties: Extra {"PROPERTY_NAME": value} on the cable.
        symbol: SPECIAL symbol for the line (default CABDL).
        variant: Symbol variant (default 0).
        generate: Assign the crossed connections and regenerate (default
            True) - see live_assign_cable_connections; EPLAN does not do it
            for an API-placed line. False places the bare line. With a
            part, the conductors also take the part's colours and
            cross-sections (reassign from part).
        scheme: Property arrangement scheme for the line's texts, by name
            (live_property_texts(..., with_schemes=True) lists them) - the
            way to show the conductor colour at every crossing and the
            "0,25 mm²" like a manufacturer macro does, when a scheme has it.
        show: Property texts to make visible on the line, each a property
            id or the displayed text (see live_set_property_texts).
        crossing_texts: Show colour / cross-section at every crossing like a
            manufacturer macro (default True; see
            live_decorate_cable_crossings); crossing_style sets the look
            ({"rotation": 0, "docked": True, "section": False} = horizontal
            colour only).
        pin_colours: Pair the part's colours with the pins instead of the
            template order ("auto" default, "sensor", {pin: colour} or
            None) - see live_assign_cable_connections.
        allow_real_project: Must be True to write outside the scratch root.
        timeout_seconds: Default 180.

    Returns:
        {"success", "handle", "name", "page", "parts", "conductors": [...]} -
        same shape as one entry of live_cables; "propertyTexts" when scheme
        or show was given; "orphanPoints" lists the crossing points of a
        DELETED line that were removed from these wires first (they would
        have kept the new line from linking).
    """
    try:
        page = cs_text(page, "page")
        name = cs_text(name, "name")
        symbol = cs_text(symbol, "symbol")
        coords = [float(v) for v in (x1, y1, x2, y2)]
        part_cs = cs_escape(cs_text(part, "part")) if part else ""
        text_cs = cs_escape(cs_text(text, "text")) if text else ""
        if properties is not None and not isinstance(properties, dict):
            raise SchematicValueError("properties must be a dict of {name: value}.")
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc) if isinstance(exc, SchematicValueError) else {
            "success": False, "error": "x1, y1, x2, y2 must be numbers."}
    props_cs = "".join('            propsIn["%s"] = "%s";\n' % (cs_escape(str(k)), cs_escape(str(v)))
                       for k, v in (properties or {}).items())

    c1 = "%r, %r" % (coords[0], coords[1])
    c2 = "%r, %r" % (coords[2], coords[3])
    orphans = None
    if generate:
        # Crossing points of a deleted line on these wires would stop the
        # new line from ever linking: remove them first (see _ORPHAN_BODY).
        orp = _orphan_points(page, coords, True, allow_real_project, timeout_seconds)
        orphans = orp.get("orphanPoints") or []
        if orp.get("removed"):
            from .generate import generate_connections
            from . import target as _target
            generate_connections(project_name=_target.get() or None, page_name=page)

    body = _guard_prelude(allow_real_project) + '''
            object page = FindPage(project, "''' + cs_escape(page) + '''");
            Type cT = FindType("Eplan.EplApi.DataModel.EObjects.Cable");
            object fn = Activator.CreateInstance(cT);
            object variant = MakeVariantT(project, "SPECIAL", "''' + cs_escape(symbol) + '''", ''' + str(int(variant)) + ''');
            MethodInfo create = RequireMethod(cT, "Create", new string[] { "Page", "SymbolVariant", "PointD", "PointD" }, false);
            Type pt = create.GetParameters()[2].ParameterType;
            Call(create, fn, new object[] { page, variant, MakePoint(pt, ''' + c1 + '''), MakePoint(pt, ''' + c2 + ''') });
            try { MethodInfo lk = MethodByShape(fn.GetType(), "LockObject", new string[] { }, false); if (lk != null) lk.Invoke(fn, null); } catch { }
            // A bare "-W1" would become "=+-W1" (empty structures): give it
            // the page's own plant/location like the GUI does.
            string cableName = "''' + cs_escape(name) + '''";
            if (!cableName.Contains("=") && !cableName.Contains("+"))
            {
                object pprops = TryRead(page, "Properties", null);
                string plant = PropText(pprops, "DESIGNATION_PLANT") ?? "";
                string loc = PropText(pprops, "DESIGNATION_LOCATION") ?? "";
                cableName = "=" + plant + "+" + loc + cableName;
            }
            GetWritable(fn.GetType(), "Name").SetValue(fn, cableName, null);
            // The tag shows only once VisibleName is set (like any
            // function); what the caller typed ("-W1") is what is shown.
            string shown = "''' + cs_escape(name) + '''";
            if (shown.Contains("=") || shown.Contains("+"))
            {
                // like the GUI: the page's own = and + are not repeated on
                // the tag ("=ST2+CAMPO-W17" on page =MOV+CAMPO shows "=ST2-W17")
                object pprops2 = TryRead(page, "Properties", null);
                string plant2 = PropText(pprops2, "DESIGNATION_PLANT") ?? "", loc2 = PropText(pprops2, "DESIGNATION_LOCATION") ?? "";
                int ip = shown.IndexOf('+'), im = shown.IndexOf('-');
                int plantEnd = ip >= 0 ? ip : (im >= 0 ? im : shown.Length);
                string sPlant = shown.StartsWith("=") && plantEnd >= 1 ? shown.Substring(1, plantEnd - 1) : null;
                string sLoc = ip >= 0 ? shown.Substring(ip + 1, (im > ip ? im : shown.Length) - ip - 1) : null;
                string rest = im >= 0 ? shown.Substring(im) : "";
                string v = "";
                if (sPlant != null && sPlant != plant2) v += "=" + sPlant;
                if (sLoc != null && sLoc != loc2) v += "+" + sLoc;
                shown = v + rest;
            }
            try { GetWritable(fn.GetType(), "VisibleName").SetValue(fn, shown, null); results["visibleName"] = shown; }
            catch (Exception ve) { results["visibleNameError"] = Flatten(ve); }
            object props = TryRead(fn, "Properties", null);
            string text = "''' + text_cs + '''";
            string part = "''' + part_cs + '''";
            if (text != "") SetProp(props, "FUNC_TEXT", text);
            if (part != "") AddPartT(fn, part, results);
            Dictionary<string, string> propsIn = new Dictionary<string, string>();
''' + props_cs + '''
            Dictionary<string, object> written = new Dictionary<string, object>();
            foreach (KeyValuePair<string, string> kv in propsIn) written[kv.Key] = SetProp(props, kv.Key, kv.Value);
            if (written.Count > 0) results["properties"] = written;
            {
''' + _CABLE_DICT + '''
                foreach (KeyValuePair<string, object> kv in d) results[kv.Key] = kv.Value;
            }
            results["success"] = true;
'''
    r = _execute_script(
        live._script("LiveCableDef_" + uuid.uuid4().hex[:6], body,
                     extra_helpers=schematic._HELPERS_SCHEMATIC + _HELPERS_TERM + _HELPERS_CABLE),
        timeout=timeout_seconds,
    )
    res = r.get("results") if isinstance(r.get("results"), dict) else r
    if isinstance(res, dict) and orphans:
        res["orphanPoints"] = orphans
    if generate and isinstance(res, dict) and res.get("success"):
        a = live_assign_cable_connections(page, res.get("handle"), generate=True,
                                          reassign_from_part=bool(part) if reassign_from_part is None else reassign_from_part,
                                          replaced_line=replaced_line,
                                          crossing_texts=crossing_texts,
                                          crossing_style=crossing_style, pin_colours=pin_colours,
                                          extra_points=extra_points,
                                          allow_real_project=allow_real_project,
                                          timeout_seconds=timeout_seconds)
        a_res = a.get("results") if isinstance(a.get("results"), dict) else a
        if isinstance(a_res, dict):
            res["crossed"] = a_res.get("crossed")
            res["conductors"] = a_res.get("conductors") or []
            res["connectionsGenerated"] = a_res.get("connectionsGenerated")
            for k in ("reassignedFromPart", "lockedPoints", "lockedConductors", "replacedLine", "previousHandle",
                      "inlineGenerate", "inlineGenerateError", "generatedFirst", "partReloaded", "cableFieldsFromPart",
                      "freshRead"):
                if k in a_res:
                    res[k] = a_res.get(k)
            if a_res.get("orphanPoints"):
                res["orphanPoints"] = (res.get("orphanPoints") or []) + list(a_res.get("orphanPoints"))
            if "crossings" in a_res:
                res["crossings"] = a_res.get("crossings")
            if "pinColours" in a_res:
                res["pinColours"] = a_res.get("pinColours")
            if not a_res.get("success"):
                res["assignError"] = a_res.get("error") or a_res.get("errors")
    if (scheme or show) and isinstance(res, dict) and res.get("success") and res.get("handle"):
        from .captions_live import live_set_property_texts
        t = live_set_property_texts(page, res["handle"], scheme=scheme, show=show,
                                    allow_real_project=allow_real_project,
                                    timeout_seconds=timeout_seconds)
        t_res = t.get("results") if isinstance(t.get("results"), dict) else t
        res["propertyTexts"] = t_res
        if isinstance(t_res, dict) and not t_res.get("success"):
            res["propertyTextsError"] = t_res.get("error") or t_res.get("errors")
    return r


def live_clean_cable_points(page: str, x1: float, y1: float, x2: float, y2: float,
                            cable: str = None, allow_real_project: bool = False,
                            timeout_seconds: float = 120.0) -> dict:
    """
    Remove the connection definition points a deleted or moved cable line
    left on the wires inside a rectangle. WRITES - scratch-only by default.

    A wire that still carries such a point stays "Cable" kind with no line
    and never links to a new line (and shows the old cable's name at the
    point). Removed here: points with no line and Cable kind, nameless
    points with no wire number, and - with `cable` - every point named
    after that cable with no wire number. Points carrying a wire number
    (CONNECTION_DESIGNATION) are kept. One connection update follows.

    Args:
        page: Page name.
        x1, y1, x2, y2: The rectangle, any corner order, in mm.
        cable: Full cable name ("=ST1+CAMPO-W90.1") whose leftover points
            go too, wherever they sit inside the rectangle.
        allow_real_project: Must be True to write outside the scratch root.
        timeout_seconds: Default 120.

    Returns:
        {"success", "removed": n, "points": [{handle, name, kind, x, y,
        removed}], "connectionsGenerated"}.
    """
    try:
        page_cs = cs_text(page, "page")
        coords = [float(v) for v in (x1, y1, x2, y2)]
        cable_cs = cs_escape(cs_text(cable, "cable")) if cable else ""
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc if isinstance(exc, SchematicValueError) else SchematicValueError(str(exc)))
    rx1, rx2 = min(coords[0], coords[2]), max(coords[0], coords[2])
    ry1, ry2 = min(coords[1], coords[3]), max(coords[1], coords[3])
    body = (_guard_prelude(allow_real_project) + '''
            object page = FindPage(project, "PAGENAME");
            double rx1 = RX1, ry1 = RY1, rx2 = RX2, ry2 = RY2; string cableName = "CABLENAME";
            LockAny(page);
            List<object> snap = new List<object>();
            foreach (object q0 in (IEnumerable)PagePlacements(page)) snap.Add(q0);
            List<Dictionary<string, object>> found = new List<Dictionary<string, object>>();
            int removed = 0;
            foreach (object pl in snap)
            {
                if (pl == null || pl.GetType().Name != "ConnectionDefinitionPoint") continue;
                object loc = TryRead(pl, "Location", null); if (loc == null) continue;
                double x = Convert.ToDouble(loc.GetType().GetProperty("X").GetValue(loc, null)), y = Convert.ToDouble(loc.GetType().GetProperty("Y").GetValue(loc, null));
                if (x < rx1 - 0.3 || x > rx2 + 0.3 || y < ry1 - 0.3 || y > ry2 + 0.3) continue;
                string kind = Convert.ToString(TryRead(pl, "KindOfWire", null));
                object line = TryRead(pl, "CableDefinitionLine", null);
                string name = PropText(pl, "Name") ?? "";
                object pp = TryRead(pl, "Properties", null);
                string des = pp == null ? "" : (PropText(pp, "CONNECTION_DESIGNATION") ?? "");
                if (des != "") continue;
                bool nameless = name == "" || name.EndsWith("+") || name.EndsWith("=");
                bool orphan = (line == null && (kind == "Cable" || nameless)) || (cableName != "" && name == cableName);
                if (!orphan) continue;
                Dictionary<string, object> d = new Dictionary<string, object>();
                d["handle"] = Handle(pl); d["name"] = name; d["kind"] = kind; d["x"] = x; d["y"] = y;
                try { LockHard(pl); pl.GetType().GetMethod("Remove", Type.EmptyTypes).Invoke(pl, null); d["removed"] = true; removed++; }
                catch (Exception e) { d["error"] = Flatten(e); }
                found.Add(d);
            }
            results["points"] = found; results["removed"] = removed; results["success"] = true;
'''.replace("PAGENAME", cs_escape(page_cs)).replace("CABLENAME", cable_cs)
            .replace("RX1", repr(rx1)).replace("RY1", repr(ry1)).replace("RX2", repr(rx2)).replace("RY2", repr(ry2)))
    r = _execute_script(
        live._script("LiveCleanPoints_" + uuid.uuid4().hex[:6], body,
                     extra_helpers=schematic._HELPERS_SCHEMATIC + _HELPERS_TERM + _HELPERS_CABLE),
        timeout=timeout_seconds,
    )
    res = r.get("results") if isinstance(r.get("results"), dict) else None
    if isinstance(res, dict) and res.get("removed"):
        from .generate import generate_connections
        from . import target as _target
        g = generate_connections(project_name=_target.get() or None, page_name=page_cs)
        res["connectionsGenerated"] = bool(g.get("success"))
    return r
