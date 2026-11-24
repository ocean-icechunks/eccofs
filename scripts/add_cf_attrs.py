"""Give an avg Icechunk store the CF coordinate attributes that OGC tile and EDR services look for.

ROMS names its 2-D coordinates `lon_rho`, `lat_rho` (and the u, v and psi equivalents) with the standard names
`grid_longitude_at_cell_center` and `grid_latitude_at_cell_center`, which no CF-aware tool treats as longitude
and latitude, so xpublish-tiles and xpublish-edr cannot tell which variables are X and Y. This sets the CF
names and units on those coordinates. In the best-estimate stores it also marks `time` as the time axis. The
forecast-run stores keep their `time` (forecast_reference_time) and `step` (forecast_period) as they are.

Only attributes change, in one small commit; no chunk or array is touched. The daily updates write time
slices and leave static arrays alone, so the attributes stay. Safe to repeat: it commits only when something
differs.

    python scripts/add_cf_attrs.py --profile sc-rsignell-esip2025 --dry-run
    python scripts/add_cf_attrs.py --profile sc-rsignell-esip2025 --prefix noaa-eccofs/eccofs-avg-best
"""
import argparse
import os

import icechunk
import zarr

from build_avg import store_storage

GRIDS = ["rho", "u", "v", "psi"]


def wanted_attrs(root):
    """{array name: attributes to set} for the arrays this store has."""
    want = {}
    for g in GRIDS:
        if f"lon_{g}" in root:
            want[f"lon_{g}"] = {"standard_name": "longitude", "units": "degrees_east"}
        if f"lat_{g}" in root:
            want[f"lat_{g}"] = {"standard_name": "latitude", "units": "degrees_north"}
    if "step" not in root and "time" in root:   # best-estimate store: one valid time per entry
        want["time"] = {"standard_name": "time", "axis": "T"}
    return want


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", default="ocean-icechunks")
    ap.add_argument("--prefix", default="noaa-eccofs/eccofs-avg-best")
    ap.add_argument("--endpoint", default="https://data.source.coop")
    ap.add_argument("--profile", help="AWS profile to read credentials from when AWS_ACCESS_KEY_ID is not set")
    ap.add_argument("--dry-run", action="store_true", help="print what would change, write nothing")
    args = ap.parse_args()
    os.environ.pop("AWS_ENDPOINT_URL", None)  # would redirect the anonymous reads of NOAA's bucket

    repo = icechunk.Repository.open(store_storage(args))
    session = repo.writable_session("main")
    root = zarr.open_group(session.store, mode="r+")
    changed = []
    for name, attrs in wanted_attrs(root).items():
        have = dict(root[name].attrs)
        diff = {k: v for k, v in attrs.items() if have.get(k) != v}
        if diff:
            print(f"{name}: " + ", ".join(f"{k}: {have.get(k)!r} -> {v!r}" for k, v in diff.items()))
            changed.append(name)
            if not args.dry_run:
                root[name].attrs.update(diff)
    if not changed:
        print("nothing to change")
        return
    if args.dry_run:
        print(f"dry run: would update {len(changed)} arrays")
        return
    print(session.commit(f"add CF coordinate attributes ({', '.join(changed)})"))


if __name__ == "__main__":
    main()
