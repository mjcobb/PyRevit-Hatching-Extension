# -*- coding: utf-8 -*-
"""Shift+Click: choose which filled region type Unite Section uses."""
from pyrevit import revit, script
from poche_utils import pick_region_type

pick_region_type(revit.doc, script.get_config(), ask=True)
