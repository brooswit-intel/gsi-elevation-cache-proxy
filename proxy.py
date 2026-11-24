"""Loopback-only caching proxy for GSI's (Geospatial Information Authority of
Japan) elevation PNG tiles.

Upstream contract (inventoried from Earth Platform 0.1.0-alpha.73 / tag
combined-73, geography/src/main/java/io/earthplatform/geography/globe/GsiElevation.java,
corroborated live against the real endpoint on 2026-11-24):
  base: https://cyberjapandata.gsi.go.jp/xyz/
  path: {product}/{zoom}/{x}/{y}.png
  products actually requested by the mod: "dem_png" (10 m model, zoom <= 14)
        and "dem5a_png" (5 m lidar, zoom >= 15; the mod additionally reads the
        dem_png parent tile at zoom 14 to fill lidar gaps -- that is still a
        "dem_png" request, just at a shifted x/y, so no third product name
        is needed here).
  The mod's RangeFiles.fetch() always sends "Range: bytes=0-4194303" on every
  request (whole-tile reads happen to fit in that bound) and treats both 200
  and 206 as success, slicing a 200 body to the requested window itself. 404
  and 403 are "no data for this tile" (cached as an explicit negative by the
  mod, not a failure). Live probe confirmed: real tiles answer 206 with
  Content-Range and an S3/CloudFront ETag + Last-Modified; a missing tile
  answers 404 with an XML body, not a PNG.

Default upstream is exactly the mod's existing default (no behavior change
for a caller that never overrides it). GSI_CACHE_UPSTREAM overrides it (e.g.
for tests pointed at a local stand-in) -- the override point the ticket asks
for, scoped to this proxy's own egress; unset, upstream behavior is
unchanged.

Cache lifetime (per John's Earth-Platform-wide decision, 2026-11-24): a
successful, validated data response has no time-based expiration (TTL -1,
not infinite-and-unbounded -- it is still subject to the byte cap/LRU
eviction below, and can be explicitly invalidated). A confirmed negative
(404/403 "this tile does not exist") is cached the same way, since that is
GSI's own stable answer for off-coverage tiles. A failure, a non-(200/206/
403/404) status, or a transfer that doesn't match its declared length is
never cached under any key. There is no dedicated upstream health-check
resource for this source (unlike Terrarium's separate EarthStatus probe) --
ordinary tile GETs are the only upstream traffic, so John's TTL-0-health-
check rule has no separate resource to apply to here.
"""
from __future__ import annotations

import hashlib
import http.client
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_UPSTREAM = "https://cyberjapandata.gsi.go.jp/xyz/"
MAX_BYTES = 4 * 1024 * 1024  # matches the mod's own RangeFiles MAX_BYTES bound
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_PATH_RE = re.compile(r"^(dem_png|dem5a_png)/(\d+)/(\d+)/(\d+)\.png$")


def parse_tile_path(path: str) -> str | None:
    """Validates the request names a GSI DEM tile this mod actually reads and
    nothing else (no arbitrary upstream forwarding); returns the normalized
    upstream path, or None to refuse."""
    m = _PATH_RE.match(path.strip("/"))
    if not m:
        return None
    product, z, x, y = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
    if product == "dem_png" and not (0 <= z <= 14):
        return None
    if product == "dem5a_png" and not (15 <= z <= 18):
        return None
    return f"{product}/{z}/{x}/{y}.png"


@dataclass
class Stats:
    upstream_requests: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    negative_hits: int = 0
    not_modified: int = 0
    bytes_downloaded: int = 0
    evictions: int = 0
    probe_requests: int = 0
    probe_failures: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def as_dict(self) -> dict:
        with self.lock:
            return {
                "upstream_requests": self.upstream_requests,
                "cache_hits": self.cache_hits,
                "cache_misses": self.cache_misses,
                "negative_hits": self.negative_hits,
                "not_modified": self.not_modified,
                "bytes_downloaded": self.bytes_downloaded,
                "evictions": self.evictions,
                "probe_requests": self.probe_requests,
                "probe_failures": self.probe_failures,
            }


