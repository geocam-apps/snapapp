# Suggested endpoint for manager-api: cell search by point

snapapp's "Find GeoCam cells" button POSTs a project's shot GPS coords
to `snapapp/api/projects/<id>/cells-search`, which in turn calls
manager-api. It first tries a (hypothetical, not-yet-existing) bulk
search endpoint and falls back to walking `/api/projects → /api/collections`
collecting cell geometries client-side.

The clean path is adding a read-only search endpoint on manager-api that
pushes the point-in-polygon work into PostGIS. Below is a Rails /
Sinatra-flavoured outline plus the SQL. Everything read-only.

## Route

```
GET /api/cells/search?points=<lon>,<lat>[;<lon>,<lat>...]
```

- `points` is a semicolon-separated list of `lon,lat` pairs (WGS84).
- Optional `project_slug=...` to constrain to one project.
- Returns `{items: [{slug, name, address, reference,
    cell_map: {slug, project_slug}, matched_points: [[lon, lat], …]}]}`.
- 200 on success even if zero matches; 400 on bad coord parsing.

## PostgreSQL / PostGIS query

`cells.space` is `geography(geometry, 4326)` so point-in-polygon on
geography uses `ST_Covers`. To return which of the input points matched
each cell, we unnest them and aggregate:

```sql
WITH query_pts AS (
  SELECT p.lon, p.lat, ST_SetSRID(ST_MakePoint(p.lon, p.lat), 4326)::geography AS g
  FROM UNNEST(:lons::float8[], :lats::float8[]) WITH ORDINALITY AS p(lon, lat, ord)
)
SELECT c.slug, c.name, c.address, c.reference,
       cm.slug AS cell_map_slug,
       pr.slug AS project_slug,
       json_agg(json_build_array(q.lon, q.lat)) AS matched_points
FROM cells c
JOIN cell_maps cm ON cm.id = c.cell_map_id
JOIN projects  pr ON pr.id = cm.project_id
JOIN query_pts q   ON ST_Covers(c.space, q.g)
WHERE (:project_slug::text IS NULL OR pr.slug = :project_slug)
GROUP BY c.id, cm.slug, pr.slug
ORDER BY c.slug;
```

Bind `:lons` and `:lats` as parallel float8 arrays parsed from the
query-string `points` param. `ST_Covers` on `geography` is equivalent to
`ST_Contains` on `geometry` but correct across the antimeridian / at the
poles, and it uses the GiST index on `cells.space` for an efficient
bounding-box prefilter.

## Authorization

- Read-only, behind the same `ApiToken` auth as the rest of manager-api.
- Apply the existing `ProjectPolicy::Scope` by joining through `projects`
  to filter to cells the caller can see (don't return extents in other
  orgs).

## Alternate: include cell `space` on collection detail

If adding a new endpoint is heavy, an equally useful change is returning
`cells[].space` as GeoJSON on `GET /api/collections/:slug`:

```ruby
cells: collection.cells.map { |c|
  {slug: c.slug, name: c.name, address: c.address, reference: c.reference,
   space: RGeo::GeoJSON.encode(c.space)}
}
```

snapapp's fallback already walks `/api/projects → /api/projects/:slug/collections
→ /api/collections/:slug` and filters locally when that field is present,
so no snapapp change is needed if you go that route.
