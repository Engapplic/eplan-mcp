"""
Copy a validated region of a page (a sensor column, a valve station) to
another place or page, renamed. WRITES.

Why: the dense I/O pages of a machine repeat one column per connector -
Y connector, two cables with part, two detectors with photo and texts,
structure box - and drawing each column again cost the agent 60-90 calls
(P26009 pages /11-/13: 184-209 calls each). EPLAN's own way to multiply a
validated piece is a window macro; this tool makes one from the region on
the fly (WindowMacro.Create over the placements inside the rectangle),
inserts it at the target (Insert.WindowMacro), and renames what came out
with a substitution map, so one call gives one more column.

What is renamed: every function's full tag (Name) and displayed tag
(VisibleName), the cables included, by literal substitution in order of
longest key first ("=ST1" -> "=ST3", "-MB01" -> "-MB50"); function texts
(FUNC_TEXT) and free page texts by `text_replace` the same way. Connection
definition points are not renamed (their name follows their cable).

Afterwards the target page gets one connection update and every copied
cable line runs the usual assignment chain (conductors, part colours by
pin, crossing texts), so the copy is wired like the original.
"""

import os
import uuid

from ._base import cs_escape
from .scripted import _execute_script
from . import live, schematic
from .schematic import _err, _guard_prelude
from .schematic_model import cs_bool, cs_text, SchematicValueError
from .terminals_live import _HELPERS_TERM


