# -*- coding: utf-8 -*-
"""Unite everything cut by the active section into one filled region.

How it works (slice-first):
  1. Collect elements visible in the view whose bounding box crosses the cut
     plane, and ask which categories to fill (the choice is remembered).
  2. Trim each element's solids to a thin slab starting at the cut plane.
     This throws away almost all of the geometry, so the union is fast.
  3. Union the slabs. A piece that refuses is retried at other slab depths;
     anything still refusing is filled separately.
  4. Take the faces lying on the cut plane, clean out segments shorter than
     Revit allows, and draw them as filled regions tagged in Comments.
  5. On the next run in the same view, tagged regions are deleted first.

Shift+Click to choose the filled region type.
"""
from pyrevit import revit, DB, forms, script
from System.Collections.Generic import List
from poche_utils import pick_region_type

# ----------------------------------------------------------------- settings
TAG = "Unite Section"          # written to Comments; marks regions to replace

# Categories offered in the picker (only those actually cut in the view are
# shown). The first time, the ones marked True are pre-checked; after that the
# picker remembers your last choice.
CANDIDATES = [
    ("OST_Walls", True),
    ("OST_Floors", True),
    ("OST_Roofs", True),
    ("OST_Windows", True),
    ("OST_Doors", True),
    ("OST_StructuralColumns", True),
    ("OST_StructuralFraming", True),
    ("OST_StructuralFoundation", True),
    ("OST_Ceilings", True),
    ("OST_Stairs", True),
    ("OST_Columns", False),
    ("OST_StairsRailing", False),
    ("OST_CurtainWallPanels", False),
    ("OST_CurtainWallMullions", False),
    ("OST_Casework", False),
    ("OST_Furniture", False),
    ("OST_SpecialityEquipment", False),
    ("OST_GenericModel", False),
    ("OST_Toposolid", False),
]

SKIP_GLASS = False             # True = leave solids with a glass/glazing material unfilled
REJECT_LINE_STYLE = "Claude"  # line style for rejected-outline detail lines
SLAB = 0.25                    # ft; how much geometry behind the cut plane to keep
# --------------------------------------------------------------------------

doc = revit.doc
view = doc.ActiveView
output = script.get_output()

if view.ViewType not in (DB.ViewType.Section, DB.ViewType.Elevation, DB.ViewType.Detail):
    forms.alert("Open a section, elevation or detail view first.", exitscript=True)

type_id = pick_region_type(doc, script.get_config())
if type_id is None:
    script.exit()

o = view.Origin
n = view.ViewDirection.Normalize()
# ViewDirection points toward the viewer; the visible part of the model is
# behind the cut plane (the -n side), so the slab is taken on that side.
plane_cut = DB.Plane.CreateByNormalAndOrigin(n.Negate(), o)
plane_back = DB.Plane.CreateByNormalAndOrigin(n, o.Subtract(n.Multiply(SLAB)))
short_tol = doc.Application.ShortCurveTolerance * 1.5


# ------------------------------------------------------------- collect
def crosses_plane(el):
    bb = el.get_BoundingBox(None)
    if bb is None:
        return False
    sides = set()
    for x in (bb.Min.X, bb.Max.X):
        for y in (bb.Min.Y, bb.Max.Y):
            for z in (bb.Min.Z, bb.Max.Z):
                sides.add(DB.XYZ(x, y, z).Subtract(o).DotProduct(n) > 0)
    return len(sides) == 2


cfg = script.get_config()

# Resolve category names (skipping any this Revit version doesn't have)
bics = []
for name, default_on in CANDIDATES:
    bic = getattr(DB.BuiltInCategory, name, None)
    if bic is not None:
        bics.append((bic, default_on))

cat_filter = DB.ElementMulticategoryFilter(List[DB.BuiltInCategory]([b for b, _ in bics]))
by_cat = {}
for el in (DB.FilteredElementCollector(doc, view.Id)
           .WherePasses(cat_filter)
           .WhereElementIsNotElementType()):
    if el.Category is not None and crosses_plane(el):
        by_cat.setdefault(el.Category.Name, []).append(el)

if not by_cat:
    forms.alert("Nothing in this view is cut by the section plane.", exitscript=True)

try:
    saved = cfg.get_option("categories", None)
except Exception:
    saved = None
saved = set(saved.split("|")) if saved else None

