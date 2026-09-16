# PMC Downloader

A small Python CLI for responsibly downloading article files from the
[PubMed Central (PMC) Open Data bucket](https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/).
It uses the bucket's anonymous HTTPS interface, so AWS credentials and the AWS
CLI are not required.

## Requirements and setup

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)

Install the locked dependencies and run the CLI from the project directory:

```console
uv sync
uv run pmc-download --help
```

To install the command in an isolated environment instead:

```console
uv tool install .
pmc-download --help
```

## Usage

Pass PMC IDs as one comma-delimited argument. The `PMC` prefix is optional and
case-insensitive:

```console
uv run pmc-download PMC10009416,PMC12855588
```

Or read one PMC ID per line from a UTF-8 text file:

```text
PMC10009416
PMC12855588
```

```console
uv run pmc-download --input-file pmcids.txt
```

PDF is the default file type. Use `--types` (or `--file-types`) to request one
or more of the core article objects:

| Value | Downloaded object |
| --- | --- |
| `pdf` | Full article PDF, when available |
| `xml` | Full article in JATS XML |
| `txt` | Plain-text article extracted from the XML |
| `json` | PMC version metadata, including license and source URLs |

For example, download PDF, XML, and JSON metadata to a chosen directory:

```console
uv run pmc-download PMC10009416,PMC12855588 \
  --types pdf,xml,json \
  --output-dir ./articles
```

Set `--email researcher@example.org` or the `NCBI_EMAIL` environment variable
to include a contact address in the HTTP `User-Agent`.

The command exits with status `0` when every requested object is downloaded or
already present. It exits with status `1` if any PMCID is absent from the data
bucket, a requested type is unavailable, or a request fails. Other IDs continue
to be processed after an individual failure.

While running, the command displays counters for the total number of unique PMC
IDs, successful IDs, failed IDs, and IDs remaining. An ID succeeds only when all
requested file types are downloaded or already present. The final screen output
contains a summary and the path to a timestamped log file. Per-file results,
saved paths, sizes, retries, and error details are written to that log in the
output directory alongside the downloaded article files.

## Version selection and output files

PMC organizes files by article version. One PMCID may identify an author
manuscript, a published version, or both. This tool treats the highest numeric
version as the latest and downloads only that version. The version suffix is
retained in the output filename:

```text
articles/
└── PMC11370360.2.pdf
```

PMC notes that a higher version number reflects processing order and does not
always identify the preferred form of an article. In particular, separate
versions may represent an author manuscript and a final published article.

Not every article visible on the PMC website is available for automated
retrieval. The bucket contains versions in the PMC Article Datasets, including
the Open Access Subset and distributed author manuscripts. Some versions do not
have a PDF.

## Responsible downloading

The downloader deliberately favors safe, predictable access:

- It makes requests sequentially, never concurrently.
- It waits at least 0.34 seconds between request starts (fewer than three per
  second), following the conservative unauthenticated rate in the
  [NCBI E-utilities usage guidelines](https://www.ncbi.nlm.nih.gov/books/NBK25497/#chapter2.Frequency_Timing_and_Registration_o).
- It honors `Retry-After` and uses exponential backoff for HTTP 429 and
  transient server errors.
- It streams article objects to temporary files and atomically moves complete
  downloads into place.
- It validates the MD5 value supplied by PMC and skips an existing object when
  its checksum already matches, avoiding unnecessary transfer.

For very large retrieval jobs, follow NCBI's recommendation to run on weekends
or between 9:00 PM and 5:00 AM US Eastern time. PMC also publishes daily S3
inventory files for workflows involving very large collections.

## Content licenses

The MIT license in this repository applies only to the downloader source code.
It does not grant rights to downloaded publications. License terms vary by
article version; consult the `license_code` and license statement for each
article. Users are responsible for complying with PMC's
[copyright notice](https://pmc.ncbi.nlm.nih.gov/about/copyright/).

## Development

Install development dependencies and run the checks with `uv`:

```console
uv sync --all-groups
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=pmc_downloader
```
