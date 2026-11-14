"""Region summaries of prepared calibrated SAR intensity, with explicit viewing groups."""

from __future__ import annotations

import math
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Annotated, Any

import numpy as np
import pyarrow as pa
from rasterio.features import geometry_mask
from rasterio.windows import Window, from_bounds
from vgi import Arg
from vgi.arguments import TableInput
from vgi.invocation import BindResponse
from vgi.table_function import BindParams, OutputCollector, ProcessParams
from vgi.table_in_out_function import TableInOutGenerator

from .analysis import read_scenes, timestamp
from .raster_support import north_up, open_raster
from .support import (
    example,
    finite_parameters,
    geometry,
    identifier,
    metadata,
    metric_crs,
    require_columns,
    schema_of,
)

RADAR_DESCRIPTION = (
    "Summarize prepared calibrated SAR intensity assets over polygon regions for "
    "time-series change analysis. Input region_id and geometry_wkb columns retain caller "
    "identity. Read local STAC ItemCollections or public STAC APIs, using vv/vh assets. "
    "This function accepts calibrated terrain-corrected linear power or decibel rasters, "
    "not raw radar, complex SLC, or an optical index. Convert dB pixels to linear power "
    "before spatial averaging, then report 10*log10(mean_power). Nonpositive linear "
    "power, nodata and nonfinite pixels are excluded. Pixel centres define membership. "
    "Keep platform, relative_orbit, orbit_state and polarization separate in temporal "
    "statistics. A viewing group's rasters must share CRS, resolution and pixel-grid "
    "alignment; differing extents are allowed. No resampling or additional speckle "
    "filter is performed. Missing observations are NULL, never zero. A change is not "
    "a damage probability. Coherence requires a separate phase-preserving SLC pipeline. "
    "Private asset signing is outside this function; use prepared local windows."
)