default_names = set()
for bic, default_on in bics:
    if default_on:
        try:
            default_names.add(doc.Settings.Categories.get_Item(bic).Name)
        except Exception:
            pass


class CatItem(forms.TemplateListItem):
    @property
    def name(self):
        return "{}  ({})".format(self.item, len(by_cat[self.item]))


items = []
for cat_name in sorted(by_cat.keys()):
    on = (cat_name in saved) if saved is not None else (cat_name in default_names)
    items.append(CatItem(cat_name, checked=on))

chosen = forms.SelectFromList.show(
    items,
    title="Unite Section: categories to fill",
    button_name="Fill",
    multiselect=True,
    width=420,
    height=520,
)
if not chosen:
    script.exit()

# Remember the choice (merging with categories not shown this time)
remember = set(chosen)
if saved is not None:
    remember |= set(c for c in saved if c not in by_cat)
cfg.categories = "|".join(sorted(remember))
script.save_config()

elements = [el for c in chosen for el in by_cat[c]]


# ------------------------------------------------------------- geometry
def iter_solids(geom):
    if geom is None:
        return
    for g in geom:
        if isinstance(g, DB.Solid):
            if g.Volume > 1e-9:
                yield g
        elif isinstance(g, DB.GeometryInstance):
            for s in iter_solids(g.GetInstanceGeometry()):
                yield s


def is_glass(solid):
    for f in solid.Faces:
        mid = f.MaterialElementId
        if mid and mid != DB.ElementId.InvalidElementId:
            mat = doc.GetElement(mid)
            if mat is not None:
                name = mat.Name.lower()
                return "glass" in name or "glaz" in name
    return False


def slab_piece(solid):
    """Keep only the part of the solid within SLAB of the cut plane."""
    a = DB.BooleanOperationsUtils.CutWithHalfSpace(solid, plane_cut)
    if a is None or a.Volume < 1e-9:
        return None
    b = DB.BooleanOperationsUtils.CutWithHalfSpace(a, plane_back)
    if b is None or b.Volume < 1e-9:
        return None
    return b


# Full model geometry (not view-clipped) at the view's detail level
opts = DB.Options()
opts.DetailLevel = view.DetailLevel
opts.ComputeReferences = False

pieces = []
failed_elems = []
solid_count = 0
missed = 0
first_error = None
for el in elements:
    try:
        for s in iter_solids(el.get_Geometry(opts)):
            solid_count += 1
            if SKIP_GLASS and is_glass(s):
                continue
            try:
                p = slab_piece(s)
            except Exception as ex:
                p = None
                failed_elems.append(el.Id)
                first_error = first_error or str(ex)
            if p is None:
                missed += 1
            else:
                pieces.append(p)
    except Exception as ex:
        failed_elems.append(el.Id)
        first_error = first_error or str(ex)

if not pieces:
    forms.alert(
        "No solid geometry was found at the cut plane.\n\n"
        "Elements checked: {}\nSolids found: {}\nSolids not reaching the plane: {}\n"
        "Trim errors: {}\nFirst error: {}".format(
            len(elements), solid_count, missed, len(failed_elems), first_error or "none"),
        exitscript=True)

def union(a, b):
    return DB.BooleanOperationsUtils.ExecuteBooleanOperation(
        a, b, DB.BooleanOperationsType.Union)


def thinner(piece, depth):
    """Re-trim a piece to a different depth so its back face no longer lies
    exactly on the other pieces' back faces (a common cause of union failure)."""
    back = DB.Plane.CreateByNormalAndOrigin(n, o.Subtract(n.Multiply(depth)))
    t = DB.BooleanOperationsUtils.CutWithHalfSpace(piece, back)
    if t is None or t.Volume < 1e-9:
        return None
    return t


RETRY_DEPTHS = [SLAB * f for f in (0.61, 0.37, 0.83, 0.19)]


def try_merge(main_solid, piece):
    """Return the merged solid, or None if every attempt fails."""
    try:
        return union(main_solid, piece)
    except Exception:
        pass
    for depth in RETRY_DEPTHS:
        try:
            t = thinner(piece, depth)
            if t is not None:
                return union(main_solid, t)
        except Exception:
            pass
    return None


# Union; pieces that fail are retried at other depths, then again at the end
# once the main shape has grown. Anything still failing is filled separately.
main = None
pending = []
for p in pieces:
    if main is None:
        main = p
        continue
    merged = try_merge(main, p)
    if merged is None:
        pending.append(p)
    else:
        main = merged

