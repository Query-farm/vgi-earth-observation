-- Optional live integration: public Sentinel-2 COGs, no credentials.
INSTALL spatial;
LOAD spatial;
SELECT parcel_id, observed_at, mean_index, valid_pixels, total_pixels, scene_ids, calibration, status,
       CASE WHEN count(mean_index) OVER () > 0
                 AND bool_and(mean_index BETWEEN -1 AND 1) OVER () THEN 'clear_pixels_verified'
            ELSE error('Live STAC query returned no clear pixels or implausible NDVI for this parcel') END AS live_check
FROM earth_observation.main.parcel_series(
  (SELECT 'iowa-field' AS parcel_id,
          ST_AsWKB(ST_MakeEnvelope(-93.502, 41.898, -93.498, 41.902)) AS geometry_wkb),
  catalog := 'https://earth-search.aws.element84.com/v1', geometry_crs := 'EPSG:4326',
  start_date := '2024-06-01', end_date := '2024-06-10T23:59:59Z',
  composite := 'observation', max_scenes := 32, max_pixels := 20000)
ORDER BY observed_at;
