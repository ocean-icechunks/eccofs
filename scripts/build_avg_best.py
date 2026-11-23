#!/usr/bin/env python
"""Build the best-estimate virtual Icechunk store for the ECCOFS `avg` product.

The FMRC store (build_avg.py) keeps every run, so valid times overlap. This store keeps one entry per
valid time, a single monotonic `time` dimension, taking each time from the file the modeler recommends:

  * the middle day of each 3-day analysis (file suffix _0002, step 1 day) for all past times,
  * then the rest of the latest run as the forecast.

The (run, step) choice is made by Rolodex `BestEstimate(offset=1)` on the FMRC store's index, so the two
stores always agree. Each chosen source file contributes exactly one time step; the NetCDF bytes stay in
the public NOAA bucket and each vertical level is one chunk, as in the FMRC store.

A new run replaces the forecast tail of the best series, so the store cannot simply be appended to.
`--update` brings an existing store up to date in one atomic commit: it compares the source file of each
stored time (the `sources` root attribute) with the current best series, cuts the `time` axis back to the
first time whose file changed, and appends references for the changed files only. Everything before that
keeps its references; a daily update opens about 7 files. A store without `sources` is rebuilt in full.

    python scripts/build_avg_best.py --dry-run
    python scripts/build_avg_best.py --prefix noaa-eccofs/eccofs-avg-best-smoke --max-times 6
    python scripts/build_avg_best.py --update --expire-days 7   # what the daily workflow runs

With `--no-subchunk` the store keeps NOAA's chunking, all vertical levels of a tile in one chunk, which
reads a vertical profile or section in far fewer requests (a surface map reads about 50x more data).
Use a separate `--prefix` for it: the chunking of an existing store cannot change.
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
from build_avg import DROP_ATTRS, SRC, expire_old, log, open_file, repo_config, store_storage  # noqa: E402


def best_files(args):
    """[(run YYYYMMDD, file number 1..8, valid time)] for the best-estimate series, read from the FMRC store."""
    storage = store_storage(args, args.source_prefix)
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


def cat(ds_list, first, attrs):
    """Concatenate along `time`. Static variables and non-time coordinates are kept for the first batch only."""
    dyn = [v for v in ds_list[0].data_vars if "time" in ds_list[0][v].dims]
    out = xr.concat([d[dyn] for d in ds_list], dim="time", coords="minimal", compat="override",
                    combine_attrs="override")
    # every append rewrites the root attributes from the batch, so each batch carries the final ones
    out.attrs = {k: v for k, v in out.attrs.items() if k not in DROP_ATTRS} | attrs
    if first:
        static = [v for v in ds_list[0].data_vars if v not in dyn]
        out = out.assign({v: ds_list[0][v].variable for v in static})
    else:
        out = out.drop_vars([c for c in out.coords if "time" not in out[c].dims])
    return out


def truncate_time(store, n):
    """Cut every array with a `time` dimension back to its first n entries (one time per chunk, so whole chunks go)."""
    for _, arr in zarr.open_group(store, mode="r+").arrays():
        dims = arr.metadata.dimension_names or ()
        if "time" in dims:
            shape = list(arr.shape)
            shape[dims.index("time")] = n
            arr.resize(tuple(shape))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", default="ocean-icechunks")
    ap.add_argument("--prefix", default="noaa-eccofs/eccofs-avg-best", help="where to write the store")
    ap.add_argument("--source-prefix", default="noaa-eccofs/eccofs-avg-fmrc", help="FMRC store the best series is derived from")
    ap.add_argument("--endpoint", default="https://data.source.coop")
    ap.add_argument("--profile", help="AWS profile to read credentials from when AWS_ACCESS_KEY_ID is not set")
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    ap.add_argument("--batch-size", type=int, default=32, help="times per write")
    ap.add_argument("--max-times", type=int, help="use only the first N times of the series (for testing)")
    ap.add_argument("--no-subchunk", dest="subchunk", action="store_false",
                    help="keep the source chunking (all vertical levels in one chunk) instead of one chunk per level, "
                         "which is faster for vertical profiles and sections")
    ap.add_argument("--update", action="store_true",
                    help="bring an existing store up to date: truncate the changed tail and append (one commit)")
    ap.add_argument("--expire-days", type=int, help="after writing, expire snapshots older than this many days and garbage-collect")
    ap.add_argument("--create", action="store_true", help="create the repository if it does not exist (otherwise an error)")
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

    sources = [f"{run}_{n:04d}" for run, n, _ in items]
    attrs = {"sources": sources, "latest_run": max(i[0] for i in items)}
    storage = store_storage(args)
    auth = icechunk.containers_credentials({SRC: icechunk.s3_anonymous_credentials()})
    keep = 0  # leading times whose references are kept
    if icechunk.Repository.exists(storage):
        if not args.update:
            raise SystemExit(f"s3://{args.bucket}/{args.prefix} already exists; pass --update to bring it up to date")
        repo = icechunk.Repository.open(storage, repo_config(), authorize_virtual_chunk_access=auth)
        old = zarr.open_group(repo.readonly_session("main").store, mode="r").attrs.get("sources")
        if old is None:
            log("store has no `sources` attribute: rebuilding in full")
        else:
            old = list(old)
            while keep < min(len(old), len(sources)) and old[keep] == sources[keep]:
                keep += 1
            if keep == len(old) == len(sources):
                log(f"up to date: {len(old)} times, newest source {old[-1]}")
                return
            log(f"store has {len(old)} times; first {keep} unchanged, replacing {len(old) - keep} and "
                f"appending {len(sources) - keep} from {sources[keep] if keep < len(sources) else '-'}")
    else:
        if not args.create:
            raise SystemExit(f"no repository at s3://{args.bucket}/{args.prefix}; pass --create to build it from scratch")
        repo = icechunk.Repository.create(storage, repo_config(), authorize_virtual_chunk_access=auth)
        repo.save_config()
        log(f"created repository s3://{args.bucket}/{args.prefix}")

    # one session and one commit: readers see the old series until the new one is complete
    t0 = time.time()
    session = repo.writable_session("main")
    if keep:
        truncate_time(session.store, keep)
    else:
        session.store.clear()
    todo = items[keep:]
    first = keep == 0
    with ProcessPoolExecutor(args.workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        for i in range(0, len(todo), args.batch_size):
            batch = todo[i:i + args.batch_size]
            ds = cat(list(pool.map(functools.partial(build_file, subchunk=args.subchunk), batch)), first, attrs)
            if first:
                ds.vz.to_icechunk(session.store, mode="w")
            else:
                ds.vz.to_icechunk(session.store, append_dim="time")
            first = False
            log(f"built {batch[0][2]:%Y-%m-%d}..{batch[-1][2]:%Y-%m-%d}  [{i + len(batch)}/{len(todo)} times, {time.time() - t0:.0f}s]")
    snap = session.commit(f"best estimate {items[0][2]:%Y-%m-%d}..{items[-1][2]:%Y-%m-%d} ({len(items)} times): "
                          f"kept {keep}, wrote {len(todo)}; latest run {attrs['latest_run']}")
    log(f"committed -> {snap}  [{time.time() - t0:.0f}s]")
    if args.expire_days:
        expire_old(repo, args.expire_days)


if __name__ == "__main__":
    main()
