# LLM Operating Guide — EPLAN AI Automation Toolkit

This file tells you (the LLM) what this toolkit lets you **do** and **configure**.
It assumes you are connected through one or more of the MCP servers described here.

The local `eplan` action server is maintained in this repository. The three
Cloudflare documentation RAG servers are maintained separately in
[eplan-cloudflare-rags](https://github.com/Canary-ECAD/eplan-cloudflare-rags):
[P8 2026](https://github.com/Canary-ECAD/eplan-cloudflare-rags/tree/main/cloudflare-rag-eplan-p8),
[P8 2027](https://github.com/Canary-ECAD/eplan-cloudflare-rags/tree/main/cloudflare-rag-eplan-2027),
and [EEC Pro 2026](https://github.com/Canary-ECAD/eplan-cloudflare-rags/tree/main/cloudflare-rag-eecpro).
The [eplan-scripting skill](https://github.com/Canary-ECAD/eplan-scripting-skill)
also has its own repository.

Human-readable guides: [English](README.md) · [Español](README.es.md) ·
[한국어](README.ko.md) · [Deutsch](README.de.md) ·
[中文](README.zh-CN.md) · [Русский](README.ru.md).

---

## 1. What you are connected to

There are up to **four MCP servers**, each a different capability:

| MCP server | Kind | What it lets you do |
|------------|------|---------------------|
| `eplan` (local) | Action server | **Control a running EPLAN Electric P8 instance** — open/close projects, export, import, reports, checks, renumber, parts DB, settings, run C# scripts, etc. |
| `eplan-rag` (remote) | Knowledge | **Look up the EPLAN P8 API** (2026 docs, actions/classes/properties/parameters) via **semantic search** (Vectorize + bge-base). |
| `eplan-wiki-2027` (remote) | Knowledge | **Look up the EPLAN P8 API** (2027 docs) via **keyword/full-text search** (SQLite FTS5 + bm25). Prefer this over `eplan-rag` when you already know or can guess the exact class/method/property name — measured head-to-head, FTS5 wins that case and semantic search wins only when the query shares no vocabulary with the docs at all. |
| `eecpro-rag` (remote) | Knowledge | **Look up the EPLAN EEC Pro 2026** documentation via semantic search. |

If `eplan-rag`/`eplan-wiki-2027` aren't connected, you can still query them over REST:
`POST https://rag2026.covaga.xyz/search` (semantic, 2026) or
`POST https://rag2027.covaga.xyz/search` (keyword, 2027), body `{"query": "...", "topK": 5}`.
Use one of these whenever you are unsure of an exact action name or parameter —
**do not guess EPLAN action parameters**.

---

## 2. The local `eplan` action server

It exposes **257 tools** (full tool-by-tool reference: [covaga.dev/docs/tools](https://covaga.dev/docs/tools/project-and-workspace-management/)):

- **8 connection/utility tools**: `eplan_versions`, `eplan_servers`,
  `eplan_connect`, `eplan_status`, `eplan_ping`, `eplan_test`,
  `eplan_disconnect`, `eplan_list_extensions`.
- **220 EPLAN action tools** → `eplan_<action>` (e.g. `eplan_open_project`).
  Includes `eplan_live_set_target_project` (pin every later call to one open
  project by its .elk path - reflection scripts then pick it from
  `ProjectManager.OpenProjects` and action wrappers fill an omitted
  `PROJECTNAME` with it, so the user clicking another project in EPLAN cannot
  redirect a write; set it right after opening the project) and 10 batched
  page-operation tools built on ONE C# interpreter that runs a list of ops per
  script and is registered as an EPLAN action for the session so later
  batches skip the compile; every tool's JSON is compact, not pretty-printed, edit ops echo only handle / name / location / the values written (`brief`, default) and `eplan_live_remove_placement` is brief by default: `eplan_live_batch` (ops: place, rect, macro, structurebox - a structure box with two corners whose = / + the devices inside take -, update, move, resize - new corners for an existing black box / structure box / PLC box -, remove, dimension, line, text - a free page text, bilingual with "es_ES@..;en_US@..;" -, image - a picture in a rectangle, from a file, a part's ARTICLE_PICTUREFILE or a device's part -, find, read) (answers with a SUMMARY by default - counts, errors, handles created -
`verbose=True` for every op's echo; `operations_file` takes the ops from a
JSON file instead of the conversation) (update also takes `symbol`: {"library","symbol","variant"} swaps the placed symbol in place keeping DT, designation, PLC data and connections - an EX600's PLCCPPO terminals to PLCCPPFEM, a DCP pin to DCPM -, `parts` - the whole part list the device ends with, `part` only ADDS -, `removePart`, and a `tag` on a black box drags its pins along; the DCP pins of a black box keep only ":n" as their name - the box supplies the DT - in macro inserts, structure boxes, copy_region and retags, since a pin with a code of its own is shown twice, "-B17-B17:1"; `pinFollow` repairs one pin) (place / rect / macro / update /
  move / remove / dimension / line / find / read, objects addressed by handle,
  by device tag, or by `$label` of an earlier op), `eplan_live_find_placements`
  (by tag - handles do not survive an EPLAN restart, tags do),
  `eplan_live_place_device` (place + tag + part + properties + function
  definition in one go; terminals and PLC points fall back to the 2-arg
  Create automatically), `eplan_live_insert_macro` (a window macro .ema or a symbol macro .ems -
  what a part's ARTICLE_GROUPSYMBOLMACRO names - by insertion point or
  top-left corner (the inserted objects are shifted, never re-inserted),
  EVERY device it brought tagged - a PLC box and its terminals share the
  tag, the part goes on the main function),
  `eplan_live_create_device_from_part` (a device FROM ITS PART, like dragging it from the
  parts navigator: DeviceService.CreateDevice places the part's macro with the box as MAIN
  function carrying the part, renames the automatic -A1 to `tag` and lists the macro's
  variants; a box from insert_macro is secondary and refuses a part, S063074. Next
  connectors: insert_macro with the same tag and no part),
  `eplan_live_place_structure_box` (=ST1+CAMPO box by two corners; the
  devices inside are renamed to its structure; label visible, inside the
  box by default via `label_scheme` - "Arriba a la izquierda, dentro 0°",
  matched by whole words, a miss lists the project's schemes),
  `eplan_live_place_image`
  (the photo of a sensor or valve in a rectangle on the page: a file -
  "$(MD_IMG)\\..." expands -, a part number (its picture from the parts
  database) or a device on the page; aspect kept, copied into the project),
  `eplan_live_resize_box` (new corners for a box that already exists; the
  Location follows to the top-left so its texts move, pins placed apart
  stay), `eplan_live_place_panel_item`
  (the part's 2D macro, or EPLAN's rectangle fallback), `eplan_live_place_plc_point`
  (DI/DO/PS/CPS with the right FunctionDefinition), `eplan_live_place_terminal`,
  `eplan_live_update_placement` (tag/part/properties on an existing object; a
  duplicate tag is refused unless allowed), `eplan_live_render_page` (export
  the page to PNG to look at it) and `eplan_live_check_page` (page check +
  itemized messages + overlapping outlines/texts + duplicate wire labels, in
  one call).
  Includes 2 Data Portal tools that use EPLAN's own Data Portal sign-in:
  `eplan_dataportal_search_count` (read-only) and
  `eplan_dataportal_add_to_download_list`, which only *queues* parts - the
  download is a dialog in EPLAN, so its result carries a `userAction` the model
  must relay to the user before waiting on the import.
  Includes 5 discovery tools (`eplan_settings_list_children`,
  `eplan_list_schemes`, `eplan_list_report_templates`, `eplan_list_layers`,
  `eplan_list_enums`) that enumerate real EPLAN catalogs instead of guessing,
  6 live-DataModel tools (`eplan_live_query_functions`,
  `eplan_live_query_pages`, `eplan_live_plc_io_list` (the PLC I/O
  assignment list as JSON: identifier split into =/+/- parts, address,
  symbolic address, data type, function text per language, module, page,
  plus the PLC boxes with CPU/rack/slot/device name),
  `eplan_live_set_function_text`,
  `eplan_live_set_connection_designations`,
  `eplan_live_read_check_messages`) that read/edit the open project's
  object model via runtime reflection (see §4 below) - the last of these reaches
  a different namespace still (`Eplan.EplApi.EServices.PrjMessagesCollection`,
  the itemized "Message management" results a check run produces, which
  `eplan_get_system_messages` cannot see), 2 API-introspection tools
  (`eplan_api_types`, `eplan_api_describe`) that ask the loaded object model
  what it declares - namespaces, types, member signatures, property
  read/write access and enum numeric values - using the same reflection but
  no project and no LockingStep, since they read metadata rather than data.
  They exist because the CS0234 block is compile-time only: all 26
  `Eplan.EplApi.*` namespaces / 606 public types are reachable at runtime, so
  writing against this API means looking members up constantly, and the
  alternative was a hand-written throwaway script per question. Deliberately
  2 tools and not a wrapper per class - same call as the action catalog
  below, for the same reason. 11 schematic-authoring
  tools on that same reflection scaffold (`eplan_live_symbol_catalog`,
  `eplan_live_create_page` (with `description`: the page description
  11011 every page should carry - "Digital inputs SMC module" -, also set
  later with `eplan_live_set_page_description`; `eplan_live_query_pages`
  lists it), `eplan_live_place_symbol`,
  `eplan_live_connect_pins`, `eplan_live_read_page`,
  `eplan_live_remove_placement`) that CREATE a schematic rather than only
  reading one - every write returns the page read-back as proof and an undo
  handle, and writes refuse a project outside the scratch root unless
  `allow_real_project=True`, plus `eplan_live_verify_page` (check a page against
  a description written in live_read_page's own schema, so the read format
  doubles as the specification format) and `eplan_live_set_device_tag` (a placed
  function is anonymous until tagged; a duplicate tag is refused by default
  because it would MERGE devices) and `eplan_live_read_connections` (the LOGICAL
  connections - what is actually wired to what, as opposed to where a line was
  drawn; read-only, and it reports when connections look ungenerated rather than
  letting an empty list read as "nothing is wired") and
  `eplan_live_connect_pins_routed` (a drawn line through a right-angled corner,
  for the diagonal case where devices share neither axis - NOT the way to wire a
  straight run, which needs no object at all) and the connection-symbol tools
  (`eplan_live_routing_catalog`, `eplan_live_place_connection_symbol`,
  `eplan_live_place_corner`, `eplan_live_place_tnode`,
  `eplan_live_place_connected` - place a device already lined up to autoconnect
  with a pin on the page, so the caller never computes a millimetre) - EPLAN wires two facing
  pins on a shared axis with an autoconnecting line and no object, so what
  actually has to be PLACED is the corners and branches, and which symbol that
  is has to be discovered rather than assumed: one measured project carries 24
  connection symbols, including two different `TNodeUp` symbols and five
  same-direction `TLRU` variants whose pins sit in different places, so these
  tools refuse to guess between rivals and report the geometry instead),
  application lifecycle
  control (`eplan_app_launch`, `eplan_app_shutdown`, `eplan_app_restart` —
  full exit/relaunch/reconnect/reopen cycles for unattended add-in
  deploy-test loops), scratch project fixtures
  (`eplan_scratch_project_create` / `_discard` / `_list` — disposable clones
  of a template project; deletion is confined to the scratch root), and
  `eplan_get_system_messages` (read EPLAN's system message tree — the same
  errors/warnings the user sees in the GUI's system messages dialog).
- **4 action-catalog tools** reaching the ~1,050 further EPLAN actions that
  exist only as GUI buttons (mined from the install's `MFTools.xml`, not
  documented, not individually wrapped — publishing one tool per action would
  have tripled the tool count): `eplan_action_catalog(search, category,
  documented_only, wrapped, available_only, limit)` to search the ~1,150-action
  registry offline, `eplan_action_describe(name)` for a registry entry + live
  `FindAction` probe, `eplan_action_run(name, params, dry_run,
  allow_unknown_params)` for validated dispatch of *any* action (rejects
  unknown names/params, with an escape hatch since many registry params are
  observed rather than documented — supersedes `execute_raw_action` for
  actions already in the registry), and `eplan_ribbon_catalog(tab, search)` to
  browse the live GUI ribbon tree and resolve a button to the action it runs.
  `available_only=True` on `action_catalog` restricts to actions the live
  probe found registered on this installation — that is necessary, not
  sufficient: `FindAction` resolving an action means its module is *loaded*,
  not that it is *licensed* to run (module licensing is enforced at
  execution time). Confirm by actually running the action.
- **4 Asset Administration Shell tools** → `aas_<action>`
  (`aas_export_part`, `aas_export_project`, `aas_inspect_package`,
  `aas_import_parts`) for AAS/AASX digital-twin export and import.

The EPLAN version is auto-detected (newest installed). If the user wants a
specific version, call `eplan_versions` to list what is installed, then
`eplan_connect(version="2026")`. Decide the version BEFORE the first connect —
once one version's DLLs are loaded, switching requires restarting the server.

Every action runs inside a C# script in EPLAN's process under `QuietMode`
(no dialogs). It is silent, safe for unattended/batch use, and returns values
EPLAN wrote back to the calling context (e.g. `PROJECT`, `PAGES`).

Each tool already carries its own description and parameter schema (generated
from the Python docstring + type hints). **Read the tool's own description before
calling it** — this guide is the map, the tool schemas are the territory.

### Result shape

Tools return JSON. Actions typically return:

```json
{ "success": true, "parameters": { "PROJECT": "C:\\...\\Proj.elk" } }
```

`success: false` with a `message`/`error` means the EPLAN action itself failed —
read the message; it usually points at a bad parameter or a missing precondition
(e.g. no project open).

`success: true` from a directly-executed utility (`ExecuteScript`) only means
EPLAN accepted the call. Generated scripts are one-shot `[Start]`-only classes
run via `ExecuteScript` alone — they are deliberately **not** passed through
`RegisterScript`/`UnregisterScript` (that pair is for installing a script's
persistent `[DeclareAction]`/`[DeclareEventHandler]`/`[DeclareMenu]` hooks,
which these scripts don't have; registering them anyway just produced a
spurious "script does not contain attributes for loading" warning in
EPLAN's own UI and two wasted remote-API round-trips per call).

---

## 3. Standard workflow

1. **Check / connect first.** Call `eplan_status` (or `eplan_servers` →
   `eplan_connect`). Almost every action needs an open connection. Port and
   EPLAN version are auto-detected; pass `version` only if the user asks for a
   specific one. If `eplan_servers` returns `[]` the connection usually still
   works — that empty list is a known limitation (especially right after EPLAN
   starts), not a failure: `eplan_connect` falls back to the TCP ports
   EPLAN.exe actually listens on (read from Windows' TCP table, so it works in
   any Windows language), then the default 49152. To
   reach EPLAN on another machine pass `host` (port required then).
2. **Pick the project context.** Most actions take an optional `project_name`. If
   omitted, EPLAN uses the **currently selected/open** project. Use
   `eplan_get_current_project` to confirm what that is.
3. **Call `eplan_<action>`** with the parameters from the tool schema.
4. **Verify unknowns via the RAG** before constructing raw actions or custom
   scripts.
5. **Report results** from the returned JSON honestly (including `success: false`).

---

## 4. What you can DO (capability map)

All of these exist as `eplan_*` tools:

- **Projects:** open, close, get current, compress, synchronize, upgrade, set
  language, switch type, project management tasks.
- **Backup / restore:** projects and master data.
- **Export:** PDF (project/pages), DXF/DWG (project/pages, by scheme), graphics
  (PNG/TIF/…), PXF/EPJ, 3D.
- **Import:** PXF projects, DXF/DWG into pages or as macros, PDF comments, 3D.
- **Print:** project or pages.
- **Check / verify:** project, pages, parts (with verification schemes).
- **Generate:** connections, cables.
- **Reports / evaluations:** `eplan_live_report_types` (the DocumentType
  names + the report pages the project already has) and
  `eplan_live_create_report` (add a cover sheet / parts list / terminal
  diagram / panel legend ... with a given form and generate its pages; the
  form must already be in the project master data, else S029153 with a
  hint), update reports, model views, copper unfolds,
  drilling views.
- **Search:** devices, texts, all properties, page data, project data.
- **Navigation / editing:** open page, go to device, open layout space, close
  pages, get selected pages, page/macro preview, navigate to EEC.
- **Renumber:** devices, pages, cables, terminals, connections.
- **Translate:** translate project, export missing translations, remove language.
- **Device lists, labels, graphical layers, macros.**
- **Settings & properties:** import/export settings, set settings, get/set
  project / page / object properties, user properties.
- **Parts:** export/import parts lists, part selection, data source, full parts
  management API export/import.
- **PLC:** bus data export/import via converters.
- **Workspace:** open/save/clean (needs the EPLAN GUI/mainframe).
- **Data exchange:** connections/functions/pages export for external editing,
  data-configuration import/export, potential/pipeline definitions, subprojects,
  master data operations.
- **Cabinet / 3D:** cabinet weight, segment filling, topology, pre-planning data,
  segment templates.
- **Production:** NC data, production wiring.
- **Ribbon & add-ons:** export/import ribbon bar, load API module, register/
  unregister add-on, and `execute_raw_action` for any action not wrapped.
- **Scripted advanced APIs (run as C#):** direct **parts database** queries
  (`parts_db_*`; `eplan_parts_db_get_part` also returns every filled record
  property - ARTICLE_NOTE with the manufacturer's technical text, voltages,
  pressure, PLC station type - and the function templates), **typed settings** get/set
  (`settings_get/set_string|bool|int|double`), **PathMap** variable substitution,
  and `execute_custom_script` to run arbitrary C# inside EPLAN.
- **Live DataModel (read/edit the open project via reflection):**
  `eplan_live_query_functions`, `eplan_live_query_pages` (read, with substring
  filter + result limit), `eplan_live_plc_io_list` (read: every PLC
  connection point with address, symbolic address, data type, channel,
  function text per language, page and module, grouped by =installation so
  repeated stations map to an array index; use it before `eplan_plc_export`
  when the PLC programmer only needs the list, not a TIA import file),
  `eplan_live_panel_captions` / `eplan_live_set_panel_captions` (the
  property texts next to a panel item - tag, part, type, legend index -
  with their boxes in page mm; hide/show/move them and set the legend
  position, always on a LOCAL property arrangement so other items are not
  dragged along),
  `eplan_live_property_texts` / `eplan_live_set_property_texts` (the same
  for ANY placement on any page - a schematic black box, a terminal, a PLC
  box: pick a property arrangement scheme by name, hide/show, move to a
  top-left corner, rotate in degrees, set height and justification; the
  dense-I/O recipe is tag above the box, function text vertical, cable and Y
  texts hidden),  - `eplan_live_set_property_texts` also takes `items=[{"ref", "positions",
  "hide", ...}, ...]`: every placement of a dense page in ONE script instead
  of one call each (612 calls on four pages, measured); the result is
  compact by default (`brief`: only the texts touched, no "before" list);
  a position may pin another corner of the text box ([x, y, "BottomLeft"]
  or `anchors`) - a 90-deg text grows from its foot,

  `eplan_live_copy_pages_to_structure` (repeated stations: copy drawn
  pages to =ST2, =ST3 ... and retag every device on the copies, keeping the
  shared ones such as the CPU; wire numbers and PLC addresses are copied
  as-is and must be renumbered afterwards),
  `eplan_live_terminal_strips` (every strip with its terminal count and
  whether it has a terminal strip definition) and
  `eplan_live_set_terminal_strip_definition` (create the missing
  definition - unplaced, or placed as SPECIAL/TDEF on a page - with text
  and part; PLC connection points are not strip terminals),
  `eplan_live_copy_region` (copy everything inside a rectangle of a page
  to another place or page as a temporary window macro, renamed through
  {old: new} maps for tags and texts - one validated sensor column becomes
  the next in one call, cables re-assigned with part colours by pin; the
  target page's =/+ applies on insertion, structure boxes are renamed too,
  page properties are not copied; compact result by default - `brief`),
  `eplan_live_cables` (cables with their conductors: wire number, colour,
  cross-section, source, target), `eplan_live_place_cable_definition`
  (draw the cable definition line across wires, name it with the page's
  structure, then assign the crossed connections and regenerate; with a
  part the conductors take the part's colours and cross-sections, the
  conductors locked first; `scheme` / `show` arrange the line's own
  property texts; keep the line clear of the wire-number symbols; the
  connection updates, the conductor read and the reassign run inside the
  assignment script (CommandLineInterpreter), ~6 scripts per cable instead
  of ~20; wires
  drawn in this session get one connection update first; the crossing
  points a DELETED line left on the wires are removed first, since a wire
  that still carries one never links to a new line - `orphanPoints`; a
  project part copy without master data hands out no colours, so it is
  reloaded from the parts database and reassigned - `partReloaded`; the
  cable's own conductor count / cross-section come from the part when
  empty - `cableFieldsFromPart`; the shown tag omits the page's own =/+
  like the GUI: "=ST2+CAMPO-W17" on =MOV+CAMPO shows "=ST2-W17"),
  `eplan_live_decorate_cable_crossings` (the zero-size "=+" connection
  definition points EPLAN leaves on the line at each crossed wire are
  named after the cable, given the conductor's colour / cross-section and
  show them rotated 90 deg at the crossing like a manufacturer macro - or
  horizontal, docked, colour only, with `style` / `crossing_style`; the
  second point "Asignar conexiones" leaves anywhere along a crossed wire
  (even 60 mm off the line, under the device pins) is hidden, removed or
  kept with `extra_points`; run by default from the two tools
  above, `crossing_texts=False` skips it),
  `eplan_live_set_conductor_colours` (colour per PIN, not per template
  order: "Asignar conexiones" hands a sensor lead's BN/BK/BU out in
  template order, so pin 3 got BK; the M8/M12 sensor map 1 BN, 2 WH, 3 BU,
  4 BK, 5 GY or an explicit {pin: colour}; `pin_colours="auto"` on the two
  tools above applies it when the part gave sensor-lead colours and
  returns at once with `skipped` otherwise; a DCP
  black-box pin is read from the DCP function's name, not the symbol pin) and
  `eplan_live_assign_cable_connections` (re-run the geometric assignment
  for a line placed by hand or moved: EPLAN does not assign conductors to
  an API-placed line on its own; a connection EPLAN linked to the line
  without writing the cable into it is assigned too, and pre-existing
  connections are locked with retries before the write; a source pin that
  feeds two conductors (a stale logical connection after rewiring) gets
  one page rebuild of the connections - `rebuiltFor`; a conductor
  whose connection is no longer placed (stale after rewiring through a new
  device) is listed in `unplacedConductors` and removed with
  `drop_unplaced=True`; when three
  updates link nothing the line is stale: its orphan points are removed
  and the line is cloned through a one-object window macro on the same
  wires, full name / part / text arrangement kept - `replacedLine`, the
  old handle is gone), `eplan_live_assign_cable_connections` / `eplan_live_place_cable_definition` answer briefly by default (conductors as count + colours, what was repaired, errors; `brief=False` for the detail) and `eplan_parts_db_get_part` keeps only es_ES/en_US texts and drops document links unless `brief=False`, `eplan_live_apply_connector_recipe` (a sensor page reworked to plugged
  hoses in ONE call - retag of the boxes, symbols in place, XBD2/XSD2
  connectors tagged with the cable, rebuild, every cable reassigned,
  the Y's designation texts, connection layers - answering with a
  summary: planned / skipped / expected vs found / warnings; `dry_run`
  only plans; running it twice changes nothing),
`eplan_live_set_connection_layers` (uniform colours
  of the autoconnect lines of a page: the colour is the layer's pen, cable
  conductors on EPLAN311 - red -, plain wires on EPLAN510 - blue -, written
  on the connection definition points; a wire without a point is only
  listed, a connection update resets it), `eplan_live_clean_cable_points` (remove the points
  a deleted or moved cable line left on the wires inside a rectangle -
  they keep a wire in Cable kind so no new line links to it),
  `eplan_live_place_strand` (the conductor bundle of a sensor lead the way
  the Festo NEBU macro draws it: angle / T strand connectors on each wire
  at the joining and splitting rows, an interconnector on the trunk,
  colour designations hidden),
  `eplan_live_project_templates` (the .zw9 basic projects / .ept templates
  under $(MD_TEMPLATES)) and `eplan_live_create_project` (new project from
  one of them via ProjectManager.CreateProject, left open and set as the
  target; the path must be under the scratch root unless
  allow_real_project),
  `eplan_live_set_function_text` (write `FUNC_TEXT`,
  defaults to one function at a time, returns the previous value),
  `eplan_live_set_connection_designations` (write the indexed
  `FUNC_CONNECTIONDESIGNATION` property, re-reads after writing to confirm),
  `eplan_live_read_check_messages` (page through the itemized "Message
  management" results of the last check run — `Eplan.EplApi.EServices.
  PrjMessagesCollection` — by 1-based index, matching the order EPLAN's own
  dialog lists them in; `eplan_get_system_messages` only ever sees the two
  summary lines a check run appends, never the individual entries).
  These reach `Eplan.EplApi.DataModel`/`HEServices`/`EServices` types via
  `AppDomain.CurrentDomain.GetAssemblies()` + `Assembly.Load` fallback instead
  of a static `using`, because that `using` doesn't compile in EPLAN's script
  engine (CS0234) and, separately, the managed assembly names changed
  (`Eplan.EplApi.DataModelu` → `...DataModelNetu`) starting with EPLAN 2025/2027
  — a hardcoded old name throws `BadImageFormatException` on newer installs.

### Escape hatches

- `eplan_execute_raw_action("ActionName /PARAM:value ...")` — run any EPLAN
  action string directly (still wrapped in QuietMode). Use after confirming the
  syntax with the RAG.
- `eplan_execute_custom_script(<C# code>, timeout_seconds=30.0)` — run a full
  C# script with access to `Eplan.EplApi.*`. Write results to
  `{{RESULT_PATH}}` as JSON. Raise `timeout_seconds` for scripts that walk
  large collections (e.g. reflection over every function/page in a big
  project); the default is tuned for small scripts, not bulk enumeration.
  **If the call times out, don't assume the script is slow or the collection
  is large** — a C# compile error (e.g. the `using Eplan.EplApi.DataModel;`
  trap above) means the script never ran and never wrote a result, which is
  indistinguishable from a hang on this end. Call
  `eplan_get_system_messages(min_level="Message")` first and look for a
  `CS####`-prefixed entry naming the generated `.cs` file before raising the
  timeout or suspecting RAM/project size (confirmed live: identical timeout
  on a 400-item and a 4-item project, same root cause both times).

---

## 5. What you can CONFIGURE

- **Target EPLAN version:** auto-detected (newest installed). Override per
  session with `eplan_connect(version="2026")`; list options with
  `eplan_versions`. Non-standard install path: set the `EPLAN_PLATFORM_ROOT`
  environment variable. Switching versions after DLLs are loaded requires a
  server restart.
- **Add a new action / tool:** implement a function in
  `api/actions/<module>.py`, export it in that package's
  `__init__.py` `__all__`, restart. It auto-registers as `eplan_<name>`. The
  docstring + type hints become the tool description and schema you will see.
- **EPLAN settings at runtime:** via `eplan_set_setting` /
  `eplan_set_project_setting` (action params `set`/`value`/`index`) or the
  typed `eplan_settings_set_*` scripted tools.
- **The MCP registration itself:** `claude mcp add eplan -- python .../server.py`.

---

## 6. Caveats & gotchas

- **Connect before acting.** Unconnected calls return a "Not connected" error.
- **`project_name` is optional** — omitting it uses the selected project. Pass the
  full `.elk` path to be explicit. Windows paths must be escaped (`\\`) or use `/`.
- **GUI-only actions** behave poorly headless/under QuietMode: `redraw_ged`
  (returns FALSE in QuietMode) and the `workspace` actions (need a mainframe).
- **Property actions on project/page** (`get/set_project_property`,
  `get/set_page_property`) act on the **current project / selected page(s)** and
  use `PropertyId`/`PropertyIndex`/`PropertyValue` — they do not take a project
  or page name.
- **`selectionset`** valid `TYPE` values are `PROJECT`, `PROJECTS`, `PAGES`,
  `LAYOUTSPACES` only.
- **Don't invent parameters.** When unsure, query the RAG (`rag2026.covaga.xyz`)
  for the authoritative action page.
- **Custom C# scripts can't `using Eplan.EplApi.DataModel;`** — that statement
  doesn't compile in EPLAN's script engine (CS0234). Reach DataModel/HEServices
  types via reflection instead (see the `live_*` tools in §4), and don't
  hardcode the managed assembly name — it's `...Netu`-suffixed on EPLAN
  2025/2027, not the pre-2025 name.

---

## 7. Safety — confirm before destructive actions

Treat these as outward/irreversible and **confirm with the user first** unless
explicitly authorized: deleting pages or device/parts lists, closing a project
with unsaved changes, renumbering (devices/pages/cables/terminals/connections),
restore/backup overwrites, settings changes, `set_*_property`, raw actions, and
custom C# scripts. Read-only actions (status, search, get current project,
exports to new files, parts DB queries) are safe to run as needed.
