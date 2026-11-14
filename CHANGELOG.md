# Changelog

## 0.1.0 (2026-11-14)

First release.

- `parcel_series`: Sentinel-2 optical index histories (NDVI/NDWI/NBR) per parcel from
  public STAC APIs or static ItemCollections, with SCL cloud masking, monthly
  pixel-median composites, coverage fractions and decline detection.
- `radar_series`: prepared SAR intensity (dB) histories per region and orbit track.
- `demo_input` table plus bundled offline demo scenes; no credentials needed.
- Composed example joining optical NDVI declines to VV backscatter change per parcel.

Targets: vgi-python 0.51 · VGI protocol 3.3 · Haybarn 2.0.0rc11 · vgi-lint-check 2.x (unpinned)