_COPY_BODY = '''
            object src = FindPage(project, "SRCPAGE");
            object dst = FindPage(project, "DSTPAGE");
            double rx1 = RX1, ry1 = RY1, rx2 = RX2, ry2 = RY2;
            double tol = 0.5;
            List<object> picked = new List<object>();
            List<Dictionary<string, object>> pickedInfo = new List<Dictionary<string, object>>();
            foreach (object pl in PagePlacements(src))
            {
                if (pl == null) continue;
                double bx0, by0, bx1, by1;
                object loc = TryRead(pl, "Location", null);
                try
                {
                    object bb = pl.GetType().GetMethod("GetBoundingBox", Type.EmptyTypes).Invoke(pl, null);
                    bx0 = double.MaxValue; by0 = double.MaxValue; bx1 = double.MinValue; by1 = double.MinValue;
                    foreach (object p in (IEnumerable)bb)
                    {
                        double px = Convert.ToDouble(p.GetType().GetProperty("X").GetValue(p, null)), py = Convert.ToDouble(p.GetType().GetProperty("Y").GetValue(p, null));
                        if (Math.Abs(px) > 1e6 || Math.Abs(py) > 1e6) continue;
                        bx0 = Math.Min(bx0, px); bx1 = Math.Max(bx1, px); by0 = Math.Min(by0, py); by1 = Math.Max(by1, py);
                    }
                    if (bx0 == double.MaxValue) throw new Exception("empty box");
                }
                catch (Exception)
                {
                    if (loc == null) continue;
                    bx0 = bx1 = Convert.ToDouble(loc.GetType().GetProperty("X").GetValue(loc, null));
                    by0 = by1 = Convert.ToDouble(loc.GetType().GetProperty("Y").GetValue(loc, null));
                }
                bool inside = TOUCH
                    ? (bx1 >= rx1 - tol && bx0 <= rx2 + tol && by1 >= ry1 - tol && by0 <= ry2 + tol)
                    : (bx0 >= rx1 - tol && bx1 <= rx2 + tol && by0 >= ry1 - tol && by1 <= ry2 + tol);
                if (!inside) continue;
                picked.Add(pl);
                Dictionary<string, object> pi = new Dictionary<string, object>();
                pi["clrType"] = pl.GetType().Name; pi["handle"] = Handle(pl); pi["name"] = PropText(pl, "Name") ?? "";
                pickedInfo.Add(pi);
            }
            results["picked"] = pickedInfo;
            if (picked.Count == 0) throw new Exception("Nothing inside the region (" + rx1 + ", " + ry1 + ")-(" + rx2 + ", " + ry2 + ") on page SRCPAGE.");

            // 1. the region as a window macro, reference point = the region's lower-left corner
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
            object rep;
            try { rep = Enum.Parse(cp[1].ParameterType, "REPTYPE"); }
            catch (Exception) { throw new Exception("Unknown representation 'REPTYPE'. Values: " + string.Join(", ", Enum.GetNames(cp[1].ParameterType))); }
            Array arr = Array.CreateInstance(cp[3].ParameterType.GetElementType(), picked.Count);
            for (int i = 0; i < picked.Count; i++) arr.SetValue(picked[i], i);
            object desc = Activator.CreateInstance(cp[4].ParameterType);
            string macroFile = @"MACROFILE";
            try { create.Invoke(wm, new object[] { macroFile, rep, 0, arr, desc, MakePoint(cp[5].ParameterType, rx1, ry1) }); }
            catch (TargetInvocationException tie) { throw new Exception("WindowMacro.Create failed: " + Flatten(tie.InnerException ?? tie)); }
            results["macroFile"] = macroFile;

            // 2. insert at the target, keeping the names (renamed below)
            Type insType = FindType("Eplan.EplApi.HEServices.Insert");
            object ins = Activator.CreateInstance(insType);
            MethodInfo wm7 = null, wm6 = null;
            foreach (MethodInfo m in insType.GetMethods())
            {
                ParameterInfo[] ps = m.GetParameters();
                if (m.Name != "WindowMacro" || ps[0].ParameterType != typeof(string)) continue;
                if (ps.Length == 7) wm7 = m;
                if (ps.Length == 6) wm6 = m;
            }
            if (wm7 == null && wm6 == null) throw new Exception("Insert.WindowMacro(string, ...) not found.");
            MethodInfo wmi = wm7 ?? wm6;
            ParameterInfo[] wp = wmi.GetParameters();
            object move = Enum.Parse(wp[5].ParameterType, "Absolute");
            object[] objs;
            try
            {
                if (wm7 != null)
                {
                    Type nmT = wp[6].ParameterType; object nm = null;
                    foreach (string cand in new string[] { "DontNumber", "DoNotNumber", "NoNumeration", "Keep" })
                        if (nm == null && Array.IndexOf(Enum.GetNames(nmT), cand) >= 0) nm = Enum.Parse(nmT, cand);
                    if (nm == null) nm = Enum.GetValues(nmT).GetValue(0);
                    results["numerationMode"] = Convert.ToString(nm);
                    objs = (object[])wmi.Invoke(ins, new object[] { macroFile, rep, 0, dst, MakePoint(wp[4].ParameterType, TX, TY), move, nm });
                }
                else objs = (object[])wmi.Invoke(ins, new object[] { macroFile, rep, 0, dst, MakePoint(wp[4].ParameterType, TX, TY), move });
            }
            catch (TargetInvocationException tie) { throw new Exception("Insert.WindowMacro failed: " + Flatten(tie.InnerException ?? tie)); }
            if (objs == null || objs.Length == 0) throw new Exception("The window macro inserted nothing on DSTPAGE.");

            // 3. rename: tags, visible tags, function texts, free texts
            string[][] nameMap = new string[][] { NAMEMAP };
            string[][] textMap = new string[][] { TEXTMAP };
            List<object> all = new List<object>();
            Stack<object> todo = new Stack<object>();
            foreach (object o in objs) if (o != null) todo.Push(o);
            while (todo.Count > 0)
            {
                object o = todo.Pop(); all.Add(o);
                object subs = TryRead(o, "SubPlacements", null);
                if (subs != null) foreach (object s0 in (IEnumerable)subs) if (s0 != null) todo.Push(s0);
            }
            List<Dictionary<string, object>> copied = new List<Dictionary<string, object>>();
            List<string> cables = new List<string>();
            List<string> renameErrors = new List<string>();
            int renamed = 0, textsChanged = 0;
            foreach (object o in all)
            {
                string tn = o.GetType().Name;
                Dictionary<string, object> d = new Dictionary<string, object>();
                d["clrType"] = tn; d["handle"] = Handle(o);
                bool isFn = GetReadable(o.GetType(), "NameParts") != null && tn != "ConnectionDefinitionPoint" && tn != "PotentialDefinition";
                if (isFn)
                {
                    string n = PropText(o, "Name") ?? ""; d["name"] = n;
                    string nn = Apply(n, nameMap);
                    try
                    {
                        if (nn != n) { LockHard(o); GetWritable(o.GetType(), "Name").SetValue(o, nn, null); d["newName"] = nn; renamed++; }
                        string vn = PropText(o, "VisibleName") ?? "";
                        string vnn = Apply(vn, nameMap);
                        if (vn != "" && vnn != vn) { LockHard(o); GetWritable(o.GetType(), "VisibleName").SetValue(o, vnn, null); d["newVisibleName"] = vnn; }
                        object props = TryRead(o, "Properties", null);
                        string ft = props == null ? "" : (PropText(props, "FUNC_TEXT") ?? "");
                        string ftn = Apply(ft, textMap);
                        if (ft != "" && ftn != ft) { LockHard(o); SetProp(props, "FUNC_TEXT", ftn); d["newText"] = ftn; textsChanged++; }
                    }
                    catch (Exception re) { d["error"] = Flatten(re); renameErrors.Add(n + ": " + Flatten(re)); }
                    if (tn == "Cable") cables.Add(Handle(o));
                }
                else if (tn == "Text")
                {
                    try
                    {
                        object tv = TryRead(o, "Contents", null);   // Graphics.Text keeps its string in Contents
                        string t = "";
                        if (tv != null)
                        {
                            MethodInfo gas = tv.GetType().GetMethod("GetAsString", Type.EmptyTypes);
                            t = gas != null ? (Convert.ToString(gas.Invoke(tv, null)) ?? "") : (Convert.ToString(tv) ?? "");
                        }
                        string tn2 = Apply(t, textMap);
                        if (t != "" && tn2 != t)
                        {
                            LockHard(o);
                            PropertyInfo tp = GetWritable(o.GetType(), "Contents");
                            object mls = Activator.CreateInstance(tp.PropertyType);
                            MethodInfo sas = tp.PropertyType.GetMethod("SetAsString", new Type[] { typeof(string) });
                            if (sas != null) sas.Invoke(mls, new object[] { tn2 }); else mls = tn2;
                            tp.SetValue(o, mls, null); d["newText"] = tn2; textsChanged++;
                        }
                    }
                    catch (Exception te) { d["error"] = Flatten(te); renameErrors.Add("text: " + Flatten(te)); }
                }
                else continue;
                copied.Add(d);
            }
            results["copied"] = copied; results["renamed"] = renamed; results["textsChanged"] = textsChanged;
            results["cables"] = cables; results["inserted"] = objs.Length;
            results["renameErrors"] = renameErrors;
            results["success"] = renameErrors.Count == 0;
'''

