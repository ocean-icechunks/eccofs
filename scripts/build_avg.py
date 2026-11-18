#!/usr/bin/env python
"""Build or extend the virtual Icechunk store for the ECCOFS `avg` product.

Each source file `avg/eccofs_avg_<run>_<NNNN>.nc` is one lead day of a forecast run; a run is
files 0001..0008 and starts at 12:00 on <run>. The store keeps every run as a forecast model run
collection (FMRC):

    time   forecast reference time, one value per run (the append dimension)
    step   lead time, 0..7 days
    temp   (time, step, s_rho, eta_rho, xi_rho), one chunk per vertical level

Valid time is `time + step` and is not stored. The NetCDF bytes stay in the public NOAA bucket.

The script is resumable: it diffs the reference times already in the repository against the
complete runs in the bucket and appends only the new ones, in increasing order, one commit per batch.

OSN credentials come from the AWS profile (default `osn-esip`) in ~/.aws/credentials.

    python scripts/build_avg.py --dry-run
    python scripts/build_avg.py --max-runs 5 --prefix rsignell/eccofs-avg-smoke
    python scripts/build_avg.py
"""
import argparse
import configparser
import functools
import multiprocessing
import os
import re
import time
import warnings
from concurrent.futures import ProcessPoolExecutor

import icechunk
import numpy as np
import pandas as pd
import xarray as xr
import zarr
from obspec_utils.registry import ObjectStoreRegistry
from obstore.store import S3Store
from virtualizarr import open_virtual_dataset
from virtualizarr.manifests import ChunkManifest, ManifestArray
from virtualizarr.parsers import HDFParser

SRC_BUCKET = "noaa-nos-eccofs-pds"
SRC = f"s3://{SRC_BUCKET}/"
NSTEPS = 8  # files per run, lead days 0..7
FILE_RE = re.compile(r"eccofs_avg_(\d{8})_(\d{4})\.nc$")
# dimensions of the spatial fields we keep. Scalar model parameters have none of them and are dropped.
SPATIAL = {"s_rho", "s_w", "eta_rho", "xi_rho", "eta_u", "xi_u", "eta_v", "xi_v", "eta_psi", "xi_psi"}
# global attributes that describe only the first source file and would mislead for the collection
DROP_ATTRS = ("file", "history")


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def source_registry():
    return ObjectStoreRegistry({f"s3://{SRC_BUCKET}": S3Store(SRC_BUCKET, region="us-east-1", skip_signature=True)})


def list_complete_runs():
    """Run dates (YYYYMMDD) that have all NSTEPS files in the source bucket."""
    files = {}
    for batch in S3Store(SRC_BUCKET, region="us-east-1", skip_signature=True).list("avg/"):
        for obj in batch:
            m = FILE_RE.search(obj["path"])
            if m:
                files.setdefault(m.group(1), set()).add(int(m.group(2)))
    complete = sorted(r for r, n in files.items() if n == set(range(1, NSTEPS + 1)))
    incomplete = sorted(set(files) - set(complete))
    return complete, incomplete


