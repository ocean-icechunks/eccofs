# eccofs

Tools and notebooks for building a **virtual [Icechunk](https://icechunk.io) store** over the
[ECCO](https://www.ecco-group.org/) (Estimating the Circulation and Climate of the Ocean) ocean state estimate.

## What is this?

ECCO distributes its output as many NetCDF files. Working with them means opening hundreds or
thousands of files one at a time, which is slow, especially over the network.

A *virtual* Icechunk store fixes this without copying any data. The Icechunk repository holds only
Zarr metadata and chunk references. The references point at byte ranges inside the original NetCDF
files, which stay where they are. Tools such as [Xarray](https://xarray.dev) then see the whole
collection as a single Zarr dataset, and read only the chunks they need.

## Goals

- Build a virtual Icechunk store that references the ECCO NetCDF files in place.
- Expose the collection as one lazily-loaded, analysis-ready dataset in Xarray.
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

# Placeholders: fill in once the store location is settled.
storage = icechunk.s3_storage(bucket="<bucket>", prefix="<prefix>", anonymous=True)
config = icechunk.RepositoryConfig.default()
# Authorize access to the bucket(s) holding the source NetCDF files:
config.set_virtual_chunk_container(
    icechunk.VirtualChunkContainer("<source-url-prefix>/", icechunk.s3_store(region="<region>"))
)
repo = icechunk.Repository.open(
    storage, config,
    authorize_virtual_chunk_access={"<source-url-prefix>/": None},  # None = anonymous
)
session = repo.readonly_session("main")
ds = xr.open_zarr(session.store, consolidated=False)
```

## Status

Early work in progress. The store location, source file paths and build scripts will be added here
as they are developed.

## Requirements

- Python 3.10+
- `icechunk`, `virtualizarr`, `xarray`, `zarr`, `obstore` (or `fsspec`/`s3fs`)

## License

To be determined.
