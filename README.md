# gsi-elevation-cache-proxy

Independently deployable, loopback-only, SQLite-backed caching proxy for
Earth Platform's GSI (Geospatial Information Authority of Japan) elevation
DEM tiles -- MINECRAFT-578 / MINECRAFT-571.

Pinned source: https://github.com/pirut/earth-platform, tag `combined-73`
(= release "Earth Platform 0.1.0-alpha.73", commit
`88b13c33954550c6ac660dceb743a4288a9aafac`). Endpoint inventory below was
read from that exact tag's
`geography/src/main/java/io/earthplatform/geography/globe/GsiElevation.java`
and `RangeFiles.java`, not `main`.

## Run

```
python3 -m unittest test_proxy -v     # 13 tests, local mock upstream only
python3 proxy.py                      # serves on 127.0.0.1:8074 by default
```

## Contract

- Loopback-only bind (`127.0.0.1`, hardcoded in `run_server`).
- Fixed, allowlisted upstream: `https://cyberjapandata.gsi.go.jp/xyz/` by
  default (unchanged from the mod's own `GsiElevation.GSI_TILES`). Override
  via `GSI_CACHE_UPSTREAM`; the default is untouched unless a caller sets it.
- Request path is validated against `{dem_png|dem5a_png}/{zoom}/{x}/{y}.png`,
  with the mod's own zoom pairing enforced (`dem_png` only to zoom 14,
  `dem5a_png` only from zoom 15) -- no arbitrary URL forwarding.
- SQLite file cache, keyed by tile path, restart-persistent.
- **Cache lifetime: no time-based expiration (TTL -1)**, per John's
  2026-11-24 Earth-Platform-wide decision -- not unbounded: still subject to
  the byte cap/LRU eviction below, and `DELETE /{path}` explicitly
  invalidates one entry. A genuine negative (upstream 404/403, "no tile
  here") is cached the same way, distinguishable on disk from a real 0-byte
  response. The dedicated `/__health` endpoint below is the separate TTL-0
  resource this decision's health-check exception applies to -- ordinary
  tile requests stay TTL -1.
- Range requests served as 206 slices of the cached object; a stable
  SHA-256 ETag supports `If-None-Match` -> 304. Matches the mod's own
  `RangeFiles.fetch()`, which always requests `Range: bytes=0-4194303` and
  accepts both 200 and 206 as success.
- A transient upstream failure (5xx, connection error, dropped transfer) is
  retried 3x with backoff and never cached; a response that is HTTP 200/206
  but isn't actually a well-formed PNG (an S3/CloudFront error body, say) is
  treated as a failed attempt -- retried, never cached -- mirroring the
  mod's own `MercatorTileLayer.read()`, which only writes its disk cache
  after `decode()` succeeds.
- Concurrent cache misses for the same tile single-flight to exactly one
  upstream request.
- Bounded storage: `max_bytes`, LRU eviction down to 90% on overflow.
- Dedicated `/__health` endpoint (HEAD or GET): a fresh, uncached, TTL-0 live
  probe against the real upstream -- 200 on success, 502 on failure -- that
  never reads or writes the ordinary tile cache in either direction. Matches
  the contract established across the other Earth Platform proxy lanes
  (elevation-cache-proxy, eox-imagery-cache-proxy) for the mod's configured-
  proxy reachability checks. An ordinary `HEAD` on a real tile path is
  unaffected: it goes through the normal TTL -1 cache, identically to `GET`
  minus the body.

## Live verification (read-only, 2026-11-24, no caching/mutation)

- `GET /dem_png/14/14552/6451.png` with `Range: bytes=0-100` against the
  real `cyberjapandata.gsi.go.jp` answered `206`, `Content-Range: bytes
  0-100/50651`, with an S3/CloudFront `ETag` and `Last-Modified` -- Range and
  conditional semantics are genuinely supported upstream, not just assumed.
- `GET /dem_png/14/0/0.png` (off Japan, no data) answered `404` with an XML
  body, not a PNG -- confirms the "negative, not a tile" case this proxy
  caches separately from a real tile.

## Terms / attribution

GSI's elevation tiles are published under Japan's Public Data License 1.0,
which permits reproduction and caching with attribution. The mod already
carries the required attribution string
(`GsiElevation.ATTRIBUTION`); this proxy does not touch or strip it. No
terms blocker found for running this cache.

## Status

Both GitHub destinations exist (`brooswit-intel/gsi-elevation-cache-proxy`
and the pinned `brooswit-minecraft/earth-platform` fork for later wiring).
The repo-only Write invitation for this agent's GitHub account (`booswrit`)
was accepted and verified (`gh repo view ... --json viewerPermission` ->
`WRITE`) before this tree was pushed.
