# Optical and radar region histories

Independent Python VGI worker: query geospatial analysis from SQL and join the resulting rows to your existing tables.

## Run it

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/Query-farm/vgi-earth-observation.git
cd vgi-earth-observation
uv sync --locked
make demo
make verify-stock
make check
```

To attach a tagged release without cloning, let uv fetch it:

```sql
ATTACH 'earth_observation' (TYPE vgi,
  LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-earth-observation@v0.1.0 vgi-earth-observation-worker');
```

`make demo` runs [demo.sql](demo.sql) in stock DuckDB 2.0.0. Demo datasets ship inside the wheel; no data credentials are required. The first run downloads dependencies and signed SQL extensions. `make verify-stock` asserts every expected result in [vgi-worker.json](vgi-worker.json), including empty inputs, joins and multiple batches.

## Analysis contract

`parcel_series((SELECT parcel_id, geometry_wkb FROM parcels), ...)` returns one row per parcel and observation or month. It searches public STAC APIs or reads static STAC ItemCollections, aligns bands to a common grid, masks Sentinel-2 SCL classes, computes NDVI/NDWI/NBR, forms pixel-median monthly composites, and compares adequately observed periods. `demo_input` is a table of two parcel polygons.

**Acceptance:** June NDVI is 0.6 despite a fully cloudy second scene; July is 0.0, producing a −0.6 decline. One parcel has 75% June coverage. The demo joins declines to owner names and monthly rain.

Real inputs need polygon WKB and `geometry_crs`. Asset keys default to `red`, `nir08`, `green`, `swir22`, and `scl`; override them for another catalog. Reflectance scale and offset come from `raster:bands`, falling back to raster metadata. Default `offset_mode := 'auto'` treats Earth Search GeoTIFFs marked `earthsearch:boa_offset_applied = true` as already offset-corrected. `metadata` always uses declared offsets; `zero` is an explicit override for independently verified harmonized imagery. The `calibration` JSON column records effective scale/offset and its source per scene/band. Missing SCL fails explicitly. `clear_scl_classes` defaults to 4,5,6. Public assets must be readable by Rasterio/GDAL; private provider signing/authentication is outside this version.

Defaults cap a parcel window at 1,000,000 pixels, a request at 128 scenes and 100 pages, and the pixel-scene stack at 20 million elements. Monthly composites calculate the median of pixel indices, then average valid parcel pixels. Coverage uses pixel centres; tiny parcels may have none. Decline detection needs at least 50% coverage and a prior eligible period within 90 days. Fully masked observations produce NULL, never zero. A partially observed index remains visible below `min_coverage`, but its status is `insufficient_coverage` and it does not participate in change detection. STAC calls retry transient failures and respect bounded Retry-After delays; paginate rather than silently truncate. Analysis assumes a bounded local parcel, not antimeridian-spanning geometry. Negative calibrated reflectances are retained; normalized differences are not clamped. Earth Search has [documented inconsistent offset metadata](https://github.com/Element84/earth-search/issues/71), including incorrect flags on some scenes: verify questionable imagery against the provider and choose an explicit offset mode. The automatic rule follows [the provider’s BOA flag guidance](https://github.com/Element84/earth-search/issues/9).

## Use your data

### Prepared SAR intensity

`main.radar_series` accepts a relation of `region_id` and polygon `geometry_wkb`,
plus a STAC catalog of calibrated terrain-corrected intensity assets. It returns
per-acquisition spatial mean power and `10*log10(mean_power)`, along with coverage,
satellite, repeat orbit and polarization. Run the offline example after attaching:

```sql
SELECT observed_at::DATE, polarization, round(mean_db, 1), status
FROM earth_observation.main.radar_series((
    SELECT parcel_id AS region_id, geometry_wkb
    FROM earth_observation.main.demo_input WHERE parcel_id = 'field-a'
))
ORDER BY observed_at, polarization;
```

For real data, set `catalog`, `geometry_crs`, `start_date` and `end_date`. The
default unit is calibrated `linear_power`; `value_unit := 'db'` converts pixels
back to power before spatial averaging. Band scale/offset metadata is applied.
Required asset keys are `vv` and `vh`, controlled by `polarizations`. Every scene
requires `platform`, `sat:relative_orbit` and `sat:orbit_state` properties.
Keep these viewing groups and polarizations separate in temporal comparisons.
Overlapping slices from one acquisition are not independent observations;
mosaic or deduplicate them before estimating uncertainty.

The function rejects unaligned native grids within a viewing group. It permits
different extents but requires a common CRS, pixel spacing and integer pixel
offsets. It performs no resampling or extra speckle filtering. Membership uses
pixel centres; tiny polygons can have no samples. Zero/negative power, nodata
and nonfinite samples are excluded; low coverage returns NULL. Defaults limit
each region window to one million pixels, scene searches to 128 items (maximum
256), and output per input batch to 100,000 rows.

This function consumes **prepared intensity**, not raw SAR or complex SLC data.
It does not estimate interferometric coherence or classify buildings as damaged.
Changes in vegetation, moisture, geometry and structures can all alter radar
returns. Assets must be accessible to GDAL; provider-specific authentication
and signing remain outside the worker. Cache bounded, prepared raster inputs
and retain their provenance when building a reproducible study.

### Optical indices

From the repository directory after `uv sync --locked`, attach the installed worker through uv:

```sql
SET allow_extension_repositories = 'allowed';
CREATE EXTENSION REPOSITORY IF NOT EXISTS query_farm
  WITH PREFIX 'https://duckdb-extensions.query.farm';
INSTALL vgi FROM query_farm;
LOAD vgi FROM query_farm;
INSTALL spatial;
LOAD spatial;
ATTACH 'earth_observation' (TYPE vgi,
  LOCATION 'uv run --locked vgi-earth-observation-worker');
```

The following template expects your own source tables and paths:

```sql
SELECT parcel_id, observed_at, mean_index, coverage_fraction, decline
FROM earth_observation.main.parcel_series(
    (SELECT parcel_id, ST_AsWKB(geom) AS geometry_wkb FROM parcels),
    catalog := 'https://earth-search.aws.element84.com/v1',
    collections := 'sentinel-2-l2a', geometry_crs := 'EPSG:4326',
    start_date := '2024-06-01', end_date := '2024-06-10T23:59:59Z',
    composite := 'month')
ORDER BY parcel_id, observed_at;
```

WKB is an ordinary SQL BLOB with an explicit companion CRS, not an automatically transformed geometry. Use `ST_AsWKB` on input and `ST_GeomFromWKB` on output when using Spatial. Coordinates always use X/Y order. Do not join different coordinate systems without transforming them. Input IDs are retained as VARCHAR for joins. Invalid input schemas, geometries, coordinates and exceeded resource limits fail explicitly.

### Optical and radar together

Both functions keep the caller's IDs, so their histories join per parcel. This
offline query counts each bundled parcel's optical NDVI declines and joins them
to the VV backscatter change between the first and last fully covered radar
acquisitions on each orbit track:

```sql
WITH optical AS (
    SELECT parcel_id, count(*) FILTER (WHERE decline) AS ndvi_declines,
           min(delta_index) AS worst_ndvi_change
    FROM earth_observation.main.parcel_series(
        (SELECT parcel_id, geometry_wkb FROM earth_observation.main.demo_input))
    GROUP BY parcel_id),
radar AS (
    SELECT region_id, relative_orbit, orbit_state,
           arg_max(mean_db, observed_at) - arg_min(mean_db, observed_at) AS vv_change_db
    FROM earth_observation.main.radar_series(
        (SELECT parcel_id AS region_id, geometry_wkb FROM earth_observation.main.demo_input),
        polarizations := 'vv')
    WHERE status = 'ok'
    GROUP BY region_id, relative_orbit, orbit_state)
SELECT o.parcel_id, o.ndvi_declines, round(o.worst_ndvi_change, 2) AS worst_ndvi_change,
       r.relative_orbit, r.orbit_state, round(r.vv_change_db, 1) AS vv_change_db
FROM optical AS o JOIN radar AS r ON r.region_id = o.parcel_id
ORDER BY o.parcel_id, r.relative_orbit, r.orbit_state;
```

Each synthetic parcel has one NDVI decline (-0.6) and a -10 dB VV change on
ascending orbit 77. The synthetic optical and radar scenes are from different
years, so this is a demonstration of the join, not evidence of one event.

## Verification and provenance

- Scientific fixtures and edge cases: [tests/test_analysis.py](tests/test_analysis.py).
- Real SQL across stdio, launch and HTTP: [test/sql/gis.test](test/sql/gis.test), run by `make check` alongside the preserved SDK regression suite.
- Stock DuckDB contract: [vgi-worker.json](vgi-worker.json), executed by `make verify-stock`.
- Reproduce local checks with `make check`; generated logs and JSON under `validation/` are not source-controlled.
- Worker language: Python, pinned locally to 3.13. SDK: `vgi-python 0.51.1` with the starter's `>=0.51.1,<0.52` compatibility bound retained.
- Starter: [Query-farm/vgi-starter-python](https://github.com/Query-farm/vgi-starter-python), commit `9cca9105474d6e0052b8e343c8ea21f3d1a7503c`, template 1.3.0.
- Stock engine: DuckDB 2.0.0, signed VGI extension `e75482339d` from `query_farm`. The starter integration runner is separately pinned to `haybarn-unittest 2.0.0rc11`; it is not the stock DuckDB check.

`make check` includes formatting, Ruff, strict mypy, docstring checks, unit tests, all three SQL transports, executed metadata examples and stock DuckDB assertions. `make lint-ai` is an optional agent evaluation and is not part of the completed checks. The `examples` schema preserves starter functions; GIS functions live in `main`. Tests use synthetic known-answer fixtures, not a claim of production accuracy on every dataset.

Run `make release-check` to build a wheel from the source archive and verify its installed SQL interface outside the checkout. `make verify-container` builds locked runtime dependencies and checks every stock SQL assertion through a non-root, read-only HTTP container. See [PRODUCTION.md](PRODUCTION.md) for deployment, capacity limits, release evidence and operational boundaries.

The optional public-data check is `uv run --with duckdb==2.0.0 python scripts/run_demo.py --sql-file examples/live_stac.sql --output validation/live-stac.json`. It checks clear pixels and plausible NDVI for one Iowa parcel; the result and effective calibration are written to the local `validation/live-stac.json`. This network-dependent check is separate from deterministic CI.

## Support

Source and issues are hosted in [Query-farm/vgi-earth-observation](https://github.com/Query-farm/vgi-earth-observation). This is a library worker, not a hosted service; publishing source does not deploy an endpoint. Keep reports tied to the input CRS, resource limits and reproducible SQL. See [LICENSE](LICENSE) for the starter's license and dependency licenses for their terms.

## Executed notebook

[Coverage controls comparisons](notebooks/README.md) uses the packaged synthetic optical fixtures to distinguish missing data, partial coverage, monthly composites and eligible index changes.