retried_ok = 0
progress = True
while pending and progress:
    progress = False
    still = []
    for p in pending:
        merged = try_merge(main, p)
        if merged is None:
            still.append(p)
        else:
            main = merged
            retried_ok += 1
            progress = True
    pending = still
loose = pending


# ------------------------------------------------------------- 2D outlines
def cut_faces(solid):
    faces = []
    for f in solid.Faces:
        if not isinstance(f, DB.PlanarFace):
            continue
        if abs(f.FaceNormal.DotProduct(n)) < 0.9999:
            continue
        if abs(f.Origin.Subtract(o).DotProduct(n)) > 1e-5:
            continue
        faces.append(f)
    return faces


def to_plane(p):
    return p.Subtract(n.Multiply(p.Subtract(o).DotProduct(n)))


def drop_straight_and_spikes(pts):
    """Remove points in the middle of a straight run, and zero-width spikes
    where the outline doubles back on itself (Revit rejects those)."""
    changed = True
    while changed and len(pts) > 3:
        changed = False
        count = len(pts)
        for i in range(count):
            prev_pt = pts[i - 1]
            pt = pts[i]
            next_pt = pts[(i + 1) % count]
            a = pt.Subtract(prev_pt)
            b = next_pt.Subtract(pt)
            if a.CrossProduct(b).GetLength() < 1e-6 or next_pt.DistanceTo(prev_pt) <= short_tol:
                del pts[i]
                changed = True
                break
    return pts


right = view.RightDirection
up = view.UpDirection


def uv(p):
    d = p.Subtract(o)
    return d.DotProduct(right), d.DotProduct(up)


def signed_area(pts):
    q = [uv(p) for p in pts]
    s = 0.0
    for i in range(len(q)):
        x1, y1 = q[i]
        x2, y2 = q[(i + 1) % len(q)]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def inside(pt_uv, poly):
    """Ray-casting point-in-polygon test in view (u, v) coordinates."""
    x, y = pt_uv
    q = [uv(p) for p in poly]
    hit = False
    j = len(q) - 1
    for i in range(len(q)):
        xi, yi = q[i]
        xj, yj = q[j]
        if (yi > y) != (yj > y):
            if x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                hit = not hit
        j = i
    return hit


def sample_inside(pts):
    """A point just inside the polygon, next to the middle of its longest edge."""
    best = 0
    best_len = -1.0
    for i in range(len(pts)):
        length = pts[i].DistanceTo(pts[(i + 1) % len(pts)])
        if length > best_len:
            best, best_len = i, length
    (x1, y1), (x2, y2) = uv(pts[best]), uv(pts[(best + 1) % len(pts)])
    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    dx, dy = (x2 - x1) / best_len, (y2 - y1) / best_len
    side = 1.0 if signed_area(pts) > 0 else -1.0
    nudge = 0.002
    return mx - dy * nudge * side, my + dx * nudge * side


def clean_points(curve_loop):
    """The loop as points, dropping any segment shorter than Revit allows."""
    pts = []
    for c in curve_loop:
        if isinstance(c, DB.Line):
            pts.append(c.GetEndPoint(0))
        else:
            tess = list(c.Tessellate())
            pts.extend(tess[:-1])
    clean = []
    for p in pts:
        p = to_plane(p)
        if not clean or p.DistanceTo(clean[-1]) > short_tol:
            clean.append(p)
    while len(clean) > 2 and clean[-1].DistanceTo(clean[0]) <= short_tol:
        clean.pop()
    return clean


def split_at_repeats(pts):
    """Split a figure-8 (a loop that touches itself at a point) into separate loops."""
    count = len(pts)
    for i in range(count):
        for j in range(i + 2, count):
            if i == 0 and j == count - 1:
                continue
            if pts[i].DistanceTo(pts[j]) <= short_tol:
                return split_at_repeats(pts[i:j]) + split_at_repeats(pts[j:] + pts[:i])
    return [pts]


def to_curveloop(pts):
    loop = DB.CurveLoop()
    for i in range(len(pts)):
        loop.Append(DB.Line.CreateBound(pts[i], pts[(i + 1) % len(pts)]))
    return loop


def loop_area(curve_loop):
    return abs(signed_area([c.GetEndPoint(0) for c in curve_loop]))