RADAR_RESULT = schema_of(
    [
        ("region_id", pa.string(), "Caller-owned region identity for joins."),
        ("observed_at", pa.timestamp("us"), "UTC acquisition instant; NULL when no scenes match."),
        (
            "scene_id",
            pa.string(),
            "STAC scene identifier; overlapping acquisitions must not be counted as independent.",
        ),
        ("platform", pa.string(), "Satellite identity normalized to lowercase; different satellites remain separate."),
        ("relative_orbit", pa.int64(), "Repeat ground-track identifier; compare within this orbit only."),
        ("orbit_state", pa.string(), "Ascending or descending pass direction."),
        ("polarization", pa.string(), "VV or VH measurement channel."),
        ("mean_power", pa.float64(), "Arithmetic spatial mean calibrated linear radar power."),
        ("mean_db", pa.float64(), "10*log10(mean_power), not the spatial mean of pixel dB values."),
        ("valid_pixels", pa.int64(), "Finite usable pixels whose centres lie in the region."),
        ("total_pixels", pa.int64(), "All region pixel centres, including nodata and cells outside raster coverage."),
        ("coverage_fraction", pa.float64(), "Usable region pixels divided by total region pixels."),
        ("grid_crs", pa.string(), "Native projected raster CRS with metre units."),
        ("pixel_width_m", pa.float64(), "Raster grid spacing in metres; not necessarily sensor resolving power."),
        ("pixel_height_m", pa.float64(), "Raster grid spacing in metres; not necessarily sensor resolving power."),
        ("status", pa.string(), "ok, insufficient_coverage, no_pixel_centres, or no_scenes."),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class RadarArgs:
    """Bounded SAR region-history controls and explicit input calibration units."""

    regions: Annotated[TableInput, Arg(0, doc="Relation with region_id and polygon geometry_wkb columns.")]
    catalog: Annotated[
        str, Arg("catalog", default="demo://radar-scenes.json", doc="Local STAC items or public API root.")
    ]
    geometry_crs: Annotated[str, Arg("geometry_crs", default="EPSG:32618", doc="Explicit CRS of input polygon WKB.")]
    collections: Annotated[
        str, Arg("collections", default="sentinel-1-rtc", doc="Comma-separated STAC collection names for API search.")
    ]
    start_date: Annotated[
        str, Arg("start_date", default="2025-01-01", doc="Inclusive UTC acquisition lower bound, ISO 8601.")
    ]
    end_date: Annotated[
        str, Arg("end_date", default="2025-12-31T23:59:59Z", doc="Inclusive UTC acquisition upper bound, ISO 8601.")
    ]
    polarizations: Annotated[
        str,
        Arg(
            "polarizations",
            default="vv,vh",
            choices=["vv", "vh", "vv,vh"],
            doc="Required polarization asset keys; missing assets fail.",
        ),
    ]
    value_unit: Annotated[
        str,
        Arg(
            "value_unit",
            default="linear_power",
            choices=["linear_power", "db"],
            doc="Calibrated source unit after band scale/offset; db is converted to linear power before averaging.",
        ),
    ]
    min_coverage: Annotated[
        float, Arg("min_coverage", default=0.9, ge=0, le=1, doc="Minimum valid fraction to return a mean measurement.")
    ]
    max_pixels: Annotated[
        int, Arg("max_pixels", default=1000000, ge=1, doc="Hard native bounding-window pixel limit for each region.")
    ]
    max_scenes: Annotated[
        int,
        Arg("max_scenes", default=128, ge=1, le=256, doc="Hard scene bound; excess results fail without truncation."),
    ]
    max_pages: Annotated[int, Arg("max_pages", default=100, ge=1, doc="Hard STAC pagination bound.")]
    max_json_bytes: Annotated[
        int, Arg("max_json_bytes", default=16777216, ge=1024, doc="Maximum bytes per STAC page or local document.")
    ]


def radar_channels(value: str) -> list[str]:
    """Require distinct supported polarization asset names."""
    channels = [v.strip().lower() for v in value.split(",")]
    if not channels or len(set(channels)) != len(channels) or not set(channels) <= {"vv", "vh"}:
        raise ValueError("polarizations must be vv, vh, or vv,vh without duplicates")
    return channels


def viewing_group(scene: dict[str, Any], channel: str) -> tuple[str, int, str, str]:
    """Require viewing metadata instead of silently mixing unrelated observations."""
    props = scene["properties"]
    platform, orbit, direction = props.get("platform"), props.get("sat:relative_orbit"), props.get("sat:orbit_state")
    if not isinstance(platform, str) or not platform.strip():
        raise ValueError("SAR scene requires properties.platform")
    if isinstance(orbit, bool) or not isinstance(orbit, int) or not 1 <= orbit <= 175:
        raise ValueError("SAR scene requires integer sat:relative_orbit from 1 to 175")
    if direction not in {"ascending", "descending"}:
        raise ValueError("SAR scene requires ascending or descending sat:orbit_state")
    return platform.strip().lower(), orbit, str(direction), channel.upper()


def check_grid(dataset: Any, reference: Any) -> None:
    """Require comparable native pixel grids within each satellite/orbit/channel."""
    north_up(dataset)
    metric_crs(dataset.crs)
    if dataset.count != 1:
        raise ValueError("SAR polarization assets must be single-band rasters")
    if dataset.crs != reference.crs or not np.allclose(dataset.res, reference.res, rtol=0, atol=1e-8):
        raise ValueError("SAR viewing group needs the same CRS and pixel resolution; align rasters first")
    offsets = [
        (dataset.transform.c - reference.transform.c) / reference.transform.a,
        (dataset.transform.f - reference.transform.f) / reference.transform.e,
    ]
    if any(abs(value - round(value)) > 1e-6 for value in offsets):
        raise ValueError("SAR viewing group has unaligned pixel grids; align rasters first")


def region_measurement(row: dict[str, Any], dataset: Any, asset: dict[str, Any], args: Any) -> dict[str, Any]:
    """Measure a bounded native window, retaining the full region coverage denominator."""
    polygon = geometry(row["geometry_wkb"], args.geometry_crs, dataset.crs)
    raw = from_bounds(*polygon.bounds, transform=dataset.transform)
    left, top = math.floor(raw.col_off), math.floor(raw.row_off)
    right, bottom = math.ceil(raw.col_off + raw.width), math.ceil(raw.row_off + raw.height)
    width, height = right - left, bottom - top
    if width * height > args.max_pixels:
        raise ValueError("SAR region bounding window exceeds max_pixels")
    window = Window(left, top, width, height)
    inside = geometry_mask(
        [polygon.__geo_interface__], out_shape=(height, width), transform=dataset.window_transform(window), invert=True
    )
    total = int(np.count_nonzero(inside))
    values = dataset.read(1, window=window, boundless=True, masked=True).astype("float64").filled(np.nan)
    bands = asset.get("raster:bands", [{}])
    if not isinstance(bands, list) or not bands or not isinstance(bands[0], dict):
        raise ValueError("SAR asset raster:bands must contain valid band metadata")
    scale = float(bands[0].get("scale", dataset.scales[0]))
    offset = float(bands[0].get("offset", dataset.offsets[0]))
    if not math.isfinite(scale) or scale <= 0 or not math.isfinite(offset):
        raise ValueError("SAR band scale must be finite and positive and offset finite")
    with np.errstate(over="ignore", invalid="ignore", under="ignore"):
        values = values * scale + offset
        if args.value_unit == "db":
            values = np.power(10.0, values / 10.0)
    valid = inside & np.isfinite(values) & (values > 0)
    count = int(np.count_nonzero(valid))
    fraction = count / total if total else 0.0
    status = "ok" if count and fraction >= args.min_coverage else "insufficient_coverage"
    if not total:
        status = "no_pixel_centres"
    # Scaling before averaging avoids overflow for otherwise finite extreme inputs.
    mean = None
    if status == "ok":
        peak = float(np.max(values[valid]))
        mean = float(np.mean(values[valid] / peak)) * peak
    return {
        "mean_power": mean,
        "mean_db": 10 * math.log10(mean) if mean is not None else None,
        "valid_pixels": count,
        "total_pixels": total,
        "coverage_fraction": fraction,
        "grid_crs": str(dataset.crs),
        "pixel_width_m": float(dataset.res[0]),
        "pixel_height_m": float(dataset.res[1]),
        "status": status,
    }


def radar_series(rows: list[dict[str, Any]], scenes: list[dict[str, Any]], args: Any) -> list[dict[str, Any]]:
    """Share raster handles across a bounded batch of region observations."""
    result: list[dict[str, Any]] = []
    identities = [identifier(row, "region_id") for row in rows]
    if not scenes:
        return [
            {"region_id": region_id, "status": "no_scenes", "valid_pixels": 0, "total_pixels": 0}
            for region_id in identities
        ]
    if len(rows) * len(scenes) * len(radar_channels(args.polarizations)) > 100000:
        raise ValueError("SAR batch exceeds 100000 region/scene/channel rows; partition the input")
    with ExitStack() as stack:
        grids: dict[tuple[str, int, str, str], Any] = {}
        for scene in scenes:
            for channel in radar_channels(args.polarizations):
                group = viewing_group(scene, channel)
                asset = scene["assets"].get(channel)
                if asset is None:
                    raise ValueError(f"SAR scene missing required polarization asset {channel}")
                dataset = stack.enter_context(open_raster(asset["href"]))
                reference = grids.setdefault(group, dataset)
                check_grid(dataset, reference)
                for region_id, row in zip(identities, rows, strict=True):
                    result.append(
                        {
                            "region_id": region_id,
                            "observed_at": scene["_time"].replace(tzinfo=None),
                            "scene_id": scene["id"],
                            "platform": group[0],
                            "relative_orbit": group[1],
                            "orbit_state": group[2],
                            "polarization": group[3],
                            **region_measurement(row, dataset, asset, args),
                        }
                    )
    return result


RADAR_SQL = (
    "SELECT observed_at::DATE AS observed_date, round(mean_db, 1), status FROM "
    "earth_observation.main.radar_series((SELECT parcel_id AS region_id, geometry_wkb "
    "FROM earth_observation.main.demo_input WHERE parcel_id='field-a'), polarizations := 'vv') "
    "ORDER BY observed_at"
)

# Composed: radar follow-up restricted to parcels whose optical history shows a decline.
RADAR_FOLLOWUP_SQL = (
    "WITH declined AS (SELECT DISTINCT parcel_id FROM earth_observation.main.parcel_series("
    "(SELECT parcel_id, geometry_wkb FROM earth_observation.main.demo_input)) WHERE decline) "
    "SELECT region_id, observed_at::DATE AS observed_date, relative_orbit, "
    "round(max(mean_db) FILTER (WHERE polarization = 'VV') - max(mean_db) FILTER (WHERE polarization = 'VH'), 1) "
    "AS vv_minus_vh_db FROM earth_observation.main.radar_series((SELECT parcel_id AS region_id, geometry_wkb "
    "FROM earth_observation.main.demo_input WHERE parcel_id IN (SELECT parcel_id FROM declined))) "
    "WHERE status = 'ok' GROUP BY region_id, observed_at, relative_orbit ORDER BY region_id, observed_at"
)
RADAR_FOLLOWUP_DESCRIPTION = (
    "Follow up parcels flagged with an optical NDVI decline: VV minus VH backscatter in dB "
    "for each fully covered radar acquisition"
)


class RadarSeries(TableInOutGenerator[RadarArgs]):
    """Expose prepared SAR region histories as SQL rows without inferring damage."""

    class Meta:
        """Radar time-series discovery metadata and executable example."""

        name = "radar_series"
        description = "Summarize calibrated SAR backscatter by region, acquisition, viewing track and polarization"
        tags = metadata(
            "SAR backscatter histories",
            RADAR_DESCRIPTION,
            RADAR_RESULT,
            RADAR_SQL,
            [["2025-08-10", -10.0, "ok"], ["2025-08-22", -20.0, "ok"], ["2025-09-03", None, "insufficient_coverage"]],
            more=[
                (
                    RADAR_FOLLOWUP_DESCRIPTION,
                    RADAR_FOLLOWUP_SQL,
                    [
                        ["field-a", "2025-08-10", 77, 10.0],
                        ["field-a", "2025-08-22", 77, 10.0],
                        ["field-b", "2025-08-10", 77, 10.0],
                        ["field-b", "2025-08-22", 77, 10.0],
                    ],
                )
            ],
        )
        examples = [
            *example(RADAR_SQL, "Inspect a synthetic radar-return decrease and an explicit missing observation"),
            *example(RADAR_FOLLOWUP_SQL, RADAR_FOLLOWUP_DESCRIPTION),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[RadarArgs]) -> BindResponse:
        """Validate input columns and constant controls before reading radar assets."""
        finite_parameters(params.args)
        require_columns(params.bind_call.input_schema, ("region_id", "geometry_wkb"))
        radar_channels(params.args.polarizations)
        if timestamp(params.args.start_date) > timestamp(params.args.end_date):
            raise ValueError("start_date must be at or before end_date")
        return BindResponse(output_schema=RADAR_RESULT)

    @classmethod
    def process(
        cls, params: ProcessParams[RadarArgs], state: None, batch: pa.RecordBatch, out: OutputCollector
    ) -> None:
        """Search once for each Arrow input batch and return per-scene region measurements."""
        rows = batch.to_pylist()
        if not rows:
            out.emit(pa.RecordBatch.from_pylist([], schema=params.output_schema))
            return
        polygons = [geometry(row["geometry_wkb"], params.args.geometry_crs, "EPSG:4326") for row in rows]
        bounds = (
            min(p.bounds[0] for p in polygons),
            min(p.bounds[1] for p in polygons),
            max(p.bounds[2] for p in polygons),
            max(p.bounds[3] for p in polygons),
        )
        scenes = read_scenes(params.args, bounds)
        # Table-in/out permits one emitted data batch per invocation in this SDK.
        # Bound the expanded result above instead of emitting multiple chunks.
        out.emit(pa.RecordBatch.from_pylist(radar_series(rows, scenes, params.args), schema=params.output_schema))
