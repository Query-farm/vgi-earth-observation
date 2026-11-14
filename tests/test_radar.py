from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from earth_observation_worker.analysis import read_scenes
from earth_observation_worker.radar import check_grid, radar_channels, radar_series, region_measurement, viewing_group


def controls(**updates: Any) -> Any:
    return SimpleNamespace(
        **{
            "catalog": "demo://radar-scenes.json",
            "geometry_crs": "EPSG:32618",
            "collections": "sentinel-1-rtc",
            "start_date": "2025-01-01",
            "end_date": "2025-12-31",
            "max_cloud": 100.0,
            "max_pages": 100,
            "max_scenes": 128,
            "max_json_bytes": 16777216,
            "polarizations": "vv,vh",
            "value_unit": "linear_power",
            "min_coverage": 0.9,
            "max_pixels": 1000000,
            **updates,
        }
    )


def region() -> dict[str, Any]:
    return {"region_id": "candidate", "geometry_wkb": box(500000, 4100000, 500040, 4100040).wkb}


def raster(path: Path, values: Any, *, shift: float = 0) -> Path:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=4,
        height=4,
        count=1,
        dtype="float32",
        crs="EPSG:32618",
        transform=from_origin(500000 + shift, 4100040, 10, 10),
        nodata=-9999,
    ) as dst:
        dst.write(np.asarray(values, dtype="float32"), 1)
    return path


def test_known_backscatter_change_and_missing_observation() -> None:
    args = controls()
    scenes = read_scenes(args, (-180, -90, 180, 90))
    rows = radar_series([region()], scenes, args)
    assert len(rows) == 6
    assert [r["mean_db"] for r in rows[:4]] == pytest.approx([-10, -20, -20, -30])
    assert [r["mean_db"] for r in rows[4:]] == [None, None]
    assert rows[4]["coverage_fraction"] == 0
    assert rows[4]["status"] == "insufficient_coverage"
    assert {r["relative_orbit"] for r in rows} == {77}
    assert [r["polarization"] for r in rows[:2]] == ["VV", "VH"]


def test_average_linear_power_before_logarithm(tmp_path: Path) -> None:
    values = np.full((4, 4), -10.0)
    values[:, 2:] = 0
    with rasterio.open(raster(tmp_path / "db.tif", values)) as ds:
        result = region_measurement(region(), ds, {}, controls(value_unit="db"))
    assert result["mean_power"] == pytest.approx(0.55)
    assert result["mean_db"] == pytest.approx(10 * np.log10(0.55))
    assert result["mean_db"] != pytest.approx(-5)


def test_invalid_samples_and_partial_outside_coverage(tmp_path: Path) -> None:
    values = np.full((4, 4), 0.1)
    values[0, :] = [-9999, np.nan, -1, 0]
    with rasterio.open(raster(tmp_path / "masked.tif", values)) as ds:
        result = region_measurement(region(), ds, {}, controls())
        assert result["valid_pixels"] == 12
        assert result["total_pixels"] == 16
        assert result["coverage_fraction"] == 0.75
        assert result["mean_db"] is None
        accepted = region_measurement(region(), ds, {}, controls(min_coverage=0.5))
        assert accepted["mean_db"] == pytest.approx(-10)
        outside = {"region_id": "outside", "geometry_wkb": box(500100, 4100000, 500140, 4100040).wkb}
        assert region_measurement(outside, ds, {}, controls())["mean_power"] is None
        tiny = {"region_id": "tiny", "geometry_wkb": box(500000, 4100000, 500001, 4100001).wkb}
        assert region_measurement(tiny, ds, {}, controls())["status"] == "no_pixel_centres"
        with pytest.raises(ValueError, match="max_pixels"):
            region_measurement(region(), ds, {}, controls(max_pixels=4))


def test_native_grid_alignment_and_scale_validation(tmp_path: Path) -> None:
    values = np.full((4, 4), 1)
    with ExitStack() as stack:
        reference = stack.enter_context(rasterio.open(raster(tmp_path / "reference.tif", values)))
        aligned = stack.enter_context(rasterio.open(raster(tmp_path / "aligned.tif", values, shift=20)))
        shifted = stack.enter_context(rasterio.open(raster(tmp_path / "shifted.tif", values, shift=5)))
        check_grid(aligned, reference)
        with pytest.raises(ValueError, match="unaligned"):
            check_grid(shifted, reference)
        result = region_measurement(region(), reference, {"raster:bands": [{"scale": 0.1, "offset": 0}]}, controls())
        assert result["mean_db"] == pytest.approx(-10)
        with pytest.raises(ValueError, match="scale"):
            region_measurement(region(), reference, {"raster:bands": [{"scale": float("nan")}]}, controls())


def test_missing_metadata_assets_no_scenes_and_channel_validation() -> None:
    args = controls()
    assert radar_series([], [], args) == []
    assert radar_series([region()], [], args)[0]["status"] == "no_scenes"
    scenes = read_scenes(args, (-180, -90, 180, 90))[:1]
    del scenes[0]["properties"]["sat:relative_orbit"]
    with pytest.raises(ValueError, match="relative_orbit"):
        radar_series([region()], scenes, args)
    scenes[0]["properties"]["sat:relative_orbit"] = 77
    del scenes[0]["assets"]["vh"]
    with pytest.raises(ValueError, match="missing required"):
        radar_series([region()], scenes, args)
    for bad in ["", "VV,vv", "hh", "vv,"]:
        with pytest.raises(ValueError, match="polarizations"):
            radar_channels(bad)


def test_platform_casing_does_not_split_one_satellite_into_two_histories() -> None:
    scenes = read_scenes(controls(), (-180, -90, 180, 90))
    scenes[0]["properties"]["platform"] = " SENTINEL-1A "
    assert viewing_group(scenes[0], "vv") == viewing_group(scenes[1], "vv")
    scenes[0]["properties"]["platform"] = "sentinel-1c"
    assert viewing_group(scenes[0], "vv") != viewing_group(scenes[1], "vv")