def shrink_hole(pts):
    """Make a hole very slightly smaller so it no longer touches the outer edge."""
    base = to_curveloop(pts)
    start = abs(signed_area(pts))
    for d in (0.003, -0.003):
        try:
            off = DB.CurveLoop.CreateViaOffset(base, d, n)
            if loop_area(off) < start:
                return off
        except Exception:
            pass
    return None


def face_groups(face):
    """Sort a face's loops into [outer, [holes]] groups."""
    polys = []
    for cl in face.GetEdgesAsCurveLoops():
        for part in split_at_repeats(clean_points(cl)):
            part = drop_straight_and_spikes(part)
            if len(part) >= 3 and abs(signed_area(part)) > short_tol * short_tol:
                polys.append(part)
    polys.sort(key=lambda p: -abs(signed_area(p)))

    groups = []
    owner = {}   # poly index -> group index, for outers
    for idx, p in enumerate(polys):
        s = sample_inside(p)
        containers = [k for k in range(idx) if inside(s, polys[k])]
        if len(containers) % 2 == 0:
            owner[idx] = len(groups)
            groups.append([p, []])
        else:
            parent = containers[-1]
            if parent in owner:
                groups[owner[parent]][1].append(p)
            else:
                owner[idx] = len(groups)
                groups.append([p, []])
    return groups


def make_region(loops):
    region = DB.FilledRegion.Create(doc, type_id, view.Id, List[DB.CurveLoop](loops))
    p = region.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
    if p and not p.IsReadOnly:
        p.Set(TAG)
    return region


def note_reason(ex, loops_pts=None):
    if loops_pts:
        rejected_outlines.extend(loops_pts)
    msg = str(ex).strip()
    if msg not in reject_reasons:
        reject_reasons.append(msg)


def make_group_region(outer, holes):
    """Try the outline with all its holes; then with holes shrunk; then hole by hole."""
    global holes_dropped, holes_shrunk
    outer_loop = to_curveloop(outer)
    try:
        return make_region([outer_loop] + [to_curveloop(h) for h in holes])
    except Exception as ex:
        if not holes:
            note_reason(ex, [outer])
            return None
    keep = [outer_loop]
    for h in holes:
        try:
            test = make_region(keep + [to_curveloop(h)])
            doc.Delete(test.Id)
            keep.append(to_curveloop(h))
            continue
        except Exception:
            pass
        shrunk = shrink_hole(h)
        if shrunk is not None:
            try:
                test = make_region(keep + [shrunk])
                doc.Delete(test.Id)
                keep.append(shrunk)
                holes_shrunk += 1
                continue
            except Exception:
                pass
        holes_dropped += 1
    try:
        return make_region(keep)
    except Exception as ex:
        note_reason(ex, [outer] + holes)
        return None


def regions_for_solid(solid, as_one):
    """One region for everything if Revit accepts it; otherwise one per outline."""
    made, bad = [], 0
    groups = []
    for f in cut_faces(solid):
        groups.extend(face_groups(f))
    outline_count[0] += len(groups)
    if not groups:
        return made, bad
    if as_one:
        try:
            loops = []
            for outer, holes in groups:
                loops.append(to_curveloop(outer))
                loops.extend(to_curveloop(h) for h in holes)
            made.append(make_region(loops))
            return made, bad
        except Exception:
            pass
    for outer, holes in groups:
        r = make_group_region(outer, holes)
        if r is None:
            bad += 1
        else:
            made.append(r)
    if not made:
        # Fallback: the simpler per-face method from the earlier version
        for f in cut_faces(solid):
            loops = []
            for cl in f.GetEdgesAsCurveLoops():
                pts = drop_straight_and_spikes(clean_points(cl))
                if len(pts) >= 3:
                    loops.append(to_curveloop(pts))
            if loops:
                try:
                    made.append(make_region(loops))
                    bad = max(0, bad - 1)
                except Exception as ex:
                    note_reason(ex)
    return made, bad


# ------------------------------------------------------------- write to model
created = []
bad_faces = 0
reject_reasons = []
holes_dropped = 0
holes_shrunk = 0
rejected_outlines = []
outline_count = [0]
DEBUG_TAG = TAG + " (rejected outline)"
reject_group = [None]
style_missing = [False]


def safe_name(text):
    for ch in '{}[]:;|\\<>?`~':
        text = text.replace(ch, "-")
    return text


