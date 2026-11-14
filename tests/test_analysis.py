import json
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from earth_observation_worker.analysis import band_calibration, parcel_series, read_scenes


def args(**updates: Any) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "catalog": "demo://scenes.json",
            "geometry_crs": "EPSG:32618",
            "collections": "sentinel-2-l2a",
            "start_date": "2026-01-01T00:00:00Z",
            "end_date": "2026-12-31T23:59:59Z",
            "max_cloud": 100.0,
            "max_pages": 100,
            "max_scenes": 128,
            "red_asset": "red",
            "nir_asset": "nir08",
            "green_asset": "green",
            "swir_asset": "swir22",
            "scl_asset": "scl",
            "index_name": "ndvi",
            "composite": "month",
            "offset_mode": "auto",
            "clear_scl_classes": "4,5,6",
            "max_pixels": 1000000,
            "max_source_pixels": 4000000,
            "max_json_bytes": 16777216,
            "min_coverage": 0.5,
            "decline_threshold": 0.3,
            "max_change_gap_days": 90.0,
            **updates,
        }
    )


def inputs() -> Any:
    return {"parcel_id": "a", "geometry_wkb": box(500000, 4100000, 500040, 4100040).wkb}


def scenes(a: Any) -> Any:
    return read_scenes(a, (-180.0, -90.0, 180.0, 90.0))


def run(a: Any, items: Any = None) -> Any:
    with ExitStack() as stack:
        return parcel_series(inputs(), scenes(a) if items is None else items, a, stack, {})


def test_cloud_mask_alignment_composite_and_decline() -> None:
    result = run(args())
    assert [r["mean_index"] for r in result] == pytest.approx([0.6, 0.0])
    assert [r["valid_pixels"] for r in result] == [12, 16]
    assert result[0]["scene_count"] == 2
    assert result[0]["coverage_fraction"] == 0.75
    assert result[1]["delta_index"] == pytest.approx(-0.6)
    assert result[1]["decline"] is True


def test_all_cloud_observation_is_unknown_not_zero() -> None:
    result = run(args(composite="observation"))
    assert len(result) == 3
    assert result[1]["mean_index"] is None
    assert result[1]["status"] == "insufficient_coverage"
    assert result[1]["decline"] is None
    assert result[2]["delta_index"] == pytest.approx(-0.6)


def test_scale_and_offset_metadata_are_applied() -> None:
    a = args()
    items = scenes(a)[:1]
    items[0]["assets"]["red"]["raster:bands"][0]["offset"] = 0.1
    assert run(a, items)[0]["mean_index"] == pytest.approx((0.8 - 0.3) / (0.8 + 0.3))


def test_no_data_missing_quality_and_resource_limits() -> None:
    assert run(args(), [])[0]["status"] == "no_scenes"
    a = args()
    items = scenes(a)
    del items[0]["assets"]["scl"]
    with pytest.raises(ValueError, match="cloud masking is mandatory"):
        run(a, items)
    with pytest.raises(ValueError, match="budget"):
        run(args(max_pixels=1))
    with pytest.raises(ValueError, match="max_scenes"):
        scenes(args(max_scenes=1))


def test_dates_coverage_and_gap_control_change_detection() -> None:
    assert len(scenes(args(start_date="2026-07-01"))) == 1
    result = run(args(min_coverage=0.9))
    assert result[0]["status"] == "insufficient_coverage"
    assert result[1]["delta_index"] is None
    assert run(args(max_change_gap_days=10))[1]["decline"] is None


@pytest.mark.parametrize(
    ("index_name", "expected"), [("ndwi", (0.3 - 0.8) / (0.3 + 0.8)), ("nbr", (0.8 - 0.4) / (0.8 + 0.4))]
)
def test_other_normalized_difference_indices(index_name: Any, expected: Any) -> None:
    a = args(index_name=index_name)
    assert run(a, scenes(a)[:1])[0]["mean_index"] == pytest.approx(expected)


def test_cloud_values_cannot_bleed_into_clear_pixels_during_downsampling(tmp_path: Path) -> None:
    a = args()
    items = scenes(a)[:1]
    nir = np.full((4, 4), 0.8, dtype="float32")
    nir[:, 2:] = 1000.0
    scl = np.full((4, 4), 4, dtype="float32")
    scl[:, 2:] = 9
    for key, values in [("red", np.full((2, 2), 0.2, dtype="float32")), ("nir08", nir), ("scl", scl)]:
        path = tmp_path / f"{key}.tif"
        resolution = 40 / values.shape[0]
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=values.shape[1],
            height=values.shape[0],
            count=1,
            dtype="float32",
            crs="EPSG:32618",
            transform=from_origin(500000, 4100040, resolution, resolution),
            nodata=-9999,
        ) as dst:
            dst.write(values, 1)
        items[0]["assets"][key] = {"href": str(path), "raster:bands": [{"scale": 1, "offset": 0}]}
    result = run(a, items)[0]
    assert result["valid_pixels"] == 2
    assert result["total_pixels"] == 4
    assert result["mean_index"] == pytest.approx(0.6)
    with pytest.raises(ValueError, match="max_source_pixels"):
        run(args(max_source_pixels=1), items)
    items[0]["assets"]["red"]["raster:bands"][0]["scale"] = float("nan")
    with pytest.raises(ValueError, match="scale must be finite"):
        run(a, items)


def test_harmonized_cogs_do_not_subtract_boa_offset_twice() -> None:
    a = args()
    items = scenes(a)[:1]
    items[0]["properties"]["earthsearch:boa_offset_applied"] = True
    for key in ("red", "nir08"):
        items[0]["assets"][key]["type"] = "image/tiff; application=geotiff; profile=cloud-optimized"
        items[0]["assets"][key]["raster:bands"][0]["offset"] = -0.1
    result = run(a, items)[0]
    assert result["mean_index"] == pytest.approx(0.6)
    audit = next(iter(json.loads(result["calibration"]).values()))
    assert audit["red"]["offset"] == 0
    assert audit["red"]["source"] == "earthsearch:boa_offset_applied"
    assert run(args(offset_mode="metadata"), items)[0]["mean_index"] == pytest.approx(0.75)
    items[0]["properties"]["earthsearch:boa_offset_applied"] = False
    assert run(a, items)[0]["mean_index"] == pytest.approx(0.75)
    assert run(args(offset_mode="zero"), items)[0]["mean_index"] == pytest.approx(0.6)


def test_harmonization_flag_does_not_override_original_jp2_or_missing_flag() -> None:
    ds = SimpleNamespace(scales=(0.0001,), offsets=(-0.1,))
    scene = {"properties": {"earthsearch:boa_offset_applied": True}}
    jp2 = {"type": "image/jp2"}
    assert band_calibration(jp2, ds, scene, "auto")["offset"] == -0.1
    assert band_calibration({"type": "image/tiff"}, ds, {}, "auto")["offset"] == -0.1
