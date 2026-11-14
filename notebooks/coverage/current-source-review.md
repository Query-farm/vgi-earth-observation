# Catalog and unchanged source review

Released v0.1.0 and current source resolve to 77a249112bafc8ed34f0acadfce6dcb6f2d1c343. The actual full catalog exports seven function signatures and one table, grouped into six logical names: main.demo_input (table/function alias), main.parcel_series, main.radar_series, examples.greet (two overloads), examples.sequence_table and examples.fetch_items. All are included; no example/authenticated functions are omitted to narrow coverage. fetch_items accepts an optional earth_observation_api SQL secret or EARTH_OBSERVATION_API_TOKEN environment variable. This example uses a credential-rejecting local authored fixture and isolates that environment setting.

Source registration in worker.py combines GIS_FUNCTIONS, RadarSeries and GIS_TABLES in main, and Greet/GreetWithStyle/SequenceTable/FetchItems in examples. Full worker/dependency content is identical to the tag. Actual current-source runtime is therefore identical to the executed release. Raster measurements execute in the worker; no analytical replacement implementation is used by the notebooks.

Exact-source CI43615415139 is successful and reused for notebook-only work (lint/unit/examples/SQL transports/metadata). New notebook code has separate execution, experiment, bootstrap, presentation and integrity checks. No worker build, dependency changes or source tests are repeated.
