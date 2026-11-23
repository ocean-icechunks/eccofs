# NOAA ECCOFS ocean model output — Icechunk

**[🌐 View data in browser](#view-it-in-a-browser)** · **[💻 Data access (code)](#how-to-open-it)** · **[📦 Data access (NOAA, AWS Open Data)](https://noaa-nos-eccofs-pds.s3.amazonaws.com/index.html)**

Daily output of the NOAA NOS [ECCOFS](https://registry.opendata.aws/noaa-nos-eccofs-pds/) forecast model
(a ROMS configuration of the East Coast Coastal Ocean Forecast System), published as
[Icechunk](https://icechunk.io) stores on
[Source Cooperative](https://source.coop/ocean-icechunks/noaa-eccofs) so that months of daily 3-D fields
open in about a second as one lazy `xarray` datacube.

| Dataset | Icechunk repository | What it is |
|---|---|---|
| **`avg` best estimate** — one continuous time series | `https://data.source.coop/ocean-icechunks/noaa-eccofs/eccofs-avg-best` | `time` is the valid time (daily, 12:00 UTC): the analysis for the past, then the latest forecast. **Start here.** |
| **`avg` forecast runs (FMRC)** — every run kept | `https://data.source.coop/ocean-icechunks/noaa-eccofs/eccofs-avg-fmrc` | `time` is the run reference time and `step` the lead day 0–7, so valid times overlap between runs. |
| **Whole-column variants** — `eccofs-avg-best-nosub`, `eccofs-avg-fmrc-nosub` | `https://data.source.coop/ocean-icechunks/noaa-eccofs/<name>` | The same two datasets with NOAA's chunking kept (all 50 levels of a tile in one chunk): faster for vertical profiles and sections, slower for maps. |

All stores are **virtual**: each holds only Zarr metadata and byte-range references to the NetCDF files in
NOAA's public bucket `s3://noaa-nos-eccofs-pds` (us-east-1). Nothing is copied, and all are kept up to date
automatically (see [How it stays up to date](#how-it-stays-up-to-date)).

## View it in a browser

| Dataset | Viewer |
|---|---|
| `avg` best estimate, surface potential temperature | [Open it in the viewer](https://eeholmes.github.io/gridlook/#icechunk+https://data.source.coop/ocean-icechunks/noaa-eccofs/eccofs-avg-best::varname=temp::dimIndices_time=160::dimIndices_s_rho=49) |
| `avg` best estimate, surface salinity | [Open it in the viewer](https://eeholmes.github.io/gridlook/#icechunk+https://data.source.coop/ocean-icechunks/noaa-eccofs/eccofs-avg-best::varname=salt::dimIndices_time=160::dimIndices_s_rho=49) |

The links use a [gridlook](https://github.com/eeholmes/gridlook) build with the fixes this store needs, which
reads any store named in the URL. `dimIndices_s_rho=49` is the surface level (the last of 50, since ROMS counts
from the bottom up) and `dimIndices_time=160` is 2026-11-16; use the sliders to change both.

The data arrays are read from NOAA's bucket, which sends `Access-Control-Allow-Origin: *` and allows `Range`
requests (checked 2026-11-19), so no CORS workaround should be needed.

## How to open it

**Required:** Python ≥ 3.12, icechunk ≥ 2.2. No credentials are needed. On older Python, `pip install icechunk`
quietly installs icechunk 1.x, which will not work.

```python
import icechunk
import xarray as xr

URL = "https://data.source.coop/ocean-icechunks/noaa-eccofs/eccofs-avg-best"
SOURCE = "s3://noaa-nos-eccofs-pds/"          # NOAA's public bucket, where the NetCDF bytes are

repo = icechunk.Repository.open(icechunk.http_storage(URL))
# the arrays live in NOAA's bucket, outside the store, so authorize that virtual chunk container (anonymous)
repo = repo.reopen(authorize_virtual_chunk_access=icechunk.containers_credentials({SOURCE: icechunk.s3_anonymous_credentials()}))

ds = xr.open_zarr(repo.readonly_session("main").store, consolidated=False, chunks={})
sst = ds.temp.isel(s_rho=-1)                   # surface level of potential temperature
sst.sel(time="2026-11-01 12:00").load()
```

For the forecast-run store use the `eccofs-avg-fmrc` URL; its variables have dimensions
`(time, step, ...)` and the best-estimate series can be rebuilt from it with
[Rolodex](https://github.com/dcherian/rolodex). A complete example with maps and a point time series is in
[`eccofs_avg_best_series_surface_temp.ipynb`](eccofs_avg_best_series_surface_temp.ipynb).

Reads are by whole chunk. In the default stores one chunk is one vertical level of one 145 × 167 tile, so a
surface map reads about 100 chunks (roughly 10 MB) and not the whole water column. The first surface map from a
laptop took a few seconds; from a cloud machine in us-east-1 it takes about half a second. Vertical profiles and
sections read all 50 levels, which is what the whole-column `-nosub` stores are for (see [Chunking](#chunking)).

## Vertical sections and currents with xroms

[xroms](https://xroms.readthedocs.io) understands ROMS grids, so it turns the stores into sections and current
maps with little code. The notebooks below all follow the same steps:

1. Open the best-estimate store as above (one `Repository.open`, anonymous).
2. Cut out the region with `xroms.subset`. The rho rows and columns that touch a lon/lat box become `X`/`Y`
   slices, and xroms slices the staggered `u`, `v` and `psi` dimensions to match. It only touches dimensions that
   have a coordinate, and the store's horizontal dimensions have none, so index coordinates are added first.
3. Build the grid with `xroms.roms_dataset`, which also computes the terrain-following depths `z_rho` from the
   scalar parameters stored with the data (`Vtransform`, `Vstretching`, `theta_s`, `theta_b`, `hc`; see
   [About the data](#about-the-data)). The store's SGRID `Conventions` attribute is dropped first (xroms builds
   its own grid from the dimension names) and the arrays are rechunked to one chunk along the horizontal
   dimensions, which xgcm requires.
4. `xroms.rotate_vectors` turns the grid-aligned `u` and `v` into east and north velocities, and
   `xroms.interpll(..., which="pairs")` interpolates any field, and `z_rho`, to points along a route given as
   lon/lat pairs.
5. Plot with hvPlot: a section drawn on the true depth of each level with the sea floor from `h`, and a map of
   surface currents.

| Notebook | What it does |
|---|---|
| [`eccofs_avg_best_ambrose_bermuda.ipynb`](eccofs_avg_best_ambrose_bermuda.ipynb) | temperature and cross-route current sections from Ambrose to Bermuda, the surface current along and across the route, and a map of surface currents ([rendered](https://nbviewer.org/gist/rsignell/a0690650f4b9c1c45bc62f0d117c9278)) |
| [`eccofs_avg_best_nj_transect_xroms.ipynb`](eccofs_avg_best_nj_transect_xroms.ipynb) | a temperature section off New Jersey with `xroms.interpll` ([rendered](https://nbviewer.org/gist/rsignell/ddd995cf809bd6f2f79d4dc6cac8e310), an earlier version of the same section) |
| [`eccofs_avg_best_nj_transect.ipynb`](eccofs_avg_best_nj_transect.ipynb) | the same section with scipy interpolation and no xroms |
| [`eccofs_avg_best_nj_isoslice_test.ipynb`](eccofs_avg_best_nj_isoslice_test.ipynb) | a test of `xroms.isoslice` for sections between two points (it works, but `interpll` is simpler) |

**Install:** `xroms` 0.6.2 needs `xgcm` 0.8.1, because newer xgcm releases break xroms' grid parsing
([xroms#77](https://github.com/xoceanmodel/xroms/issues/77), [#82](https://github.com/xoceanmodel/xroms/issues/82)).
`xroms.interpll` wraps `xesmf`, which is easiest to get from conda-forge. Each notebook has a commented
`%pip install` line with the packages. Fixes for the problems met while writing the notebooks are proposed upstream
in [xroms#83](https://github.com/xoceanmodel/xroms/pull/83), [xroms#84](https://github.com/xoceanmodel/xroms/issues/84)
and [cf-xarray#697](https://github.com/xarray-contrib/cf-xarray/pull/697).

**Which store:** the notebooks read `eccofs-avg-best`, which works as soon as it is built. A section reads every
vertical level in the box, so the whole-column `eccofs-avg-best-nosub` store is the better fit for it: change `URL`
at the top of the notebook. That store needs the scalars added too, see
[How the stores are built](#how-the-stores-are-built).

## About the data

| | |
|---|---|
| Product | `avg/` of the NOAA ECCOFS bucket: daily means of the 3-D model state and surface fluxes |
| Grid | ROMS curvilinear grid, 1443 × 1667 cells (`eta_rho` × `xi_rho`), 50 terrain-following levels (`s_rho`); 2-D `lon_rho` / `lat_rho` give the cell positions, and staggered `u`, `v`, `psi` and `w` points are included |
| Variables | 37, of which 26 vary in time: `temp`, `salt`, `u`, `v`, `w`, `zeta`, `ubar`, `vbar`, `rho`, vorticity, vertical mixing coefficients, surface heat, salt and momentum fluxes, `mld`, `ohc`; and 11 static fields (bathymetry `h`, masks, metrics, Coriolis, stretching curves) |
| Time | daily means stamped 12:00 UTC, from 2026-06-09; each run covers 8 daily files (lead days 0–7) |
| Scalars | the ROMS scalar parameters that have no spatial dimension (`Vtransform`, `Vstretching`, `theta_s`, `theta_b`, `Tcline`, `hc`, `rho0`, `spherical`). The build scripts drop them, and `scripts/add_scalars.py` writes them back as 0-d arrays, which xroms needs to compute depths |
| Source | `s3://noaa-nos-eccofs-pds/avg/eccofs_avg_<run>_<NNNN>.nc`, uncompressed NetCDF-4, so a vertical level can be one chunk |

### The best-estimate series

Each ECCOFS run is a short analysis followed by a forecast, so the same day appears in several runs. The best
series takes each valid time from one run, following the modeler's recommendation for `avg`:

- the middle day of each 3-day analysis (the `_0002` file, lead day 1) for all past times, which avoids
  start-of-cycle transients, and
- the rest of the latest run (`_0003` to `_0008`) as the forecast, so the series always ends about a week
  after the newest run.

If a run is missing (a few days, such as 2026-06-15 and 2026-06-20, are absent from the bucket), the
next most recent run that covers that day is used. Choosing the files is done with Rolodex
`BestEstimate(offset=1)` on the forecast-run store, so the two always agree. The root attribute `sources`
lists the source file behind every time, and `latest_run` is the newest run reflected.

The last six times are a forecast, not an analysis, and each new run replaces them. Past times do not change.
Old snapshots are expired after a week, so Icechunk time travel on the live stores only reaches back that far.

### Chunking

Spatial chunks (145 × 167, a 10 × 10 tiling) are inherited from NOAA's files and cannot be changed in a
virtual store. The files are not compressed, so each vertical level inside a source chunk is a contiguous byte
range, and a store can reference either a whole chunk or one level of it. That gives two versions of each
dataset:

| | Default stores | Whole-column `-nosub` stores |
|---|---|---|
| Names | `eccofs-avg-best`, `eccofs-avg-fmrc` | `eccofs-avg-best-nosub`, `eccofs-avg-fmrc-nosub` |
| One chunk is | one vertical level of one tile (subchunked) | all 50 levels of one tile (NOAA's chunk) |
| Good for | maps of one level, point time series | vertical profiles and sections |
| Built with | `build_avg.py`, `build_avg_best.py` | the same scripts with `--no-subchunk` and their own `--prefix` |

In one measurement from a laptop, a surface map moved about 30 MB over the network from a default store where
whole 50-level chunks moved about 530 MB. A section does the opposite: it reads all 50 levels, so one chunk per
tile is far fewer requests than 50. The chunking of an existing store cannot change, which is why these are
separate repositories. Pick the store that matches how you slice the data.

## How the stores are built

The stores are made from NOAA's NetCDF files without copying them. Each of the four is built in two steps and then
given its scalars, by three scripts in [`scripts/`](scripts):

1. **Forecast-run store** (`scripts/build_avg.py`). Each file `avg/eccofs_avg_<run>_<NNNN>.nc` is one lead day of
   a run. [VirtualiZarr](https://virtualizarr.readthedocs.io) reads the metadata and the byte ranges of each
   file, and the script turns the file's valid time into a reference `time` (one value per run) plus a lead
   `step`, then appends the new runs in batches, one commit per batch. It diffs the reference times already
   stored against the complete runs in the bucket, so it can be re-run after a failure and only adds what is
   missing. By default it splits each source chunk along the vertical, one chunk per level; with `--no-subchunk`
   it keeps NOAA's chunks.
2. **Best-estimate store** (`scripts/build_avg_best.py`). It chooses one source file for every valid time (see
   [The best-estimate series](#the-best-estimate-series)) with Rolodex on the forecast-run store, builds
   references for those files and concatenates them along the valid time. `--no-subchunk` has the same meaning
   here, and `--update` brings an existing store up to date.
3. **Scalars** (`scripts/add_scalars.py`). The build drops the scalar model parameters, so this reads them from
   one NOAA file and writes them into a store as 0-d arrays in a single commit, without touching the existing
   data. xroms needs them to compute depths. Run it once on each store after it is built; it is safe to repeat.

The two chunkings are built by running the same scripts twice, with `--no-subchunk --prefix <name>-nosub` for the
whole-column pair. Per-level references need a
[fork of VirtualiZarr](https://github.com/rsignell/VirtualiZarr/tree/subchunk-first-nontrivial-axis)
that splits a chunk along its first non-trivial axis (here, the vertical); `scripts/requirements.txt` installs it.
The scripts also build by hand the one level of the 51-level `s_w` variables that the fork leaves in a trailing
partial chunk. The stores were checked against the source files by comparing values read through the store with
values read directly.

The scripts run locally (credentials come from the environment or an AWS profile) and `--dry-run` shows what
they would change. The GitHub Actions below run them for the live stores.

## How it stays up to date

Two [GitHub Actions workflows](.github/workflows) run daily at 15:30 UTC, one after the other:

1. **Update ECCOFS avg FMRC store** appends any new complete run to `eccofs-avg-fmrc`, then does the same for
   `eccofs-avg-fmrc-nosub`.
2. **Update ECCOFS avg best-estimate store** runs when the first has succeeded and brings `eccofs-avg-best`, then
   `eccofs-avg-best-nosub`, up to date: it compares the source file of each stored time with the current best
   series, cuts the time axis back to the first time that changed, and appends references for the changed files
   only (about a week of files per run, one commit).

Both sign in to Source Cooperative with GitHub's OIDC token, so the repository holds no stored credentials. All
workflows share one concurrency group, so a daily update never overlaps a one-off job. After each update, snapshots
older than 7 days are expired and garbage-collected.

Three workflows are run by hand, from the Actions tab:

- **Build avg stores without subchunking (one-off)** creates the two `-nosub` stores. It resumes where it stopped
  if the sign-in token expires, so re-running it is safe. After it, the daily workflows keep them current.
- **Add scalar model parameters to the avg stores** runs `add_scalars.py`. Run it with `dry_run` first. Its
  `stores` input names the stores to change.
- **Copy avg stores from OSN to Source Cooperative (one-off)** moved the first stores from OSN to Source Cooperative
  and is kept for the record.

A fourth, **Publish README to Source Cooperative**, copies this README to the repository's Source Cooperative
page whenever it changes on `main`. The `his/` product and the other prefixes of the bucket (`qck`, `sta`, `frc`,
`obs`, `riv`) are not included yet.

## Files in this repository

| | |
|---|---|
| `scripts/build_avg.py` | builds and extends the forecast-run store: one reference time per run, per-level or whole-column chunks |
| `scripts/build_avg_best.py` | builds and incrementally updates the best-estimate store |
| `scripts/add_scalars.py` | writes the dropped scalar model parameters into a store |
| `.github/workflows/` | the daily updates and the one-off builds, scalar restore, migration and README publishing described above |
| `eccofs_avg_best_series_surface_temp.ipynb` | opens the best-estimate store anonymously, maps the surface temperature and plots a point time series |
| `eccofs_avg_best_ambrose_bermuda.ipynb` and the other `eccofs_avg_best_nj_*` notebooks | vertical sections and currents with xroms, see above |

## Reuse and citation

This work is released under [Apache-2.0](LICENSE). The stores do not contain the ECCOFS data, only references
to NOAA's files, so please follow NOAA's terms for the data itself and acknowledge NOAA NOS and the ECCOFS
developers.

If you use the stores or scripts in published work, please cite:

> Signell, R. (2026). *NOAA ECCOFS as virtual Icechunk stores*. ocean-icechunks/noaa-eccofs.
> https://github.com/ocean-icechunks/eccofs

## Credits

- **Data:** NOAA National Ocean Service (NOS), ECCOFS, via the
  [AWS Open Data registry](https://registry.opendata.aws/noaa-nos-eccofs-pds/).
- **Best-estimate recipe:** the modeler's guidance on which `avg` files to use, from John Wilkin.
- **Forecast-collection indexing:** [Rolodex](https://github.com/dcherian/rolodex).
- **Packaging:** built with [Icechunk](https://icechunk.io), [VirtualiZarr](https://virtualizarr.readthedocs.io)
  and [Xarray](https://xarray.dev), hosted on [Source Cooperative](https://source.coop/ocean-icechunks/noaa-eccofs);
  the original file bytes remain with NOAA.
- **Viewer:** [gridlook](https://github.com/d70-t/gridlook), a WebGL visualizer for cloud-hosted Earth system model data.
