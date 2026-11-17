# eccofs

Tools and notebooks for building a **virtual [Icechunk](https://icechunk.io) store** over the
NOAA NOS ECCOFS ocean model output published in the public AWS bucket
[`noaa-nos-eccofs-pds`](https://noaa-nos-eccofs-pds.s3.amazonaws.com/index.html).

## What is this?

ECCOFS output is published as many large NetCDF files. Working with them means opening them one at
a time over the network, which is slow, and the files are several GB each.

A *virtual* Icechunk store fixes this without copying any data. The Icechunk repository holds only
Zarr metadata and chunk references. The references point at byte ranges inside the original NetCDF
files, which stay in the bucket. Tools such as [Xarray](https://xarray.dev) then see the whole
collection as a single Zarr dataset, and read only the chunks they need.

## Source data

| | |
|---|---|
| Bucket | `s3://noaa-nos-eccofs-pds` (arn:aws:s3:::noaa-nos-eccofs-pds) |
| Region | `us-east-1` |
| Access | Public, anonymous (`--no-sign-request`) |

Top-level prefixes, with example file names:

| Prefix | Example file |
|---|---|
| `avg/` | `eccofs_avg_20260608_0001.nc` |
| `his/` | `eccofs_his_20260608_0001.nc` |
| `qck/` | `eccofs_qck_20260608.nc`, `eccofs_qck_20260608_fwd.nc` |
| `sta/` | `eccofs_sta_20260727.nc` |
| `frc/` | `eccofs_frc_Pair_20261024.nc` |
| `obs/` | `eccofs_obs_ssh_sst_cmems_v2_20261024.nc` |
| `riv/` | `eccofs_riv_20261024.nc` |

New daily files continue to be added, so the store will need to be extended over time.

## Goals

- Build a virtual Icechunk store that references the ECCOFS NetCDF files in place.
- Expose the collection as one lazily-loaded dataset in Xarray.
- Validate the store by comparing values read through it with the source NetCDF files.
- Document how to build and load the store so others can reproduce or extend it.

## How it works

1. **Inventory** the source NetCDF files and check that their structure (variables, chunking,
   compression, coordinates) is consistent.
2. **Create references** to each file's chunks with [VirtualiZarr](https://virtualizarr.readthedocs.io).
3. **Commit** the references to an Icechunk repository.
4. **Validate** by comparing data read through the store with data read directly from the NetCDF files.

## Usage

Once the store is built, it can be opened with Xarray along these lines:

```python
import icechunk
import xarray as xr

SOURCE = "s3://noaa-nos-eccofs-pds/"

# Placeholders: fill in once the store location is settled.
storage = icechunk.s3_storage(bucket="<bucket>", prefix="<prefix>", anonymous=True)
config = icechunk.RepositoryConfig.default()
# Allow reads of the source NetCDF bytes in the public NOAA bucket:
config.set_virtual_chunk_container(
    icechunk.VirtualChunkContainer(SOURCE, icechunk.s3_store(region="us-east-1"))
)
repo = icechunk.Repository.open(
    storage, config,
    authorize_virtual_chunk_access={SOURCE: None},  # None = anonymous
)
session = repo.readonly_session("main")
ds = xr.open_zarr(session.store, consolidated=False)
```

## Status

Early work in progress. The store location and build scripts will be added here as they are developed.

## Requirements

- Python 3.10+
- `icechunk`, `virtualizarr`, `xarray`, `zarr`, `obstore` (or `fsspec`/`s3fs`)

## License

[Apache License 2.0](LICENSE).
