# Method and limitations

PlanetDownloader searches PlanetScope imagery for vector-defined areas, prepares an order plan for confirmation, and produces local daily mosaics. The GUI is in Italian and accepts a Planet API key. The Python client also accepts a `requests.auth.AuthBase` authenticator as an alternative; OAuth is not exposed in the GUI.

## Search and geometric coverage

Each Polygon or MultiPolygon feature is processed as a separate area of interest (AOI). Geometries are transformed to WGS84 for API requests; areas are calculated geodesically. Search dates are inclusive UTC calendar dates. All catalog pages are read, and all available scenes satisfying the filters are retained, rather than selecting one scene per day or month.

The search first applies spatial, temporal, product and scene-quality filters, then checks access to the required assets. Supported bundles combine orthorectified PlanetScope surface reflectance or radiance, with four or eight bands, and UDM2. Results depend on catalog publication, asset availability and account permissions at the time of the request.

Optional AOI cloud and clear filters use Planet's coverage endpoint with `mode=UDM2`, not thumbnail estimates. Coverage is evaluated over the observed intersection of the scene and AOI. Geometric coverage measures the fraction of the full AOI intersected by the scene footprint. These are separate quantities: a high clear percentage does not imply complete AOI coverage. Errors are reported rather than replaced with estimated percentages.

UDM2 coverage may activate a mask and take several minutes. The application starts activations for the first required band in groups of eight candidate scenes, then evaluates the requested cloud and clear filters. Polling remains cancellable and reports waiting progress. Search does not submit imagery orders, but may activate mask assets.

## Order plan and quota estimate

The saved plan specifies scenes, reference images, processing zones and order payloads. The principal estimate sums the geodesic area of each scene footprint intersected with its clipping zone, for every scene in every order:

```text
estimated km² = sum over orders and ordered scenes of area(scene footprint ∩ clipping zone)
```

A reference scene counts each time it is included in an order. Separate AOI features count separately, including overlapping features. Scene–AOI pairs may therefore outnumber unique scene identifiers; the reported day count is an AOI–date count. A separate unique clipped-area estimate unions intersections by scene across all AOIs. AOI area and full-scene area are also reported as geometric comparisons.

These quantities are not guaranteed billing bounds. The application does not know the account contract, remaining quota, permitted reuse, rounding rules or conversion to credits. Clipping-related quota reductions depend on the Planet plan. No monetary price or conversion between km² and Monitoring Credits is inferred. GUI confirmation authorizes the displayed plan; changing filters requires a new search and confirmation.

## Coregistration and clipping

Planet's `coregister` tool aligns ordered scenes to an included reference. The application partitions the covered area into zones and retains one fixed reference per zone, including across order batches. A common output grid is subsequently constructed locally. Fixed references support temporal consistency within a zone; they do not establish global alignment between zones with different references or guarantee subpixel accuracy.

Orders request clipping and coregistration. Planet applies a fixed tool sequence with clipping before coregistration, irrespective of JSON tool order. Very small clips may contain too few recognizable features. Complex geometries are split to respect the 1,500-vertex limit; unsupported geometries are reported. Planet also requires polygon and ring areas of at least 1 m².

For a non-reference scene, both the delivered image and UDM2 must show successful coregistration through the `_coreg` suffix. The suffix can also indicate that an image was already sufficiently aligned; an empty post-registration quality field alone is not treated as failure. The reference does not need alignment to itself. Failed or unverifiable results are excluded and reported, with partial status where appropriate. Quality metadata are retained. Limited overlap, clouds, weak image texture and temporal change may cause failures or gaps. Coregistration does not correct radiometric differences, BRDF effects or differences between sensors.

## Pixel masking and daily mosaics

The eight UDM2 bands describe clear pixels, snow, shadow, light haze, heavy haze, cloud, confidence and unusable-pixel flags. In UDM2.1, haze uses band 4 and band 5 remains zero for compatibility. Band 8 is a bitmask: the application conservatively rejects any nonzero value. Confidence is a classifier output, not a guarantee of classification accuracy.

The default mask requires the clear class, excludes the selected unwanted classes and requires confidence of at least 70%. Source NoData, non-finite values, pixels without a UDM2 class, unusable pixels and pixels outside the zone or AOI remain invalid. Invalid source pixels are masked **before bilinear reprojection**, preventing contaminated values from entering interpolation. UDM2 classes and confidence use nearest-neighbour reprojection, and output validity is checked again against the mask.

One mosaic is produced per AOI and UTC acquisition date. Where valid observations overlap, selection is deterministic: clear class first, then higher UDM2 confidence; ties use descending scene clear percentage, ascending scene cloud percentage, scene identifier and zone identifier. All bands of a selected pixel come from the same scene. A provenance raster uses stable scene indices across dates and zones, with their identifiers recorded in JSON. Repeated reference deliveries are deduplicated within each zone. No radiometric balancing, temporal interpolation, monthly compositing or gap filling is applied; seams may remain visible.

All dates for an AOI share a projected CRS, origin, resolution and extent. Automatic CRS selection uses a local UTM zone; large AOIs or areas crossing UTM zones may require a user-selected projected CRS. A smaller output pixel size does not add spatial information. Processing uses raster blocks and temporary daily files while retaining the downloaded source files.

Mosaics are `float32` with NoData `-9999`. Source numerical scaling is retained: the application does not automatically convert stored values to physical reflectance or radiance. Compatible GeoTIFF scale and offset metadata are preserved; differing band counts, scales or offsets prevent mosaicking. PlanetScope surface reflectance commonly stores reflectance multiplied by 10,000; radiance must be interpreted using its own product metadata. Four-band order is Blue, Green, Red, NIR; eight-band order is Coastal Blue, Blue, Green I, Green, Yellow, Red, Red Edge, NIR.

## Integrity, resume and data handling

The application saves submission intent before creating an order. Deterministic names and matching payloads support reconciliation after an uncertain response. If an ambiguous submission cannot be found, the application reports the uncertainty and does not automatically resubmit. Stopping the application does not cancel existing remote orders or reverse quota consumption.

Downloads use temporary `.part` files and are committed only after completion. Manifest checksums and sizes are checked when available, and a local SHA256 is retained. Resume reuses verified complete files; incomplete files restart from the beginning rather than continuing by byte range. Mosaics may be recomputed. Resume depends on the order remaining accessible through Planet; it does not guarantee indefinite remote retention.

API credentials remain in application memory and are not saved in settings or result URLs. Signed storage downloads receive no Planet credentials. Configured proxies and CA certificates are supported without implicitly using `.netrc` credentials. Plans, vectors and reports can contain sensitive project geometries and area names.

The public repository contains synthetic examples only. Imagery and project files from the real validation are not redistributed: redistribution rights for that dataset have not been verified. Aggregate results and the limits of testing are documented in [validation.md](validation.md).

## Planet references

- [Data API items and assets](https://docs.planet.com/develop/apis/data/items/)
- [Orders product bundles](https://docs.planet.com/develop/apis/orders/product_bundles/)
- [Orders tools: clipping and coregistration](https://docs.planet.com/develop/apis/orders/tools/)
- [Usable Data Mask](https://docs.planet.com/data/imagery/udm/)
- [PlanetScope products](https://docs.planet.com/data/imagery/planetscope/)
- [Clipping and quota](https://support.planet.com/hc/en-us/articles/360019181798-How-to-Place-an-Order-with-Clipping-in-Planet-Explorer)
- [Imagery scaling and colour correction](https://support.planet.com/hc/en-us/articles/4404691539857-Planet-Imagery-and-Color-Correction)