REJECT_GROUP_NAME = safe_name("Unite Section rejected - " + view.Name)


def reject_line_style():
    lines_cat = doc.Settings.Categories.get_Item(DB.BuiltInCategory.OST_Lines)
    for sub in lines_cat.SubCategories:
        if sub.Name == REJECT_LINE_STYLE:
            return sub.GetGraphicsStyle(DB.GraphicsStyleType.Projection)
    style_missing[0] = True
    return None
with revit.Transaction(TAG):
    old = List[DB.ElementId]()
    for fr in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.FilledRegion):
        p = fr.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if p and p.AsString() == TAG:
            old.Add(fr.Id)
    for ce in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.CurveElement):
        if ce.GroupId != DB.ElementId.InvalidElementId:
            continue
        p = ce.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if p and p.AsString() == DEBUG_TAG:
            old.Add(ce.Id)
    for gt in DB.FilteredElementCollector(doc).OfClass(DB.GroupType):
        p = gt.get_Parameter(DB.BuiltInParameter.ALL_MODEL_TYPE_NAME)
        if p and p.AsString() == REJECT_GROUP_NAME:
            old.Add(gt.Id)
    if old.Count:
        doc.Delete(old)

    made, bad = regions_for_solid(main, True)
    created += made
    bad_faces += bad
    for s in loose:
        made, bad = regions_for_solid(s, False)
        created += made
        bad_faces += bad

    # Draw rejected outlines as grouped detail lines so they can be inspected
    style = reject_line_style()
    line_ids = List[DB.ElementId]()
    for pts in rejected_outlines:
        for i in range(len(pts)):
            try:
                dc = doc.Create.NewDetailCurve(
                    view, DB.Line.CreateBound(pts[i], pts[(i + 1) % len(pts)]))
                if style is not None:
                    dc.LineStyle = style
                p = dc.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                if p and not p.IsReadOnly:
                    p.Set(DEBUG_TAG)
                line_ids.Add(dc.Id)
            except Exception:
                pass
    if line_ids.Count:
        try:
            grp = doc.Create.NewGroup(line_ids)
            grp.GroupType.Name = REJECT_GROUP_NAME
            reject_group[0] = grp.Id
        except Exception as ex:
            note_reason(ex)

# ------------------------------------------------------------- report
if not created:
    forms.alert(
        "No filled region could be created.\n\n"
        "Solid pieces: {}  (merged into 1, {} separate)\n"
        "Outlines found at the cut plane: {}\n"
        "Rejected outlines drawn as grouped detail lines: {}\n\n"
        "Revit said:\n{}".format(
            len(pieces), len(loose), outline_count[0], len(rejected_outlines),
            "\n".join(reject_reasons) or "nothing (no outlines reached Revit)"))
elif failed_elems or loose or bad_faces or holes_dropped or holes_shrunk:
    output.print_md("### Unite Section: {} region(s) created".format(len(created)))
    if loose:
        output.print_md("- {} piece(s) wouldn't merge and were filled separately "
                        "(they may overlap the main fill).".format(len(loose)))
    if bad_faces:
        output.print_md("- {} outline(s) were rejected by Revit and left unfilled. Revit said:".format(bad_faces))
        for r in reject_reasons:
            print("    " + r)
        if rejected_outlines:
            output.print_md("- The rejected outline(s) are drawn as detail lines in the "
                            "group \"{}\"; it's replaced on the next run.".format(REJECT_GROUP_NAME))
            if reject_group[0] is not None:
                print(output.linkify(reject_group[0]))
            if style_missing[0]:
                output.print_md("- Line style \"{}\" wasn't found, so the default "
                                "line style was used.".format(REJECT_LINE_STYLE))
    if holes_shrunk:
        output.print_md("- {} opening(s) touched the outer edge and were shrunk by about 1/32\" "
                        "so Revit would accept them.".format(holes_shrunk))
    if holes_dropped:
        output.print_md("- {} opening(s) couldn't be kept and were filled over.".format(holes_dropped))
    output.print_md("- Regions created:")
    for r in created:
        print(output.linkify(r.Id))
    if failed_elems:
        uniq = []
        for i in failed_elems:
            if i not in uniq:
                uniq.append(i)
        output.print_md("- Geometry couldn't be read or trimmed for these elements:")
        for i in uniq:
            print(output.linkify(i))
