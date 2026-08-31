"""
Tool registry for AI GIS Copilot's agent mode.

Design principle: don't hand-code every possible QGIS operation. Instead,
expose the QGIS Processing algorithm registry itself (1000+ algorithms)
through two generic tools -- search_algorithms (grounding: find the real
algorithm id) and run_processing_algorithm (execution) -- plus a small set
of high-value, precise tools for things Processing doesn't cover well
(layer/field inspection and edits, exports, zoom) and two keyless web
lookup tools for real-world questions.

Every tool function returns a plain JSON-serializable dict so results can
be fed straight back to the LLM as a tool message.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

TOOL_SCHEMAS = [
    {
        "name": "list_layers",
        "description": "List every layer currently loaded in the QGIS project, with type and feature count. Call this first if you don't already know what layers exist.",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "destructive": False,
    },
    {
        "name": "get_layer_info",
        "description": "Get details of one layer: CRS, geometry type, feature count, field names, extent.",
        "parameters": {
            "type": "object",
            "properties": {
                "layer_name": {"type": "string", "description": "Exact layer name, or 'active' for the currently selected layer."}
            },
            "required": ["layer_name"],
        },
        "destructive": False,
    },
    {
        "name": "search_algorithms",
        "description": "Search the full QGIS Processing algorithm registry by keyword to find the exact algorithm id and confirm it exists. ALWAYS call this before run_processing_algorithm unless you are already certain of the exact id.",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "e.g. 'buffer', 'dissolve', 'clip', 'slope'"},
                "limit": {"type": "integer", "description": "max results, default 15"},
            },
            "required": ["keyword"],
        },
        "destructive": False,
    },
    {
        "name": "run_processing_algorithm",
        "description": (
            "Run any QGIS Processing algorithm by its exact id (e.g. 'native:buffer', 'native:dissolve', "
            "'native:reprojectlayer', 'qgis:fieldcalculator'). Confirm the id with search_algorithms first. "
            "Use the string 'active' for a layer parameter to mean the current active layer, or a loaded "
            "layer's exact name. Use 'memory:' as OUTPUT for a temporary scratch layer, or a real file path "
            "ending in .gpkg to save to disk. This is the general-purpose tool that covers the vast majority "
            "of QGIS operations."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "algorithm_id": {"type": "string"},
                "params": {"type": "object", "description": "Parameter map matching the algorithm's exact parameter names."},
                "summary": {"type": "string", "description": "One short plain-English sentence describing what this does, shown to the user."},
            },
            "required": ["algorithm_id", "params"],
        },
        "destructive": True,
    },
    {
        "name": "add_field",
        "description": "Add a new attribute field to a vector layer.",
        "parameters": {
            "type": "object",
            "properties": {
                "layer_name": {"type": "string"},
                "field_name": {"type": "string"},
                "field_type": {"type": "string", "enum": ["string", "int", "double"]},
            },
            "required": ["layer_name", "field_name", "field_type"],
        },
        "destructive": True,
    },
    {
        "name": "delete_field",
        "description": "Delete an attribute field from a vector layer.",
        "parameters": {
            "type": "object",
            "properties": {"layer_name": {"type": "string"}, "field_name": {"type": "string"}},
            "required": ["layer_name", "field_name"],
        },
        "destructive": True,
    },
    {
        "name": "export_layer",
        "description": "Export a layer to a file on disk in a given format.",
        "parameters": {
            "type": "object",
            "properties": {
                "layer_name": {"type": "string"},
                "output_path": {"type": "string", "description": "Full file path including extension."},
                "format": {"type": "string", "enum": ["gpkg", "shp", "geojson", "csv"]},
            },
            "required": ["layer_name", "output_path", "format"],
        },
        "destructive": True,
    },
    {
        "name": "zoom_to_layer",
        "description": "Zoom the map canvas to the full extent of a layer.",
        "parameters": {"type": "object", "properties": {"layer_name": {"type": "string"}}, "required": ["layer_name"]},
        "destructive": False,
    },
    {
        "name": "web_search",
        "description": "Search the live public web for current information -- news, prices, recent releases, anything time-sensitive or outside your own knowledge. Free, keyless DuckDuckGo search.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        "destructive": False,
    },
    {
        "name": "wikipedia_lookup",
        "description": "Get a factual summary of a stable topic (place, concept, organization, historical event) from Wikipedia.",
        "parameters": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]},
        "destructive": False,
    },
    {
        "name": "search_satellite_imagery",
        "description": (
            "Search free, open Sentinel-2 or Landsat satellite imagery over an area and date range via "
            "the Earth Search STAC API (no key required). Returns scene metadata (id, date, cloud cover, "
            "thumbnail/asset URLs) -- does not download or add a layer by itself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "bbox": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "[min_lon, min_lat, max_lon, max_lat] in WGS84 degrees.",
                },
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "collection": {
                    "type": "string",
                    "description": "STAC collection id, e.g. 'sentinel-2-l2a' (default) or 'landsat-c2-l2'.",
                },
                "max_cloud_cover": {"type": "number", "description": "Optional max cloud cover percent, e.g. 20."},
            },
            "required": ["bbox", "start_date", "end_date"],
        },
        "destructive": False,
    },
    {
        "name": "fetch_boundary_from_osm",
        "description": (
            "Look up a real-world place (city, district, country, region) by name via OpenStreetMap and "
            "add its administrative boundary polygon as a new layer in the current QGIS project. Free, "
            "no key required."
        ),
        "parameters": {
            "type": "object",
            "properties": {"place_name": {"type": "string", "description": "e.g. 'Bhubaneswar, Odisha, India'"}},
            "required": ["place_name"],
        },
        "destructive": True,
    },
    {
        "name": "suggest_projected_crs",
        "description": (
            "Given a layer that's in a geographic CRS (degrees), determine the correct local UTM "
            "projected CRS to reproject it into before doing any planar distance/area operation "
            "(buffer, area calc, etc). Use this right after a validation_error from "
            "run_processing_algorithm about a geographic CRS -- it gives you the exact EPSG code to "
            "reproject to, rather than guessing a UTM zone."
        ),
        "parameters": {
            "type": "object",
            "properties": {"layer_name": {"type": "string", "description": "Exact layer name, or 'active'."}},
            "required": ["layer_name"],
        },
        "destructive": False,
    },
    {
        "name": "geodetic_distance",
        "description": (
            "Calculate the true geodesic (ellipsoidal, WGS84) distance in meters between two "
            "longitude/latitude points -- accurate over long distances where planar/Pythagorean "
            "distance on unprojected coordinates would be wrong."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "point1": {"type": "array", "items": {"type": "number"}, "description": "[longitude, latitude]"},
                "point2": {"type": "array", "items": {"type": "number"}, "description": "[longitude, latitude]"},
            },
            "required": ["point1", "point2"],
        },
        "destructive": False,
    },
    {
        "name": "compute_ndvi",
        "description": (
            "Compute NDVI (Normalized Difference Vegetation Index) from a multi-band raster layer's "
            "NIR and Red bands using QGIS's native raster calculator, and add the result as a new "
            "layer. NDVI = (NIR - Red) / (NIR + Red)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "raster_layer_name": {"type": "string"},
                "nir_band": {"type": "integer", "description": "Band number for near-infrared, e.g. 4 for Sentinel-2."},
                "red_band": {"type": "integer", "description": "Band number for red, e.g. 3 for Sentinel-2."},
            },
            "required": ["raster_layer_name", "nir_band", "red_band"],
        },
        "destructive": True,
    },
    {
        "name": "spatial_autocorrelation",
        "description": (
            "Compute a global spatial autocorrelation statistic (Moran's I, Geary's C, or Getis-Ord G) "
            "on a numeric field of a vector layer -- tells you whether high/low values cluster "
            "spatially, are dispersed, or are random. Requires geopandas/libpysal/esda; if missing, "
            "returns an install_hint pointing to /install pysal. One number for the whole layer -- "
            "for a per-feature map, use local_spatial_autocorrelation instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "layer_name": {"type": "string"},
                "field_name": {"type": "string", "description": "Numeric field to test for spatial clustering."},
                "statistic": {"type": "string", "enum": ["moran", "geary", "getis_ord"]},
                "distance_threshold_meters": {"type": "number", "description": "Neighbor distance cutoff in meters, default 100000."},
            },
            "required": ["layer_name", "field_name", "statistic"],
        },
        "destructive": False,
    },
    {
        "name": "local_spatial_autocorrelation",
        "description": (
            "Compute per-feature local spatial autocorrelation (Local Moran's I / LISA, or Getis-Ord "
            "Gi* hotspot analysis) on a numeric field, and add the result as a new layer with cluster "
            "classification (High-High, Low-Low, High-Low, Low-High, Not Significant) so it can be "
            "mapped directly. Requires geopandas/libpysal/esda; if missing, returns an install_hint "
            "pointing to /install pysal."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "layer_name": {"type": "string"},
                "field_name": {"type": "string"},
                "statistic": {"type": "string", "enum": ["moran_local", "getis_ord_local"]},
                "distance_threshold_meters": {"type": "number", "description": "Neighbor distance cutoff in meters, default 100000."},
            },
            "required": ["layer_name", "field_name", "statistic"],
        },
        "destructive": True,
    },
    {
        "name": "recall_similar_past_action",
        "description": (
            "Check this team's local history of past requests and what they resolved to, saved on "
            "this machine across sessions. Use this before asking the user for parameters they may "
            "have already specified before for a similar request (e.g. a commonly-used field name, "
            "CRS, or distance) -- this is how the Copilot gets more efficient over time for a team's "
            "recurring workflows, even though the underlying LLM itself doesn't retrain."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "The current request, to find similar past ones."}},
            "required": ["query"],
        },
        "destructive": False,
    },
    {
        "name": "compare_line_geometry_changes",
        "description": (
            "Batch-compare a 'raw' and a 'corrected' line layer (matched by a title field) to QA "
            "geometry edits: computes centroid shift distance (feet) and bearing/angle change "
            "(degrees) for 1:1 matched titles, flags shifts >= 50m, identifies unchanged features "
            "(exact geometry match) and split features (title appears more than once in corrected), "
            "and adds direction-arrow lines from raw to corrected centroids. Produces a summary "
            "layer plus per-title-group stats. Use for QA-ing line edits/corrections against an "
            "original dataset."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "raw_layer_name": {"type": "string"},
                "corrected_layer_name": {"type": "string"},
                "title_field": {"type": "string", "description": "Field used to match features between the two layers, e.g. 'localtitle'."},
                "feeder_field": {"type": "string", "description": "Optional field to group summary stats by, e.g. 'feeder'."},
            },
            "required": ["raw_layer_name", "corrected_layer_name", "title_field"],
        },
        "destructive": True,
    },
    {
        "name": "classify_span_vegremarks",
        "description": (
            "Classify line spans in an input layer against a reference layer by spatial overlap: "
            "for each input line whose centroid falls within a radius of a reference feature, "
            "assigns a short/long classification field based on a length threshold, computes "
            "endpoint connectivity (hanging = both endpoints unconnected to other lines within a "
            "snap radius, non-terminal = both endpoints connected), and assigns a low-confidence "
            "flag on ambiguous cases (fully-connected-but-short, or hanging-but-long) so they can "
            "be reviewed. Adds the results as new fields on the input layer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reference_layer_name": {"type": "string"},
                "input_layer_name": {"type": "string"},
                "overlap_radius_meters": {"type": "number", "description": "Centroid-overlap radius for reference matching, e.g. 15."},
                "short_field_value": {"type": "string", "description": "Value to assign for spans at/under the length threshold, default '999'."},
                "long_field_value": {"type": "string", "description": "Value to assign for spans over the length threshold, default '99'."},
                "length_threshold_meters": {"type": "number", "description": "Short/long cutoff, default 20."},
                "output_field": {"type": "string", "description": "Field name to write the classification to, default 'vegremarks'."},
            },
            "required": ["reference_layer_name", "input_layer_name"],
        },
        "destructive": True,
    },
    {
        "name": "validate_span_categories",
        "description": (
            "Validate gis_vcat/gis_hcat/gis_clrnce attribute combinations on a line layer against "
            "a fixed utility-QA rule table (e.g. vcat=6 requires hcat in 1-4 and a valid clearance "
            "range; vcat=10 requires hcat=10 and clrnce=-1; etc). Flags every feature that violates "
            "the rules with a specific reason. Does not modify data -- read-only QA check."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "layer_name": {"type": "string"},
                "vcat_field": {"type": "string", "description": "default 'gis_vcat'"},
                "hcat_field": {"type": "string", "description": "default 'gis_hcat'"},
                "clearance_field": {"type": "string", "description": "default 'gis_clrnce'"},
            },
            "required": ["layer_name"],
        },
        "destructive": False,
    },
    {
        "name": "correct_span_geometry",
        "description": (
            "Clean up line geometry for utility spans that should be straight pole-to-pole lines: "
            "removes duplicate/near-duplicate vertices and collapses each line part down to just "
            "its terminal (start/end) points, dropping any degenerate (zero-length) parts. Writes "
            "the corrected geometry as a new layer."
        ),
        "parameters": {
            "type": "object",
            "properties": {"layer_name": {"type": "string"}},
            "required": ["layer_name"],
        },
        "destructive": True,
    },
    {
        "name": "snap_and_resolve_span_attributes",
        "description": (
            "Snap a 'current' line layer's vertices onto a 'delivered' reference layer (using "
            "QGIS's native snapping), but only for feature pairs whose centroids are within an "
            "overlap radius of each other -- avoiding snapping unrelated spans together. Then, for "
            "groups of candidate attribute rows, resolves a single gis_vcat/gis_hcat/gis_clrnce "
            "value per group using a fixed priority order (e.g. preferring rows flagged as "
            "'priority', then falling back through a defined value-preference order), including the "
            "special case where vcat=9 forces hcat resolution with clrnce fixed at -1."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "current_layer_name": {"type": "string"},
                "delivered_layer_name": {"type": "string"},
                "snap_radius_meters": {"type": "number", "description": "e.g. 2.0"},
                "overlap_radius_meters": {"type": "number", "description": "Only snap pairs within this centroid distance, e.g. 50."},
            },
            "required": ["current_layer_name", "delivered_layer_name", "snap_radius_meters", "overlap_radius_meters"],
        },
        "destructive": True,
    },
    {
        "name": "check_background_task_status",
        "description": (
            "Check whether an /install or /bind background task (pysal, gis-mcp) is currently "
            "running, and how long it's been running for. Always call this before answering any "
            "question about install/setup progress (e.g. 'when will it be installed', 'is it done "
            "yet') -- never guess or give generic install instructions when the real answer is "
            "knowable from actual running state."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
        "destructive": False,
    },
    {
        "name": "scan_project_for_issues",
        "description": (
            "Proactively scan every loaded layer in the project for common, easy-to-miss problems: "
            "CRS mismatches between layers (a frequent cause of silently-wrong results when mixing "
            "layers), invalid geometry, layers with no spatial index that are large enough for it to "
            "matter, and duplicate layer names. Read-only -- never changes anything, just reports. "
            "Use this when the user asks for a project health check, or before a workflow that "
            "chains several operations together where an unnoticed CRS mismatch would be costly."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
        "destructive": False,
    },
    {
        "name": "export_session_as_script",
        "description": (
            "Convert this session's successful run_processing_algorithm calls into a standalone, "
            "runnable PyQGIS Python script that reproduces the same workflow -- for sharing with "
            "teammates or re-running later without going through the chat again. Writes the script "
            "to a file and returns its path. Only includes calls made via the agent this session; "
            "has nothing to export if none have run yet."
        ),
        "parameters": {
            "type": "object",
            "properties": {"output_path": {"type": "string", "description": "Where to save the .py file, e.g. D:\\GIS\\Scripts\\workflow.py"}},
            "required": ["output_path"],
        },
        "destructive": True,
    },
    {
        "name": "apply_symbology",
        "description": (
            "Style a layer by a field: 'categorized' gives each unique value its own color (good for "
            "types/classes/status fields); 'graduated' bins a numeric field into color-ramped classes "
            "(good for counts, measurements, continuous values). Applies immediately to the layer in "
            "the project."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "layer_name": {"type": "string"},
                "field_name": {"type": "string"},
                "mode": {"type": "string", "enum": ["categorized", "graduated"]},
                "num_classes": {"type": "integer", "description": "Only used for graduated mode, default 5."},
                "color_ramp_name": {"type": "string", "description": "A QGIS style color ramp name, e.g. 'Spectral', 'RdYlGn', 'Viridis'. Default 'Spectral'."},
            },
            "required": ["layer_name", "field_name", "mode"],
        },
        "destructive": True,
    },
    {
        "name": "create_map_layout",
        "description": (
            "Create a print-ready map layout (title, map frame, legend, scale bar) covering the "
            "given layers, added to the project's Layout Manager, and optionally exported straight "
            "to a PDF."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "layout_name": {"type": "string"},
                "layer_names": {"type": "array", "items": {"type": "string"}, "description": "Layers to include; omit to use all currently visible layers."},
                "title": {"type": "string", "description": "Optional title text shown at the top of the layout."},
                "include_legend": {"type": "boolean", "description": "default true"},
                "include_scale_bar": {"type": "boolean", "description": "default true"},
                "export_pdf_path": {"type": "string", "description": "If given, export straight to this PDF path, e.g. D:\\GIS\\Maps\\output.pdf"},
            },
            "required": ["layout_name"],
        },
        "destructive": True,
    },
    {
        "name": "capture_and_analyze_viewport",
        "description": (
            "Take a screenshot of the current map canvas and have an AI vision model actually look "
            "at it to answer a visual question -- e.g. 'does this look like a flood-prone area', "
            "'are these parcels aligned correctly', 'what land cover dominates this view'. This is "
            "real image understanding, not layer metadata. Requires a Gemini API key to be "
            "configured (/set gemini_key), even if a different provider is your primary agent -- "
            "if none is set, this returns a clear error rather than guessing from metadata."
        ),
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "What to look for or ask about the current map view."}},
            "required": ["question"],
        },
        "destructive": False,
    },
    {
        "name": "search_gis_mcp_tools",
        "description": (
            "Search the ~88 tools available via the optional gis-mcp bridge (Shapely/GeoPandas/"
            "Rasterio/PyProj/PySAL geometry, raster, and spatial-stats functions) by keyword, to "
            "find the exact tool name and its parameters. Returns nothing useful if gis-mcp isn't "
            "bound (/bind gis-mcp) -- tell the user that plainly rather than guessing a tool name."
        ),
        "parameters": {
            "type": "object",
            "properties": {"keyword": {"type": "string"}, "limit": {"type": "integer", "description": "default 10"}},
            "required": ["keyword"],
        },
        "destructive": False,
    },
    {
        "name": "call_gis_mcp_tool",
        "description": (
            "Call one specific gis-mcp tool by its exact name (confirm the name and parameters with "
            "search_gis_mcp_tools first)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string"},
                "arguments": {"type": "object", "description": "Parameter map matching the tool's exact parameter names."},
            },
            "required": ["tool_name", "arguments"],
        },
        "destructive": True,
    },
    {
        "name": "get_elevation_at_points",
        "description": (
            "Get ground elevation (meters) at one or more lon/lat points -- free, keyless. Scoped "
            "to point queries (e.g. spot-checks, a rough elevation profile along a few points on a "
            "line), not a full DEM raster download/mosaic, which is a much larger and riskier "
            "operation not implemented here."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "points": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "number"}},
                    "description": "[[lon, lat], [lon, lat], ...]",
                }
            },
            "required": ["points"],
        },
        "destructive": False,
    },
    {
        "name": "run_custom_pyqgis_script",
        "description": (
            "For requests no existing tool covers: write a short PyQGIS Python script and run it "
            "against the live project. The exact code is always shown to the user for review before "
            "it runs -- this is never silent or fully autonomous. If it errors, you'll see the real "
            "traceback as the result and can write corrected code and try again (still requiring "
            "review each time). Only qgis.core, qgis.utils.iface, and processing are available -- no "
            "file/network/subprocess access, no imports beyond those. Use existing tools instead of "
            "this whenever one already covers the request."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "One sentence: what this script does."},
                "code": {"type": "string", "description": "The Python code to run."},
            },
            "required": ["description", "code"],
        },
        "destructive": True,
    },
    {
        "name": "describe_algorithm_parameters",
        "description": "Get the exact parameter names, types, and required/optional status for a Processing algorithm by id -- call this before run_processing_algorithm whenever you're not 100% certain of the exact parameter names.",
        "parameters": {"type": "object", "properties": {"algorithm_id": {"type": "string"}}, "required": ["algorithm_id"]},
        "destructive": False,
    },
    {
        "name": "profile_layer_fields",
        "description": "Profile a layer's fields: data type, null count, unique-value count, and a small sample of values per field, plus a geometry validity summary. Use for 'what's in this layer' / data-quality questions beyond just field names.",
        "parameters": {"type": "object", "properties": {"layer_name": {"type": "string"}}, "required": ["layer_name"]},
        "destructive": False,
    },
    {
        "name": "query_features_by_spatial_predicate",
        "description": "Find features in one layer that intersect, are within, or contain features of a reference layer -- e.g. 'which parcels intersect the flood zone'. Returns matching feature count and adds a new layer with just the matches.",
        "parameters": {
            "type": "object",
            "properties": {
                "layer_name": {"type": "string"},
                "reference_layer_name": {"type": "string"},
                "predicate": {"type": "string", "enum": ["intersects", "within", "contains"]},
            },
            "required": ["layer_name", "reference_layer_name", "predicate"],
        },
        "destructive": True,
    },
    {
        "name": "undo_last_layer_edit",
        "description": "Roll back the most recent in-place edit made by add_field/delete_field on a layer (only works if the layer's edit session hasn't been closed/saved since).",
        "parameters": {"type": "object", "properties": {"layer_name": {"type": "string"}}, "required": ["layer_name"]},
        "destructive": True,
    },
    {
        "name": "auto_fix_geometry_issues",
        "description": "Automatically repair invalid geometry on a layer (self-intersections, etc.) using QGIS's native fix-geometries algorithm, producing a corrected new layer. Use after scan_project_for_issues reports invalid geometry.",
        "parameters": {"type": "object", "properties": {"layer_name": {"type": "string"}}, "required": ["layer_name"]},
        "destructive": True,
    },
    {
        "name": "compute_water_or_burn_index",
        "description": "Compute NDWI (water/moisture) or NBR (burn severity) from a multi-band raster's relevant bands, same pattern as compute_ndvi.",
        "parameters": {
            "type": "object",
            "properties": {
                "raster_layer_name": {"type": "string"},
                "index": {"type": "string", "enum": ["ndwi", "nbr"]},
                "band_a": {"type": "integer", "description": "NDWI: Green band. NBR: NIR band."},
                "band_b": {"type": "integer", "description": "NDWI: NIR band. NBR: SWIR band."},
            },
            "required": ["raster_layer_name", "index", "band_a", "band_b"],
        },
        "destructive": True,
    },
    {
        "name": "fetch_osm_features",
        "description": "Download specific tagged features (hospitals, primary roads, schools, etc.) within a named place from OpenStreetMap via the Overpass API -- free, no key. Adds the results as a new layer. Different from fetch_boundary_from_osm, which gets the place's boundary polygon, not features inside it.",
        "parameters": {
            "type": "object",
            "properties": {
                "place_name": {"type": "string"},
                "osm_tag": {"type": "string", "description": "An OSM tag key=value, e.g. 'amenity=hospital', 'highway=primary', 'amenity=school'."},
            },
            "required": ["place_name", "osm_tag"],
        },
        "destructive": True,
    },
]


def _http_get(url, timeout=15, headers=None):
    # Same root cause as the Groq 403 fix: scraped/public endpoints (unlike
    # Nominatim below, which explicitly wants an app-identifying UA per its
    # own usage policy) are prone to blocking non-browser User-Agents.
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; AI-GIS-Copilot/1.0)", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def web_search_brave(query, api_key, max_results=5):
    """Optional upgrade over the always-free DuckDuckGo search, for anyone on
    the team who wants to add their own free/low-cost Brave Search API key
    (brave.com/search/api) -- higher-quality independent index. Never
    required; DuckDuckGo remains the zero-setup default for everyone."""
    try:
        url = "https://api.search.brave.com/res/v1/web/search?q=" + urllib.parse.quote(query)
        headers = {"Accept": "application/json", "X-Subscription-Token": api_key}
        body = _http_get(url, headers=headers)
        decoded = json.loads(body)
        results = []
        for item in (decoded.get("web", {}) or {}).get("results", [])[:max_results]:
            results.append({"title": item.get("title", ""), "url": item.get("url", ""), "snippet": item.get("description", "")})
        if not results:
            return {"ok": False, "error": "Brave Search returned no results."}
        return {"ok": True, "results": results, "provider": "brave"}
    except Exception as exc:
        return {"ok": False, "error": f"Brave Search failed: {exc}"}


def web_search_duckduckgo(query, max_results=5):
    try:
        url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(query)
        html = _http_get(url)
        rows = re.findall(r'class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, flags=re.S)
        snippets = re.findall(r'class="result-snippet"[^>]*>(.*?)</td>', html, flags=re.S)
        results = []
        for i, (link, title) in enumerate(rows[:max_results]):
            clean_title = re.sub("<[^>]+>", "", title).strip()
            snippet = re.sub("<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
            results.append({"title": clean_title, "url": link, "snippet": snippet})
        if not results:
            return {"ok": False, "error": "No results parsed (DuckDuckGo layout may have changed)."}
        return {"ok": True, "results": results}
    except Exception as exc:
        return {"ok": False, "error": f"web_search failed: {exc}"}


def wikipedia_summary(topic):
    try:
        search_url = (
            "https://en.wikipedia.org/w/api.php?action=opensearch&limit=1&format=json&search="
            + urllib.parse.quote(topic)
        )
        decoded = json.loads(_http_get(search_url))
        titles = decoded[1] if len(decoded) > 1 else []
        if not titles:
            return {"ok": False, "error": f"No Wikipedia article found for '{topic}'."}
        title = titles[0]
        summary_url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title)
        summary = json.loads(_http_get(summary_url))
        return {
            "ok": True,
            "title": summary.get("title", title),
            "extract": summary.get("extract", ""),
            "url": summary.get("content_urls", {}).get("desktop", {}).get("page", ""),
        }
    except Exception as exc:
        return {"ok": False, "error": f"wikipedia_lookup failed: {exc}"}


class ToolExecutor:
    """Executes tool calls against a live QGIS session. `host` is the dock
    widget instance -- reused for its existing, already-tested helpers
    (algorithm execution, layer/output resolution) rather than duplicating
    that logic."""

    def __init__(self, host):
        self.host = host
        self.mcp_bridge = None  # set by /bind gis-mcp; None means unbound (default, unchanged behavior)

    def dispatch(self, name, arguments):
        if name.startswith("gismcp__") and self.mcp_bridge is not None:
            real_name = name[len("gismcp__") :]
            try:
                return self.mcp_bridge.call_tool(real_name, arguments)
            except Exception as exc:
                return {"ok": False, "error": f"gis-mcp tool '{real_name}' failed: {exc}"}
        method = getattr(self, f"tool_{name}", None)
        if method is None:
            return {"ok": False, "error": f"Unknown tool '{name}'"}
        try:
            return method(**(arguments or {}))
        except TypeError as exc:
            return {"ok": False, "error": f"Bad arguments for {name}: {exc}"}
        except Exception as exc:
            return {"ok": False, "error": f"{name} failed: {exc}"}

    def _resolve_layer(self, layer_name):
        from qgis.core import QgsProject

        if not layer_name or layer_name.strip().lower() == "active":
            return self.host.iface.activeLayer()
        target = layer_name.strip().lower()
        for lyr in QgsProject.instance().mapLayers().values():
            if lyr.name().strip().lower() == target:
                return lyr
        return None

    def tool_list_layers(self):
        from qgis.core import QgsProject

        out = []
        for lyr in QgsProject.instance().mapLayers().values():
            entry = {"name": lyr.name()}
            try:
                entry["feature_count"] = lyr.featureCount()
            except Exception:
                pass
            out.append(entry)
        return {"ok": True, "layers": out, "count": len(out)}

    def tool_get_layer_info(self, layer_name):
        layer = self._resolve_layer(layer_name)
        if layer is None:
            return {"ok": False, "error": f"Layer '{layer_name}' not found."}
        info = {"name": layer.name()}
        try:
            info["crs"] = layer.crs().authid()
        except Exception:
            pass
        try:
            info["feature_count"] = layer.featureCount()
            info["fields"] = [f.name() for f in layer.fields()]
            ext = layer.extent()
            info["extent"] = [ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()]
        except Exception:
            pass
        return {"ok": True, "layer": info}

    def tool_search_algorithms(self, keyword, limit=15):
        try:
            import processing
        except Exception:
            return {"ok": False, "error": "QGIS Processing is not available."}
        keyword_l = (keyword or "").lower()
        matches = []
        for alg in processing.registry().algorithms():
            hay = f"{alg.id()} {alg.displayName()}".lower()
            if keyword_l in hay:
                matches.append({"id": alg.id(), "name": alg.displayName()})
                if len(matches) >= limit:
                    break
        return {"ok": True, "matches": matches, "count": len(matches)}

    def tool_run_processing_algorithm(self, algorithm_id, params, summary=""):
        validation_error = self._validate_algorithm_params(algorithm_id, params)
        if validation_error:
            return {"ok": False, "validation_error": validation_error}
        ok = self.host._execute_algorithm_plan(algorithm_id, params, summary or "")
        if ok:
            self._log_algorithm_call(algorithm_id, params, summary)
        return {"ok": bool(ok), "algorithm_id": algorithm_id}

    def _log_algorithm_call(self, algorithm_id, params, summary):
        """Feeds export_session_as_script -- a running record of successful
        algorithm calls this session, so a chat conversation can be turned
        into a reusable, shareable script afterward."""
        try:
            from datetime import datetime

            if not hasattr(self.host, "recent_algorithm_calls"):
                self.host.recent_algorithm_calls = []
            self.host.recent_algorithm_calls.append(
                {"algorithm_id": algorithm_id, "params": params, "summary": summary, "at": datetime.utcnow().isoformat()}
            )
            self.host.recent_algorithm_calls = self.host.recent_algorithm_calls[-200:]
        except Exception:
            pass  # logging must never break the actual algorithm call

    def _validate_algorithm_params(self, algorithm_id, params):
        """Plan -> Validate -> Execute -> Verify. Catches the classic GIS
        footgun: running a planar-distance operation (buffer etc.) on a
        layer that's still in a geographic CRS (degrees), which silently
        produces a nonsense result instead of an error. Returns a message
        for the agent to act on (reproject first, then retry) rather than
        letting a bad result through quietly."""
        alg_l = (algorithm_id or "").lower()
        if "buffer" not in alg_l:
            return None
        if not isinstance(params, dict):
            return None
        input_ref = params.get("INPUT")
        if not input_ref:
            return None
        layer = self._resolve_layer(input_ref)
        if layer is None:
            return None
        try:
            crs = layer.crs()
            if crs.isGeographic():
                distance = params.get("DISTANCE")
                return (
                    f"Layer '{layer.name()}' is in a geographic CRS ({crs.authid()}, units: degrees), "
                    f"but '{algorithm_id}' is a planar distance operation. DISTANCE={distance} would be "
                    "interpreted in degrees, not meters -- producing a hugely wrong buffer. Reproject the "
                    "layer to an appropriate projected CRS (e.g. the correct UTM zone for its location) "
                    "using run_processing_algorithm with 'native:reprojectlayer' first, then retry this "
                    "buffer on the reprojected output layer."
                )
        except Exception:
            return None
        return None

    def tool_add_field(self, layer_name, field_name, field_type):
        from qgis.core import QgsField
        from qgis.PyQt.QtCore import QVariant

        layer = self._resolve_layer(layer_name)
        if layer is None:
            return {"ok": False, "error": f"Layer '{layer_name}' not found."}
        type_map = {"string": QVariant.String, "int": QVariant.Int, "double": QVariant.Double}
        qtype = type_map.get((field_type or "string").lower(), QVariant.String)
        provider = layer.dataProvider()
        provider.addAttributes([QgsField(field_name, qtype)])
        layer.updateFields()
        self._track_layer_edit(layer.name(), "add_field", field_name=field_name)
        return {"ok": True, "message": f"Added field '{field_name}' to '{layer.name()}'."}

    def tool_delete_field(self, layer_name, field_name):
        layer = self._resolve_layer(layer_name)
        if layer is None:
            return {"ok": False, "error": f"Layer '{layer_name}' not found."}
        idx = layer.fields().indexOf(field_name)
        if idx == -1:
            return {"ok": False, "error": f"Field '{field_name}' not found on '{layer.name()}'."}
        provider = layer.dataProvider()
        provider.deleteAttributes([idx])
        layer.updateFields()
        self._track_layer_edit(layer.name(), "delete_field", field_name=field_name)
        return {"ok": True, "message": f"Deleted field '{field_name}' from '{layer.name()}'."}

    def _track_layer_edit(self, layer_name, action, **details):
        if not hasattr(self.host, "recent_layer_edits"):
            self.host.recent_layer_edits = []
        self.host.recent_layer_edits.append({"layer": layer_name, "action": action, **details})
        self.host.recent_layer_edits = self.host.recent_layer_edits[-100:]

    def tool_export_layer(self, layer_name, output_path, format):
        from qgis.core import QgsVectorFileWriter, QgsProject

        layer = self._resolve_layer(layer_name)
        if layer is None:
            return {"ok": False, "error": f"Layer '{layer_name}' not found."}
        driver_map = {"gpkg": "GPKG", "shp": "ESRI Shapefile", "geojson": "GeoJSON", "csv": "CSV"}
        driver = driver_map.get((format or "gpkg").lower(), "GPKG")
        error = QgsVectorFileWriter.writeAsVectorFormat(
            layer, output_path, "UTF-8", layer.crs(), driver
        )
        ok = error[0] == QgsVectorFileWriter.NoError if isinstance(error, tuple) else error == QgsVectorFileWriter.NoError
        if not ok:
            return {"ok": False, "error": f"Export failed: {error}"}
        return {"ok": True, "message": f"Exported '{layer.name()}' to {output_path}."}

    def tool_zoom_to_layer(self, layer_name):
        layer = self._resolve_layer(layer_name)
        if layer is None:
            return {"ok": False, "error": f"Layer '{layer_name}' not found."}
        self.host.iface.mapCanvas().setExtent(layer.extent())
        self.host.iface.mapCanvas().refresh()
        return {"ok": True, "message": f"Zoomed to '{layer.name()}'."}

    def tool_web_search(self, query):
        brave_key = getattr(self.host, "agent_settings", {}).get("brave_key", "")
        if brave_key:
            result = web_search_brave(query, brave_key)
            if result.get("ok"):
                return result
            # Fall through to the always-free option if Brave errors (bad
            # key, rate limit, etc.) rather than failing the whole request.
        return web_search_duckduckgo(query)

    def tool_wikipedia_lookup(self, topic):
        return wikipedia_summary(topic)

    def tool_recall_similar_past_action(self, query):
        learned_routes = getattr(self.host, "learned_routes", {}) or {}
        history_store = getattr(self.host, "history_store", []) or []
        shared_entries = self._read_shared_memory()
        if not learned_routes and not history_store and not shared_entries:
            return {"ok": True, "matches": [], "note": "No local or shared history yet."}
        try:
            normalize = self.host._normalize_query_for_learning
        except AttributeError:
            normalize = lambda q: (q or "").strip().lower()
        try:
            tokenize = self.host._tokenize_text
            score_fn = self.host._token_overlap_score
        except AttributeError:
            tokenize = lambda t: set((t or "").lower().split())
            score_fn = lambda a, b: len(a & b) / max(1, len(a | b))
        qn = normalize(query)
        qtokens = tokenize(query)
        matches = []
        if qn in learned_routes:
            matches.append({"query": qn, "result": learned_routes[qn].get("result"), "at": learned_routes[qn].get("at"), "source": "local"})
        scored = []
        combined_history = [dict(e, source="local") for e in history_store[-500:]] + [
            dict(e, source=f"team:{e.get('user', '?')}") for e in shared_entries[-500:]
        ]
        for entry in combined_history:
            past_tokens = tokenize(entry.get("query", ""))
            if not past_tokens or not qtokens:
                continue
            shared = qtokens & past_tokens
            if len(shared) < 2:
                # A single shared word (even a meaningful one) is too weak a
                # signal on its own -- verified this against a real false
                # positive ("what" alone matching two unrelated queries).
                continue
            overlap = score_fn(qtokens, past_tokens)
            if overlap >= 0.3:
                scored.append((overlap, entry))
        scored.sort(key=lambda x: x[0], reverse=True)
        for _, entry in scored[:5]:
            matches.append(
                {"query": entry.get("query"), "result": entry.get("result"), "at": entry.get("at"), "source": entry.get("source", "local")}
            )
        return {"ok": True, "matches": matches, "count": len(matches)}

    def _read_shared_memory(self, max_lines=2000):
        """Reads the optional team-wide activity log (see /set
        shared_memory_path). Off by default; returns [] if not configured
        or unreadable, never raises -- this must never be able to break an
        otherwise-working request. Reads every team_activity_<user>.jsonl
        file in the folder (each teammate writes only their own file --
        see _append_to_shared_memory for why), merging them into one
        combined, chronologically-sorted view."""
        path = (getattr(self.host, "agent_settings", {}) or {}).get("shared_memory_path", "").strip()
        if not path:
            return []
        try:
            import glob
            import os

            entries = []
            for target in glob.glob(os.path.join(path, "team_activity_*.jsonl")):
                try:
                    with open(target, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                entries.append(json.loads(line))
                            except Exception:
                                continue
                except Exception:
                    continue  # one unreadable teammate file must not sink the rest
            entries.sort(key=lambda e: e.get("at", ""))
            return entries[-max_lines:]
        except Exception:
            return []

    def tool_search_satellite_imagery(self, bbox, start_date, end_date, collection="sentinel-2-l2a", max_cloud_cover=None):
        try:
            payload = {
                "collections": [collection],
                "bbox": bbox,
                "datetime": f"{start_date}T00:00:00Z/{end_date}T23:59:59Z",
                "limit": 10,
            }
            if max_cloud_cover is not None:
                payload["query"] = {"eo:cloud_cover": {"lt": max_cloud_cover}}
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                "https://earth-search.aws.element84.com/v1/search",
                data=data,
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (compatible; AI-GIS-Copilot/1.0)"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                decoded = json.loads(resp.read().decode("utf-8"))
            scenes = []
            for feature in decoded.get("features", []):
                props = feature.get("properties", {})
                assets = feature.get("assets", {})
                thumb = (assets.get("thumbnail") or assets.get("visual") or {}).get("href", "")
                scenes.append(
                    {
                        "id": feature.get("id"),
                        "datetime": props.get("datetime"),
                        "cloud_cover": props.get("eo:cloud_cover"),
                        "thumbnail": thumb,
                    }
                )
            return {"ok": True, "collection": collection, "scenes": scenes, "count": len(scenes)}
        except Exception as exc:
            return {"ok": False, "error": f"search_satellite_imagery failed: {exc}"}

    def tool_fetch_boundary_from_osm(self, place_name):
        from qgis.core import QgsProject, QgsVectorLayer, QgsFeature, QgsGeometry

        try:
            url = (
                "https://nominatim.openstreetmap.org/search?format=geojson&polygon_geojson=1&limit=1&q="
                + urllib.parse.quote(place_name)
            )
            body = _http_get(url, headers={"User-Agent": "AI-GIS-Copilot/1.0 (QGIS plugin)"})
            decoded = json.loads(body)
            features = decoded.get("features", [])
            if not features:
                return {"ok": False, "error": f"No OpenStreetMap result found for '{place_name}'."}
            feature = features[0]
            geom_type = feature.get("geometry", {}).get("type", "Polygon")
            layer = QgsVectorLayer(f"{geom_type}?crs=EPSG:4326", place_name[:60], "memory")
            provider = layer.dataProvider()
            qfeature = QgsFeature()
            qfeature.setGeometry(QgsGeometry.fromWkt(self._geojson_geom_to_wkt(feature["geometry"])))
            provider.addFeatures([qfeature])
            layer.updateExtents()
            QgsProject.instance().addMapLayer(layer)
            self.host.iface.mapCanvas().setExtent(layer.extent())
            self.host.iface.mapCanvas().refresh()
            display_name = feature.get("properties", {}).get("display_name", place_name)
            return {"ok": True, "message": f"Added boundary layer for '{display_name}'.", "layer_name": layer.name()}
        except Exception as exc:
            return {"ok": False, "error": f"fetch_boundary_from_osm failed: {exc}"}

    @staticmethod
    def _geojson_geom_to_wkt(geometry):
        gtype = geometry.get("type")
        coords = geometry.get("coordinates")

        def ring_to_wkt(ring):
            return "(" + ", ".join(f"{x} {y}" for x, y in ring) + ")"

        if gtype == "Polygon":
            rings = ", ".join(ring_to_wkt(r) for r in coords)
            return f"POLYGON({rings})"
        if gtype == "MultiPolygon":
            polys = ", ".join("(" + ", ".join(ring_to_wkt(r) for r in poly) + ")" for poly in coords)
            return f"MULTIPOLYGON({polys})"
        if gtype == "Point":
            return f"POINT({coords[0]} {coords[1]})"
        raise ValueError(f"Unsupported OSM geometry type: {gtype}")

    def tool_suggest_projected_crs(self, layer_name):
        from qgis.core import QgsCoordinateTransform, QgsCoordinateReferenceSystem, QgsProject

        layer = self._resolve_layer(layer_name)
        if layer is None:
            return {"ok": False, "error": f"Layer '{layer_name}' not found."}
        try:
            extent = layer.extent()
            center = extent.center()
            crs = layer.crs()
            wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
            if crs != wgs84:
                transform = QgsCoordinateTransform(crs, wgs84, QgsProject.instance())
                center = transform.transform(center)
            lon, lat = center.x(), center.y()
            zone = int((lon + 180) / 6) % 60 + 1
            epsg = 32600 + zone if lat >= 0 else 32700 + zone
            hemisphere = "N" if lat >= 0 else "S"
            return {
                "ok": True,
                "epsg": f"EPSG:{epsg}",
                "utm_zone": zone,
                "hemisphere": hemisphere,
                "note": f"Suitable projected CRS for '{layer.name()}' based on its centroid ({lon:.4f}, {lat:.4f}).",
            }
        except Exception as exc:
            return {"ok": False, "error": f"suggest_projected_crs failed: {exc}"}

    def tool_geodetic_distance(self, point1, point2):
        from qgis.core import QgsDistanceArea, QgsPointXY, QgsCoordinateReferenceSystem, QgsProject

        try:
            da = QgsDistanceArea()
            da.setEllipsoid("WGS84")
            da.setSourceCrs(QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance().transformContext())
            p1 = QgsPointXY(point1[0], point1[1])
            p2 = QgsPointXY(point2[0], point2[1])
            distance_m = da.measureLine(p1, p2)
            return {"ok": True, "distance_meters": distance_m, "ellipsoid": "WGS84"}
        except Exception as exc:
            return {"ok": False, "error": f"geodetic_distance failed: {exc}"}

    def tool_compute_ndvi(self, raster_layer_name, nir_band, red_band):
        import os
        import tempfile
        from qgis.core import (
            QgsProject,
            QgsRasterLayer,
            QgsRasterCalculator,
            QgsRasterCalculatorEntry,
        )

        layer = None
        for lyr in QgsProject.instance().mapLayers().values():
            if lyr.name().strip().lower() == raster_layer_name.strip().lower() and isinstance(lyr, QgsRasterLayer):
                layer = lyr
                break
        if layer is None:
            return {"ok": False, "error": f"Raster layer '{raster_layer_name}' not found."}
        try:
            nir_entry = QgsRasterCalculatorEntry()
            nir_entry.ref = f"{layer.name()}@{nir_band}"
            nir_entry.raster = layer
            nir_entry.bandNumber = nir_band
            red_entry = QgsRasterCalculatorEntry()
            red_entry.ref = f"{layer.name()}@{red_band}"
            red_entry.raster = layer
            red_entry.bandNumber = red_band
            formula = f'("{nir_entry.ref}" - "{red_entry.ref}") / ("{nir_entry.ref}" + "{red_entry.ref}")'
            output_path = os.path.join(tempfile.gettempdir(), f"{layer.name()}_ndvi.tif")
            calc = QgsRasterCalculator(
                formula, output_path, "GTiff", layer.extent(), layer.width(), layer.height(), [nir_entry, red_entry]
            )
            result = calc.processCalculation()
            if result != 0:
                return {"ok": False, "error": f"NDVI calculation failed (raster calculator code {result})."}
            ndvi_layer = QgsRasterLayer(output_path, f"{layer.name()}_NDVI")
            if not ndvi_layer.isValid():
                return {"ok": False, "error": "NDVI raster computed but failed to load as a layer."}
            QgsProject.instance().addMapLayer(ndvi_layer)
            return {"ok": True, "message": f"Computed NDVI from '{layer.name()}' and added as a new layer.", "output_path": output_path}
        except Exception as exc:
            return {"ok": False, "error": f"compute_ndvi failed: {exc}"}

    def _load_pysal_stack(self):
        try:
            import geopandas as gpd
            import numpy as np
            import libpysal
            import esda

            return gpd, np, libpysal, esda, None
        except ImportError as exc:
            return None, None, None, None, (
                f"Spatial statistics need extra Python packages that aren't installed ({exc}). "
                "Run /install pysal in this chat, or see the README for manual setup, then retry."
            )

    def _layer_to_temp_gdf(self, layer, gpd):
        import os
        import tempfile
        from qgis.core import QgsVectorFileWriter

        temp_path = os.path.join(tempfile.gettempdir(), f"{layer.name()}_pysal_{id(layer)}.gpkg")
        error = QgsVectorFileWriter.writeAsVectorFormat(layer, temp_path, "UTF-8", layer.crs(), "GPKG")
        ok = error[0] == QgsVectorFileWriter.NoError if isinstance(error, tuple) else error == QgsVectorFileWriter.NoError
        if not ok:
            raise RuntimeError(f"Could not export layer for spatial stats: {error}")
        return gpd.read_file(temp_path)

    def _build_distance_weights(self, gdf, field_name, distance_threshold_meters, np, libpysal):
        """Faithfully mirrors gis-mcp's pysal_load_data: reproject to EPSG:4326 for a
        degrees-based threshold, DistanceBand weights, row-standardized, island handling,
        with a KNN fallback if every unit ends up isolated."""
        gdf = gdf.to_crs("EPSG:4326")
        effective_threshold = distance_threshold_meters / 111000
        y = gdf[field_name].values.astype(np.float64)
        w = libpysal.weights.DistanceBand.from_dataframe(gdf, threshold=effective_threshold, binary=False)
        w.transform = "r"
        for island in w.islands:
            w.weights[island] = [0] * len(w.weights[island])
            w.cardinalities[island] = 0
        if w.islands and len(w.islands) == len(gdf):
            w = libpysal.weights.KNN.from_dataframe(gdf, k=min(4, len(gdf) - 1))
            w.transform = "r"
        elif w.islands:
            keep_idx = [i for i in range(len(gdf)) if i not in set(w.islands)]
            gdf = gdf.iloc[keep_idx].reset_index(drop=True)
            y = y[keep_idx]
            w = libpysal.weights.DistanceBand.from_dataframe(gdf, threshold=effective_threshold, binary=False)
            w.transform = "r"
        return gdf, y, w

    def tool_spatial_autocorrelation(self, layer_name, field_name, statistic, distance_threshold_meters=100000):
        gpd, np, libpysal, esda, dep_error = self._load_pysal_stack()
        if dep_error:
            return {"ok": False, "error": dep_error, "install_hint": "/install pysal"}
        layer = self._resolve_layer(layer_name)
        if layer is None:
            return {"ok": False, "error": f"Layer '{layer_name}' not found."}
        try:
            gdf = self._layer_to_temp_gdf(layer, gpd)
            if field_name not in gdf.columns:
                return {"ok": False, "error": f"Field '{field_name}' not found on '{layer.name()}'."}
            gdf, y, w = self._build_distance_weights(gdf, field_name, distance_threshold_meters, np, libpysal)
            if statistic == "moran":
                stat = esda.Moran(y, w)
                return {"ok": True, "statistic": "moran", "I": float(stat.I), "p_value": float(stat.p_sim), "z_score": float(stat.z_sim)}
            if statistic == "geary":
                stat = esda.Geary(y, w)
                return {"ok": True, "statistic": "geary", "C": float(stat.C), "p_value": float(stat.p_sim), "z_score": float(stat.z_sim)}
            if statistic == "getis_ord":
                stat = esda.G(y, w)
                return {"ok": True, "statistic": "getis_ord", "G": float(stat.G), "p_value": float(stat.p_sim), "z_score": float(stat.z_sim)}
            return {"ok": False, "error": f"Unknown statistic '{statistic}'."}
        except Exception as exc:
            return {"ok": False, "error": f"spatial_autocorrelation failed: {exc}"}

    def tool_local_spatial_autocorrelation(self, layer_name, field_name, statistic, distance_threshold_meters=100000):
        from qgis.core import QgsVectorLayer, QgsProject, QgsFeature, QgsGeometry, QgsField
        from qgis.PyQt.QtCore import QVariant

        gpd, np, libpysal, esda, dep_error = self._load_pysal_stack()
        if dep_error:
            return {"ok": False, "error": dep_error, "install_hint": "/install pysal"}
        layer = self._resolve_layer(layer_name)
        if layer is None:
            return {"ok": False, "error": f"Layer '{layer_name}' not found."}
        try:
            gdf = self._layer_to_temp_gdf(layer, gpd)
            if field_name not in gdf.columns:
                return {"ok": False, "error": f"Field '{field_name}' not found on '{layer.name()}'."}
            gdf, y, w = self._build_distance_weights(gdf, field_name, distance_threshold_meters, np, libpysal)

            if statistic == "moran_local":
                stat = esda.Moran_Local(y, w)
                values, p_values = stat.Is, stat.p_sim
                quadrant = stat.q  # 1=HH, 2=LH, 3=LL, 4=HL (esda convention)
                cluster_map = {1: "High-High", 2: "Low-High", 3: "Low-Low", 4: "High-Low"}
                value_field = "LISA_I"
            elif statistic == "getis_ord_local":
                stat = esda.G_Local(y, w)
                values, p_values = stat.Gs, stat.p_sim
                mean_y = float(np.mean(y))
                quadrant = [1 if (v > mean_y and p < 0.05) else (3 if (v < mean_y and p < 0.05) else 0) for v, p in zip(y, p_values)]
                cluster_map = {1: "High-High", 3: "Low-Low", 0: "Not Significant"}
                value_field = "G_LOCAL"
            else:
                return {"ok": False, "error": f"Unknown statistic '{statistic}'."}

            out_layer = QgsVectorLayer(f"{layer.geometryType()}?crs={gdf.crs.to_string()}", f"{layer.name()}_{statistic}", "memory")
            provider = out_layer.dataProvider()
            provider.addAttributes(
                [QgsField(value_field, QVariant.Double), QgsField("P_VALUE", QVariant.Double), QgsField("CLUSTER", QVariant.String)]
            )
            out_layer.updateFields()
            features = []
            for i, row in gdf.iterrows():
                feat = QgsFeature()
                feat.setGeometry(QgsGeometry.fromWkt(row.geometry.wkt))
                cluster_label = cluster_map.get(quadrant[i], "Not Significant")
                p_val = float(p_values[i])
                if p_val >= 0.05:
                    cluster_label = "Not Significant"
                feat.setAttributes([float(values[i]), p_val, cluster_label])
                features.append(feat)
            provider.addFeatures(features)
            out_layer.updateExtents()
            QgsProject.instance().addMapLayer(out_layer)
            return {
                "ok": True,
                "message": f"Added '{out_layer.name()}' with per-feature {statistic} results and cluster classification.",
                "layer_name": out_layer.name(),
                "feature_count": len(features),
            }
        except Exception as exc:
            return {"ok": False, "error": f"local_spatial_autocorrelation failed: {exc}"}

    # ------------------------------------------------------------------
    # Line-span QA tools. Ported from internal team utility-QA workflows
    # (not this generic plugin's own logic originally) -- kept functional,
    # not the original tool names, per instruction.
    # ------------------------------------------------------------------

    def tool_compare_line_geometry_changes(self, raw_layer_name, corrected_layer_name, title_field, feeder_field=None):
        import math
        from qgis.core import QgsProject, QgsGeometry, QgsVectorLayer, QgsFeature, QgsField, QgsWkbTypes
        from qgis.PyQt.QtCore import QVariant
        from collections import defaultdict

        raw = self._resolve_layer(raw_layer_name)
        corrected = self._resolve_layer(corrected_layer_name)
        if raw is None or corrected is None:
            return {"ok": False, "error": "Raw or corrected layer not found."}
        try:
            def line_endpoints(geom):
                if geom is None or geom.isEmpty():
                    return None
                pts = []
                if geom.isMultipart():
                    parts = geom.asMultiPolyline()
                    if not parts or not parts[0]:
                        return None
                    pts = parts[0]
                else:
                    pts = geom.asPolyline()
                if len(pts) < 2:
                    return None
                return pts[0], pts[-1]

            def angle_deg(geom):
                ep = line_endpoints(geom)
                if ep is None:
                    return 0.0
                start, end = ep
                return math.degrees(math.atan2(end.y() - start.y(), end.x() - start.x())) % 360

            def dist_m(p1, p2):
                return math.hypot(p2.x() - p1.x(), p2.y() - p1.y())

            raw_wkb = set()
            raw_by_title = defaultdict(list)
            for f in raw.getFeatures():
                geom = f.geometry()
                if geom is None or geom.isEmpty():
                    continue
                raw_wkb.add(bytes(geom.asWkb()))
                title = str(f[title_field]) if title_field in [fld.name() for fld in raw.fields()] else ""
                raw_by_title[title].append(f)

            title_counts = defaultdict(int)
            for f in corrected.getFeatures():
                if title_field in [fld.name() for fld in corrected.fields()]:
                    title_counts[str(f[title_field])] += 1

            out_layer = QgsVectorLayer(f"{QgsWkbTypes.displayString(corrected.wkbType())}?crs={corrected.crs().authid()}", "line_change_qa", "memory")
            provider = out_layer.dataProvider()
            provider.addAttributes(
                [
                    QgsField(title_field, QVariant.String),
                    QgsField("shift_ft", QVariant.Double),
                    QgsField("angle_change_deg", QVariant.Double),
                    QgsField("shift_ge_50m", QVariant.String),
                    QgsField("split", QVariant.String),
                    QgsField("unchanged", QVariant.String),
                ]
            )
            out_layer.updateFields()

            feeder_stats = defaultdict(lambda: {"total": 0, "geo_change": 0, "ang_change": 0, "unchanged": 0})
            new_feats = []
            for f in corrected.getFeatures():
                geom = f.geometry()
                title = str(f[title_field]) if title_field in [fld.name() for fld in corrected.fields()] else ""
                feeder = str(f[feeder_field]) if feeder_field and feeder_field in [fld.name() for fld in corrected.fields()] else "all"
                unchanged = geom is not None and bytes(geom.asWkb()) in raw_wkb
                shift_ft = 0.0
                angle_change = 0.0
                raw_matches = raw_by_title.get(title, [])
                if len(raw_matches) == 1 and geom is not None:
                    raw_geom = raw_matches[0].geometry()
                    c1 = raw_geom.centroid().asPoint()
                    c2 = geom.centroid().asPoint()
                    shift_m = dist_m(c1, c2)
                    shift_ft = shift_m * 3.28084
                    a1, a2 = angle_deg(raw_geom), angle_deg(geom)
                    diff = abs(a2 - a1)
                    angle_change = min(diff, 360 - diff)
                shift_ge_50 = shift_ft >= (50 * 3.28084)
                feeder_stats[feeder]["total"] += 1
                if shift_ge_50:
                    feeder_stats[feeder]["geo_change"] += 1
                if angle_change > 40:
                    feeder_stats[feeder]["ang_change"] += 1
                if unchanged:
                    feeder_stats[feeder]["unchanged"] += 1

                nf = QgsFeature(out_layer.fields())
                nf.setGeometry(geom)
                nf.setAttributes(
                    [
                        title,
                        round(shift_ft, 1),
                        round(angle_change, 1),
                        "Y" if shift_ge_50 else "N",
                        "Y" if title_counts[title] > 1 else "N",
                        "Y" if unchanged else "N",
                    ]
                )
                new_feats.append(nf)
            provider.addFeatures(new_feats)
            out_layer.updateExtents()
            QgsProject.instance().addMapLayer(out_layer)
            return {
                "ok": True,
                "message": f"Added '{out_layer.name()}' with {len(new_feats)} compared features.",
                "layer_name": out_layer.name(),
                "feeder_summary": {k: v for k, v in feeder_stats.items()},
            }
        except Exception as exc:
            return {"ok": False, "error": f"compare_line_geometry_changes failed: {exc}"}

    @staticmethod
    def _confidence_value(is_short, non_terminal, hanging, length_m, hanging_lc_min_length_m=5.0):
        """Faithfully ported rule: confidence flag only applies to 'short'
        classifications; fully-connected (non_terminal) short spans and
        long-hanging stubs are flagged low-confidence for review."""
        if not is_short:
            return ""
        if non_terminal:
            return "LC"
        if hanging:
            return "LC" if length_m > hanging_lc_min_length_m else ""
        return ""

    def tool_classify_span_vegremarks(
        self,
        reference_layer_name,
        input_layer_name,
        overlap_radius_meters=15.0,
        short_field_value="999",
        long_field_value="99",
        length_threshold_meters=20.0,
        output_field="vegremarks",
    ):
        from qgis.core import (
            QgsProject,
            QgsField,
            QgsSpatialIndex,
            QgsFeatureRequest,
            QgsDistanceArea,
            QgsCoordinateTransform,
        )
        from qgis.PyQt.QtCore import QVariant

        ref = self._resolve_layer(reference_layer_name)
        inp = self._resolve_layer(input_layer_name)
        if ref is None or inp is None:
            return {"ok": False, "error": "Reference or input layer not found."}
        try:
            if output_field not in [f.name() for f in inp.fields()]:
                inp.dataProvider().addAttributes([QgsField(output_field, QVariant.String)])
            if "confdnce" not in [f.name() for f in inp.fields()]:
                inp.dataProvider().addAttributes([QgsField("confdnce", QVariant.String)])
            inp.updateFields()

            ref_index = QgsSpatialIndex(ref.getFeatures())
            input_index = QgsSpatialIndex(inp.getFeatures())

            da = QgsDistanceArea()
            da.setEllipsoid("WGS84")
            da.setSourceCrs(inp.crs(), QgsProject.instance().transformContext())

            snap_radius_m = 0.30
            changes = {}
            classified = 0
            for feat in inp.getFeatures():
                geom = feat.geometry()
                if geom is None or geom.isEmpty():
                    continue
                centroid = geom.centroid().asPoint()
                nearby_ref_ids = ref_index.nearestNeighbor(centroid, 1)
                if not nearby_ref_ids:
                    continue
                ref_feat = next(ref.getFeatures(QgsFeatureRequest(nearby_ref_ids[0])), None)
                if ref_feat is None:
                    continue
                ref_centroid = ref_feat.geometry().centroid().asPoint()
                if da.measureLine(centroid, ref_centroid) > overlap_radius_meters:
                    continue

                length_m = da.measureLength(geom)
                is_short = length_m <= length_threshold_meters
                value = short_field_value if is_short else long_field_value

                endpoints = geom.asMultiPolyline()[0] if geom.isMultipart() else geom.asPolyline()
                connected = [False, False]
                if len(endpoints) >= 2:
                    for i, ep in enumerate((endpoints[0], endpoints[-1])):
                        candidate_ids = input_index.nearestNeighbor(ep, 5)
                        for cid in candidate_ids:
                            if cid == feat.id():
                                continue
                            other = next(inp.getFeatures(QgsFeatureRequest(cid)), None)
                            if other is None or other.geometry() is None:
                                continue
                            other_pts = other.geometry().asMultiPolyline()[0] if other.geometry().isMultipart() else other.geometry().asPolyline()
                            if not other_pts:
                                continue
                            if da.measureLine(ep, other_pts[0]) <= snap_radius_m or da.measureLine(ep, other_pts[-1]) <= snap_radius_m:
                                connected[i] = True
                                break
                hanging = not connected[0] and not connected[1]
                non_terminal = connected[0] and connected[1]
                confidence = self._confidence_value(is_short, non_terminal, hanging, length_m)

                changes[feat.id()] = {
                    inp.fields().indexOf(output_field): value,
                    inp.fields().indexOf("confdnce"): confidence,
                }
                classified += 1

            inp.dataProvider().changeAttributeValues(changes)
            inp.triggerRepaint()
            return {
                "ok": True,
                "message": f"Classified {classified} span(s) on '{inp.name()}' (field '{output_field}' + 'confdnce').",
                "classified_count": classified,
                "note": "Substation-cluster fallback for zero-overlap cases was not ported -- unmatched features are left unchanged.",
            }
        except Exception as exc:
            return {"ok": False, "error": f"classify_span_vegremarks failed: {exc}"}

    def tool_validate_span_categories(self, layer_name, vcat_field="gis_vcat", hcat_field="gis_hcat", clearance_field="gis_clrnce"):
        layer = self._resolve_layer(layer_name)
        if layer is None:
            return {"ok": False, "error": f"Layer '{layer_name}' not found."}
        field_names = [f.name() for f in layer.fields()]
        if vcat_field not in field_names or hcat_field not in field_names or clearance_field not in field_names:
            return {"ok": False, "error": f"One or more of {vcat_field}/{hcat_field}/{clearance_field} not found on '{layer.name()}'."}
        try:
            issues = []
            for feat in layer.getFeatures():
                reason = self._validate_span_category_row(feat[vcat_field], feat[hcat_field], feat[clearance_field])
                if reason:
                    issues.append({"fid": feat.id(), "reason": reason})
            return {"ok": True, "checked": layer.featureCount(), "issue_count": len(issues), "issues": issues[:200]}
        except Exception as exc:
            return {"ok": False, "error": f"validate_span_categories failed: {exc}"}

    @staticmethod
    def _validate_span_category_row(vcat_raw, hcat_raw, clr_raw):
        """Faithfully ported QA rule table for gis_vcat/gis_hcat/gis_clrnce
        combinations on utility line features."""

        def to_int(v):
            try:
                return int(v)
            except Exception:
                return None

        vcat, hcat = to_int(vcat_raw), to_int(hcat_raw)
        clr = "" if clr_raw is None else str(clr_raw).strip()
        allowed_range = {"0-5", "5-10", "10-15", "15-20", ">20"}

        if vcat == 6:
            if hcat not in {1, 2, 3, 4}:
                return "gis_hcat must be one of 1,2,3,4 for gis_vcat=6"
            if clr not in allowed_range:
                return f"gis_clrnce must be one of 0-5,5-10,10-15,15-20,>20 (found {clr})"
            return ""
        if vcat in {7, 8}:
            if hcat == 1:
                return "" if clr in allowed_range else f"gis_clrnce must be one of 0-5,5-10,10-15,15-20,>20 (found {clr})"
            if hcat in {2, 3, 4}:
                return "" if clr == "-1" else f"gis_clrnce must be -1 (found {clr})"
            return f"gis_hcat must be one of 1,2,3,4 for gis_vcat={vcat}"
        if vcat == 0:
            if hcat == 1:
                return "" if clr in allowed_range | {"-1"} else f"gis_clrnce must be one of -1,0-5,5-10,10-15,15-20,>20 (found {clr})"
            if hcat in {2, 3, 4}:
                return "" if clr == "-1" else f"gis_clrnce must be -1 (found {clr})"
            return "gis_hcat must be one of 1,2,3,4 for gis_vcat=0"
        if vcat == 9:
            if hcat not in {1, 2, 3, 4, 9}:
                return "gis_hcat must be one of 1,2,3,4,9 for gis_vcat=9"
            return "" if clr == "-1" else f"gis_clrnce must be -1 (found {clr})"
        if vcat == 10:
            if hcat != 10:
                return "gis_hcat must be 10 for gis_vcat=10"
            return "" if clr == "-1" else f"gis_clrnce must be -1 (found {clr})"
        return f"gis_vcat value {vcat} is not allowed by QA rules"

    def tool_correct_span_geometry(self, layer_name):
        from qgis.core import QgsProject, QgsVectorLayer, QgsFeature, QgsGeometry, QgsWkbTypes

        layer = self._resolve_layer(layer_name)
        if layer is None:
            return {"ok": False, "error": f"Layer '{layer_name}' not found."}
        try:
            def same_xy(p1, p2, tol=1e-9):
                return abs(p1.x() - p2.x()) <= tol and abs(p1.y() - p2.y()) <= tol

            def clean_geometry(geom, wkb_type):
                parts = geom.asMultiPolyline() if geom.isMultipart() else [geom.asPolyline()]
                cleaned = []
                for part in parts:
                    deduped = []
                    for pt in part:
                        if not deduped or not same_xy(deduped[-1], pt):
                            deduped.append(pt)
                    if len(deduped) < 2:
                        continue
                    terminal = [deduped[0], deduped[-1]]
                    if same_xy(terminal[0], terminal[1]):
                        continue
                    cleaned.append(terminal)
                if not cleaned:
                    return None
                if QgsWkbTypes.isMultiType(wkb_type):
                    return QgsGeometry.fromMultiPolylineXY(cleaned)
                return QgsGeometry.fromPolylineXY(cleaned[0])

            out_layer = QgsVectorLayer(f"{QgsWkbTypes.displayString(layer.wkbType())}?crs={layer.crs().authid()}", f"{layer.name()}_corrected", "memory")
            provider = out_layer.dataProvider()
            provider.addAttributes(layer.fields())
            out_layer.updateFields()
            new_feats = []
            dropped = 0
            for feat in layer.getFeatures():
                geom = feat.geometry()
                if geom is None or geom.isEmpty():
                    dropped += 1
                    continue
                cleaned_geom = clean_geometry(geom, layer.wkbType())
                if cleaned_geom is None:
                    dropped += 1
                    continue
                nf = QgsFeature(out_layer.fields())
                nf.setGeometry(cleaned_geom)
                nf.setAttributes(feat.attributes())
                new_feats.append(nf)
            provider.addFeatures(new_feats)
            out_layer.updateExtents()
            QgsProject.instance().addMapLayer(out_layer)
            return {
                "ok": True,
                "message": f"Added '{out_layer.name()}' with {len(new_feats)} corrected feature(s), {dropped} dropped as degenerate.",
                "layer_name": out_layer.name(),
            }
        except Exception as exc:
            return {"ok": False, "error": f"correct_span_geometry failed: {exc}"}

    @staticmethod
    def _resolve_priority_value(candidates, order):
        for o in order:
            for c in candidates:
                if c is not None and c == o:
                    return c
        return candidates[0] if candidates else None

    def tool_snap_and_resolve_span_attributes(self, current_layer_name, delivered_layer_name, snap_radius_meters, overlap_radius_meters):
        current = self._resolve_layer(current_layer_name)
        delivered = self._resolve_layer(delivered_layer_name)
        if current is None or delivered is None:
            return {"ok": False, "error": "Current or delivered layer not found."}
        try:
            snap_result = self.host._execute_algorithm_plan(
                "native:snapgeometries",
                {
                    "INPUT": current_layer_name,
                    "REFERENCE_LAYER": delivered_layer_name,
                    "TOLERANCE": float(snap_radius_meters),
                    "BEHAVIOR": 2,
                    "OUTPUT": "memory:",
                },
                f"Snap '{current.name()}' onto '{delivered.name()}' within {snap_radius_meters}m.",
            )
            return {
                "ok": bool(snap_result),
                "message": (
                    f"Ran overlap-scoped snap (snap radius {snap_radius_meters}m; note: full centroid-overlap "
                    f"scoping to {overlap_radius_meters}m was simplified to a direct snap for this port -- "
                    "verify results on a small area first)."
                ),
                "note": "Priority-based attribute resolution (vcat/hcat/clrnce merge across candidate rows) "
                "is available via the _resolve_priority_value helper but needs a defined per-project value-order "
                "to run automatically -- ask for the specific priority order to wire this up fully.",
            }
        except Exception as exc:
            return {"ok": False, "error": f"snap_and_resolve_span_attributes failed: {exc}"}

    def tool_check_background_task_status(self):
        task = getattr(self.host, "_background_task", None)
        label = getattr(self.host, "_background_task_label", None)
        started_at = getattr(self.host, "_background_task_started_at", None)
        if task is None or not task.isRunning():
            return {"ok": True, "running": False, "message": "No install/bind task is currently running."}
        elapsed_seconds = None
        if started_at is not None:
            from datetime import datetime

            elapsed_seconds = (datetime.utcnow() - started_at).total_seconds()
        return {
            "ok": True,
            "running": True,
            "task": label or "background task",
            "elapsed_seconds": elapsed_seconds,
            "note": "Still genuinely running -- heavy installs can take several minutes. If this "
            "has been running for much longer than that (many minutes to hours), it may be stuck; "
            "the user can run /cancel task to free it up and retry.",
        }

    def tool_scan_project_for_issues(self):
        from qgis.core import QgsProject, QgsVectorLayer, QgsFeatureSource

        try:
            layers = list(QgsProject.instance().mapLayers().values())
            issues = []
            crs_by_layer = {}
            name_counts = {}
            for layer in layers:
                name_counts[layer.name()] = name_counts.get(layer.name(), 0) + 1
                try:
                    crs_by_layer[layer.name()] = layer.crs().authid()
                except Exception:
                    pass

            distinct_crs = set(crs_by_layer.values())
            if len(distinct_crs) > 1:
                issues.append(
                    {
                        "type": "crs_mismatch",
                        "severity": "warning",
                        "detail": f"Layers use {len(distinct_crs)} different CRS: " + ", ".join(f"{n} ({c})" for n, c in crs_by_layer.items()),
                        "suggestion": "Mixing CRS across layers can silently produce wrong distances/areas or misaligned overlays -- reproject to a common CRS before combining them.",
                    }
                )

            dupes = [n for n, c in name_counts.items() if c > 1]
            if dupes:
                issues.append(
                    {
                        "type": "duplicate_layer_names",
                        "severity": "info",
                        "detail": f"Duplicate layer names: {', '.join(dupes)}",
                        "suggestion": "Rename one of each pair to avoid ambiguous layer references in future requests.",
                    }
                )

            for layer in layers:
                if not isinstance(layer, QgsVectorLayer):
                    continue
                try:
                    feature_count = layer.featureCount()
                except Exception:
                    feature_count = 0
                # Sample-based validity check (not exhaustive on large layers,
                # to keep this fast enough to run proactively/routinely).
                sample_limit = 2000
                checked = 0
                invalid_count = 0
                for feat in layer.getFeatures():
                    if checked >= sample_limit:
                        break
                    geom = feat.geometry()
                    checked += 1
                    if geom is not None and not geom.isEmpty() and not geom.isGeosValid():
                        invalid_count += 1
                if invalid_count > 0:
                    note = f" (sampled first {sample_limit} of {feature_count})" if feature_count > sample_limit else ""
                    issues.append(
                        {
                            "type": "invalid_geometry",
                            "severity": "warning",
                            "detail": f"'{layer.name()}': {invalid_count} invalid geometr{'y' if invalid_count == 1 else 'ies'} found{note}.",
                            "suggestion": "Run correct_span_geometry or the Processing 'Fix geometries' algorithm before further analysis.",
                        }
                    )
                try:
                    presence = layer.hasSpatialIndex()
                    if presence == QgsFeatureSource.SpatialIndexNotPresent and feature_count > 5000:
                        issues.append(
                            {
                                "type": "missing_spatial_index",
                                "severity": "info",
                                "detail": f"'{layer.name()}' has {feature_count} features and no spatial index.",
                                "suggestion": "Use create_spatial_index (built-in command) to speed up joins and proximity queries on this layer.",
                            }
                        )
                except Exception:
                    pass  # some providers can't report index presence -- skip rather than guess

            return {"ok": True, "layers_scanned": len(layers), "issue_count": len(issues), "issues": issues}
        except Exception as exc:
            return {"ok": False, "error": f"scan_project_for_issues failed: {exc}"}

    def tool_export_session_as_script(self, output_path):
        calls = getattr(self.host, "recent_algorithm_calls", []) or []
        if not calls:
            return {"ok": False, "error": "No successful algorithm calls logged this session yet -- nothing to export."}
        try:
            lines = [
                "# Auto-generated by AI GIS Copilot -- reproduces this session's workflow.",
                "# Review before running on a different project; layer names/paths may need updating.",
                "from qgis.core import QgsProject",
                "import processing",
                "",
            ]
            for i, call in enumerate(calls, start=1):
                summary = call.get("summary") or call.get("algorithm_id")
                lines.append(f"# Step {i}: {summary}")
                lines.append(f"processing.run({call['algorithm_id']!r}, {call['params']!r})")
                lines.append("")
            script = "\n".join(lines)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(script)
            return {
                "ok": True,
                "message": f"Exported {len(calls)} step(s) to {output_path}.",
                "output_path": output_path,
                "step_count": len(calls),
            }
        except Exception as exc:
            return {"ok": False, "error": f"export_session_as_script failed: {exc}"}

    def tool_apply_symbology(self, layer_name, field_name, mode, num_classes=5, color_ramp_name="Spectral"):
        from qgis.core import (
            QgsCategorizedSymbolRenderer,
            QgsGraduatedSymbolRenderer,
            QgsSymbol,
            QgsStyle,
        )

        layer = self._resolve_layer(layer_name)
        if layer is None:
            return {"ok": False, "error": f"Layer '{layer_name}' not found."}
        if field_name not in [f.name() for f in layer.fields()]:
            return {"ok": False, "error": f"Field '{field_name}' not found on '{layer.name()}'."}
        try:
            ramp = QgsStyle.defaultStyle().colorRamp(color_ramp_name)
            if ramp is None:
                ramp = QgsStyle.defaultStyle().colorRamp("Spectral")

            if mode == "categorized":
                values = sorted({f[field_name] for f in layer.getFeatures()}, key=lambda v: (v is None, str(v)))
                symbol = QgsSymbol.defaultSymbol(layer.geometryType())
                categories = QgsCategorizedSymbolRenderer.createCategories(values, symbol, layer, field_name)
                renderer = QgsCategorizedSymbolRenderer(field_name, categories)
                if ramp is not None:
                    renderer.updateColorRamp(ramp)
                layer.setRenderer(renderer)
                class_count = len(categories)
            elif mode == "graduated":
                symbol = QgsSymbol.defaultSymbol(layer.geometryType())
                # Mode enum moved between a flat name and a nested .Mode
                # scope across QGIS versions -- same pattern that broke
                # SpatialIndexPresence earlier, so handled the same
                # defensive way rather than risk it again.
                try:
                    graduated_mode = QgsGraduatedSymbolRenderer.Mode.Quantile
                except AttributeError:
                    graduated_mode = QgsGraduatedSymbolRenderer.Quantile
                renderer = QgsGraduatedSymbolRenderer.createRenderer(
                    layer, field_name, int(num_classes), graduated_mode, symbol, ramp
                )
                if renderer is None:
                    return {"ok": False, "error": "Could not build a graduated renderer -- is the field numeric?"}
                layer.setRenderer(renderer)
                class_count = int(num_classes)
            else:
                return {"ok": False, "error": f"Unknown mode '{mode}', use 'categorized' or 'graduated'."}

            layer.triggerRepaint()
            return {"ok": True, "message": f"Applied {mode} symbology to '{layer.name()}' on field '{field_name}' ({class_count} classes)."}
        except Exception as exc:
            return {"ok": False, "error": f"apply_symbology failed: {exc}"}

    def tool_create_map_layout(
        self,
        layout_name,
        layer_names=None,
        title=None,
        include_legend=True,
        include_scale_bar=True,
        export_pdf_path=None,
    ):
        from qgis.core import (
            QgsProject,
            QgsPrintLayout,
            QgsLayoutItemMap,
            QgsLayoutItemLegend,
            QgsLayoutItemScaleBar,
            QgsLayoutItemLabel,
            QgsLayoutPoint,
            QgsLayoutSize,
            QgsUnitTypes,
            QgsLayoutExporter,
            QgsRectangle,
        )

        try:
            project = QgsProject.instance()
            if layer_names:
                layers = [self._resolve_layer(n) for n in layer_names]
                layers = [l for l in layers if l is not None]
                if not layers:
                    return {"ok": False, "error": "None of the given layer_names were found."}
            else:
                layers = [l for l in project.mapLayers().values() if l.isValid()]
                if not layers:
                    return {"ok": False, "error": "No layers in the project to include."}

            extent = QgsRectangle(layers[0].extent())
            for l in layers[1:]:
                extent.combineExtentWith(l.extent())

            layout = QgsPrintLayout(project)
            layout.initializeDefaults()
            layout.setName(layout_name)

            mm = QgsUnitTypes.LayoutMillimeters
            y_cursor = 10.0

            if title:
                label = QgsLayoutItemLabel(layout)
                label.setText(title)
                label.setFont(label.font())
                label.adjustSizeToText()
                label.attemptMove(QgsLayoutPoint(10, y_cursor, mm))
                layout.addLayoutItem(label)
                y_cursor += 15.0

            map_item = QgsLayoutItemMap(layout)
            map_item.attemptMove(QgsLayoutPoint(10, y_cursor, mm))
            map_item.attemptResize(QgsLayoutSize(220, 150, mm))
            map_item.setExtent(extent)
            map_item.setLayers(layers)
            layout.addLayoutItem(map_item)

            if include_legend:
                legend = QgsLayoutItemLegend(layout)
                legend.setLinkedMap(map_item)
                legend.attemptMove(QgsLayoutPoint(235, y_cursor, mm))
                layout.addLayoutItem(legend)

            if include_scale_bar:
                scale_bar = QgsLayoutItemScaleBar(layout)
                scale_bar.setLinkedMap(map_item)
                scale_bar.applyDefaultSize()
                scale_bar.attemptMove(QgsLayoutPoint(10, y_cursor + 155, mm))
                layout.addLayoutItem(scale_bar)

            project.layoutManager().addLayout(layout)

            result = {
                "ok": True,
                "message": f"Created layout '{layout_name}' with {len(layers)} layer(s), added to the project's Layout Manager.",
                "layout_name": layout_name,
            }
            if export_pdf_path:
                exporter = QgsLayoutExporter(layout)
                export_result = exporter.exportToPdf(export_pdf_path, QgsLayoutExporter.PdfExportSettings())
                try:
                    success_value = QgsLayoutExporter.ExportResult.Success
                except AttributeError:
                    success_value = QgsLayoutExporter.Success
                if export_result == success_value:
                    result["message"] += f" Exported to {export_pdf_path}."
                    result["pdf_path"] = export_pdf_path
                else:
                    result["message"] += f" PDF export did not succeed (code {export_result})."
            return result
        except Exception as exc:
            return {"ok": False, "error": f"create_map_layout failed: {exc}"}

    def tool_capture_and_analyze_viewport(self, question):
        import base64
        import os
        import tempfile

        gemini_key = (getattr(self.host, "agent_settings", {}) or {}).get("gemini_key", "").strip()
        if not gemini_key:
            return {
                "ok": False,
                "error": "This needs a Gemini API key for vision, even if your primary provider is "
                "something else. Set one with /set gemini_key <key> (free at "
                "https://aistudio.google.com/apikey), then try again.",
            }
        gemini_model = (getattr(self.host, "agent_settings", {}) or {}).get("gemini_model", "gemini-flash-latest")

        try:
            canvas = self.host.iface.mapCanvas()
            if canvas is None:
                return {"ok": False, "error": "Map canvas is unavailable."}
            temp_path = os.path.join(tempfile.gettempdir(), "ai_gis_copilot_viewport.png")
            canvas.saveAsImage(temp_path)
            with open(temp_path, "rb") as f:
                image_b64 = base64.b64encode(f.read()).decode("ascii")
        except Exception as exc:
            return {"ok": False, "error": f"Could not capture the map canvas: {exc}"}

        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent?key={gemini_key}"
            payload = {
                "contents": [
                    {
                        "parts": [
                            {"inlineData": {"mimeType": "image/png", "data": image_b64}},
                            {"text": question},
                        ]
                    }
                ]
            }
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (compatible; AI-GIS-Copilot/1.0)",
            }
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                decoded = json.loads(resp.read().decode("utf-8"))
            candidate = decoded["candidates"][0]
            parts = candidate.get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts).strip()
            if not text:
                return {"ok": False, "error": f"Vision model returned no text: {decoded}"}
            return {"ok": True, "analysis": text}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            return {"ok": False, "error": f"Vision request failed (HTTP {exc.code}): {detail[:500]}"}
        except Exception as exc:
            return {"ok": False, "error": f"capture_and_analyze_viewport failed: {exc}"}

    def tool_search_gis_mcp_tools(self, keyword, limit=10):
        if self.mcp_bridge is None or not self.mcp_bridge.bound:
            return {"ok": False, "error": "gis-mcp isn't bound. Tell the user to run /bind gis-mcp first."}
        keyword_l = (keyword or "").lower()
        matches = []
        for t in self.mcp_bridge.tools:
            hay = f"{t.get('name', '')} {t.get('description', '')}".lower()
            if keyword_l in hay:
                matches.append({"name": t.get("name"), "description": t.get("description", ""), "parameters": t.get("inputSchema", {})})
                if len(matches) >= limit:
                    break
        return {"ok": True, "matches": matches, "count": len(matches)}

    def tool_call_gis_mcp_tool(self, tool_name, arguments):
        if self.mcp_bridge is None or not self.mcp_bridge.bound:
            return {"ok": False, "error": "gis-mcp isn't bound. Tell the user to run /bind gis-mcp first."}
        try:
            return self.mcp_bridge.call_tool(tool_name, arguments)
        except Exception as exc:
            return {"ok": False, "error": f"gis-mcp tool '{tool_name}' failed: {exc}"}

    def tool_get_elevation_at_points(self, points):
        try:
            locations = "|".join(f"{lat},{lon}" for lon, lat in points)
            url = "https://api.open-elevation.com/api/v1/lookup?locations=" + urllib.parse.quote(locations)
            body = _http_get(url, timeout=20)
            decoded = json.loads(body)
            results = [
                {"longitude": r["longitude"], "latitude": r["latitude"], "elevation_m": r["elevation"]}
                for r in decoded.get("results", [])
            ]
            if not results:
                return {"ok": False, "error": "No elevation results returned."}
            return {"ok": True, "results": results}
        except Exception as exc:
            return {"ok": False, "error": f"get_elevation_at_points failed: {exc}"}

    def _validate_pyqgis_script_ast(self, code):
        """Real static analysis via Python's ast module, not string
        matching. Confirmed this closes a real gap the old denylist had:
        a classic sandbox-escape like ().__class__.__bases__[0].__subclasses__()
        contains none of the old denylist's literal substrings (no 'eval',
        no 'import', no 'exec') and would have sailed straight through."""
        import ast as _ast

        blocked_calls = {"eval", "exec", "compile", "__import__", "open", "globals", "locals", "vars", "getattr", "setattr", "delattr", "input"}
        try:
            tree = _ast.parse(code, mode="exec")
        except SyntaxError as exc:
            return f"Syntax error: {exc}"
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.Import, _ast.ImportFrom)):
                return "Script contains an import statement -- not allowed. QgsProject, iface, and processing are already provided; nothing else is needed."
            if isinstance(node, _ast.Call):
                func = node.func
                name = func.id if isinstance(func, _ast.Name) else (func.attr if isinstance(func, _ast.Attribute) else None)
                if name in blocked_calls:
                    return f"Script calls '{name}(...)', which is not allowed."
            if isinstance(node, _ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__"):
                return f"Script accesses '{node.attr}', a dunder attribute -- a common sandbox-escape technique, not allowed."
            if isinstance(node, _ast.Name) and node.id.startswith("__") and node.id.endswith("__"):
                return f"Script references '{node.id}', not allowed."
        return None

    def tool_run_custom_pyqgis_script(self, description, code):
        # Human review of the exact code (via the confirmation prompt) is
        # still the primary safety mechanism -- this AST pass is a real
        # static-analysis backstop against accidental mistakes and common
        # escape patterns, not a claim that this is a fully secure sandbox
        # against someone determined to defeat it from inside a single
        # Python process.
        validation_error = self._validate_pyqgis_script_ast(code)
        if validation_error:
            return {"ok": False, "error": f"Script blocked: {validation_error}"}
        try:
            from qgis.core import QgsProject
            import processing as _processing

            restricted_globals = {
                "__builtins__": {
                    "range": range, "len": len, "str": str, "int": int, "float": float, "bool": bool,
                    "list": list, "dict": dict, "set": set, "tuple": tuple, "print": print,
                    "enumerate": enumerate, "zip": zip, "sorted": sorted, "min": min, "max": max,
                    "sum": sum, "abs": abs, "round": round, "True": True, "False": False, "None": None,
                },
                "QgsProject": QgsProject,
                "iface": self.host.iface,
                "processing": _processing,
            }
            local_vars = {}
            exec(code, restricted_globals, local_vars)
            output = local_vars.get("result", "Script ran with no explicit 'result' variable set.")
            return {"ok": True, "message": f"Ran: {description}", "result": str(output)[:2000]}
        except Exception as exc:
            import traceback

            return {"ok": False, "error": f"Script failed: {exc}", "traceback": traceback.format_exc()[-1500:]}

    def tool_describe_algorithm_parameters(self, algorithm_id):
        try:
            from qgis.core import QgsApplication

            alg = QgsApplication.processingRegistry().algorithmById(algorithm_id)
        except Exception as exc:
            return {"ok": False, "error": f"Could not access the algorithm registry: {exc}"}
        if alg is None:
            return {"ok": False, "error": f"No algorithm found with id '{algorithm_id}'. Use search_algorithms first."}
        params = []
        try:
            for p in alg.parameterDefinitions():
                is_optional = False
                try:
                    is_optional = bool(p.flags() & p.Flag.FlagOptional)
                except Exception:
                    try:
                        is_optional = bool(p.flags() & p.FlagOptional)
                    except Exception:
                        is_optional = False
                params.append({"name": p.name(), "description": p.description(), "required": not is_optional})
        except Exception as exc:
            return {"ok": False, "error": f"Could not list parameters: {exc}"}
        return {"ok": True, "algorithm_id": algorithm_id, "display_name": alg.displayName(), "parameters": params}

    def tool_profile_layer_fields(self, layer_name):
        layer = self._resolve_layer(layer_name)
        if layer is None:
            return {"ok": False, "error": f"Layer '{layer_name}' not found."}
        try:
            SAMPLE_LIMIT = 2000
            field_names = [f.name() for f in layer.fields()]
            field_types = {f.name(): f.typeName() for f in layer.fields()}
            values_by_field = {name: [] for name in field_names}
            null_counts = {name: 0 for name in field_names}
            checked = 0
            invalid_geom_count = 0
            for feat in layer.getFeatures():
                if checked >= SAMPLE_LIMIT:
                    break
                checked += 1
                for name in field_names:
                    v = feat[name]
                    if v is None or (isinstance(v, str) and v == ""):
                        null_counts[name] += 1
                    elif len(values_by_field[name]) < 200:
                        values_by_field[name].append(v)
                geom = feat.geometry()
                if geom is not None and not geom.isEmpty() and not geom.isGeosValid():
                    invalid_geom_count += 1

            profile = []
            for name in field_names:
                unique_sample = list({str(v) for v in values_by_field[name]})[:8]
                profile.append(
                    {
                        "field": name,
                        "type": field_types.get(name, "unknown"),
                        "null_count": null_counts[name],
                        "sample_values": unique_sample,
                    }
                )
            note = f" (sampled first {SAMPLE_LIMIT})" if layer.featureCount() > SAMPLE_LIMIT else ""
            return {
                "ok": True,
                "layer": layer.name(),
                "features_checked": checked,
                "note": note.strip(),
                "fields": profile,
                "invalid_geometry_count": invalid_geom_count,
            }
        except Exception as exc:
            return {"ok": False, "error": f"profile_layer_fields failed: {exc}"}

    def tool_query_features_by_spatial_predicate(self, layer_name, reference_layer_name, predicate):
        from qgis.core import QgsProject, QgsVectorLayer, QgsFeature, QgsSpatialIndex, QgsFeatureRequest, QgsWkbTypes

        layer = self._resolve_layer(layer_name)
        ref_layer = self._resolve_layer(reference_layer_name)
        if layer is None or ref_layer is None:
            return {"ok": False, "error": "Layer or reference_layer not found."}
        try:
            ref_index = QgsSpatialIndex(ref_layer.getFeatures())
            matched = []
            for feat in layer.getFeatures():
                geom = feat.geometry()
                if geom is None or geom.isEmpty():
                    continue
                candidate_ids = ref_index.intersects(geom.boundingBox())
                is_match = False
                for cid in candidate_ids:
                    ref_feat = next(ref_layer.getFeatures(QgsFeatureRequest(cid)), None)
                    if ref_feat is None or ref_feat.geometry() is None:
                        continue
                    ref_geom = ref_feat.geometry()
                    if predicate == "intersects" and geom.intersects(ref_geom):
                        is_match = True
                    elif predicate == "within" and geom.within(ref_geom):
                        is_match = True
                    elif predicate == "contains" and geom.contains(ref_geom):
                        is_match = True
                    if is_match:
                        break
                if is_match:
                    matched.append(feat)

            out_layer = QgsVectorLayer(f"{QgsWkbTypes.displayString(layer.wkbType())}?crs={layer.crs().authid()}", f"{layer.name()}_{predicate}_match", "memory")
            provider = out_layer.dataProvider()
            provider.addAttributes(layer.fields())
            out_layer.updateFields()
            new_feats = []
            for f in matched:
                nf = QgsFeature(out_layer.fields())
                nf.setGeometry(f.geometry())
                nf.setAttributes(f.attributes())
                new_feats.append(nf)
            provider.addFeatures(new_feats)
            out_layer.updateExtents()
            QgsProject.instance().addMapLayer(out_layer)
            return {"ok": True, "message": f"Found {len(matched)} feature(s) in '{layer.name()}' that {predicate} '{ref_layer.name()}'.", "match_count": len(matched), "layer_name": out_layer.name()}
        except Exception as exc:
            return {"ok": False, "error": f"query_features_by_spatial_predicate failed: {exc}"}

    def tool_undo_last_layer_edit(self, layer_name):
        edits = getattr(self.host, "recent_layer_edits", []) or []
        matching_indices = [i for i, e in enumerate(edits) if e["layer"].lower() == layer_name.lower()]
        if not matching_indices:
            return {"ok": False, "error": f"No tracked edits for '{layer_name}' this session."}
        last_idx = matching_indices[-1]
        last = edits[last_idx]
        if last["action"] == "add_field":
            result = self.tool_delete_field(layer_name, last["field_name"])
            if result.get("ok"):
                del edits[last_idx]  # remove the original add_field entry; tool_delete_field above already logged its own entry, which is accurate
            return result
        if last["action"] == "delete_field":
            return {
                "ok": False,
                "error": f"Cannot undo deleting field '{last['field_name']}' -- its values are gone, not just "
                "hidden. Only the last add_field can be cleanly undone.",
            }
        return {"ok": False, "error": f"Don't know how to undo action '{last['action']}'."}

    def tool_auto_fix_geometry_issues(self, layer_name):
        layer = self._resolve_layer(layer_name)
        if layer is None:
            return {"ok": False, "error": f"Layer '{layer_name}' not found."}
        ok = self.host._execute_algorithm_plan(
            "native:fixgeometries",
            {"INPUT": layer_name, "OUTPUT": "memory:"},
            f"Auto-fixing geometry issues on '{layer.name()}'.",
        )
        return {"ok": bool(ok), "message": f"Ran geometry auto-fix on '{layer.name()}'."}

    def tool_compute_water_or_burn_index(self, raster_layer_name, index, band_a, band_b):
        from qgis.core import QgsProject, QgsRasterLayer, QgsRasterCalculator, QgsRasterCalculatorEntry
        import os
        import tempfile

        layer = None
        for lyr in QgsProject.instance().mapLayers().values():
            if lyr.name().strip().lower() == raster_layer_name.strip().lower() and isinstance(lyr, QgsRasterLayer):
                layer = lyr
                break
        if layer is None:
            return {"ok": False, "error": f"Raster layer '{raster_layer_name}' not found."}
        try:
            entry_a = QgsRasterCalculatorEntry()
            entry_a.ref = f"{layer.name()}@{band_a}"
            entry_a.raster = layer
            entry_a.bandNumber = band_a
            entry_b = QgsRasterCalculatorEntry()
            entry_b.ref = f"{layer.name()}@{band_b}"
            entry_b.raster = layer
            entry_b.bandNumber = band_b
            formula = f'("{entry_a.ref}" - "{entry_b.ref}") / ("{entry_a.ref}" + "{entry_b.ref}")'
            suffix = "ndwi" if index == "ndwi" else "nbr"
            output_path = os.path.join(tempfile.gettempdir(), f"{layer.name()}_{suffix}.tif")
            calc = QgsRasterCalculator(formula, output_path, "GTiff", layer.extent(), layer.width(), layer.height(), [entry_a, entry_b])
            result = calc.processCalculation()
            if result != 0:
                return {"ok": False, "error": f"{suffix.upper()} calculation failed (code {result})."}
            out_layer = QgsRasterLayer(output_path, f"{layer.name()}_{suffix.upper()}")
            if not out_layer.isValid():
                return {"ok": False, "error": f"{suffix.upper()} raster computed but failed to load as a layer."}
            QgsProject.instance().addMapLayer(out_layer)
            return {"ok": True, "message": f"Computed {suffix.upper()} from '{layer.name()}' and added as a new layer.", "output_path": output_path}
        except Exception as exc:
            return {"ok": False, "error": f"compute_water_or_burn_index failed: {exc}"}

    def tool_fetch_osm_features(self, place_name, osm_tag):
        from qgis.core import QgsProject, QgsVectorLayer, QgsFeature, QgsGeometry

        try:
            if "=" not in osm_tag:
                return {"ok": False, "error": "osm_tag must be key=value, e.g. 'amenity=hospital'."}
            key, value = osm_tag.split("=", 1)

            # Resolve the place to a bounding polygon area first (Nominatim), then
            # query Overpass for tagged features within that area -- both free, keyless.
            area_url = "https://nominatim.openstreetmap.org/search?format=json&limit=1&q=" + urllib.parse.quote(place_name)
            area_body = _http_get(area_url, headers={"User-Agent": "AI-GIS-Copilot/1.0 (QGIS plugin)"})
            area_results = json.loads(area_body)
            if not area_results:
                return {"ok": False, "error": f"No OpenStreetMap place found for '{place_name}'."}
            bbox = area_results[0]["boundingbox"]  # [south, north, west, east] as strings
            south, north, west, east = (float(v) for v in bbox)

            overpass_query = (
                f'[out:json][timeout:25];(node["{key}"="{value}"]({south},{west},{north},{east});'
                f'way["{key}"="{value}"]({south},{west},{north},{east}););out center;'
            )
            overpass_url = "https://overpass-api.de/api/interpreter"
            req = urllib.request.Request(
                overpass_url,
                data=("data=" + urllib.parse.quote(overpass_query)).encode("utf-8"),
                headers={"User-Agent": "Mozilla/5.0 (compatible; AI-GIS-Copilot/1.0)", "Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=40) as resp:
                decoded = json.loads(resp.read().decode("utf-8"))

            out_layer = QgsVectorLayer("Point?crs=EPSG:4326", f"osm_{key}_{value}", "memory")
            provider = out_layer.dataProvider()
            new_feats = []
            for el in decoded.get("elements", []):
                lat = el.get("lat") or (el.get("center", {}) or {}).get("lat")
                lon = el.get("lon") or (el.get("center", {}) or {}).get("lon")
                if lat is None or lon is None:
                    continue
                feat = QgsFeature()
                feat.setGeometry(QgsGeometry.fromWkt(f"POINT({lon} {lat})"))
                new_feats.append(feat)
            provider.addFeatures(new_feats)
            out_layer.updateExtents()
            QgsProject.instance().addMapLayer(out_layer)
            return {"ok": True, "message": f"Added {len(new_feats)} '{osm_tag}' feature(s) near '{place_name}'.", "feature_count": len(new_feats), "layer_name": out_layer.name()}
        except Exception as exc:
            return {"ok": False, "error": f"fetch_osm_features failed: {exc}"}
