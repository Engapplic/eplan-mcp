"""
Page recipes: a whole page's routine rework in ONE tool call.

A recipe reads the page once, plans every op in Python (no model turns),
runs the ops in a couple of scripts and answers with a SUMMARY - counters,
expected vs found, warnings. It replaces hundreds of generated ops pasted
into the conversation and hundreds of KB of echoes coming back (P26009,
FIJ/10-13 and MOV/10-13, 2026-11-09: one page cost ~30 tool calls and
~200 KB of results by hand).

The plan is a pure function of the page read (`plan_connector_recipe`), so
it is tested offline and a dry run shows exactly what would be written.
"""

import re
import time

from . import schematic, target
from .ops import run_ops, summarize_batch
from .schematic_model import SchematicValueError, cs_text

# ---------------------------------------------------------------------------
# The connector recipe: sensor leads drawn as plugged hoses
# ---------------------------------------------------------------------------
#
# Validated on P26009 (house style, design rules "Mangueras con haz"):
#
#   sensor box pin (DCP -> DCPM v2)                      y_pin
#   XBD2 v2 (cable's female connector XD1:n)            y_pin - 9
#   strand bundle ... cable definition line             y_line
#   XSD2 v2 (cable's male connector XD2:n)              y_line - 2
#   Y box X1/X2 pins (DCP -> DCPFEM v0), X3 (DCP -> DCPM v2)
#   EX600 terminals (PLCCPPO -> PLCCPPFEM v0), designation "XIk:n",
#   k = port by x relative to the box's first terminal / port_pitch (48 mm)
#
# The Y's X1/X2 designation text (property 20036) sits TopCenter at
# (x - 2.26, y - 1.5) of the pin, X3's BottomCenter at (x + 1.3, y + 2.8).
# Positions are the TOP-LEFT target set_property_texts measures toward,
# with that justification (P26009 labels.py, 2026-11-09).

LABEL_ID = "20036"
LABEL_X12 = ("TopCenter", -2.26, -1.5)
LABEL_X3 = ("BottomCenter", 1.3, 2.8)

_DCP_FAMILY = ("DCP", "DCPM", "DCPFEM")


