"""Add the scalar ROMS parameters that build_avg.py drops back into an avg Icechunk store.

The stores keep only the spatial fields, but ROMS tools need a few scalars to turn the
terrain-following s-coordinates into depths (Vtransform, Vstretching, theta_s, theta_b, Tcline, hc)
and rho0 for density-based quantities. They are constants of the model configuration, so they are read
once from one NOAA source file and written to the store as 0-d arrays. Existing arrays and chunks are
not touched: it is one small commit.

    python scripts/add_scalars.py --profile sc-rsignell-esip2025 --dry-run
    python scripts/add_scalars.py --profile sc-rsignell-esip2025 --prefix noaa-eccofs/eccofs-avg-best

Run it for each store (eccofs-avg-fmrc and eccofs-avg-best). Safe to repeat. If a CI rebuild ever clears
a store from scratch (`session.store.clear()`), run it again.
"""
import argparse
import os

import icechunk
import numpy as np
import zarr
from obspec_utils.registry import ObjectStoreRegistry
from obstore.store import S3Store
from virtualizarr import open_virtual_dataset
from virtualizarr.parsers import HDFParser

from build_avg import SRC, SRC_BUCKET, store_storage

SCALARS = ["Vtransform", "Vstretching", "theta_s", "theta_b", "Tcline", "hc", "rho0", "spherical"]


def read_scalars(url):
    reg = ObjectStoreRegistry({f"s3://{SRC_BUCKET}": S3Store(SRC_BUCKET, region="us-east-1", skip_signature=True)})
    ds = open_virtual_dataset(url, registry=reg, parser=HDFParser(), loadable_variables=SCALARS, decode_times=False)
    return {v: (np.asarray(ds[v].values), dict(ds[v].attrs)) for v in SCALARS}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", default="ocean-icechunks")
    ap.add_argument("--prefix", default="noaa-eccofs/eccofs-avg-best")
    ap.add_argument("--endpoint", default="https://data.source.coop")
    ap.add_argument("--profile", help="AWS profile to read credentials from when AWS_ACCESS_KEY_ID is not set")
    ap.add_argument("--source", default=f"{SRC}avg/eccofs_avg_20261116_0002.nc", help="NOAA file to read the scalars from")
    ap.add_argument("--dry-run", action="store_true", help="print the values and the store's current state, write nothing")
    args = ap.parse_args()
    os.environ.pop("AWS_ENDPOINT_URL", None)  # would redirect the anonymous reads of NOAA's bucket

    vals = read_scalars(args.source)
    for v, (a, _) in vals.items():
        print(f"{v} = {a}")

    repo = icechunk.Repository.open(store_storage(args))
    session = repo.writable_session("main")
    root = zarr.open_group(session.store, mode="r+")
    print("already in store:", [v for v in SCALARS if v in root])
    if args.dry_run:
        return
    for v, (a, attrs) in vals.items():
        arr = root.create_array(v, shape=(), dtype=a.dtype, dimension_names=(), attributes=attrs, overwrite=True)
        arr[...] = a
    print(session.commit(f"add scalar model parameters ({', '.join(SCALARS)}) from {args.source.rsplit('/', 1)[-1]}"))


if __name__ == "__main__":
    main()
