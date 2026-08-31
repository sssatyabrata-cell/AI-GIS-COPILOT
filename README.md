# AI GIS Copilot (QGIS Plugin)

`AI GIS Copilot` is a chat-style QGIS plugin for QGIS Q&A and operation execution.

## Agent mode (v0.6) -- free, real tool-calling AI

The plugin now has a real tool-calling agent loop: your prompt goes to an LLM
along with a set of live tools (list/inspect layers, search the full QGIS
Processing algorithm registry, run any algorithm, edit fields, export, zoom,
web search, Wikipedia lookup). The LLM decides which tools to call, the
plugin executes them against your real project, and the LLM keeps going
until it has a final answer -- instead of matching your words against a
fixed list of hardcoded phrases.

**All supported LLM backends are free:**

| Provider | Setup | Notes |
|---|---|---|
| Gemini | `/set provider gemini` then `/set gemini_key <key>` | Free key at https://aistudio.google.com/apikey. Best quality of the free options. |
| Groq | `/set provider groq` then `/set groq_key <key>` | Free key at https://console.groq.com/keys. Fast, strong open models. |
| Ollama | `/set provider ollama` | 100% local, no key, no internet. Weakest tool-calling reliability of the three -- use as an offline fallback. |

Check current setup any time with `/agent status`.

Any tool that edits, deletes, or writes data (running a Processing
algorithm, adding/deleting a field, exporting) always pauses for your
explicit `/yes` / `/no` confirmation before running -- the agent never
silently modifies your project.

If no agent provider is configured, or the agent errors on a request, the
plugin falls back to the built-in deterministic handlers described below.

## What we took from GIS-MCP, and what we built instead (v0.7)

Reviewed https://github.com/mahdin75/gis-mcp against this plugin's architecture. Verdict, using their own comparison structure:

| GIS-MCP component | Verdict for this plugin |
|---|---|
| MCP-style tool architecture (LLM reasoning -> tools -> execution) | Already built (v0.6 agent) |
| Multi-step workflow planning | Already built -- the agent loop chains tool calls until done |
| Multiple LLM providers | Already built -- Gemini/Groq/Ollama fallback |
| Vector geometry ops (buffer, intersection, union, dissolve, clip, centroid, convex hull, etc.) | **Not duplicated.** QGIS's own Processing registry (exposed via `search_algorithms` + `run_processing_algorithm`) already covers nearly all of this as native QGIS algorithms -- reimplementing it via Shapely/GeoPandas would be redundant and would require bundling new pip dependencies into QGIS's Python. |
| Raster ops, PySAL spatial statistics | Same reasoning -- QGIS Processing (native/GDAL/GRASS providers) covers most of it; genuine PySAL-specific stats (LISA, Getis-Ord) are a real gap, noted below for a future pass. |
| Satellite imagery / open geospatial data fetch (Sentinel-2, Landsat, admin boundaries) | **This was a real, non-redundant gap.** Added `search_satellite_imagery` (free Sentinel-2/Landsat via Element84's Earth Search STAC API) and `fetch_boundary_from_osm` (free OpenStreetMap boundary lookup, adds a real layer to your project). |
| A validation layer (Plan -> Validate -> Execute -> Verify) | **Real gap, now added.** `run_processing_algorithm` checks distance-based operations (buffer, etc.) against the input layer's CRS before running. If the layer is still in a geographic CRS (degrees), it refuses and tells the agent to reproject first instead of silently producing a garbage result -- exactly the "500m buffer on a WGS84 layer" scenario from the suggestion doc. |

Not pulled in: PySAL spatial statistics (LISA, Getis-Ord, spatial regression) and general-purpose Shapely/GeoPandas geometry ops, since QGIS Processing already covers the geometry side and PySAL would need new pip dependencies bundled into QGIS's Python -- worth a dedicated follow-up if the team needs those specific stats.

## GIS-MCP integration -- read from the actual source, not marketing copy (v0.8)

Cloned and read the real repository (https://github.com/mahdin75/gis-mcp) module by module -- `src/gis_mcp/{shapely,geopandas,rasterio,pyproj,pysal}_functions.py`, `docs/llms.txt`, and their worked examples -- rather than relying on search-result summaries. Corrected, code-verified counts: **87 actual tool-decorated functions** (28 Shapely, 11 GeoPandas, 18 Rasterio, 11 PyProj, 18 PySAL).

Bringing this across bit by bit rather than as one dump. Progress so far:

| Module (real function count) | Verdict | Status |
|---|---|---|
| Shapely (28), GeoPandas (11) -- vector geometry/I-O ops | Redundant: QGIS's own Processing registry (native/GDAL/GRASS providers, exposed via `search_algorithms` + `run_processing_algorithm`) already covers buffer/clip/dissolve/union/intersection/join/explode/etc. | Skipped -- no new value to port |
| PyProj (11) -- CRS transforms, UTM zone lookup, geodetic distance/area | Genuine value, but their implementation needs the `pyproj` package. Rebuilt the two most-used pieces (UTM zone auto-detection, geodesic distance) on QGIS's own native `QgsCoordinateTransform`/`QgsDistanceArea` APIs instead -- same result, zero new dependencies. | **Ported** (`suggest_projected_crs`, `geodetic_distance`) |
| Rasterio (18) -- raster ops, NDVI, zonal stats, hillshade, etc. | Mostly redundant with QGIS's native/GDAL raster algorithms (zonal stats, hillshade, clip, resample, reproject all exist as Processing algorithms). `compute_ndvi` was the one genuinely convenient shortcut worth having as a one-call tool instead of asking the agent to hand-write a raster calculator formula. | **Ported** (`compute_ndvi`, via QGIS's native `QgsRasterCalculator` -- no new dependency) |
| PySAL (18) -- Moran's I, Getis-Ord, LISA, spatial weights, spatial regression | **Real, unfilled gap** -- no QGIS-native equivalent exists for spatial autocorrelation statistics. Their implementation needs `pysal`/`esda`/`libpysal`, real new Python dependencies with their own transitive requirements. | **Ported (v0.9)** -- see below |

Also verified directly from their own docs (`docs/examples/crs_area_distance_analysis.md`): GIS-MCP handles the CRS-safety problem by *prompting the LLM to remember to check and reproject manually* -- it isn't enforced in the tool layer. This plugin's `run_processing_algorithm` validation (added in v0.7) enforces it in code before execution, which is a genuine advantage over their actual implementation, not just a documentation claim.

Their admin-boundary tool uses GADM (via `pygadm`, a real new dependency) rather than OpenStreetMap; this plugin's `fetch_boundary_from_osm` (v0.7) trades some authoritativeness for staying dependency-free. Their satellite imagery tool downloads and clips actual GeoTIFFs (Microsoft Planetary Computer); this plugin's `search_satellite_imagery` (v0.7) currently only searches/returns metadata -- actual download+clip is a reasonable next bit if wanted.

## PySAL spatial statistics (v0.9) -- new dependency, by explicit decision

Team decided to accept new Python dependencies for this one, since there's no QGIS-native way to get it otherwise. Run `/install pysal` in the chat to install `geopandas`, `pandas`, `libpysal`, `esda` into QGIS's Python (restart QGIS after). If they're not installed, the tools below fail with a clear `install_hint` instead of a confusing traceback.

Two tools, covering the 6 most commonly used of the real 18 PySAL functions (weights construction is handled internally, matching gis-mcp's own `DistanceBand` + row-standardization + island-handling logic, verified line-for-line against their source):

- `spatial_autocorrelation` -- global Moran's I, Geary's C, or Getis-Ord G on a numeric field. One summary statistic for the whole layer.
- `local_spatial_autocorrelation` -- per-feature Local Moran's I (LISA) or Getis-Ord Gi*, materialized as an actual new layer with a `CLUSTER` field (High-High / Low-Low / High-Low / Low-High / Not Significant) so results are immediately mappable -- gis-mcp only returns raw JSON arrays, this adds real map output since the agent runs inside a live QGIS session.

Not ported: join counts, Gamma statistic, ADBSCAN, and the standalone weights-construction/OLS-regression tools (6 of the 18) -- the two consolidated tools above cover the statistics a GIS team actually reaches for day to day; the rest are a fast follow if needed.

Tested end-to-end against the real `esda`/`libpysal` libraries (not just written and assumed correct): built a synthetic dataset with a deliberate spatial cluster, confirmed global Moran's I correctly detected it (I=0.85, p<0.001) and Local Moran's I correctly labeled the actual clustered points "High-High".

## Web search: why not Google, and the memory question (v0.10)

**Why not Google Search:** checked current pricing/availability directly rather than assume. Google's Custom Search JSON API is **closed to new signups as of 2026 and fully shuts down January 1, 2027** -- not just costly, a dead end. Every other "real search" option (Brave, SerpAPI, Tavily, Exa) caps free usage at 100-2,000 queries/month and most now require a card on file even for the free tier. The only genuinely free, no-signup, no-card option in 2026 is DuckDuckGo's public interface -- which is what `web_search` already used. Left as the always-free default; added an **optional** upgrade path for anyone who wants it: `/set brave_key <key>` (free tier at brave.com/search/api) makes `web_search` try Brave first and fall back to DuckDuckGo automatically if the key is missing, invalid, or rate-limited.

**Does the Copilot learn on its own?** Direct answer: no, not in the sense of the underlying model (Gemini/Groq) updating its own weights from your usage -- no deployment here does that. But the plugin already had a persistent local memory (`learned_routes`, `history_store`, saved to disk across sessions) that only the old legacy pipeline used -- the new agent never saw it. Added `recall_similar_past_action`, which lets the agent check this team's own history of past requests before asking you for something you may have already specified before (a commonly-used field name, CRS, distance). This is genuine "gets more efficient for your team's recurring patterns over time" behavior -- just not literal model retraining, and it's honest to be clear about that distinction.

Tested the matching logic against a realistic fake history and caught a real false-positive before shipping: a threshold-only match let a single shared stopword-adjacent token ("what") wrongly link two unrelated queries. Fixed by requiring at least 2 shared meaningful tokens, not just a score threshold -- verified against both the intended match (two past buffer requests correctly surfaced) and the false-positive case (now correctly excluded).

## Live gis-mcp binding -- optional, opt-in, off by default (v0.11)

This is the real thing: `mcp_bridge.py` spawns the actual gis-mcp server as a subprocess and speaks the real MCP protocol to it over HTTP, so all ~88 of its tools become available to the agent -- not just the handful selectively ported in v0.9. Entirely separate from (and additive to) the manually-ported tools; nothing about default behavior changes unless you explicitly opt in.

**Setup:**
1. `/install gis-mcp` -- installs gis-mcp and its core dependencies (shapely, geopandas, rasterio, pyproj, pysal, fastmcp) into QGIS's Python. Restart QGIS after.
2. `/bind gis-mcp` -- starts the server as a local subprocess (HTTP transport, localhost only) and connects. Takes up to ~20s; the chat may be unresponsive while it starts.
3. `/mcp status` to check, `/unbind gis-mcp` to disconnect and stop the process cleanly.

Bridged tools appear to the agent prefixed `gismcp__` (e.g. `gismcp__buffer`) so their origin is unambiguous and they can never collide with the plugin's own tool names.

**Verified against a real running instance before shipping, not assumed from docs** -- the MCP-over-HTTP protocol turned out to have real specifics that weren't guessable: the response comes back wrapped in `text/event-stream` (SSE) even for a plain JSON-RPC POST, an `Mcp-Session-Id` header from the `initialize` response must be sent on every subsequent call, and a `notifications/initialized` message is required before `tools/list` will work. Installed and ran the actual server, confirmed the full handshake, discovered all 88 real tools, executed a real `buffer` call end-to-end and got back a correct computed polygon, and confirmed the subprocess actually terminates on `/unbind` with no leaked process -- all tested against production code, not a mock.

**Trade-offs to know before deciding whether to keep using this:** a second process alongside QGIS that can crash or fail to start; the full gis-mcp dependency stack instead of just the pieces actually used; every bridged tool is treated as destructive (confirmation required) since gis-mcp's own schemas don't carry that distinction and several of them write files.

## Built-in deterministic handlers (v0.4)

- Built-in direct handlers (no API key required):
  - Polygon validity check (`native:checkvalidity`)
  - Fix geometries (`native:fixgeometries`)
  - Span disconnected diagnosis
  - Buffer creation
  - Dissolve
  - Clip / Intersection / Union (active layer + first other layer)
  - Reproject to EPSG
  - Convex hull
  - Centroids
  - Multipart to singleparts
  - Poles within distance of roads export
  - Merge loaded layers
  - Split by field into value-based folders/files
  - Map refresh, processing toolbox open, snapping enable, snap geometries
  - Null geometry create and clear
- Safety and memory:
  - Pending action confirmation (`/yes`, `/no`) for null-geometry cleanup
  - Automatic backup layer before destructive cleanup
  - Action memory (`/memory`) and rerun (`/rerun`)
  - Pinned commands (`/pin`, `/pins`)
  - Reusable recipes (`/recipe save`, `/recipe run`, `/recipe list`)
- Layer-aware and batch workflows:
  - `buffer roads by 20m and clip parcels`
  - `merge -> reproject -> dissolve`
- Raster and QA helpers:
  - Clip raster by mask
  - Reproject raster
  - Slope / aspect / hillshade
  - Null audit, duplicate audit, topology QA with CSV summary
- Export assistant:
  - GPKG export + QML style + metadata JSON sidecar in organized folder structure
- LLM planner mode (optional):
  - Understands open-ended prompts
  - Chooses answer vs Processing execution
  - Produces a JSON action plan, then plugin executes it in QGIS
- Generic Processing support:
  - Executes explicit algorithm commands like `native:*`, `qgis:*` with JSON params
  - Resolves `"active"` and exact layer names automatically
- Autonomous toolkit mode (no external plugin dependency):
  - Feeder splitter, overlap checker/mismatch, tile index, shapefile merger
  - Geo export, dynamic bbox, bbox conf manual, shapefile renamer, vertex mismatch checker
  - Runs inside AI GIS Copilot from prompts directly
- Feeder Comparison + AI QC window:
  - Version A vs Version B feeder diff (added/deleted/moved poles, changed spans)
  - Attribute and geometry change detection
  - Deterministic network validation (disconnected endpoints, duplicate geometry, short spans)
  - Automated screenshot evidence capture (zoomed + full extent)
  - AI-assisted prioritization and correction suggestions
  - One-click PDF QC report generation with before/after maps

## Install (Local Plugin)

1. Keep this folder as:
   - `D:\Cursor Project\AI GIS Copilot`
2. In QGIS, open:
   - `Plugins > Manage and Install Plugins... > Install from ZIP`
3. Zip this folder and install, or copy it to your QGIS plugins directory.
4. Enable `AI GIS Copilot`.
5. Open from:
   - `Plugins > AI GIS Copilot`

## Notes

- Without LLM setup, plugin runs built-in deterministic actions.
- In chat, run `/intents` to see built-in operations.
- In chat, run `/plugins` to inspect discovered plugin integrations.
- You can run explicit algorithms directly:
  - `native:buffer {"INPUT":"active","DISTANCE":25,"OUTPUT":"memory:"}`
- To enable broad natural-language coverage, configure LLM inside chat:
  - `/set api_key <token>`
  - `/set base_url <openai_compatible_url>` (optional, default OpenAI)
  - `/set model <model_name>`
  - `/status`
- This architecture is extensible; complete QGIS coverage is achieved progressively by adding more safe executors and richer planner context.
