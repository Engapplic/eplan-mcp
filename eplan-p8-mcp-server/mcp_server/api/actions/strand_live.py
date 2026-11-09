"""
Conductor bundles ("haz", strand) on a schematic page. WRITES.

A sensor lead drawn the way the Festo NEBU macro does it: the three wires
leave the connector side by side, each turns into one trunk line at a
joining row, the trunk runs to a splitting row where the wires fan out
again. EPLAN draws that with strand connectors - SPECIAL/COST (an angle,
StrandConnectorAngle), SPECIAL/TST (a T, StrandConnectorTNode) - on each
wire, and a strand interconnector - SPECIAL/TS (StrandInterconnector) - on
the trunk. They are SymbolReferences, not Functions: Function.Create
refuses them ("No se puede crear la función"); StrandConnector /
StrandInterConnector.Create(Page, SymbolVariant) + Location works.

Measured on the NEBU macro (P26009 =MOV+CAMPO/99, 2026-11-09), wires at
x = 24, 32, 40, trunk at x = 28:

    joining row (y = 136)      splitting row (y = 100)
    x=24  COST variant 0       COST variant 6     (left of the trunk, angle)
    x=28  TS   variant 4       TS   variant 2     (trunk)
    x=32  TST  variant 4       TST  variant 2     (right of the trunk, inner: T)
    x=40  COST variant 4       COST variant 2     (right of the trunk, outer: angle)

each strand connector carrying STRANDCONNECTOR_DESIGNATION (the colour)
with its text 19072 hidden.
"""

import uuid

from ._base import cs_escape
from .scripted import _execute_script
from . import live, schematic
from .schematic import _err, _guard_prelude
from .schematic_model import cs_bool, cs_text, SchematicValueError
from .terminals_live import _HELPERS_TERM


_STRAND_BODY = '''
            object page = FindPage(project, "PAGENAME");
            LockAny(page);
            string[][] items = new string[][] { ITEMS };
            List<Dictionary<string, object>> placed = new List<Dictionary<string, object>>();
            List<string> errors = new List<string>();
            foreach (string[] it in items)
            {
                // it = { kind (connector|inter), symbol, variant, x, y, designation }
                Dictionary<string, object> d = new Dictionary<string, object>();
                d["symbol"] = it[1]; d["variant"] = it[2]; d["x"] = it[3]; d["y"] = it[4]; d["designation"] = it[5];
                try
                {
                    Type t = FindType(it[0] == "inter" ? "Eplan.EplApi.DataModel.StrandInterConnector" : "Eplan.EplApi.DataModel.StrandConnector");
                    object o = Activator.CreateInstance(t);
                    object variant = MakeVariantT(project, "SPECIAL", it[1], int.Parse(it[2]));
                    MethodInfo create = RequireMethod(t, "Create", new string[] { "Page", "SymbolVariant" }, false);
                    Call(create, o, new object[] { page, variant });
                    LockHard(o);
                    PropertyInfo locPi = RequireWritable(o.GetType(), "Location");
                    double x = double.Parse(it[3], System.Globalization.CultureInfo.InvariantCulture), y = double.Parse(it[4], System.Globalization.CultureInfo.InvariantCulture);
                    locPi.SetValue(o, MakePoint(locPi.PropertyType, x, y), null);
                    if (it[0] != "inter" && it[5] != "")
                    {
                        object props = TryRead(o, "Properties", null);
                        SetProp(props, "STRANDCONNECTOR_DESIGNATION", it[5]);
                        if (HIDE)
                        {
                            try
                            {
                                PropertyInfo ul = GetWritable(o.GetType(), "UseLocalPropertyPlacements");
                                if (ul != null && !(bool)ul.GetValue(o, null)) ul.SetValue(o, true, null);
                                object pps = TryRead(o, "PropertyPlacements", null);
                                int hidden = 0;
                                if (pps != null) foreach (object pp in (IEnumerable)pps)
                                {
                                    if (pp == null) continue;
                                    object dp = TryRead(pp, "DisplayedProperty", null);
                                    object ai = dp == null ? null : TryRead(dp, "AsInt", null); int id = ai == null ? -1 : Convert.ToInt32(ai);
                                    if (id == 19072) { try { LockHard(pp); } catch { } GetWritable(pp.GetType(), "IsVisible").SetValue(pp, false, null); hidden++; }
                                }
                                d["hiddenTexts"] = hidden;
                            }
                            catch (Exception he) { d["hideError"] = Flatten(he); }
                        }
                    }
                    d["handle"] = Handle(o); d["clrType"] = o.GetType().Name; d["success"] = true;
                }
                catch (Exception e) { d["success"] = false; d["error"] = Flatten(e); errors.Add(it[1] + "/" + it[2] + " at " + it[3] + "," + it[4] + ": " + Flatten(e)); }
                placed.Add(d);
            }
            results["placed"] = placed; results["errors"] = errors; results["count"] = placed.Count;
            results["success"] = errors.Count == 0;
'''


