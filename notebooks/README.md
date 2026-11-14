# Earth Observation notebook

[01 · Coverage controls comparisons](01_coverage_controls_comparisons.ipynb) processes the worker's bundled synthetic optical rasters and two parcel polygons. It distinguishes a measured zero index from unavailable data, reconciles coverage and change rules in SQL, and shows how monthly composites can conceal a fully masked acquisition.

Run from this directory alone with [uv](https://docs.astral.sh/uv/) and Git:

```sh
uv sync --locked
uv run --locked python execute.py
uv run --locked python execute.py --parameters '{"MIN_COVERAGE":0.90}' --output-dir validation/coverage-90
```

For interactive use, register the locked environment and select it in your Jupyter installation:

```sh
uv run --locked python -m ipykernel install --user --name earth-observation-notebooks --display-name 'Python (Earth Observation notebooks)'
```

Restart Kernel → Run All after changing the visible parameter. The runner creates a temporary kernel, executes with this directory as cwd and replaces saved output only after success. Overrides require a separate destination, and experiment copies include branding, documentation and runtime companions. Generated artifacts and validation copies are ignored.

## Portable runtime and synthetic data

The literal ATTACH launches `uvx --python 3.13 --from git+https://github.com/Query-farm/vgi-earth-observation.git@542b71d763991e2bd8176f31920298f2e1a8925b --constraints worker-constraints.txt vgi-earth-observation-worker`. That full source revision is public and immutable. uv fetches/builds the Python package and caches it with registry dependencies; no worker checkout, parent directory or local worker binary is needed. The notebook engine is Haybarn `2.0.0rc11`. Worker constraints are exported from the source revision's lock, including SDK `0.51.1`, RPC `0.53.1`, Rasterio `1.5.2`, PyArrow `26.0.1`, NumPy `2.5.4`, Shapely `2.2.0` and pyproj `3.8.0`. Upgrade the source pin and constraints together only after repeating default and isolated experiment checks.

`demo://scenes.json` resolves inside the installed package. The small TIFFs and polygons are synthetic scientific fixtures, not actual satellite imagery. The three constructed acquisition labels are June 1, June 15 and July 1, 2026 UTC. The notebook explicitly uses the inclusive June 1–July 1 window, EPSG:32618, X/Y order in metres, a three-scene cap, one JSON page, and 100-pixel aligned/native window limits. It makes three deterministic worker calls (reference observations, selected observations, monthly composites) and materializes each before downstream SQL/plots. It does not contact a STAC API or support a fake replay/live toggle.

A fresh temporary Haybarn secret directory and `allow_persistent_secrets=False` isolate saved user credentials. The notebook fully drains and asserts an empty secret inventory before ATTACH, and closes the connection and directory during cleanup. The selected computational functions need no credentials.

## Interpretation

Coverage uses valid grid-cell centres divided by all parcel grid-cell centres. Fully masked rows have NULL means. A partially observed mean remains visible below the chosen coverage threshold, but does not participate in temporal comparisons. `status`, counts and means must be interpreted together. NDVI is dimensionless; small floating-point values around zero are expected for the constructed equal-band July scene.

The worker first computes pixel indices, then forms a pixelwise temporal median for a monthly composite, then takes a spatial mean. Monthly rows label the first UTC day of the month. `scene_count` counts considered scenes, including scenes that supplied no valid pixel values; it is not an effective sample size. A decline flag applies an explicit −0.3-unit rule to eligible means separated by at most 90 days. It does not diagnose stress, attribute a cause or validate an operational threshold.

Changing `MIN_COVERAGE` from 0.50 to 0.90 should leave raw measured values and coverage fixed, make field-a's June observation ineligible, and remove its July change comparison. Field-b provides a control. A source fingerprint, eight SQL checks and additional date/CRS checks verify those distinctions. The default and notebook-only detached experiment are executed separately; both charts and runner guards are reviewed. Logs, extracted charts and audit JSON are under ignored `validation/` and `artifacts/`.

Source checks cover Python 3.13/3.14 units, Haybarn stdio/Unix/HTTP SQL, metadata, stock DuckDB 2.0.0 and an isolated sdist-built wheel with its bundled data. Docker is unavailable on this host, so no container build or deployment was performed. See [STANDARD.md](STANDARD.md) and the repository's source documentation for the full contract.