def subchunk_levels(m, axis=1):
    """One chunk per vertical level for a (time, level, y, x) ManifestArray.

    The fork's slicing splits chunks that are only partly selected, but returns a trailing partial
    chunk (the 51st level of an s_w variable stored in chunks of 50) unsplit, so those levels are
    built here. Each level is a contiguous byte range inside its source chunk.
    """
    cl, n = m.metadata.chunks[axis], m.shape[axis]
    full = (n // cl) * cl
    parts = [m[:, k:k + 1, :, :] for k in range(full)]
    if full < n:
        base = m.manifest.dict()
        level_bytes = base["0.0.0.0"]["length"] // cl
        tmpl = parts[-1]
        for k in range(full, n):
            vc, kk = divmod(k, cl)
            ent = {}
            for key, e in base.items():
                _, c1, i, j = key.split(".")
                if int(c1) == vc:
                    ent[f"0.0.{i}.{j}"] = {"path": e["path"], "offset": e["offset"] + kk * level_bytes,
                                           "length": level_bytes}
            parts.append(ManifestArray(metadata=tmpl.metadata, chunkmanifest=ChunkManifest(entries=ent)))
    return np.concatenate(parts, axis=axis)


_registry = None


def open_file(run, n, subchunk=True, tries=3):
    global _registry
    if _registry is None:
        _registry = source_registry()
    url = f"{SRC}avg/eccofs_avg_{run}_{n:04d}.nc"
    for attempt in range(tries):
        try:
            ds = open_virtual_dataset(url, registry=_registry, parser=HDFParser(), loadable_variables=["ocean_time"])
            break
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(5 * (attempt + 1))
    keep = [v for v in ds.data_vars if "ocean_time" in ds[v].dims or SPATIAL & set(ds[v].dims)]
    ds = ds[keep]
    for v in list(ds.data_vars):
        if subchunk and "ocean_time" in ds[v].dims and {"s_rho", "s_w"} & set(ds[v].dims):
            old = ds[v]
            ds[v] = xr.Variable(old.dims, subchunk_levels(old.data), old.attrs, old.encoding)
    return ds


def fix_ds(ds):
    """(ocean_time) valid times -> scalar time (reference) + step (lead time)."""
    assert ds.sizes["ocean_time"] == NSTEPS, ds.sizes
    t0 = pd.Timestamp(ds.ocean_time.values[0])
    # snap to the exact daily grid: float round-off in the source otherwise makes step differ per file
    ds = ds.assign_coords(ocean_time=pd.date_range(t0, periods=NSTEPS, freq="1D"))
    ds = ds.rename_vars(ocean_time="valid_time").rename_dims(ocean_time="step")
    step = (ds.valid_time - ds.valid_time[0]).assign_attrs({"standard_name": "forecast_period"})
    ref = ds.valid_time[0].assign_attrs({"standard_name": "forecast_reference_time"})
    ds = ds.assign_coords(step=step, time=ref)
    return ds.drop_indexes("valid_time").drop_vars("valid_time")


def build_run(run, subchunk=True):
    """Virtual dataset for one run, (step, ...) with a scalar reference `time`."""
    dss = [open_file(run, n, subchunk) for n in range(1, NSTEPS + 1)]
    ref = pd.Timestamp(dss[0].ocean_time.values[0])
    if ref.strftime("%Y%m%d") != run:
        raise ValueError(f"run {run}: first valid time {ref} does not match the file name date")
    out = xr.concat(dss, dim="ocean_time", data_vars="minimal", coords="minimal",
                    compat="override", combine_attrs="override")
    return fix_ds(out)


def cat(ds_list, first):
    """Concatenate runs along `time`. Static variables are kept from the first run, for the first batch only."""
    dyn = [v for v in ds_list[0].data_vars if "step" in ds_list[0][v].dims]
    out = xr.concat([d[dyn] for d in ds_list], dim="time", coords="minimal", compat="override",
                    combine_attrs="override")
    # every append rewrites the root attributes from the batch, so drop these for every batch
    for a in DROP_ATTRS:
        out.attrs.pop(a, None)
    if first:
        static = [v for v in ds_list[0].data_vars if v not in dyn]
        out = out.assign({v: ds_list[0][v].variable for v in static})
    else:
        # appending must not rewrite static variables or coordinates that have no time dimension
        out = out.drop_vars([c for c in out.coords if "time" not in out[c].dims])
    return out


def osn_storage(args):
    p = configparser.ConfigParser()
    p.read(os.path.expanduser("~/.aws/credentials"))
    c = p[args.profile]
    return icechunk.s3_storage(
        bucket=args.bucket, prefix=args.prefix, region="us-east-1", endpoint_url=args.endpoint,
        access_key_id=c["aws_access_key_id"], secret_access_key=c["aws_secret_access_key"], force_path_style=True,
    )


def repo_config():
    cfg = icechunk.RepositoryConfig.default()
    cfg.set_virtual_chunk_container(
        icechunk.VirtualChunkContainer(SRC, icechunk.s3_store(region="us-east-1", anonymous=True)))
    return cfg


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", default="esip")
    ap.add_argument("--prefix", default="rsignell/eccofs-avg")
    ap.add_argument("--endpoint", default="https://usgs.osn.mghpcc.org")
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE", "osn-esip"))
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    ap.add_argument("--batch-size", type=int, default=16, help="runs per commit")
    ap.add_argument("--max-runs", type=int, help="stop after this many new runs (for testing)")
    ap.add_argument("--no-subchunk", dest="subchunk", action="store_false",
                    help="keep the source chunking (all vertical levels in one chunk) instead of one chunk per level")
    ap.add_argument("--dry-run", action="store_true", help="list what would be added and exit")
    args = ap.parse_args()
    warnings.filterwarnings("ignore")
    zarr.config.set({"async.concurrency": 128})

    storage = osn_storage(args)
    auth = icechunk.containers_credentials({SRC: icechunk.s3_anonymous_credentials()})
    exists = icechunk.Repository.exists(storage)
    have = set()
    if exists:
        repo = icechunk.Repository.open(storage, repo_config(), authorize_virtual_chunk_access=auth)
        ds = xr.open_zarr(repo.readonly_session("main").store, consolidated=False, chunks=None)
        have = set(pd.to_datetime(ds.time.values).strftime("%Y%m%d"))

    complete, incomplete = list_complete_runs()
    new = sorted(set(complete) - have)
    log(f"source: {len(complete)} complete runs, {len(incomplete)} incomplete (skipped: {incomplete}); "
        f"repo: {len(have)} runs; new: {len(new)}")
    if have and new and new[0] < max(have):
        raise SystemExit(f"run {new[0]} is older than the newest run in the repo ({max(have)}); "
                         "appending cannot insert it, rebuild the repository instead")
    if args.max_runs:
        new = new[:args.max_runs]
    if not new or args.dry_run:
        log("nothing to add" if not new else f"dry run: would add {new[0]}..{new[-1]} ({len(new)} runs)")
        return

    if not exists:
        repo = icechunk.Repository.create(storage, repo_config(), authorize_virtual_chunk_access=auth)
        repo.save_config()
        log(f"created repository s3://{args.bucket}/{args.prefix}")

    t0 = time.time()
    first = not have
    # spawn, not fork: the parent already runs Rust async runtimes (obstore, icechunk) and forking them deadlocks
    with ProcessPoolExecutor(args.workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        for i in range(0, len(new), args.batch_size):
            batch = new[i:i + args.batch_size]
            runs = list(pool.map(functools.partial(build_run, subchunk=args.subchunk), batch))
            ds = cat(runs, first)
            session = repo.writable_session("main")
            if first:
                ds.vz.to_icechunk(session.store)
            else:
                ds.vz.to_icechunk(session.store, append_dim="time")
            snap = session.commit(f"avg runs {batch[0]}..{batch[-1]} ({len(batch)} runs)")
            first = False
            log(f"committed {batch[0]}..{batch[-1]} -> {snap}  [{i + len(batch)}/{len(new)} runs, {time.time() - t0:.0f}s]")


if __name__ == "__main__":
    main()