def strand_layout(wires_x, y_join, y_split, trunk_x, designations):
    """The strand symbols for one bundle, as (kind, symbol, variant, x, y, designation) rows.

    Wires left of the trunk get the "left" variants (angle 0 / 6), wires
    right of it the "right" ones (4 / 2); on each side the outermost wire is
    an angle (COST), the inner ones a T (TST); the trunk an interconnector
    (TS, 4 / 2). Pure geometry, no EPLAN - unit-tested on the NEBU layout.
    """
    xs = sorted(float(x) for x in wires_x)
    if len(set(xs)) != len(xs) or len(xs) < 2:
        raise SchematicValueError("wires_x needs at least two distinct x values.")
    des = list(designations or [])
    if des and len(des) != len(xs):
        raise SchematicValueError("designations must have one entry per wire (%d)." % len(xs))
    by_x = dict(zip(xs, des)) if des else {}
    # designations follow the caller's wire order, not the sorted one
    if des:
        by_x = {float(x): d for x, d in zip([float(v) for v in wires_x], des)}
    left = [x for x in xs if x < trunk_x]
    right = [x for x in xs if x > trunk_x]
    if any(abs(x - trunk_x) < 1e-9 for x in xs):
        raise SchematicValueError("trunk_x must not coincide with a wire.")
    rows = []
    # The variants follow the geometry, not the names: at the UPPER row the
    # wires carry on upwards and the trunk leaves downwards (0 / 4 / 4), at
    # the LOWER row the other way round (6 / 2 / 2). EPLAN's y grows upwards.
    upper, lower = max(float(y_join), float(y_split)), min(float(y_join), float(y_split))
    if upper == lower:
        raise SchematicValueError("y_join and y_split must differ.")
    for row, (y, angle_l, angle_r, inter) in (("upper", (upper, 0, 4, 4)), ("lower", (lower, 6, 2, 2))):
        for x in left:
            sym = "COST" if x == left[0] else "TST"
            rows.append(("connector", sym, angle_l, x, y, by_x.get(x, "")))
        for x in right:
            sym = "COST" if x == right[-1] else "TST"
            rows.append(("connector", sym, angle_r, x, y, by_x.get(x, "")))
        rows.append(("inter", "TS", inter, float(trunk_x), y, ""))
    return rows


def live_place_strand(page: str, wires_x: list, y_join: float, y_split: float, trunk_x: float,
                      designations: list = None, hide_designation: bool = True,
                      allow_real_project: bool = False, timeout_seconds: float = 120.0) -> dict:
    """
    Draw the conductor bundle of a sensor lead: the wires at `wires_x`
    join into one trunk at `trunk_x` between the rows y_join and y_split,
    the way the Festo NEBU macro draws it. WRITES - scratch-only by default.

    Places a strand connector (SPECIAL/COST angle on the outermost wire of
    each side of the trunk, SPECIAL/TST T on the inner ones) at both rows
    of every wire and a strand interconnector (SPECIAL/TS) at both rows of
    the trunk, variants as measured on the macro. Each connector gets the
    wire's colour in STRANDCONNECTOR_DESIGNATION, its text (19072) hidden
    by default. The wires themselves are the autoconnect lines already on
    the page; nothing else is drawn.

    Args:
        page: Page name.
        wires_x: The x of each wire, e.g. [24, 32, 40].
        y_join: Row where the wires join the trunk (the connector side).
        y_split: Row where they fan out again (the sensor side). Which of
            the two is higher on the page does not matter: the upper row
            always gets the "wires go up, trunk goes down" variants.
        trunk_x: x of the trunk, between two wires (28 for 24/32/40).
        designations: One per wire, in wires_x order: ["BN", "BK", "BU"].
        hide_designation: Hide text 19072 on every connector (default).
        allow_real_project: Must be True to write outside the scratch root.
        timeout_seconds: Default 120.

    Returns:
        {"success", "count", "placed": [{symbol, variant, x, y, designation,
        handle, clrType, hiddenTexts?, success, error?}], "errors"}.
    """
    try:
        page_cs = cs_escape(cs_text(page, "page"))
        if not isinstance(wires_x, (list, tuple)):
            raise SchematicValueError("wires_x must be a list of x values.")
        rows = strand_layout(wires_x, float(y_join), float(y_split), float(trunk_x), designations)
        rows_cs = [(k, sym, str(int(v)), repr(float(x)), repr(float(y)), cs_escape(cs_text(str(d), "designation")) if d else "")
                   for k, sym, v, x, y, d in rows]
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc if isinstance(exc, SchematicValueError) else SchematicValueError(str(exc)))
    table = ", ".join('new string[] { "%s", "%s", "%s", "%s", "%s", "%s" }' % r for r in rows_cs)
    body = (_guard_prelude(allow_real_project)
            + _STRAND_BODY.replace("PAGENAME", page_cs).replace("ITEMS", table).replace("HIDE", cs_bool(hide_designation)))
    from .cables_live import _HELPERS_CABLE
    return _execute_script(
        live._script("LiveStrand_" + uuid.uuid4().hex[:6], body,
                     extra_helpers=schematic._HELPERS_SCHEMATIC + _HELPERS_TERM + _HELPERS_CABLE),
        timeout=timeout_seconds,
    )
