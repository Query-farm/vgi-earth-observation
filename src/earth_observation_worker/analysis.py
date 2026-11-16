"""Cloud-masked parcel index time series from STAC items and aligned raster assets."""

from __future__ import annotations

import json
import math
import time
import warnings
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Annotated, Any, ClassVar
from urllib.parse import urljoin, urlparse

import httpx
import numpy as np
import pyarrow as pa
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.vrt import WarpedVRT
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import Window, from_bounds
from rasterio.windows import bounds as window_bounds
from shapely.geometry import box, mapping, shape
from vgi import Arg
from vgi.arguments import TableInput
from vgi.catalog import Table
from vgi.catalog.constraints import constraint_record
from vgi.invocation import BindResponse
from vgi.table_function import (
    BindParams,
    OutputCollector,
    ProcessParams,
    TableFunctionGenerator,
    bind_fixed_schema,
    init_single_worker,
)
from vgi.table_in_out_function import TableInOutGenerator
from vgi_rpc import ArrowSerializableDataclass

from .raster_support import north_up, open_raster
from .support import (
    emit_rows,
    example,
    finite_parameters,
    geometry,
    identifier,
    metadata,
    read_json,
    require_columns,
    resource,
    schema_of,
)

GIS_DESCRIPTION = (
    "Build parcel vegetation or water-index histories from STAC ItemCollections or "
    "public STAC APIs. "
    "Align bands and categorical SCL masks, apply scale/offset metadata, exclude cloudy"
    " pixels, optionally "
    "form monthly pixel-median composites, and flag declines between adequately "
    "observed periods. "
    "Output parcel IDs join directly to ownership and weather tables. Synthetic offline"
    " assets ship in the wheel."
)
INPUT = schema_of(
    [
        ("parcel_id", pa.string(), "Caller-owned parcel identity."),
        ("geometry_wkb", pa.binary(), "Polygon WKB in geometry_crs."),
    ]
)
RESULT = schema_of(
    [
        ("parcel_id", pa.string(), "Input parcel ID for joins to ownership and weather."),
        ("observed_at", pa.timestamp("us"), "UTC observation time, or first day of the month for monthly composites."),
        ("index_name", pa.string(), "ndvi, ndwi, or nbr."),
        (
            "mean_index",
            pa.float64(),
            "Mean of valid pixel indices, after cloud masking and optional pixel-median compositing.",
        ),
        ("valid_pixels", pa.int64(), "Valid, cloud-free polygon pixels in the aligned grid."),
        ("total_pixels", pa.int64(), "Grid pixel centres inside the polygon, including cloudy and missing cells."),
        ("coverage_fraction", pa.float64(), "Valid pixels divided by total polygon pixels."),
        (
            "delta_index",
            pa.float64(),
            "Change from the previous adequately observed result within max_change_gap_days.",
        ),
        (
            "decline",
            pa.bool_(),
            "True when delta_index is at most negative decline_threshold; NULL without a valid comparison.",
        ),
        ("scene_count", pa.int64(), "Number of contributing candidate scenes in this observation or composite."),
        ("scene_ids", pa.string(), "JSON array of source scene identifiers for provenance."),
        ("calibration", pa.string(), "JSON per scene and band: effective reflectance scale, offset and source."),
        (
            "status",
            pa.string(),
            "ok, insufficient_coverage, or no_scenes; missing coverage never becomes a zero index.",
        ),
        ("analysis_crs", pa.string(), "CRS of the first selected denominator-band grid, used for all aligned bands."),
    ]
)


