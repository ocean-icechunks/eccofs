#!/usr/bin/env python
"""Build the best-estimate virtual Icechunk store for the ECCOFS `avg` product.

The FMRC store (build_avg.py) keeps every run, so valid times overlap. This store keeps one entry per
valid time, a single monotonic `time` dimension, taking each time from the file the modeler recommends:

  * the middle day of each 3-day analysis (file suffix _0002, step 1 day) for all past times,
  * then the rest of the latest run as the forecast.

The (run, step) choice is made by Rolodex `BestEstimate(offset=1)` on the FMRC store's index, so the two
stores always agree. Each chosen source file contributes exactly one time step; the NetCDF bytes stay in
the public NOAA bucket and each vertical level is one chunk, as in the FMRC store.

The best series changes whenever a new run arrives (the forecast tail is replaced), so this store is
rebuilt rather than appended to: build under a new prefix, check it, then swap.

    python scripts/build_avg_best.py --dry-run
    python scripts/build_avg_best.py --prefix rsignell/eccofs-avg-best-smoke --max-times 6
    python scripts/build_avg_best.py
"""
import argparse
import functools
import multiprocessing
import os
import sys
import time
import warnings
from concurrent.futures import ProcessPoolExecutor

import icechunk
import pandas as pd
import rolodex.forecast
import xarray as xr
import zarr
from rolodex.forecast import BestEstimate, ForecastIndex

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_avg import DROP_ATTRS, SRC, log, open_file, osn_storage, repo_config  # noqa: E402


def best_files(args):
    """[(run YYYYMMDD, file number 1..8, valid time)] for the best-estimate series, read from the FMRC store."""
    storage = icechunk.s3_storage(bucket=args.bucket, prefix=args.source_prefix, region="us-east-1",
                                  endpoint_url=args.endpoint, anonymous=True, force_path_style=True)
    repo = icechunk.Repository.open(
        storage, repo_config(),
        authorize_virtual_chunk_access=icechunk.containers_credentials({SRC: icechunk.s3_anonymous_credentials()}))
    ds = xr.open_zarr(repo.readonly_session("main").store, consolidated=False, chunks=None)
    fc = xr.Dataset(coords={"time": ds.time, "step": ds.step})
    fc.coords["valid_time"] = rolodex.forecast.create_lazy_valid_time_variable(reference_time=fc.time, period=fc.step)
    fc = fc.drop_indexes(["time", "step"]).set_xindex(["time", "step", "valid_time"], ForecastIndex)
    best = fc.sel(valid_time=BestEstimate(offset=1))
    out = [(pd.Timestamp(t).strftime("%Y%m%d"), int(s / pd.Timedelta(days=1)) + 1, pd.Timestamp(v))
           for t, s, v in zip(best.time.values, best.step.values, best.valid_time.values)]
    vt = pd.DatetimeIndex([o[2] for o in out])
    assert vt.is_monotonic_increasing and vt.is_unique
    return out


def build_file(item, subchunk=True):
    """Virtual dataset holding the single time step of one source file, with dimension `time`."""
    run, n, valid = item
    ds = open_file(run, n, subchunk)
    assert ds.sizes["ocean_time"] == 1, ds.sizes
    # the source stores float seconds; snap to the minute so times are exact and equal Rolodex's valid_time
    t = pd.Timestamp(ds.ocean_time.values[0]).round("1min")
    if t != valid:
        raise ValueError(f"{run}_{n:04d}: file time {t} != expected {valid}")
    ds = ds.assign_coords(ocean_time=[t])
    return ds.rename({"ocean_time": "time"})


def cat(ds_list, first):
    """Concatenate along `time`. Static variables and non-time coordinates are kept for the first batch only."""
    dyn = [v for v in ds_list[0].data_vars if "time" in ds_list[0][v].dims]
    out = xr.concat([d[dyn] for d in ds_list], dim="time", coords="minimal", compat="override",
                    combine_attrs="override")
    for a in DROP_ATTRS:
        out.attrs.pop(a, None)
    if first:
        static = [v for v in ds_list[0].data_vars if v not in dyn]
        out = out.assign({v: ds_list[0][v].variable for v in static})
    else:
        out = out.drop_vars([c for c in out.coords if "time" not in out[c].dims])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", default="esip")
    ap.add_argument("--prefix", default="rsignell/eccofs-avg-best", help="where to write the new store")
    ap.add_argument("--source-prefix", default="rsignell/eccofs-avg", help="FMRC store the best series is derived from")
    ap.add_argument("--endpoint", default="https://usgs.osn.mghpcc.org")
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE", "osn-esip"))
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    ap.add_argument("--batch-size", type=int, default=32, help="times per commit")
    ap.add_argument("--max-times", type=int, help="stop after this many times (for testing)")
    ap.add_argument("--dry-run", action="store_true", help="print the chosen files and exit")
    args = ap.parse_args()
    warnings.filterwarnings("ignore")
    zarr.config.set({"async.concurrency": 128})

    items = best_files(args)
    log(f"best series: {len(items)} times, {items[0][2]} .. {items[-1][2]}; "
        f"runs used: {len({i[0] for i in items})}; steps used: {sorted({i[1] - 1 for i in items})}")
    if args.max_times:
        items = items[:args.max_times]
    if args.dry_run:
        for run, n, v in items:
            print(f"{v:%Y-%m-%d %H:%M}  eccofs_avg_{run}_{n:04d}.nc")
        return

    storage = osn_storage(args)
    if icechunk.Repository.exists(storage):
        raise SystemExit(f"s3://{args.bucket}/{args.prefix} already exists; build under a new prefix")
    auth = icechunk.containers_credentials({SRC: icechunk.s3_anonymous_credentials()})
    repo = icechunk.Repository.create(storage, repo_config(), authorize_virtual_chunk_access=auth)
    repo.save_config()
    log(f"created repository s3://{args.bucket}/{args.prefix}")

    t0 = time.time()
    first = True
    with ProcessPoolExecutor(args.workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        for i in range(0, len(items), args.batch_size):
            batch = items[i:i + args.batch_size]
            ds = cat(list(pool.map(functools.partial(build_file), batch)), first)
            session = repo.writable_session("main")
            if first:
                ds.vz.to_icechunk(session.store)
            else:
                ds.vz.to_icechunk(session.store, append_dim="time")
            snap = session.commit(f"best estimate {batch[0][2]:%Y-%m-%d}..{batch[-1][2]:%Y-%m-%d} ({len(batch)} times)")
            first = False
            log(f"committed {batch[0][2]:%Y-%m-%d}..{batch[-1][2]:%Y-%m-%d} -> {snap}  "
                f"[{i + len(batch)}/{len(items)} times, {time.time() - t0:.0f}s]")


if __name__ == "__main__":
    main()