class TileCache:
    """SQLite-backed, restart-persistent, bounded tile cache. No time-based
    expiration (TTL -1): a stored row is valid until explicitly invalidated or
    evicted for space. Negative ("no tile here") results are stored with
    body=NULL so they are distinguishable from a 0-byte real response.

    Never stores a response unless the fetch resolved to one of: a genuine
    tile (200/206, well-formed PNG), or a genuine negative (404/403). A
    timeout, connection error, 5xx, or a malformed/truncated "200" body is
    never written, so a transient failure is never served as cached data.
    """

    def __init__(self, db_path: str, max_bytes: int):
        self.max_bytes = max_bytes
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS tiles (
                 path TEXT PRIMARY KEY,
                 etag TEXT,
                 body BLOB,
                 is_negative INTEGER NOT NULL DEFAULT 0,
                 size INTEGER NOT NULL,
                 created_at REAL NOT NULL,
                 last_used_at REAL NOT NULL
               )"""
        )
        self._conn.commit()
        self._db_lock = threading.Lock()

    def get(self, path: str):
        """Returns ("hit", etag, body), ("negative", None, None), or None."""
        with self._db_lock:
            row = self._conn.execute(
                "SELECT etag, body, is_negative FROM tiles WHERE path = ?", (path,)
            ).fetchone()
            if row is None:
                return None
            self._conn.execute("UPDATE tiles SET last_used_at = ? WHERE path = ?", (time.time(), path))
            self._conn.commit()
        etag, body, is_negative = row
        return ("negative", None, None) if is_negative else ("hit", etag, body)

    def put(self, path: str, body: bytes) -> str:
        etag = hashlib.sha256(body).hexdigest()
        now = time.time()
        with self._db_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tiles (path, etag, body, is_negative, size, created_at, last_used_at) "
                "VALUES (?, ?, ?, 0, ?, ?, ?)",
                (path, etag, body, len(body), now, now),
            )
            self._conn.commit()
        return etag

    def put_negative(self, path: str) -> None:
        now = time.time()
        with self._db_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tiles (path, etag, body, is_negative, size, created_at, last_used_at) "
                "VALUES (?, NULL, NULL, 1, 0, ?, ?)",
                (path, now, now),
            )
            self._conn.commit()

    def invalidate(self, path: str) -> bool:
        with self._db_lock:
            cur = self._conn.execute("DELETE FROM tiles WHERE path = ?", (path,))
            self._conn.commit()
            return cur.rowcount > 0

    def total_bytes(self) -> int:
        with self._db_lock:
            return self._conn.execute("SELECT COALESCE(SUM(size), 0) FROM tiles").fetchone()[0]

    def evict_to_fit(self, stats: Stats) -> None:
        if self.max_bytes <= 0:
            return
        target = self.max_bytes * 9 // 10
        with self._db_lock:
            total = self._conn.execute("SELECT COALESCE(SUM(size), 0) FROM tiles").fetchone()[0]
            if total <= self.max_bytes:
                return
            rows = self._conn.execute("SELECT path, size FROM tiles ORDER BY last_used_at ASC").fetchall()
            removed = 0
            for path, size in rows:
                if total - removed <= target:
                    break
                self._conn.execute("DELETE FROM tiles WHERE path = ?", (path,))
                removed += size
                with stats.lock:
                    stats.evictions += 1
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()


class UpstreamError(Exception):
    pass


def _is_well_formed_png(body: bytes) -> bool:
    return body.startswith(PNG_SIGNATURE) and body.rstrip().endswith(b"IEND\xaeB`\x82")


#: generous bound for an error/non-tile body we only need to classify, not use.
_DRAIN_CAP = 4096


def fetch_upstream(upstream_base: str, tile_path: str, timeout: float = 10.0, retries: int = 3, max_response_bytes: int = MAX_BYTES):
    """GET with the same Range window the mod itself sends. Returns
    ("ok", body) for 200/206 with a well-formed PNG, ("negative", None) for a
    genuine 404/403, and raises UpstreamError for anything else (including a
    structurally invalid "success").

    Status is checked BEFORE any body is read, and every read is capped
    (`max_response_bytes` for a claimed success, `_DRAIN_CAP` otherwise) --
    never an unbounded `resp.read()`. An origin that ignores Range and tries
    to answer with something far larger than expected is read only up to the
    cap, found truncated/oversized by the length check below, and rejected
    -- it is never allowed to force an unbounded download before that
    rejection happens."""
    parsed = urllib.parse.urlsplit(upstream_base)
    full_path = parsed.path.rstrip("/") + "/" + tile_path
    last_exc: Exception | None = None
    for attempt in range(retries):
        if attempt:
            time.sleep(0.25 * (2 ** attempt))
        conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(parsed.netloc, timeout=timeout)
        try:
            conn.request(
                "GET",
                full_path,
                headers={
                    "User-Agent": "earth-platform/gsi-dem-v1",
                    "Range": f"bytes=0-{MAX_BYTES - 1}",
                },
            )
            resp = conn.getresponse()
            if resp.status in (404, 403):
                resp.read(_DRAIN_CAP)
                return "negative", None
            if resp.status not in (200, 206):
                last_exc = UpstreamError(f"HTTP {resp.status} for {full_path}")
                resp.read(_DRAIN_CAP)
                continue
            body = resp.read(max_response_bytes + 1)
            if len(body) > max_response_bytes:
                last_exc = UpstreamError(f"response exceeded the {max_response_bytes}-byte cap for {full_path} (origin may be ignoring Range)")
                continue
            declared = resp.getheader("Content-Length")
            if declared is not None and int(declared) != len(body):
                last_exc = UpstreamError(f"truncated transfer for {full_path}: declared {declared}, got {len(body)}")
                continue
            if not _is_well_formed_png(body):
                last_exc = UpstreamError(f"malformed PNG (HTTP {resp.status} but not a valid tile) for {full_path}")
                continue
            return "ok", body
        except (OSError, http.client.HTTPException) as net_err:
            last_exc = net_err
            continue
        finally:
            conn.close()
    raise UpstreamError(f"upstream fetch failed after {retries} attempts: {last_exc}")


class _Flight(threading.Event):
    result = None
    error: Exception | None = None


class _SingleFlight:
    def __init__(self):
        self._lock = threading.Lock()
        self._inflight: dict[str, _Flight] = {}

    def run(self, key: str, loader):
        with self._lock:
            flight = self._inflight.get(key)
            is_leader = flight is None
            if is_leader:
                flight = _Flight()
                self._inflight[key] = flight
        if is_leader:
            try:
                flight.result = loader()
            except Exception as exc:  # noqa: BLE001 - propagated to every waiter
                flight.error = exc
            finally:
                with self._lock:
                    self._inflight.pop(key, None)
                flight.set()
        else:
            flight.wait()
        if flight.error is not None:
            raise flight.error
        return flight.result


class GsiCacheProxy:
    def __init__(self, db_path: str, upstream_base: str | None = None, max_bytes: int = 256 * 1024 * 1024):
        self.upstream_base = upstream_base or os.environ.get("GSI_CACHE_UPSTREAM", DEFAULT_UPSTREAM)
        self.cache = TileCache(db_path, max_bytes)
        self.stats = Stats()
        self._single_flight = _SingleFlight()

    def serve_tile(self, request_path: str, if_none_match: str | None, range_header: str | None):
        tile_path = parse_tile_path(request_path)
        if tile_path is None:
            return 404, {}, b""

        cached = self.cache.get(tile_path)
        if cached is None:
            def load():
                outcome, body = fetch_upstream(self.upstream_base, tile_path)
                if outcome == "negative":
                    self.cache.put_negative(tile_path)
                    return "negative", None, None
                with self.stats.lock:
                    self.stats.upstream_requests += 1
                    self.stats.bytes_downloaded += len(body)
                etag = self.cache.put(tile_path, body)
                self.cache.evict_to_fit(self.stats)
                return "hit", etag, body

            with self.stats.lock:
                self.stats.cache_misses += 1
            try:
                kind, etag, body = self._single_flight.run(tile_path, load)
            except UpstreamError:
                return 502, {}, b""
        else:
            kind, etag, body = cached
            with self.stats.lock:
                if kind == "negative":
                    self.stats.negative_hits += 1
                else:
                    self.stats.cache_hits += 1

        if kind == "negative":
            return 404, {}, b""

        if if_none_match is not None and if_none_match == etag:
            with self.stats.lock:
                self.stats.not_modified += 1
            return 304, {"ETag": etag}, b""

        if range_header and range_header.startswith("bytes="):
            start_s, _, end_s = range_header[len("bytes="):].partition("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else len(body) - 1
            end = min(end, len(body) - 1)
            if start > end or start >= len(body):
                return 416, {"ETag": etag, "Content-Range": f"bytes */{len(body)}"}, b""
            chunk = body[start:end + 1]
            headers = {"ETag": etag, "Content-Range": f"bytes {start}-{end}/{len(body)}", "Content-Type": "image/png"}
            return 206, headers, chunk

        return 200, {"ETag": etag, "Content-Type": "image/png"}, body

    def invalidate(self, request_path: str) -> bool:
        tile_path = parse_tile_path(request_path)
        return False if tile_path is None else self.cache.invalidate(tile_path)

    #: a representative in-coverage tile the health probe uses - any valid tile works.
    HEALTH_PROBE_TILE = "dem_png/14/14552/6451.png"

    #: a real GSI DEM tile this size is comfortably over any real one; still far below MAX_BYTES,
    #: so a misbehaving/wrong origin that ignores Range can't force a large read off this probe.
    HEALTH_PROBE_CAP = 256 * 1024

    def health_check(self) -> tuple[int, dict]:
        """The dedicated upstream health-check endpoint (/__health): TTL 0, always a fresh
        live probe against the real upstream, never reads or writes the tile cache in either
        direction. Reflects upstream reachability only, independent of EarthStatus's separate
        plain-HEAD probe contract and HostBackoff's own Range recovery probe against an
        ordinary tile URI -- both of those exercise the normal cache (TTL -1) on purpose; this
        one deliberately never does. Status is validated before any body is read, and the read
        itself is capped at HEALTH_PROBE_CAP -- tiny relative to the ordinary MAX_BYTES bound --
        so a probe can never be tricked into an unbounded download."""
        with self.stats.lock:
            self.stats.probe_requests += 1
        try:
            outcome, _ = fetch_upstream(self.upstream_base, self.HEALTH_PROBE_TILE, max_response_bytes=self.HEALTH_PROBE_CAP)
            if outcome != "ok":
                raise UpstreamError(f"health probe tile {self.HEALTH_PROBE_TILE} answered negative")
            return 200, {}
        except UpstreamError:
            with self.stats.lock:
                self.stats.probe_failures += 1
            return 502, {}

    def close(self):
        self.cache.close()


def make_handler(proxy: GsiCacheProxy):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet; /__stats covers verification
            pass

        def do_GET(self):
            self._dispatch(write_body=True)

        def do_HEAD(self):
            # No method-based special-casing on the ordinary tile resource: a HEAD gets exactly
            # the headers/status a GET would, through the normal TTL -1 cache, same as GET. The
            # dedicated /__health endpoint below is the TTL-0 live-reachability probe.
            self._dispatch(write_body=False)

        def _dispatch(self, write_body):
            if self.path == "/__stats":
                import json
                payload = json.dumps(proxy.stats.as_dict()).encode()
                self._respond(200, {"Content-Type": "application/json"}, payload, write_body)
                return
            if self.path == "/__health":
                status, headers = proxy.health_check()
                self._respond(status, headers, b"", write_body)
                return
            status, headers, body = proxy.serve_tile(self.path, self.headers.get("If-None-Match"), self.headers.get("Range"))
            self._respond(status, headers, body, write_body)

        def do_DELETE(self):
            # Explicit invalidation: John's TTL -1 decision still requires a
            # way to force a re-fetch after a known upstream data revision.
            removed = proxy.invalidate(self.path)
            self._respond(200 if removed else 404, {}, b"", write_body=True)

        def _respond(self, status, headers, body, write_body=True):
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if write_body and body:
                self.wfile.write(body)

    return Handler


def run_server(db_path: str, port: int, upstream_base: str | None = None, max_bytes: int = 256 * 1024 * 1024):
    proxy = GsiCacheProxy(db_path, upstream_base, max_bytes)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(proxy))
    return server, proxy


if __name__ == "__main__":
    port = int(os.environ.get("GSI_CACHE_PORT", "8074"))
    db = os.environ.get("GSI_CACHE_DB", "gsi-elevation-cache.sqlite3")
    server, proxy = run_server(db, port)
    print(f"gsi elevation cache proxy on http://127.0.0.1:{port} -> upstream {proxy.upstream_base}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        proxy.close()
