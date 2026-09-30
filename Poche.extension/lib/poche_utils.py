# -*- coding: utf-8 -*-
"""Shared helpers for the Poche extension."""
from pyrevit import DB, forms, script


def region_types(doc):
    """Return {type name: ElementId} for every filled region type in the project."""
    found = {}
    for t in DB.FilteredElementCollector(doc).OfClass(DB.FilledRegionType):
        p = t.get_Parameter(DB.BuiltInParameter.ALL_MODEL_TYPE_NAME)
        if p:
            found[p.AsString()] = t.Id
    return found


def pick_region_type(doc, cfg, ask=False):
    """Return the saved filled region type, or ask for one (first run / Shift+Click)."""
    types = region_types(doc)
    if not types:
        forms.alert("This project has no filled region types.")
        return None

    # Some pyRevit versions raise instead of returning the default
    try:
        saved = cfg.get_option("region_type", None)
    except Exception:
        saved = None
    if saved in types and not ask:
        return types[saved]

    choice = forms.SelectFromList.show(
        sorted(types.keys()),
        title="Filled region type for Unite Section",
        button_name="Use this type",
        multiselect=False,
    )
    if not choice:
        return None
    cfg.region_type = choice
    script.save_config()
    return types[choice]
