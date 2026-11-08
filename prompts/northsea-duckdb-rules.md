# NorthSea DuckDB rules

Follow every rule in this file.

## Connect to the database

```python
import duckdb
with duckdb.connect(DATABASE_PATH, read_only=True) as con:
    con.execute("LOAD spatial")
```

- Never change the database.
- Write files only to the folder in `os.environ["RWEE_ARTIFACT_DIR"]`.
- Only printed output comes back. Output over 100,000 bytes is cut from the start, so print summaries of large results.

## Use the metadata

The metadata file has one item in `layers` for each table: `table_name` for SQL, `stype` for the geometry type, `display` for the column that names a feature, and `columns`.

- Match the words in a question to the `alias` of a column. `utype` is the unit. `values` shows real spelling and case.
- A question uses the meaning in `keyval`, and SQL uses its code.
- Obey `hints` exactly.
- Do not guess a column or a value. If the metadata does not identify it, query the distinct values. If a term still has more than one meaning, state the meaning that you use.
- Each table also has columns that the metadata does not list. These include `OBJECTID` and `geometry`. Use `DESCRIBE` to see them.

## Rules for each result

- A null value means no data. It does not mean 0. Exclude nulls from a comparison or a statistic, and report how many you excluded.
- A result has one row for each feature. A `JOIN` can give duplicates, so keep one row for each `OBJECTID`.

## Spatial rules

All `geometry` values are WGS84, in degrees.

- Never give a distance in meters to a function on WGS84 geometry. `ST_DWithin(geometry, geometry, 5000)` means 5000 degrees, so it is true for all features.
- Do not use EPSG:3035 or one UTM zone for a final distance. Their errors change which features pass a limit.
- Measure a distance in a projection centered on one of the two features. Center on a point feature. If there is no point feature, center on `ST_Centroid` of the smaller feature, and transform both features.

```python
crs = f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +no_defs"
sql = f"""SELECT pipe_name,
                 ST_Distance(ST_Transform(geometry, 'EPSG:4326', '{crs}', always_xy := true), ST_Point(0, 0)) AS d
          FROM Pipelines WHERE ... ORDER BY d LIMIT 1"""
```

- Run this query once for each feature, in a Python loop. `lon` and `lat` come from `ST_X` and `ST_Y`. `ST_Point(0, 0)` is the center point.
- If a question uses a distance, add `distance_m` and the `display` value of the nearest feature to each result.

## Statistics

If a question asks for statistics, report the count, the minimum, the maximum, the mean, the median and the sample standard deviation.

Calculate statistics and z-scores on the features in the result, unless the question asks for a different population. State the population in your answer. If the standard deviation is 0 or there is one feature, the z-score is 0.

## Color by z-score

If a question asks for color by z-score, add `z_score`, `z_class` and `z_color` to each feature. Each class includes its lower limit.

| `z_class` | `z_color` |
|---|---|
| `z < -1` | `#4575b4` |
| `-1 to 0` | `#91bfdb` |
| `0 to 1` | `#e0f3f8` |
| `1 to 2` | `#fee090` |
| `2 to 3` | `#fc8d59` |
| `z >= 3` | `#d73027` |

Also write `renderer.json` to the artifact folder. It is an ArcGIS class breaks renderer: `"type": "class-breaks"` and `"field": "z_score"`. Put one item in `classBreakInfos` for each class, with `minValue`, `maxValue`, `label` and a `symbol` that has the color above. For the first class, use a `minValue` below the lowest z-score in the data. For the last class, use a `maxValue` above the highest z-score. Select the symbol from `stype`: `simple-marker` for Point, `simple-line` for Polyline, and `simple-fill` for Polygon. Give a marker or a fill a dark outline.

## Output files

If a question asks to show, map or export features, write a GeoJSON file. Also write a GeoParquet file if the question asks for it. Do not use a file for a question that only asks for a number.

Both files have the same fields: the fields that the question needs, the `display` column, and any fields that you added. In both files:

- Geometry is WGS84, and all features have one geometry type.
- `OBJECTID` is an integer.
- A date is an ISO 8601 string. Use `strftime` in SQL, because a cast to `VARCHAR` is not ISO 8601.

### GeoJSON

The file is for the ArcGIS `GeoJSONLayer`. Use the extension `.geojson`, and write it with Python `json`.

- The top level is a `FeatureCollection` with no `crs` member.
- Coordinates are longitude and then latitude. Do not use `GeometryCollection`.
- The `id` of each feature is `OBJECTID`.
- All features have the same property names. The layer reads its fields from the first feature, so the first feature must have every property. If a value is null in the first feature, put a feature with a value first.
- Each property value is a string, a number or null.

### GeoParquet

The file is for the ArcGIS `ParquetLayer`. Use the extension `.parquet`, and write it with DuckDB `COPY`.

```python
con.execute(f"COPY ({select}) TO '{path}' (FORMAT PARQUET, COMPRESSION SNAPPY, GEOPARQUET_VERSION 'V1')")
```

- `{select}` must return one `GEOMETRY` column with the name `geometry`, or DuckDB does not write the `geo` metadata.
- Keep `'V1'`. The layer cannot parse a file from `V2`, `BOTH` or `NONE`.
- Do not use ZSTD. The layer rejects it.
- Cast each number that is not an integer to `DOUBLE`, including values from Python. The layer does not draw a feature if the renderer field is `DECIMAL`. Run `DESCRIBE` on the file after `COPY`, and confirm that no column is `DECIMAL`.
- To use data from Python in the file, such as distances and z-scores, register it as a table with `con.register`, and join it in `{select}`.

Tell the user that this file needs an explicit geometry encoding in the layer. Without it, the layer loads and counts the features, but it draws nothing. Give this code with the correct `geometryType`:

```js
new ParquetLayer({
  data: new ParquetFilesData({ urls: [url] }),
  geometryEncoding: new GeometryEncodingWkb({ field: "geometry" }),
  spatialReference: { wkid: 4326 },
  geometryType: "point"
});
```

## Get the files back

Each item in `artifacts` in the `execute` result is one file.

- For a file of 12,000,000 bytes or less, read the resource at `uri` with your MCP resource tool. Claude Code saves it to a local file with a `.bin` name. Copy that file to the correct name.
- For a larger file, send `Authorization: Bearer <token>` with an HTTP GET to the server address and `download_path`.
- Compare the `sha256` value after you get the file. Give the local path in your answer.

Get files soon after the run, because the server deletes them later.
