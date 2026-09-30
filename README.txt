Poche - pyRevit extension
=========================

Unite Section: fills everything cut by the active section, elevation or
detail view with one filled region, merging touching elements into a single
outline. Re-running replaces the previous fill in that view (regions are
tagged "Unite Section" in Comments). Shift+Click the button to change the
filled region type.

Install
-------
1. Unzip so you have a folder named "Poche.extension" somewhere permanent,
   e.g. C:\Users\<you>\pyRevitExtensions\Poche.extension
2. In Revit: pyRevit tab > Settings > Custom Extension Directories > Add
   folder, and pick the folder that CONTAINS Poche.extension.
3. Save Settings and Reload. A "Poche" tab appears with the button.

Settings (top of script.py)
---------------------------
CATEGORIES  which categories get filled
SKIP_GLASS  True to leave glass/glazing unfilled
SLAB        depth of geometry kept behind the cut plane (ft)
