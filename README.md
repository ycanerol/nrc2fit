# nrc2fit

Convert a Nike Run Club (NRC) data export into FIT files that self-hosted trackers can import. Tested with [Endurain](https://github.com/endurain-project/endurain).

## Why

Nike has no export button, but on request it sends a zip with one TCX file per run. Those files don't import well as they are:

- Every value is wrapped in newlines. That breaks `tcxreader`, so Endurain rejects every file.
- GPS, heart rate, distance and cadence are stored as separate trackpoints. GPS-only parsers keep the route and drop the rest.
- TCX has no moving time, so pauses inflate duration and pace.
- The run name, mood and perceived effort live in a Nike extension that importers ignore.

`nrc2fit` writes one FIT file per run, with the missing data filled in:

| FIT | Taken from |
|---|---|
| Records | GPS points, with heart rate, cadence, altitude and distance matched by time |
| Moving time | Nike's lap time (pauses excluded) |
| Name | `com.nike.name`. If a run has none, an NRC-style name like "Tuesday Evening Run" is generated from the local start time |
| Sport | Running. `trail` if tagged so; `treadmill` for indoor runs without GPS |
| Feel / RPE | NRC mood (`emotion`) and `rpe` tags |

Runs without GPS keep their distance and time. A run that appears twice in the export is kept once.

## Get your data

1. Sign in at <https://www.nike.com/member/settings/privacy> and click "Request Data" under "Download Your Data" at the bottom of the page. There is no format choice.
2. Nike sends a zip (`nikeuserdata.zip`) with one TCX per run in `tcx/`, plus a PDF summary.

## Convert

```sh
uvx --from git+https://github.com/ycanerol/nrc2fit nrc2fit nikeuserdata.zip nrc-fit/
```

Or `pip install git+https://github.com/ycanerol/nrc2fit` and run `nrc2fit`. The input can be the zip or an extracted `tcx/` folder. `--skip-no-gps` drops runs without a route.

The output folder holds one `.fit` per run, plus a `manifest.csv` you can review before importing. It lists every run's date, name, distance and times. Shoes and notes are in the manifest only.

## Import into Endurain

1. Back up the database. A bulk import can't be undone.
2. Copy the `.fit` files into `data/activity_files/bulk_import`.
3. Go to Settings → Import → "Import activities" next to "Bulk Import".

## Limitations

- Tested on a single export (97 runs, 2014–2026, phone and watch recordings).
- Runs without GPS have no location, so their timezone is borrowed from the nearest run that has one.
- Elevation gain is left for the importer to compute. Old phone altitude data is noisy.

## License

MIT