def timestamp(value: str) -> datetime:
    """Parse STAC UTC instants; naive values are interpreted as UTC."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def absolute_href(base: str, href: str) -> str:
    """Resolve asset and pagination links without depending on worker cwd."""
    if urlparse(href).scheme:
        return href
    if urlparse(base).scheme in {"https", "http"}:
        return urljoin(base, href)
    return str((Path(base).parent / href).resolve())


def band_calibration(asset: dict[str, Any], ds: Any, scene: dict[str, Any], mode: str) -> dict[str, Any]:
    """Resolve effective reflectance calibration, including already-harmonized Earth Search COGs."""
    bands = asset.get("raster:bands", [{}])
    if not isinstance(bands, list) or not bands or not isinstance(bands[0], dict):
        raise ValueError("STAC raster:bands must contain valid band metadata")
    scale = float(bands[0].get("scale", ds.scales[0]))
    offset = float(bands[0].get("offset", ds.offsets[0]))
    if not math.isfinite(scale) or scale <= 0 or not math.isfinite(offset):
        raise ValueError("Reflectance scale must be finite and positive and its offset finite")
    source = "raster:bands" if bands[0] else "raster_metadata"
    # This flag describes the Earth Search COGs, not the original JP2 assets.
    # Provider confirmation: https://github.com/Element84/earth-search/issues/9
    harmonized = scene.get("properties", {}).get("earthsearch:boa_offset_applied") is True
    is_tiff = str(asset.get("type", "")).startswith("image/tiff")
    if mode == "zero":
        offset, source = 0.0, "caller_zero_offset"
    elif mode == "auto" and harmonized and is_tiff:
        offset, source = 0.0, "earthsearch:boa_offset_applied"
    return {"scale": scale, "offset": offset, "source": source}


def request_page(
    client: httpx.Client, method: str, url: str, body: Any, max_bytes: int = 16 * 1024 * 1024
) -> httpx.Response:
    """Bound response bytes and retry transient network/status failures with backoff."""
    for attempt in range(5):
        delay = 0.5 * 2**attempt
        try:
            with client.stream(method, url, json=body) as response:
                if response.status_code != 429 and response.status_code < 500:
                    response.raise_for_status()
                    payload = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        if len(payload) + len(chunk) > max_bytes:
                            raise ValueError(f"STAC response exceeds max_json_bytes={max_bytes}")
                        payload.extend(chunk)
                    return httpx.Response(
                        response.status_code,
                        headers={
                            k: v for k, v in response.headers.items() if k not in {"content-encoding", "content-length"}
                        },
                        content=bytes(payload),
                        request=response.request,
                    )
                if attempt == 4:
                    response.raise_for_status()
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        try:
                            delay = (parsedate_to_datetime(retry_after) - datetime.now(UTC)).total_seconds()
                        except TypeError, ValueError, OverflowError:
                            delay = 0.5 * 2**attempt
                    if not math.isfinite(delay) or delay > 30:
                        raise ValueError(f"STAC HTTP {response.status_code}: Retry-After exceeds 30s; retry later")
        except httpx.TimeoutException, httpx.NetworkError:
            if attempt == 4:
                raise
        time.sleep(max(0.0, delay))
    raise RuntimeError("STAC retry loop exhausted")


def read_scenes(args: Any, bounds: tuple[float, ...]) -> list[dict[str, Any]]:
    """Read static items or search and paginate a public STAC endpoint with hard limits."""
    start = timestamp(args.start_date)
    end = timestamp(args.end_date)
    if start > end:
        raise ValueError("start_date must be at or before end_date")
    url = resource(args.catalog)
    method, body = "GET", None
    seen: set[tuple[str, str, str]] = set()
    result: dict[str, dict[str, Any]] = {}
    searched = False
    collections = [c.strip() for c in args.collections.split(",") if c.strip()]
    transport = httpx.HTTPTransport(retries=0)
    with httpx.Client(transport=transport, timeout=30, follow_redirects=True) as client:
        for _ in range(args.max_pages):
            signature = (url, method, json.dumps(body, sort_keys=True))
            if signature in seen:
                raise ValueError("STAC pagination cycle detected")
            seen.add(signature)
            if urlparse(url).scheme in {"http", "https"}:
                response = request_page(client, method, url, body, args.max_json_bytes)
                page = response.json()
                base = str(response.url)
            else:
                page = read_json(url, args.max_json_bytes)
                base = url
            if not isinstance(page, dict):
                raise ValueError("STAC response must be a JSON object")
            if "features" not in page and page.get("type") != "Feature":
                searches = [link for link in page.get("links", []) if link.get("rel") == "search"]
                search = next((link for link in searches if link.get("method") == "POST"), next(iter(searches), None))
                if searched or search is None:
                    raise ValueError("Expected a STAC ItemCollection, Item, or API root with a search link")
                url = absolute_href(base, search["href"])
                method = search.get("method", "GET").upper()
                body = {
                    "bbox": list(bounds),
                    "datetime": f"{start.isoformat()}/{end.isoformat()}",
                    "collections": collections,
                    "limit": min(100, args.max_scenes),
                }
                if not collections:
                    body.pop("collections")
                if method == "GET":
                    query = {k: ",".join(map(str, v)) if isinstance(v, list) else str(v) for k, v in body.items()}
                    url = str(httpx.URL(url).copy_merge_params(query))
                    body = None
                searched = True
                continue
            for item in page.get("features", [page] if page.get("type") == "Feature" else []):
                props = item.get("properties", {})
                when = timestamp(props.get("datetime") or props.get("start_datetime") or "")
                if not start <= when <= end:
                    continue
                cloud = props.get("eo:cloud_cover")
                if cloud is not None and (not math.isfinite(float(cloud)) or not 0 <= float(cloud) <= 100):
                    raise ValueError("STAC eo:cloud_cover must be a finite percentage")
                if cloud is not None and float(cloud) > getattr(args, "max_cloud", 100.0):
                    continue
                if item.get("geometry") and not shape(item["geometry"]).intersects(box(*bounds)):
                    continue
                if searched and collections and item.get("collection") not in collections:
                    continue
                item = {
                    **item,
                    "assets": {
                        key: {**asset, "href": absolute_href(base, asset["href"])}
                        for key, asset in item.get("assets", {}).items()
                    },
                    "_time": when,
                }
                result[str(item.get("collection", "")) + ":" + str(item["id"])] = item
                if len(result) > args.max_scenes:
                    raise ValueError(
                        "STAC result exceeds max_scenes; narrow the date range or increase the explicit limit"
                    )
            next_link = next((link for link in page.get("links", []) if link.get("rel") == "next"), None)
            if next_link is None:
                return sorted(result.values(), key=lambda item: (item["_time"], item["id"]))
            url = absolute_href(base, next_link["href"])
            method = next_link.get("method", "GET").upper()
            body = {**(body or {}), **next_link.get("body", {})} if next_link.get("merge") else next_link.get("body")
        raise ValueError("STAC pagination exceeds max_pages")


def parcel_series(
    row: dict[str, Any], scenes: list[dict[str, Any]], args: Any, stack: ExitStack, datasets: dict[str, Any]
) -> list[dict[str, Any]]:
    """Compute pixel-level indices and temporal composites for one parcel."""
    key = identifier(row, "parcel_id")
    poly_wgs84 = geometry(row["geometry_wkb"], args.geometry_crs, "EPSG:4326")
    selected = [s for s in scenes if s.get("geometry") is None or shape(s["geometry"]).intersects(poly_wgs84)]
    empty = {
        "parcel_id": key,
        "observed_at": None,
        "index_name": args.index_name,
        "mean_index": None,
        "valid_pixels": 0,
        "total_pixels": 0,
        "coverage_fraction": 0.0,
        "delta_index": None,
        "decline": None,
        "scene_count": 0,
        "scene_ids": "[]",
        "calibration": "{}",
        "status": "no_scenes",
        "analysis_crs": None,
    }
    if not selected:
        return [empty]

    def dataset(asset: dict[str, Any]) -> Any:
        href = asset["href"]
        if href not in datasets:
            datasets[href] = stack.enter_context(open_raster(href))
            if datasets[href].crs is None:
                raise ValueError("Raster assets must declare a CRS")
            north_up(datasets[href])
        return datasets[href]

    pairs = {
        "ndvi": (args.nir_asset, args.red_asset),
        "ndwi": (args.green_asset, args.nir_asset),
        "nbr": (args.nir_asset, args.swir_asset),
    }
    first_key, second_key = pairs[args.index_name]
    if second_key not in selected[0]["assets"]:
        raise ValueError(f"Scene {selected[0]['id']} lacks required reference asset {second_key}")
    reference = dataset(selected[0]["assets"][second_key])
    if reference.crs is None:
        raise ValueError("Raster assets must declare a CRS")
    if (
        reference.transform.b != 0
        or reference.transform.d != 0
        or reference.transform.a <= 0
        or reference.transform.e >= 0
    ):
        raise ValueError("Reference asset must use a north-up grid")
    poly = geometry(row["geometry_wkb"], args.geometry_crs, reference.crs)
    raw = from_bounds(*poly.bounds, transform=reference.transform)
    col, r = math.floor(raw.col_off), math.floor(raw.row_off)
    width, height = math.ceil(raw.col_off + raw.width) - col, math.ceil(raw.row_off + raw.height) - r
    if width * height > args.max_pixels or width * height * len(selected) > 20000000:
        raise ValueError("Parcel analysis exceeds pixel/memory budget; use a smaller area or date range")
    if width < 1 or height < 1:
        return [{**empty, "status": "insufficient_coverage"}]
    grid = reference.window_transform(Window(col, r, width, height))
    inside = geometry_mask([mapping(poly)], (height, width), grid, invert=True)
    total = int(inside.sum())
    groups: dict[datetime, list[tuple[str, Any]]] = {}
    calibrations: dict[str, dict[str, Any]] = {}
    clear_classes = [int(v) for v in args.clear_scl_classes.split(",")]

    def aligned(asset: dict[str, Any]) -> Any:
        ds = dataset(asset)
        with WarpedVRT(
            ds,
            crs=reference.crs,
            transform=grid,
            width=width,
            height=height,
            dtype="float64",
            nodata=np.nan,
            resampling=Resampling.nearest,
        ) as vrt:
            values = vrt.read(1, masked=True).filled(np.nan)
        return values

    def reflectance(asset: dict[str, Any], quality_asset: dict[str, Any], calibration: dict[str, Any]) -> Any:
        ds = dataset(asset)
        extent = window_bounds(Window(0, 0, width, height), grid)
        source_bounds = transform_bounds(reference.crs, ds.crs, *extent, densify_pts=21)
        raw_window = from_bounds(*source_bounds, transform=ds.transform)
        left, top = max(0, math.floor(raw_window.col_off) - 2), max(0, math.floor(raw_window.row_off) - 2)
        right = min(ds.width, math.ceil(raw_window.col_off + raw_window.width) + 2)
        bottom = min(ds.height, math.ceil(raw_window.row_off + raw_window.height) + 2)
        output = np.full((height, width), np.nan)
        if right <= left or bottom <= top:
            return output
        if (right - left) * (bottom - top) > args.max_source_pixels:
            raise ValueError("Native band window exceeds max_source_pixels; reduce the parcel size")
        window = Window(left, top, right - left, bottom - top)
        source_grid = ds.window_transform(window)
        values = ds.read(1, window=window, masked=True).astype("float64").filled(np.nan)
        with WarpedVRT(
            dataset(quality_asset),
            crs=ds.crs,
            transform=source_grid,
            width=right - left,
            height=bottom - top,
            dtype="float64",
            nodata=np.nan,
            resampling=Resampling.nearest,
        ) as vrt:
            quality = vrt.read(1, masked=True).filled(np.nan)
        values[~np.isin(quality, clear_classes)] = np.nan
        reproject(
            source=values,
            destination=output,
            src_transform=source_grid,
            src_crs=ds.crs,
            src_nodata=np.nan,
            dst_transform=grid,
            dst_crs=reference.crs,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
        return output * calibration["scale"] + calibration["offset"]

    for scene in selected:
        assets = scene["assets"]
        missing = {first_key, second_key, args.scl_asset} - set(assets)
        if missing:
            raise ValueError(f"Scene {scene['id']} lacks required assets {sorted(missing)}; cloud masking is mandatory")
        calibration = {
            key: band_calibration(assets[key], dataset(assets[key]), scene, args.offset_mode)
            for key in (first_key, second_key)
        }
        calibrations[str(scene["id"])] = calibration
        a, b = (
            reflectance(assets[first_key], assets[args.scl_asset], calibration[first_key]),
            reflectance(assets[second_key], assets[args.scl_asset], calibration[second_key]),
        )
        quality = aligned(assets[args.scl_asset])
        valid = inside & np.isfinite(a) & np.isfinite(b) & np.isin(quality, clear_classes) & (np.abs(a + b) > 1e-12)
        values = np.full((height, width), np.nan)
        np.divide(a - b, a + b, out=values, where=valid)
        when = scene["_time"].replace(tzinfo=None)
        if args.composite == "month":
            when = when.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        groups.setdefault(when, []).append((str(scene["id"]), values))
    output = []
    previous: tuple[datetime, float] | None = None
    for when, items in sorted(groups.items()):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            values = np.nanmedian(np.stack([values for _, values in items]), axis=0)
        valid_count = int(np.isfinite(values).sum())
        coverage = valid_count / total if total else 0.0
        average = float(np.nanmean(values)) if valid_count else None
        usable = average is not None and coverage >= args.min_coverage
        delta = None
        if (
            average is not None
            and usable
            and previous is not None
            and (when - previous[0]).total_seconds() <= args.max_change_gap_days * 86400
        ):
            delta = average - previous[1]
        output.append(
            {
                **empty,
                "observed_at": when,
                "mean_index": average,
                "valid_pixels": valid_count,
                "total_pixels": total,
                "coverage_fraction": coverage,
                "delta_index": delta,
                "decline": delta <= -args.decline_threshold if delta is not None else None,
                "scene_count": len(items),
                "scene_ids": json.dumps([sid for sid, _ in items]),
                "calibration": json.dumps({sid: calibrations[sid] for sid, _ in items}, sort_keys=True),
                "status": "ok" if usable else "insufficient_coverage",
                "analysis_crs": reference.crs.to_string(),
            }
        )
        if usable and average is not None:
            previous = (when, float(average))
    return output


@dataclass(slots=True, frozen=True, kw_only=True)
class SeriesArgs:
    """Parcel relation and explicit observation, cloud, and resource controls."""

    data: Annotated[
        TableInput, Arg(0, doc="Rows with parcel_id and polygon geometry_wkb; NULL/invalid geometries fail.")
    ]
    catalog: Annotated[
        str,
        Arg(
            "catalog",
            default="demo://scenes.json",
            doc="STAC ItemCollection JSON path/URL or public API root with a search link.",
        ),
    ]
    geometry_crs: Annotated[
        str,
        Arg(
            "geometry_crs", default="EPSG:32618", doc="Explicit CRS of all input parcel WKB geometries; XY axis order."
        ),
    ]
    collections: Annotated[
        str, Arg("collections", default="sentinel-2-l2a", doc="Comma-separated collection IDs for STAC API search.")
    ]
    start_date: Annotated[
        str, Arg("start_date", default="2026-01-01T00:00:00Z", doc="Inclusive UTC ISO observation start.")
    ]
    end_date: Annotated[str, Arg("end_date", default="2026-12-31T23:59:59Z", doc="Inclusive UTC ISO observation end.")]
    index_name: Annotated[
        str,
        Arg(
            "index_name",
            default="ndvi",
            choices=["ndvi", "ndwi", "nbr"],
            doc=(
                "Normalized difference: NDVI=(NIR-red)/(NIR+red), NDWI=(green-NIR)/(green+NIR), "
                "NBR=(NIR-SWIR)/(NIR+SWIR)."
            ),
        ),
    ]
    composite: Annotated[
        str,
        Arg(
            "composite",
            default="month",
            choices=["month", "observation"],
            doc="Monthly pixel-median composites or individual observation-time composites.",
        ),
    ]
    red_asset: Annotated[str, Arg("red_asset", default="red", doc="STAC asset key for red reflectance.")]
    nir_asset: Annotated[str, Arg("nir_asset", default="nir08", doc="STAC asset key for near-infrared reflectance.")]
    green_asset: Annotated[
        str, Arg("green_asset", default="green", doc="STAC asset key for green reflectance when computing NDWI.")
    ]
    swir_asset: Annotated[
        str,
        Arg(
            "swir_asset", default="swir22", doc="STAC asset key for shortwave infrared reflectance when computing NBR."
        ),
    ]
    scl_asset: Annotated[
        str,
        Arg(
            "scl_asset",
            default="scl",
            doc="Required categorical Scene Classification Layer asset; never bilinearly resampled.",
        ),
    ]
    offset_mode: Annotated[
        str,
        Arg(
            "offset_mode",
            default="auto",
            choices=["auto", "metadata", "zero"],
            doc=(
                "auto honors Earth Search's already-applied BOA flag for GeoTIFF assets, otherwise uses band "
                "metadata. metadata always applies declared offsets; zero ignores offsets for independently "
                "verified harmonized data. Effective calibration is returned for audit."
            ),
        ),
    ]
    clear_scl_classes: Annotated[
        str,
        Arg(
            "clear_scl_classes",
            default="4,5,6",
            doc="Comma-separated SCL codes treated as clear; default vegetation, bare soil, and water.",
        ),
    ]
    max_cloud: Annotated[
        float,
        Arg(
            "max_cloud",
            default=100.0,
            ge=0,
            le=100,
            doc="Maximum scene-level cloud percentage; pixel cloud masks are still applied.",
        ),
    ]
    min_coverage: Annotated[
        float,
        Arg(
            "min_coverage",
            default=0.5,
            ge=0,
            le=1,
            doc="Minimum clear pixel fraction before a result participates in change detection.",
        ),
    ]
    decline_threshold: Annotated[
        float,
        Arg("decline_threshold", default=0.3, gt=0, doc="Minimum absolute negative index change to flag as a decline."),
    ]
    max_change_gap_days: Annotated[
        float,
        Arg(
            "max_change_gap_days",
            default=90.0,
            gt=0,
            doc="Maximum time separation between change-comparison observations.",
        ),
    ]
    max_scenes: Annotated[
        int,
        Arg(
            "max_scenes",
            default=128,
            ge=1,
            doc="Hard scene limit; an oversized result fails instead of silently truncating.",
        ),
    ]
    max_pages: Annotated[
        int, Arg("max_pages", default=100, ge=1, doc="Hard STAC pagination bound; cyclic or excessive paging fails.")
    ]
    max_pixels: Annotated[
        int,
        Arg(
            "max_pixels",
            default=1000000,
            ge=1,
            doc=(
                "Maximum aligned bounding-window pixels per parcel; a 20 million pixel-scene memory cap also applies."
            ),
        ),
    ]
    max_json_bytes: Annotated[
        int,
        Arg(
            "max_json_bytes",
            default=16777216,
            ge=1024,
            doc="Maximum decoded bytes per STAC JSON page or static document.",
        ),
    ]
    max_source_pixels: Annotated[
        int,
        Arg(
            "max_source_pixels",
            default=4000000,
            ge=1,
            doc="Maximum native pixels per reflectance crop before cloud masking and resampling.",
        ),
    ]


SERIES_SQL = (
    "SELECT parcel_id, round(mean_index, 2), decline FROM "
    "earth_observation.main.parcel_series((SELECT * FROM "
    "earth_observation.main.sample_parcels)) ORDER BY parcel_id, observed_at"
)

# Composed: optical decline counts joined to the VV backscatter change on each viewing track.
OPTICAL_RADAR_SQL = (
    "WITH optical AS (SELECT parcel_id, count(*) FILTER (WHERE decline) AS ndvi_declines, "
    "min(delta_index) AS worst_ndvi_change FROM earth_observation.main.parcel_series("
    "(SELECT parcel_id, geometry_wkb FROM earth_observation.main.sample_parcels)) GROUP BY parcel_id), "
    "radar AS (SELECT region_id, relative_orbit, orbit_state, "
    "arg_max(mean_db, observed_at) - arg_min(mean_db, observed_at) AS vv_change_db "
    "FROM earth_observation.main.radar_series((SELECT parcel_id AS region_id, geometry_wkb "
    "FROM earth_observation.main.sample_parcels), polarizations := 'vv') WHERE status = 'ok' "
    "GROUP BY region_id, relative_orbit, orbit_state) "
    "SELECT o.parcel_id, o.ndvi_declines, round(o.worst_ndvi_change, 2) AS worst_ndvi_change, "
    "r.relative_orbit, r.orbit_state, round(r.vv_change_db, 1) AS vv_change_db "
    "FROM optical AS o JOIN radar AS r ON r.region_id = o.parcel_id "
    "ORDER BY o.parcel_id, r.relative_orbit, r.orbit_state"
)
OPTICAL_RADAR_DESCRIPTION = (
    "Per parcel, join the count of optical NDVI declines and the worst NDVI change to the VV "
    "backscatter change in dB between the first and last fully covered radar acquisitions on each orbit track"
)


class ParcelSeries(TableInOutGenerator[SeriesArgs]):
    """Return observation/composite rows for each input parcel."""

    class Meta:
        """Parcel history discovery metadata."""

        name = "parcel_series"
        description = "Cloud-mask and align satellite bands into parcel index histories and decline flags"
        tags = metadata(
            "Parcel satellite history",
            GIS_DESCRIPTION
            + (
                " SCL codes 4,5,6 are clear by default; missing quality assets fail. Pixel centres "
                "determine polygon membership. No-scenes and low-coverage outcomes are explicit. "
                "Declines indicate index changes, not a diagnosed cause. Public HTTP access uses "
                "timeouts and bounded pagination."
            ),
            RESULT,
            SERIES_SQL,
            [["field-a", 0.6, None], ["field-a", 0.0, True], ["field-b", 0.6, None], ["field-b", 0.0, True]],
            more=[
                (
                    OPTICAL_RADAR_DESCRIPTION,
                    OPTICAL_RADAR_SQL,
                    [
                        ["field-a", 1, -0.6, 77, "ascending", -10.0],
                        ["field-b", 1, -0.6, 77, "ascending", -10.0],
                    ],
                )
            ],
        )
        examples = [
            *example(SERIES_SQL, "Find vegetation declines in two synthetic parcels"),
            *example(OPTICAL_RADAR_SQL, OPTICAL_RADAR_DESCRIPTION),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[SeriesArgs]) -> BindResponse:
        """Require parcel identities and WKB geometries."""
        finite_parameters(params.args)
        if timestamp(params.args.start_date) > timestamp(params.args.end_date):
            raise ValueError("start_date must be at or before end_date")
        try:
            codes = {int(code.strip()) for code in params.args.clear_scl_classes.split(",")}
        except ValueError as exc:
            raise ValueError("clear_scl_classes must contain integer SCL codes from 0 through 11") from exc
        if not codes or not codes <= set(range(12)):
            raise ValueError("clear_scl_classes must contain integer SCL codes from 0 through 11")
        require_columns(params.bind_call.input_schema, ("parcel_id", "geometry_wkb"))
        return BindResponse(output_schema=RESULT)

    @classmethod
    def process(
        cls, params: ProcessParams[SeriesArgs], state: None, batch: pa.RecordBatch, out: OutputCollector
    ) -> None:
        """Share scene search and opened datasets across each input Arrow batch."""
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
        with ExitStack() as stack:
            datasets: dict[str, Any] = {}
            emit_rows(
                (item for row in rows for item in parcel_series(row, scenes, params.args, stack, datasets)),
                params.output_schema,
                out,
            )


@dataclass(slots=True, frozen=True, kw_only=True)
class SampleParcelsArgs:
    """No arguments: use the bundled synthetic parcels."""


@dataclass(kw_only=True)
class Cursor(ArrowSerializableDataclass):
    """Whether the sample parcels have been sent."""

    done: bool = False


SAMPLE_PARCELS_SQL = "SELECT parcel_id FROM earth_observation.main.sample_parcels ORDER BY parcel_id"


@init_single_worker
@bind_fixed_schema
class SampleParcels(TableFunctionGenerator[SampleParcelsArgs, Cursor]):
    """Return polygons covering the offline synthetic satellite scenes."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = INPUT

    class Meta:
        """Synthetic parcel metadata."""

        name = "sample_parcels"
        description = "Two synthetic agricultural parcels matching the bundled offline satellite scenes"
        tags = metadata(
            "Synthetic test parcels",
            "Two polygons in EPSG:32618 over demo://scenes.json. These are synthetic test "
            "parcels with known cloud masks and reflectances, not observations of real farms.",
            INPUT,
            SAMPLE_PARCELS_SQL,
            [["field-a"], ["field-b"]],
        )
        examples = example(SAMPLE_PARCELS_SQL, "List the bundled parcel IDs")

    @classmethod
    def initial_state(cls, params: ProcessParams[SampleParcelsArgs]) -> Cursor:
        """Start before the sample rows."""
        return Cursor()

    @classmethod
    def process(cls, params: ProcessParams[SampleParcelsArgs], state: Cursor, out: OutputCollector) -> None:
        """Send both polygons once."""
        if state.done:
            out.finish()
            return
        out.emit(
            pa.RecordBatch.from_pylist(
                [
                    {"parcel_id": "field-a", "geometry_wkb": box(500000, 4100000, 500040, 4100040).wkb},
                    {"parcel_id": "field-b", "geometry_wkb": box(500020, 4100000, 500040, 4100040).wkb},
                ],
                schema=params.output_schema,
            )
        )
        state.done = True


GIS_FUNCTIONS: list[type[Any]] = [SampleParcels, ParcelSeries]

GIS_TABLES = [
    Table(
        name="sample_parcels",
        function=SampleParcels,
        comment=SampleParcels.Meta.description,
        constraints=(
            constraint_record(
                ["main"],
                "sample_parcels",
                "id_not_null",
                "NOT_NULL",
                [["parcel_id"]],
                enforcement="ENFORCED",
                validation="ALL_ROWS",
            ),
            constraint_record(
                ["main"],
                "sample_parcels",
                "sample_parcels_pk",
                "PRIMARY_KEY",
                [["parcel_id"]],
                enforcement="ENFORCED",
                validation="ALL_ROWS",
            ),
        ),
        tags={
            **SampleParcels.Meta.tags,
            "domain": "geospatial",
            "vgi.example_queries": json.dumps(
                [{"description": ex.description, "sql": ex.sql} for ex in SampleParcels.Meta.examples]
            ),
        },
        column_comments={f.name: (f.metadata or {}).get(b"comment", b"").decode() for f in SampleParcels.FIXED_SCHEMA},
    )
]