def _bbox(p):
    pts = [b for b in (p.get("boundingBox") or []) if isinstance(b, dict) and b.get("x") is not None
           and abs(b["x"]) < 1e6 and abs(b["y"]) < 1e6]
    if not pts:
        loc = p.get("location") or {}
        if loc.get("x") is None:
            return None
        return (loc["x"], loc["y"], loc["x"], loc["y"])
    xs = [b["x"] for b in pts]; ys = [b["y"] for b in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _inside(loc, bb, tol=0.3):
    return bb[0] - tol <= loc["x"] <= bb[2] + tol and bb[1] - tol <= loc["y"] <= bb[3] + tol


def _designation(name):
    # "=ST1+CAMPO-MB01.1:1" -> "1"; "=+:X1:4" -> "X1:4" (the FIRST colon)
    name = name or ""
    i = name.find(":")
    return name[i + 1:] if i >= 0 else None


def _sym(p):
    s = p.get("symbol") or {}
    return (s.get("library") or "", s.get("name") or "", s.get("variantNr"))


def _num_suffix(tag):
    m = re.search(r"-[A-Za-z]+(\d+(?:\.\d+)?)$", tag or "")
    return m.group(1) if m else None


def plan_connector_recipe(placements, port_pitch=48.0, plug_drop=9.0, line_gap=2.0):
    """Plan the connector recipe from a page read. Pure - no EPLAN.

    Returns {"retag", "symbols", "terminals", "plugs", "labels",
    "sensorPins", "warnings", "skipped"} where every list but warnings holds
    ready-to-run ops (labels: set_property_texts items)."""
    boxes, cables, plugs, pins, terms = [], [], [], [], []
    for p in placements:
        t = p.get("clrType")
        if t == "BoxedDevice":
            bb = _bbox(p)
            if bb:
                boxes.append((p, bb))
        elif t == "Cable":
            bb = _bbox(p)
            loc = p.get("location") or {}
            if bb and loc.get("y") is not None:
                cables.append((bb[0], bb[2], p.get("name") or "", loc["y"], p))
        elif t == "Plug":
            plugs.append(p.get("location") or {})
        elif t == "Function":
            pins.append(p)
        elif t == "Terminal":
            terms.append(p)

    def box_of(loc):
        best = None
        for b, bb in boxes:
            if _inside(loc, bb):
                area = (bb[2] - bb[0]) * (bb[3] - bb[1])
                if best is None or area < best[0]:
                    best = (area, b)
        return best[1] if best else None

    def plug_at(x, y):
        return any(abs((q.get("x") or -1e9) - x) < 0.3 and abs((q.get("y") or -1e9) - y) < 0.3 for q in plugs)

    retag = [{"op": "update", "ref": b["handle"], "tag": b.get("name")} for b, _ in boxes if b.get("name")]
    symbols, plug_ops, labels, warnings, sensor_pins = [], [], [], [], []
    skipped = {"symbols": 0, "terminals": 0, "plugs": 0}

    def want_symbol(p, name, variant):
        lib, cur, var = _sym(p)
        if cur == name and var == variant:
            skipped["symbols"] += 1
            return
        symbols.append({"op": "update", "ref": p["handle"],
                        "symbol": {"library": "SPECIAL", "symbol": name, "variant": variant}})

    for p in pins:
        loc = p.get("location") or {}
        if loc.get("x") is None:
            continue
        dd = _designation(p.get("name"))
        lib, cur, var = _sym(p)
        if dd is None or cur not in _DCP_FAMILY:
            continue
        head = dd.split(":")[0]
        if head in ("X1", "X2"):
            want_symbol(p, "DCPFEM", 0)
            j, dx, dy = LABEL_X12
            labels.append({"ref": p["handle"], "justifications": {LABEL_ID: j},
                           "positions": {LABEL_ID: [round(loc["x"] + dx, 2), round(loc["y"] + dy, 2)]}})
            continue
        if head == "X3":
            want_symbol(p, "DCPM", 2)
            j, dx, dy = LABEL_X3
            labels.append({"ref": p["handle"], "justifications": {LABEL_ID: j},
                           "positions": {LABEL_ID: [round(loc["x"] + dx, 2), round(loc["y"] + dy, 2)]}})
            continue
        box = box_of(loc)
        if box is None:
            warnings.append("pin %s at (%.2f, %.2f) is in no box" % (p.get("name"), loc["x"], loc["y"]))
            continue
        # a sensor pin: in a box above a cable definition line
        cov = [c for c in cables if c[0] - 0.05 <= loc["x"] <= c[1] + 0.05 and c[3] < loc["y"]]
        if cur in ("DCP", "DCPM"):
            want_symbol(p, "DCPM", 2)
        if len(cov) != 1:
            warnings.append("sensor pin %s at x=%.2f is crossed by %d cable lines" % (p.get("name"), loc["x"], len(cov)))
            continue
        cab = cov[0]
        if _num_suffix(box.get("name")) and _num_suffix(cab[2]) and _num_suffix(box.get("name")) != _num_suffix(cab[2]):
            warnings.append("sensor %s is on cable %s (numbers differ)" % (box.get("name"), cab[2]))
        sensor_pins.append({"pin": p["handle"], "x": loc["x"], "cable": cab[2], "designation": dd})
        y1, y2 = loc["y"] - plug_drop, cab[3] - line_gap
        for sym, y, xd in (("XBD2", y1, "XD1"), ("XSD2", y2, "XD2")):
            if plug_at(loc["x"], y):
                skipped["plugs"] += 1
                continue
            plug_ops.append({"op": "place", "library": "IEC_symbol", "symbol": sym, "variant": 2,
                             "x": loc["x"], "y": y, "snap": False, "tag": cab[2], "visibleName": "",
                             "allowMerge": True, "properties": {"FUNC_CONNECTIONDESIGNATION[1]": "%s:%s" % (xd, dd)}})
    pin_y = {q["handle"]: (q.get("location") or {}).get("y") for q in pins}
    ys = [pin_y[s["pin"]] for s in sensor_pins if pin_y.get(s["pin"]) is not None]
    if ys:
        mode = max(set(ys), key=ys.count)
        odd = [y for y in ys if abs(y - mode) > 0.1]
        if odd:
            warnings.append("%d sensor pin(s) not on the row y=%.2f" % (len(odd), mode))

    # EX600 terminals: socket symbol and "XIk:n" by port
    term_ops = []
    by_box = {}
    for p in terms:
        loc = p.get("location") or {}
        if loc.get("x") is None:
            continue
        box = box_of(loc)
        by_box.setdefault(box["handle"] if box else None, []).append(p)
    for bh, group in by_box.items():
        x0 = min(q["location"]["x"] for q in group)
        for p in group:
            dd = _designation(p.get("name")) or ""
            n = dd.split(":")[-1][-1:] if dd else ""
            if not n:
                warnings.append("terminal %s has no designation" % p.get("name"))
                continue
            k = int(round(p["location"]["x"] - x0) // port_pitch)
            want = "XI%d:%s" % (k, n)
            lib, cur, var = _sym(p)
            if cur == "PLCCPPFEM" and var == 0 and dd == want:
                skipped["terminals"] += 1
                continue
            op = {"op": "update", "ref": p["handle"], "properties": {"FUNC_CONNECTIONDESIGNATION[1]": want}}
            if not (cur == "PLCCPPFEM" and var == 0):
                op["symbol"] = {"library": "SPECIAL", "symbol": "PLCCPPFEM", "variant": 0}
            term_ops.append(op)

    return {"retag": retag, "symbols": symbols, "terminals": term_ops, "plugs": plug_ops,
            "labels": labels, "sensorPins": sensor_pins, "cables": [c[4]["handle"] for c in cables],
            "cableNames": [c[2] for c in cables], "warnings": warnings, "skipped": skipped}


_STRAY_BODY = r'''
            object page = FindPage(project, "PAGENAME");
            LockAny(page);
            // every cable definition line of the page: name, x range, line y
            List<object[]> lines = new List<object[]>();
            foreach (object pl in PagePlacements(page))
            {
                if (pl == null || pl.GetType().Name != "Cable") continue;
                object loc = TryRead(pl, "Location", null); if (loc == null) continue;
                double cx1 = 1e9, cx2 = -1e9;
                foreach (object q in (IEnumerable)pl.GetType().GetMethod("GetBoundingBox", Type.EmptyTypes).Invoke(pl, null))
                { double qx = Convert.ToDouble(q.GetType().GetProperty("X").GetValue(q, null)); if (Math.Abs(qx) < 1e6) { cx1 = Math.Min(cx1, qx); cx2 = Math.Max(cx2, qx); } }
                lines.Add(new object[] { PropText(pl, "Name") ?? "", cx1, cx2, Convert.ToDouble(loc.GetType().GetProperty("Y").GetValue(loc, null)) });
            }
            List<object> snap = new List<object>();
            foreach (object q0 in (IEnumerable)PagePlacements(page)) if (q0 != null && q0.GetType().Name == "ConnectionDefinitionPoint") snap.Add(q0);
            int removed = 0; List<string> errors = new List<string>(); List<string> gone = new List<string>();
            foreach (object pl in snap)
            {
                string nm = PropText(pl, "Name") ?? "";
                object loc = TryRead(pl, "Location", null); if (loc == null) continue;
                double x = Convert.ToDouble(loc.GetType().GetProperty("X").GetValue(loc, null)), y = Convert.ToDouble(loc.GetType().GetProperty("Y").GetValue(loc, null));
                object pp = TryRead(pl, "Properties", null);
                if (pp != null && (PropText(pp, "CONNECTION_DESIGNATION") ?? "") != "") continue;   // a real wire number
                bool stray = false;
                foreach (object[] ln in lines)
                {
                    if ((string)ln[0] != nm) continue;
                    if (x < (double)ln[1] - 1 || x > (double)ln[2] + 1) continue;
                    // named after this cable, on its wires, but not on its
                    // line: the second point "Asignar conexiones" leaves, or
                    // one the old wire kept when the plugs split it
                    if (Math.Abs(y - (double)ln[3]) > 0.3) stray = true;
                }
                if (!stray) continue;
                try { LockHard(pl); pl.GetType().GetMethod("Remove", Type.EmptyTypes).Invoke(pl, null); removed++; gone.Add(Handle(pl)); }
                catch (Exception e) { errors.Add(Handle(pl) + ": " + Flatten(e)); }
            }
            results["removed"] = removed; results["handles"] = gone; results["errors"] = errors; results["success"] = errors.Count == 0;
'''


def _remove_stray_points(page, allow_real_project, timeout_seconds):
    """Remove the points named after a page cable that sit on its wires but
    off its definition line (no wire number): the recipe's plugs split the
    old wires, and those points draw a slash and keep a stub on the cable's
    layer."""
    import uuid
    from . import live
    from .cables_live import _guard_prelude, _execute_script, _HELPERS_CABLE
    from .terminals_live import _HELPERS_TERM
    from ._base import cs_escape
    body = _guard_prelude(allow_real_project) + _STRAY_BODY.replace("PAGENAME", cs_escape(page))
    return _execute_script(live._script("RecipeStray_" + uuid.uuid4().hex[:6], body,
                                        extra_helpers=schematic._HELPERS_SCHEMATIC + _HELPERS_TERM + _HELPERS_CABLE),
                           timeout=timeout_seconds)


def live_apply_connector_recipe(page: str, dry_run: bool = False, port_pitch: float = 48.0,
                                reassign: bool = True, labels: bool = True, layers: bool = True,
                                verbose: bool = False, allow_real_project: bool = False,
                                timeout_seconds: float = 180.0) -> dict:
    """
    Rework a sensor page to plugged hoses in ONE call. WRITES - scratch-only
    by default.

    What P26009 did by hand on FIJ/10-13 and MOV/10-13 (30 calls, ~200 KB
    per page), as one recipe:
      1. retag every black box with its own tag (its pins keep only ":n"
         and follow it);
      2. symbols in place: sensor pins DCP -> DCPM v2, Y X3 DCP -> DCPM v2,
         Y X1/X2 DCP -> DCPFEM v0, EX600 terminals PLCCPPO -> PLCCPPFEM v0
         with designation "XIk:n" (k = port by x from the box's first
         terminal / port_pitch);
      3. for every sensor pin crossed by a cable definition line: the
         cable's female connector XBD2 v2 at (x, y_pin - 9) "XD1:n" and its
         male connector XSD2 v2 at (x, y_line - 2) "XD2:n", tagged with the
         cable (an existing plug at that spot is kept);
      4. connection update with rebuild_all, every cable reassigned from
         its part (crossing_texts=False: hand-placed colour texts stay),
         then the points named after a cable that sit on its wires but off
         its line removed (the old wires' points, split by the plugs);
      5. the Y's designation texts (20036): X1/X2 TopCenter at
         (x - 2.26, y - 1.5), X3 BottomCenter at (x + 1.3, y + 2.8);
      6. eplan_live_set_connection_layers with the reset.
    Running it again on a reworked page changes nothing (every step skips
    what is already right).

    Args:
        page: Page name.
        dry_run: Only plan: counts, warnings, nothing written.
        port_pitch: mm between the EX600 ports (default 48).
        reassign, labels, layers: Steps 4, 5 and 6 (default all True).
        verbose: Add the planned ops and every step's result.
        allow_real_project: Must be True to write outside the scratch root.
        timeout_seconds: For each script (default 180; a step takes a few
            seconds). When a script gets no answer, or another session holds
            EPLAN past the lock timeout, the recipe STOPS: the remaining steps
            are not sent to a hung EPLAN ("aborted" names the step; P26009
            2026-11-09: a hang made one run wait 1456 s, 600 s per step).

    Returns:
        A SUMMARY: {"success", "page", "dryRun", "planned": {retag, symbols,
        terminals, plugs, labels}, "skipped", "sensorPins", "cables",
        "steps": {name: {ok, failed, errors?}}, "conductors": {"expected",
        "found"}, "plugs": {"expected", "found"}, "connections": {kind: n},
        "warnings", "seconds"}.
    """
    t0 = time.time()
    try:
        page = cs_text(page, "page")
    except SchematicValueError as exc:
        return {"success": False, "error": str(exc)}

    def R(r):
        return r.get("results") if isinstance(r.get("results"), dict) else r

    read = R(schematic.live_read_page(page, include_pins=False, limit=5000,
                                      types=["BoxedDevice", "Function", "Terminal", "Cable", "Plug"],
                                      timeout_seconds=timeout_seconds))
    if not read.get("success"):
        return {"success": False, "page": page, "error": read.get("error") or "page read failed"}
    plan = plan_connector_recipe(read.get("placements") or [], port_pitch=float(port_pitch))
    out = {"success": True, "page": page, "dryRun": bool(dry_run),
           "planned": {k: len(plan[k]) for k in ("retag", "symbols", "terminals", "plugs", "labels")},
           "skipped": plan["skipped"], "sensorPins": len(plan["sensorPins"]),
           "cables": len(plan["cables"]), "warnings": plan["warnings"]}
    if verbose:
        out["plan"] = plan
    if dry_run:
        out["seconds"] = round(time.time() - t0, 1)
        return out

    steps = {}
    clock = [time.time()]

    class _Abort(Exception):
        pass

    def step(name, res, ok=None):
        res = res if isinstance(res, dict) else {}
        r = R(res) if isinstance(res.get("results"), dict) else res
        e = r.get("errors") or ([r.get("error")] if r.get("error") else [])
        steps[name] = {"ok": bool(r.get("success")) if ok is None else ok, "failed": r.get("failed", 0 if not e else len(e)),
                       "seconds": round(time.time() - clock[0], 1)}
        clock[0] = time.time()
        if e:
            steps[name]["errors"] = e[:8]
        if verbose:
            steps[name]["result"] = r
        if _dead(res) or _dead(r):
            out["aborted"] = name
            raise _Abort()
        return r

    try:
        _run_steps(locals(), step)
    except _Abort:
        out["steps"] = steps
        out["success"] = False
        out["error"] = ("EPLAN stopped answering during step %r: the rest of the recipe was not sent. "
                        "Check eplan_ping; once EPLAN answers again, run the recipe again - it skips what is done."
                        % out["aborted"])
        out["seconds"] = round(time.time() - t0, 1)
        return out
    return _finish(out, steps, plan, page, timeout_seconds, t0)


def _dead(r):
    """A result that says EPLAN did not answer (no result file, timeout) or
    another session held it past the lock timeout."""
    if not isinstance(r, dict):
        return False
    et = r.get("errorType") or ""
    if et in ("McpScriptNoResult", "EplanBusy", "McpOpsTimeout"):
        return True
    msg = str(r.get("error") or r.get("message") or "")
    return "no result file appeared" in msg or "did not answer" in msg


def _run_steps(env, step):
    def R(r):
        return r.get("results") if isinstance(r.get("results"), dict) else r
    page, plan = env["page"], env["plan"]
    allow_real_project, timeout_seconds = env["allow_real_project"], env["timeout_seconds"]
    reassign, labels, layers, steps, out = env["reassign"], env["labels"], env["layers"], env["steps"], env["out"]
    edit_ops = plan["retag"] + plan["symbols"] + plan["terminals"]
    if edit_ops:
        step("symbols", summarize_batch(run_ops(page, edit_ops, stop_on_error=False,
                                                allow_real_project=allow_real_project,
                                                timeout_seconds=timeout_seconds)))
    if plan["plugs"]:
        step("plugs", summarize_batch(run_ops(page, plan["plugs"], stop_on_error=False,
                                              allow_real_project=allow_real_project,
                                              timeout_seconds=timeout_seconds)))
    from .generate import generate_connections
    g = generate_connections(project_name=target.get() or None, page_name=page, rebuild_all=True)
    step("rebuild", g, ok=bool(g.get("success")))

    found = None
    if reassign and plan["cables"]:
        from .cables_live import live_assign_cable_connections
        a = R(live_assign_cable_connections(page, "all", reassign_from_part=True, crossing_texts=False,
                                            allow_real_project=allow_real_project,
                                            timeout_seconds=timeout_seconds))
        found = sum(int(c.get("conductors") or 0) for c in a.get("cables") or [])
        out["_found"] = found
        if any("no result file appeared" in str(c.get("error") or "") for c in a.get("cables") or []):
            a = dict(a); a["errorType"] = "McpScriptNoResult"
        step("reassign", a)
    if plan["cables"]:
        st = step("strayPoints", _remove_stray_points(page, allow_real_project, timeout_seconds))
        steps["strayPoints"]["removed"] = st.get("removed", 0)
        if st.get("removed"):
            generate_connections(project_name=target.get() or None, page_name=page)
    if labels and plan["labels"]:
        from .captions_live import live_set_property_texts
        step("labels", live_set_property_texts(page, items=plan["labels"], allow_real_project=allow_real_project,
                                               timeout_seconds=timeout_seconds))
    if layers:
        from .cables_live import live_set_connection_layers
        ly = R(live_set_connection_layers(page, allow_real_project=allow_real_project,
                                          timeout_seconds=timeout_seconds))
        st = step("layers", ly)
        steps["layers"]["changed"] = len(st.get("changed") or [])
        steps["layers"]["noPoint"] = len(st.get("noPoint") or [])


def _finish(out, steps, plan, page, timeout_seconds, t0):
    def R(r):
        return r.get("results") if isinstance(r.get("results"), dict) else r
    found = out.pop("_found", None)
    after = R(schematic.live_read_page(page, include_pins=False, limit=5000, types=["Plug"],
                                       timeout_seconds=timeout_seconds))
    conns = R(schematic.live_read_connections(page, summary=True, timeout_seconds=timeout_seconds))
    out["steps"] = steps
    out["plugs"] = {"expected": 2 * len(plan["sensorPins"]),
                    "found": len(after.get("placements") or []) if after.get("success") else None}
    out["conductors"] = {"expected": len(plan["sensorPins"]), "found": found}
    out["connections"] = conns.get("byKind") if conns.get("success") else None
    out["success"] = all(s.get("ok") for s in steps.values()) and (found is None or found >= len(plan["sensorPins"]))
    out["seconds"] = round(time.time() - t0, 1)
    return out
