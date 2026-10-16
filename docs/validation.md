# Validation

Validation completed on 15–16 October 2026 comprises offline tests, browser checks and one authorized backend run against the real Planet APIs. These checks establish the observed behavior of this version, not the positional accuracy or availability of future imagery. Methods are described in [method.md](method.md).

## Offline tests and interface checks

**69 automated tests passed with Python 3.12 on Linux.** The suite uses synthetic geometries, rasters and HTTP responses, without Planet credentials or quota consumption. Deprecation warnings were reported but no tests failed.

Coverage includes:

- Geometry transformation, holes and MultiPolygons, vertex limits and partition residuals without silently discarding area.
- Search filters, asset checks, UDM2 activation in groups of eight, cancellation and equivalence with sequential evaluation.
- Authenticated API requests, credential separation from storage downloads, proxy and CA settings, retry rules and uncertain-submission reconciliation.
- Mandatory confirmation of the current plan, prevention of duplicate starts, partial-result reporting and periodic waiting messages.
- An integrated search-to-mosaic workflow with two orders, a repeated reference, checksum verification and actual raster processing on two dates. Four cloudy pixels are correctly replaced by another scene from the same day, preserving band and provenance consistency. Both dates have the same grid.
- Resume without new orders or downloads of already verified complete files.

Offline GUI checks in Chromium 153 on Linux exercised vector upload, all four product options, the plan summary, invalidation of confirmation after filter changes, confirmation acceptance and cancellation, partial results and credential-free settings export. No browser storage, JavaScript or Content Security Policy errors, or horizontal overflow at 1024 pixels were observed.

On **2026-10-16**, [public CI](https://github.com/saveriofrancini/PlanetDownloader/actions/runs/38873613778) at commit `28ccccb85b336161a5f423165031a8a4bc6cf228` successfully installed binary dependencies and passed the test suite on Windows and Ubuntu with Python 3.12. The Windows job also passed `verifica_installazione.py`. These automated checks do not cover interactive BAT launchers, the folder picker or GUI operation on Windows. The Linux browser checks were not a live end-to-end test of the GUI using an API key.

## Real API run

The backend was run on Linux with OAuth authentication for an authorized university account. The test used one approximately **0.9953 km²** AOI and PlanetScope four-band surface reflectance with UDM2 for **4–5 July 2023**, inclusive UTC dates. Filters required scene cloud ≤10%, AOI cloud ≤20%, AOI clear ≥80%, geometric coverage ≥99%, standard quality and ground control. Local masking required clear pixels, confidence ≥70% and no unusable-pixel flags.

Search, asset permissions and UDM2 coverage completed successfully. One scene was available for each date; both had 0% scene and AOI cloud, 99% AOI clear and 100% geometric coverage. One confirmed order used the first date as the fixed reference and requested clipping and coregistration. Planet returned `success`; the local workflow returned `complete`, with no excluded scenes.

| Check | Observed result |
| --- | --- |
| Download integrity | 14 files, including the manifest; 1,582,225 bytes. Independently recalculated sizes, MD5 and SHA256 matched all 13 files listed in the manifest. |
| Daily outputs | Two four-band `float32` mosaics with NoData `-9999`, validity masks, provenance and metadata. |
| Shared grid | Identical CRS, affine transform and dimensions; 383 × 383 pixels at 3 m. |
| AOI validity | 110,562 valid AOI pixels per date, or 100% under the output grid's pixel-centre inclusion rule. |
| Planet coregistration report | Matching score increased from 0.687 to 0.704; the second date's image and UDM2 both carried `_coreg`. |
| Resume | Completed using the same order, submission intent and payload hash. All 14 downloaded files retained their SHA256, size and modification time. Mosaics were recomputed. |

The manifest is not itself one of the 13 files checked against its entries. Output checks found no inconsistencies among mosaics, masks, provenance and metadata. The 100% valid-pixel result uses a different aggregation from Planet's 99% clear coverage. The validator checked consistency with the recorded AOI-pixel denominator rather than independently rasterizing the AOI again; neither percentage measures cloud-classification accuracy.

The geometric order estimate was approximately **1.9907 km²**, twice the scene–AOI intersection. Actual contractual quota consumption was not verified. Resume made order-status GET requests; unchanged local files demonstrate reuse, not absence of API calls.

## Exploratory alignment check

Relative alignment was examined with phase cross-correlation on 64 × 64-pixel windows, using the red band and agreement with the green band, standardization and a Hann window. Of 25 candidate windows, 11 were not fully valid in both dates. Depending on normalization and subsequent screening, 4–14 windows were retained and median estimated displacement ranged from approximately 0.161 to 0.602 pixels. The 0.05-pixel search step was numerical resolution, not demonstrated accuracy.

The initial phase-normalized estimates reduced median red-band correlation after diagnostic translation. Correlation-based screening and an alternative without phase normalization were examined after observing this result. This sensitivity analysis does not establish one certifiable accuracy value. No diagnostic shift was applied to the delivered rasters. Unregistered source images and independent ground-control points were not compared, so neither independent before/after geometric improvement nor absolute positional error was measured. Planet's matching-score change is a service-reported metric, not a ground-measured error.

## Scope and remaining checks

The real run contained one scene per date and no masked-out pixels inside the AOI. Same-day multiscene selection and contaminated-pixel rejection were exercised by the synthetic tests. Other products, larger areas, long time series and multiple reference zones remain outside the real test's scope.

Several-minute UDM2 activation delays motivated periodic progress messages and activation in groups of eight. The revised search completed with the two masks already active; its speed benefit for eight initially inactive masks was not measured. Order polling reports elapsed session waiting time approximately once per minute without inventing completion percentages.

Interactive BAT launchers, the folder picker and GUI operation on Windows remain untested, as does a real Planet API workflow on Windows. Actual contractual quota accounting and absolute geometric accuracy also remain to be verified. The GUI is in English and uses an API-key field; the real backend test used OAuth on Linux.

The public repository includes synthetic examples only. Real imagery, AOI geometry, identifiers, account details and private diagnostic files are omitted. Redistribution rights for the real dataset have not been verified.

To run the offline suite after installing the development dependencies:

```console
python -m pytest -q
```
