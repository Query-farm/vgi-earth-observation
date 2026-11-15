"""Discovery metadata: the one file to edit when you rename or extend the worker.

DuckDB, coding agents and the ``vgi-lint`` metadata checker all read these
tags. Keep every object documented and ``make lint`` stays at 100.

* Catalog-level tags (title, license, author, links, long description) live in
  ``CATALOG_TAGS`` below.
* Every function and table carries ``object_tags(...)``: a title, a category
  from ``SCHEMA_CATEGORIES``, search keywords, and LLM- and human-facing docs.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa

CATALOG_NAME = "earth_observation"

SOURCE_URL = "https://github.com/Query-farm/vgi-earth-observation"

# Categories group functions in docs and agent tools. Every object's
# ``vgi.category`` must name one of these.
SCHEMA_CATEGORIES = [
    {
        "name": "gis",
        "title": "GIS Analysis",
        "description": "Spatial analysis over query results and geospatial assets.",
    },
]


def keywords_array(keywords: str) -> str:
    """Turn ``"a, b, c"`` into the JSON array string ``vgi.keywords`` requires."""
    return json.dumps([k.strip() for k in keywords.split(",") if k.strip()])


def object_tags(*, title: str, category: str, keywords: str, doc_llm: str, doc_md: str) -> dict[str, str]:
    """Build the standard per-object discovery tags."""
    if category not in {c["name"] for c in SCHEMA_CATEGORIES}:
        raise ValueError(f"unknown category {category!r}; add it to SCHEMA_CATEGORIES")
    return {
        "vgi.title": title,
        "vgi.category": category,
        "vgi.keywords": keywords_array(keywords),
        "vgi.doc_llm": doc_llm,
        "vgi.doc_md": doc_md,
    }


def column(name: str, type_: pa.DataType, comment: str, *, nullable: bool = True) -> pa.Field[Any]:
    """Build an Arrow field whose ``comment`` shows up in DuckDB's ``DESCRIBE`` and agent tools."""
    return pa.field(name, type_, nullable=nullable, metadata={b"comment": comment.encode()})


AGENT_TEST_TASKS = [
    {
        "name": "gis_sample_parcels",
        "prompt": "List the bundled parcel IDs. Use the bundled synthetic inputs and return the "
        "columns and ordering shown in its catalog example.",
    },
    {
        "name": "gis_parcel_series",
        "prompt": "Find vegetation declines in two synthetic parcels. Use the bundled synthetic "
        "inputs and return the columns and ordering shown in its catalog example.",
    },
    {
        "name": "gis_radar_series",
        "prompt": "Inspect the synthetic VV radar history for field-a using the radar_series function. "
        "Return observed_date, rounded mean_db and status, ordered by acquisition as in its catalog example.",
    },
    {
        "name": "gis_optical_radar_drop",
        "prompt": "Using the bundled synthetic parcels, which parcels have both an optical NDVI decline and a "
        "VV radar backscatter drop of at least 5 dB between consecutive fully covered (status 'ok') "
        "acquisitions on the same orbit track? Return two columns: parcel_id (VARCHAR) and "
        "largest_vv_drop_db (DOUBLE, the largest such drop as a positive dB value rounded to 1 decimal), "
        "ordered by parcel_id.",
    },
]

CATALOG_TAGS = {
    "vgi.title": "Earth Observation Time Series",
    # vgi.subtitle, vgi.doc_llm and vgi.doc_md are set in worker.py from the GIS descriptions.
    "vgi.source_url": SOURCE_URL,
    "vgi.icon_url": "https://query.farm/media-kit/products/vgi/vgi-logo.png",
    "vgi.license": "0BSD",
    "vgi.author": "Query.Farm",
    "vgi.copyright": "Copyright 2026 Query.Farm LLC",
    "vgi.keywords": keywords_array(
        "vgi, earth observation, optical indices, radar intensity, STAC, raster, parcel histories"
    ),
    "vgi.support_contact": f"{SOURCE_URL}/issues",
    "vgi.support_policy_url": f"{SOURCE_URL}#support",
    # Public analyst tasks for `vgi-lint simulate`; private graders live in vgi-agent-tests.yaml.
    "vgi.agent_test_tasks": json.dumps(AGENT_TEST_TASKS),
}