_HELPERS_COPY = r'''
    // literal substitutions, longest key first, every occurrence
    static string Apply(string s, string[][] map)
    {
        if (string.IsNullOrEmpty(s) || map.Length == 0) return s;
        List<string[]> sorted = new List<string[]>(map);
        sorted.Sort(delegate(string[] a, string[] b) { return b[0].Length.CompareTo(a[0].Length); });
        foreach (string[] kv in sorted) if (kv[0].Length > 0) s = s.Replace(kv[0], kv[1]);
        return s;
    }
'''


def _map_rows(d, what):
    if d is None:
        return []
    if not isinstance(d, dict):
        raise SchematicValueError("%s must be a {old: new} dict." % what)
    rows = []
    for k, v in d.items():
        rows.append((cs_escape(cs_text(str(k), what + " key")), cs_escape(cs_text(str(v), what + " value"))))
    return rows


def live_copy_region(page: str, x1: float, y1: float, x2: float, y2: float,
                     to_page: str = None, x: float = None, y: float = None,
                     rename: dict = None, text_replace: dict = None,
                     touch: bool = False, representation: str = "MultiLine",
                     cables: bool = True, pin_colours="auto", crossing_style: dict = None,
                     allow_real_project: bool = False, timeout_seconds: float = 300.0) -> dict:
    """
    Copy everything inside a rectangle of a page to another place - the same
    page or another - renamed by a substitution map. WRITES - scratch-only
    by default.

    One validated sensor column (Y connector, two cables with part, two
    detectors with photo and texts) becomes the next one in a single call,
    with its text positions, parts, pictures and cables; the agent no
    longer rebuilds the column object by object. Under the hood the region
    is written to a temporary window macro (WindowMacro.Create over the
    placements inside the rectangle) and inserted at the target
    (Insert.WindowMacro, names kept), then every function tag, visible tag,
    function text and free text is rewritten through the maps. The target
    page then gets a connection update and each copied cable line runs the
    assignment chain (conductors, part colours by pin, crossing texts).

    Args:
        page: Source page.
        x1, y1, x2, y2: The rectangle on the source page, any corner
            order, in mm. An object is copied when its bounding box lies
            inside (0.5 mm tolerance); `touch=True` copies anything the
            rectangle overlaps.
        to_page: Target page (default: the source page).
        x, y: Where the rectangle's LOWER-LEFT corner (min x, min y) lands
            on the target page. Default: the same place (needs a different
            to_page).
        rename: {old: new} applied literally to every function's Name and
            VisibleName, longest key first - {"=ST1": "=ST3", "-MB01":
            "-MB50", "-W01": "-W05"}. Cables included. Without it the copy
            keeps the original tags (duplicates!).
        text_replace: {old: new} for function texts and free page texts
            ("Cilindro 1" -> "Cilindro 3").
        touch: See x1..y2.
        representation: Representation type of the macro (MultiLine).
        cables: Run the cable assignment chain on every copied cable line
            (default True).
        pin_colours, crossing_style: Passed to that chain (see
            live_assign_cable_connections).
        allow_real_project: Must be True to write outside the scratch root.
        timeout_seconds: Default 300.

    Returns:
        {"success", "picked": [{clrType, handle, name}] (what the rectangle
        held), "inserted": n, "copied": [{clrType, handle, name, newName?,
        newVisibleName?, newText?, error?}], "renamed", "textsChanged",
        "cables": [handles], "cableResults": [{handle, success, conductors,
        error?}], "renameErrors"}.
    """
    try:
        page_cs = cs_escape(cs_text(page, "page"))
        to_cs = cs_escape(cs_text(to_page, "to_page")) if to_page else page_cs
        rep_cs = cs_escape(cs_text(representation, "representation"))
        coords = [float(v) for v in (x1, y1, x2, y2)]
        rx1, rx2 = min(coords[0], coords[2]), max(coords[0], coords[2])
        ry1, ry2 = min(coords[1], coords[3]), max(coords[1], coords[3])
        tx = rx1 if x is None else float(x)
        ty = ry1 if y is None else float(y)
        if (to_page is None or to_page == page) and abs(tx - rx1) < 0.01 and abs(ty - ry1) < 0.01:
            raise SchematicValueError("The copy would land on itself: give x, y or another to_page.")
        name_rows = _map_rows(rename, "rename")
        text_rows = _map_rows(text_replace, "text_replace")
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc if isinstance(exc, SchematicValueError) else SchematicValueError(str(exc)))

    macro_dir = os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or ".", "eplan_mcp_macros")
    os.makedirs(macro_dir, exist_ok=True)
    macro_file = os.path.join(macro_dir, "region_%s.ema" % uuid.uuid4().hex[:8])

    def table(rows):
        return ", ".join('new string[] { "%s", "%s" }' % (k, v) for k, v in rows)

    body = (_guard_prelude(allow_real_project)
            + _COPY_BODY.replace("SRCPAGE", page_cs).replace("DSTPAGE", to_cs)
            .replace("RX1", repr(rx1)).replace("RY1", repr(ry1)).replace("RX2", repr(rx2)).replace("RY2", repr(ry2))
            .replace("TOUCH", cs_bool(touch)).replace("REPTYPE", rep_cs)
            .replace("MACROFILE", macro_file.replace('"', '""'))
            .replace("TX", repr(tx)).replace("TY", repr(ty))
            .replace("NAMEMAP", table(name_rows)).replace("TEXTMAP", table(text_rows)))
    r = _execute_script(
        live._script("LiveCopyRegion_" + uuid.uuid4().hex[:6], body,
                     extra_helpers=schematic._HELPERS_SCHEMATIC + _HELPERS_TERM + _cable_helpers() + _HELPERS_COPY),
        timeout=timeout_seconds,
    )
    try:
        if os.path.exists(macro_file):
            os.remove(macro_file)
    except OSError:
        pass
    res = r.get("results") if isinstance(r.get("results"), dict) else None
    if isinstance(res, dict) and res.get("inserted"):
        from .generate import generate_connections
        from . import target as _target
        g = generate_connections(project_name=_target.get() or None, page_name=to_page or page)
        res["connectionsGenerated"] = bool(g.get("success"))
        if cables and res.get("cables"):
            from .cables_live import live_assign_cable_connections
            outs = []
            for h in res["cables"]:
                a = live_assign_cable_connections(to_page or page, h, reassign_from_part=True,
                                                  pin_colours=pin_colours, crossing_style=crossing_style,
                                                  allow_real_project=allow_real_project,
                                                  timeout_seconds=timeout_seconds)
                a_res = a.get("results") if isinstance(a.get("results"), dict) else a
                outs.append({"handle": h, "success": bool(a_res.get("success")),
                             "newHandle": a_res.get("handle") if a_res.get("replacedLine") else None,
                             "conductors": [(c.get("designation"), c.get("colour")) for c in a_res.get("conductors") or []],
                             "error": a_res.get("error") or a_res.get("errors") or a_res.get("assignError")})
            res["cableResults"] = outs
            if any(not o["success"] for o in outs):
                res["success"] = False
                res["error"] = "cable assignment failed on " + ", ".join(o["handle"] for o in outs if not o["success"])
                r["success"] = False
    return r


def _cable_helpers():
    from .cables_live import _HELPERS_CABLE
    return _HELPERS_CABLE
