-- Offline acceptance demos. Run with: uv run --with duckdb==2.0.0 python scripts/run_demo.py
-- The runner loads VGI, attaches this worker, and installs Spatial for geometry joins.

-- Sample parcels on bundled synthetic data
SELECT parcel_id FROM earth_observation.main.sample_parcels ORDER BY parcel_id;

-- Parcel satellite history on bundled synthetic data
SELECT parcel_id, round(mean_index, 2), decline FROM earth_observation.main.parcel_series((SELECT * FROM earth_observation.main.sample_parcels)) ORDER BY parcel_id, observed_at;

-- Declines joined to ownership and weather
WITH owners(parcel_id, owner_name) AS (VALUES ('field-a', 'North Farm'), ('field-b', 'River Farm')),
weather(month_start, rain_mm) AS (VALUES (DATE '2026-07-01', 10.0)),
series AS (SELECT * FROM earth_observation.main.parcel_series((SELECT * FROM earth_observation.main.sample_parcels)))
SELECT s.parcel_id, o.owner_name, round(s.delta_index, 2) AS delta_ndvi, w.rain_mm::DOUBLE AS rain_mm
FROM series s JOIN owners o USING(parcel_id)
JOIN weather w ON s.observed_at::DATE = w.month_start
WHERE s.decline ORDER BY s.parcel_id;
