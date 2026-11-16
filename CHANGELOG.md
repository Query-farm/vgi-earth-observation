# Changelog

## 0.2.2 (2026-11-16)

- Bump vgi-python to 0.52.2.

## 0.2.1

- Docs link to upstream sources: the catalog `vgi.doc_md` gains a Sources and references section linking the STAC specification, Earth Search, the Sentinel-2 processing notes (SCL codes), the Sentinel-1 RTC collection on Planetary Computer and Rasterio (VGI183).
- The README quick-start install pin now points at v0.2.1.

## 0.2.0 (2026-11-14)

- Removed the starter template's demo schema (`examples.greet`/`fetch_items`/`sequence_table`),
  `demo_input` and the placeholder `earth_observation_api` secret type. The worker now
  exposes only its `main` schema and needs no credentials.
- The bundled synthetic parcels are now `main.sample_parcels` (formerly `main.demo_input`);
  same two rows and columns.
- Dropped the starter's SDK recipe scripts (`examples/*.py`), mock REST API and their tests.
- The catalog comment is now a short plain-text description of what the worker covers; the detailed analysis notes remain in the catalog's `vgi.doc_md` and `vgi.doc_llm` documentation.

## 0.1.0 (2026-11-14)

First release.

- `parcel_series`: Sentinel-2 optical index histories (NDVI/NDWI/NBR) per parcel from
  public STAC APIs or static ItemCollections, with SCL cloud masking, monthly
  pixel-median composites, coverage fractions and decline detection.
- `radar_series`: prepared SAR intensity (dB) histories per region and orbit track.
- `demo_input` table plus bundled offline demo scenes; no credentials needed.
- Composed example joining optical NDVI declines to VV backscatter change per parcel.

Targets: vgi-python 0.51 · VGI protocol 3.3 · Haybarn 2.0.0rc11 · vgi-lint-check 2.x (unpinned)
