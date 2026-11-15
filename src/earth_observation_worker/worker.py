"""Assemble the functions into a catalog and run the worker.

Optical functions live in ``analysis.py`` and radar functions in ``radar.py``;
add a new function class to ``GIS_FUNCTIONS`` below.

Run it:

    vgi-earth-observation-worker                      # stdio: what DuckDB launches by default
    vgi-earth-observation-worker --http --port 8000   # HTTP: for Docker / hosted deploys
    vgi-earth-observation-worker --help

Attach it from DuckDB:

    ATTACH 'earth_observation' (TYPE vgi, LOCATION '/abs/path/to/.venv/bin/vgi-earth-observation-worker');
"""

from __future__ import annotations

import json
from typing import Any

from vgi import Worker
from vgi.catalog import Catalog, Schema

from .analysis import GIS_DESCRIPTION, GIS_FUNCTIONS, GIS_TABLES
from .meta import (
    CATALOG_NAME,
    CATALOG_TAGS,
    SCHEMA_CATEGORIES,
    SOURCE_URL,
    keywords_array,
)
from .radar import RADAR_DESCRIPTION, RadarSeries

GIS_FUNCTIONS = [*GIS_FUNCTIONS, RadarSeries]
GIS_DESCRIPTION = GIS_DESCRIPTION + " " + RADAR_DESCRIPTION


# One-paragraph catalog description shown in directories (VGI001); the long
# analysis guidance lives in the vgi.doc_md / vgi.doc_llm tags.
CATALOG_COMMENT = (
    "Satellite imagery histories for land parcels and regions from STAC catalogs: cloud-masked Sentinel-2 "
    "vegetation and water indices and calibrated Sentinel-1 radar backscatter, with change flags. Reads "
    "local STAC files or public STAC APIs; offline sample scenes are included."
)


def _stamp_example_queries(functions: list[type[Any]]) -> list[dict[str, str]]:
    """Publish each function's examples as ``vgi.example_queries`` (with descriptions).

    Overloads share one SQL name, so their examples are merged per name.
    Returns every example, for the schema-level list.
    """
    by_name: dict[str, list[dict[str, str]]] = {}
    for fn in functions:
        bucket = by_name.setdefault(fn.Meta.name, [])
        for ex in getattr(fn.Meta, "examples", []):
            entry = {"description": ex.description, "sql": ex.sql}
            if entry not in bucket:
                bucket.append(entry)
    for fn in functions:
        fn.Meta.tags = {**fn.Meta.tags, "vgi.example_queries": json.dumps(by_name[fn.Meta.name])}
    return [entry for entries in by_name.values() for entry in entries]


_GIS_EXAMPLES = _stamp_example_queries(GIS_FUNCTIONS)


class EarthObservationWorker(Worker):
    """The worker process: one catalog, one ``main`` schema."""

    catalog = Catalog(
        name=CATALOG_NAME,
        comment=CATALOG_COMMENT,
        source_url=SOURCE_URL,
        tags={
            **CATALOG_TAGS,
            "vgi.doc_llm": GIS_DESCRIPTION,
            "vgi.doc_md": "## GIS analysis\n\n" + GIS_DESCRIPTION + "\n\n### SQL workflow\n\n"
            "Pass query results to the main schema functions, retaining IDs for joins. "
            "Use bundled synthetic data to try the executable examples without network access. "
            "CRS, coverage, inference limits and resource bounds are explicit function parameters.",
            "vgi.subtitle": "Optical index and calibrated radar histories for geospatial SQL queries.",
        },
        schemas=[
            Schema(
                path=["main"],
                comment="Spatial functions and bundled inputs for " + CATALOG_NAME.replace("_", " ") + " analysis.",
                functions=GIS_FUNCTIONS,
                tables=GIS_TABLES,
                tags={
                    "vgi.title": "GIS Analysis",
                    "domain": "geospatial",
                    "vgi.keywords": keywords_array("GIS, spatial, raster, analysis"),
                    "vgi.doc_llm": GIS_DESCRIPTION,
                    "vgi.doc_md": "## GIS analysis\n\n" + GIS_DESCRIPTION + "\n\n### SQL workflow\n\n"
                    "Pass query results to the main schema functions, retaining IDs for joins. "
                    "Use bundled synthetic data to try the executable examples without network access. "
                    "CRS, coverage, inference limits and resource bounds are explicit function parameters.",
                    "vgi.categories": json.dumps(SCHEMA_CATEGORIES),
                    "vgi.example_queries": json.dumps(_GIS_EXAMPLES),
                },
            ),
        ],
    )


def main() -> None:
    """Console-script entry point: parse ``--http`` / ``--unix`` / logging flags and serve."""
    EarthObservationWorker.main()


if __name__ == "__main__":
    main()
