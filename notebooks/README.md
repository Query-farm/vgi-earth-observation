# Earth Observation examples

Three independent notebooks use the public **v0.1.0** worker and the shared
Query.Farm example tools. Each has saved SQL results, SVG figures and a parameter
experiment. Inputs are explicitly synthetic; no provider account or imagery
credentials are needed.

| Notebook | Question | Colab |
| --- | --- | --- |
| [Coverage controls comparisons](01_coverage_controls_comparisons.ipynb) | Do unchanged optical measurements remain eligible when the coverage threshold increases? | [Run](https://colab.research.google.com/github/Query-farm/vgi-earth-observation/blob/main/notebooks/01_coverage_controls_comparisons.ipynb) |
| [Latest radar row versus latest measurement](02_latest_radar_row_is_not_latest_measurement.ipynb) | What should a report say when its newest radar acquisition has no usable pixels? | [Run](https://colab.research.google.com/github/Query-farm/vgi-earth-observation/blob/main/notebooks/02_latest_radar_row_is_not_latest_measurement.ipynb) |
| [Paginated inventory report](03_paginated_inventory_report.ipynb) | Have all synthetic inventory pages arrived before a budget comparison and local report preview? | [Run](https://colab.research.google.com/github/Query-farm/vgi-earth-observation/blob/main/notebooks/03_paginated_inventory_report.ipynb) |

The first two use the release's packaged TIFFs and polygon fixtures. Raster
measurement runs inside the actual worker; SQL checks coverage, changes and grouping.
The third demonstrates the separately named `examples` schema, a retained generic
starter API/generator/formatter. It is not STAC or an optical/radar analysis service.
Its local server serves six authored equipment prices, rejects credentials and
records sanitized request evidence. Greeting outputs are strings, never messages.

## Run all

Open a Colab link and use **Run all**. Only the base shared package is installed;
compatible preloaded plotting/kernel libraries stay installed. Companions download
from immutable public revisions with verified hashes. Cloning this repository is
unnecessary. Initial dependency and VGI extension downloads need network access.

Locally, from this directory with `uv` installed:

```sh
uv sync --locked
uv run --locked vgi-notebooks --timeout 300
```

Local `[runner]` dependencies are not installed by the notebooks into Colab. Each
visible ATTACH uses a short Git-tag launcher:

```sql
ATTACH 'earth_observation' (TYPE vgi,
 LOCATION 'uvx --python 3.13 --from git+https://github.com/Query-farm/vgi-earth-observation@v0.1.0 vgi-earth-observation-worker');
```

The release owns its dependency compatibility. No worker constraint file, local
checkout or source build command appears in the examples. SQL credentials start
empty; the starter fixture temporarily isolates and then restores
`EARTH_OBSERVATION_API_TOKEN` without reading it into outputs.

## Save experiments separately

```sh
uv run --locked vgi-notebooks 01_coverage_controls_comparisons.ipynb --parameters '{"MIN_COVERAGE":0.9}' --output-dir artifacts/coverage-90 --timeout 300
uv run --locked vgi-notebooks 02_latest_radar_row_is_not_latest_measurement.ipynb --parameters '{"END_DATE":"2025-08-22T00:00:00Z"}' --output-dir artifacts/august-window --timeout 300
uv run --locked vgi-notebooks 03_paginated_inventory_report.ipynb --parameters '{"BUDGET_USD":75}' --output-dir artifacts/budget-75 --timeout 300
```

Optical reference measurements stay fixed while field-a loses its earlier eligible
baseline. The shorter radar window excludes unavailable September rows while keeping
all shared measurements and August power changes fixed. A larger inventory budget
changes affordability while preserving prices, pages and the complete scenario grid.

## Boundaries and provenance

The optical dates are in 2026 and radar dates in 2025: the same parcel IDs do not
establish one shared event. Cloud masking, coverage and calibration are explicit;
monthly optical composites are pixelwise index medians, not daily reconstructions.
Radar inputs are prepared intensity, not raw SAR or complex SLC. The worker averages
linear power before taking its logarithm. Viewing geometry and polarizations remain
separate; NULL values are not zero power, and changes are not damage probabilities.
These synthetic checks do not validate a sensor or a real-world causal explanation.

Figures use SVG MIME in Jupyter/Colab and content-addressed SVG assets for GitHub.
GitHub does not fold executable cells, so setup remains concise and ATTACH visible.
The shared [standard and runner](https://github.com/Query-farm/vgi-example-notebooks)
replace copied local tooling. `coverage.json` records all six logical public names,
including both greeting overloads and the demo table/function alias. Local testing
with preloaded libraries checks compatibility; it is not a manual hosted-Colab run.
