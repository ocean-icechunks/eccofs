# Handoff: eccofs virtual Icechunk

Status as of 2026-11-17. Nothing has been written to OSN yet. Work so far was research and a local smoke test. Follow the `virtual-icechunk` skill order: plan, smoke test, production build, project docs.

## Goal

Build a virtual Icechunk store over NOAA NOS ECCOFS output (`s3://noaa-nos-eccofs-pds`, public, `us-east-1`, anonymous). The store holds only metadata and byte-range references. The NetCDF bytes stay in the NOAA bucket.

## Destination

- OSN S3-compatible storage, AWS CLI profile `osn-esip`.
- Endpoint: `https://usgs.osn.mghpcc.org` (from the profile).
- Target: `s3://esip/rsignell/eccofs`. Checked that no `eccofs` prefix exists there yet.
- The profile works (`aws --profile osn-esip s3 ls s3://esip/rsignell/`). Never put keys in code or commits.
- Not yet checked: the public read URL for the store, and whether the OSN bucket allows anonymous reads and CORS.

## Repo and workflow

- https://github.com/rsignell/eccofs (public). `main` has a README, an Apache-2.0 `LICENSE`, and this file.
- Agreed approach: code changes go through branches and PRs, which the user merges. A summary goes in the chat and in the PR description. Never merge without being asked. Never force-push `main` without asking.
- Local git identity is set to the user's own email, at their request.
- The README still has placeholders: the store location in the usage example, and what "ECCOFS" stands for.

## Source findings

| Prefix | Files | Size | Format | Time steps |
|---|---|---|---|---|
| `his/` | 1,332 | 6.2 TB | NetCDF-3 64-bit offset | 1 per file, daily at 00:00 |
| `avg/` | 1,184 | 9.0 TB | NetCDF-4/HDF5 | 1 per file, daily at 12:00 |
| `qck/` | 897 | 2.0 TB | not inspected | not inspected |
| `sta/` | 106 | small | not inspected | not inspected |
| `frc/` | 189 | 0.2 TB | not inspected | not inspected |
| `obs/` | 42 | small | not inspected | not inspected |
| `riv/` | 21 | small | not inspected | not inspected |

- **Different parsers per product.** `his` needs VirtualiZarr's `NetCDF3Parser`. `avg` needs `HDFParser`. The `NetCDF3Parser` has no chunking or compression to inherit.
- **Forecast structure.** Each file is one lead day of a forecast run. A run starting on date D has files `0001` to `0009` (`his`) or `0001` to `0008` (`avg`), with valid times D, D+1, and so on. That is 148 runs per product so far, from 2026-06-08 to 2026-11-13. Valid times overlap between runs, so concatenating along time gives duplicate, non-monotonic values.
- **Grid.** ROMS curvilinear, 1443 × 1667 horizontal, 50 vertical levels, with staggered `rho`, `u`, `v` and `psi` grids. `his` has 109 variables, including many scalar model parameters.
- **Layout caveat.** One time step per file means a long time series at a single point reads one chunk from thousands of multi-GB files. The files belong to NOAA, so this can't be fixed on our side.

## Decisions needed from the user

1. **Time structure** (blocks everything else):
   - (a) two dimensions, run date × lead day, keeping every forecast;
   - (b) one "best estimate" series, taking lead day `0001` of each run;
   - (c) both, as separate groups.
2. **Which products to include.** The user said to start with a few `avg` files for the smoke test, and the first production build is probably `avg/` or `his/`.
3. **Scalar model parameters** (`ntimes`, `dstart`, `nHIS` and similar) vary per run. Drop them, or keep them with a run dimension?
4. **Repository layout:** one repository with one group per product, or separate repositories.

## Smoke test status

- Local scripts were in the session scratchpad, which may be cleared: `smoke_his.py`, `times.py`, `times_avg.py`. They are not saved in the repo.
- A five-file `his` run opened a virtual dataset in 8.4 s with the NetCDF-3 parser. It was killed with exit 137, likely out of memory, before writing to the temporary repository.
- Likely cause: the script compared values by opening a source file with `xr.open_dataset(..., engine="scipy")` on a file object, which reads the whole 4.6 GB file into memory. Fix: use `scipy.io.netcdf_file(fileobj, mmap=False)` and read only the needed slice, or compare against ranged reads.
- Another script bug, since fixed: `vds.vz.to_icechunk` takes `session.store`, not the session.
- Not yet done: a successful write and read-back of a few `avg` files into a local temporary repository, comparing values against the source. This still has to pass before any build to OSN.

## Settings to carry into the build

- `zarr.config.set({"async.concurrency": 128})` for writers and readers.
- Write the time coordinate as one chunk: `encoding={"ocean_time": {"chunks": (n,)}}`.
- Combine with `coords="minimal"`, `compat="override"`, `combine_attrs="override"`.
- Authorize the source prefix `s3://noaa-nos-eccofs-pds/` anonymously for readers. Call `repo.save_config()`.
- Source registry: `obspec_utils.registry.ObjectStoreRegistry` with `obstore` `S3Store(..., skip_signature=True)`.
- `VirtualiZarr` imports `ObjectStoreRegistry` from `virtualizarr.registry` with a deprecation warning, so use `obspec_utils`.
- Installed versions: icechunk 2.3.1, virtualizarr 2.8.0, xarray 2026.9.0, zarr 3.4.2, obstore 0.12.1, Python 3.13.
- Do not name a scratch script `inspect.py` or anything that shadows the standard library.

## Next steps

1. Get the time-structure decision and the product list from the user.
2. Finish the local smoke test on a few `avg` files, using the memory-safe source comparison.
3. Review the smoke-test results with the user.
4. Write to a scratch prefix on OSN, read it back through the public path, and check CORS on both hosts if browser access matters.
5. Write the production build script, in a PR.
6. Finish the README with the real store location, a tested loading example and the notes above.
