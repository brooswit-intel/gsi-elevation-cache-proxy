import http.client
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import proxy as gsi

MINIMAL_PNG = (
    b"\x89PNG\r\n\x1a\n"
    + b"\x00" * 8
    + b"IHDR" + b"\x00" * 13
    + b"IEND\xaeB`\x82"
)


class FakeUpstream(BaseHTTPRequestHandler):
    """Stands in for cyberjapandata.gsi.go.jp: serves one real tile, one
    malformed-but-200 tile, one that drops the connection, one that ignores
    Range and sends far more than expected, and 404s the rest."""

    hits = []

    def do_GET(self):
        FakeUpstream.hits.append(self.path)
        if self.path == "/dem_png/14/100/100.png":
            self._png(MINIMAL_PNG)
        elif self.path == "/dem_png/14/200/200.png":
            body = b"<html>not a tile</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/dem_png/14/300/300.png":
            self.close_connection = True
            return  # dropped connection -> client-side OSError
        elif self.path == "/dem_png/14/400/400.png":
            # Ignores Range entirely and answers with more than the Range window allows.
            body = MINIMAL_PNG + b"\x00" * (gsi.MAX_BYTES + 4096)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/dem_png/14/500/500.png":
            # 206 with the right length but a Content-Range offset that isn't 0 --
            # the exact equal-length wrong-offset case from MINECRAFT-571 55866.
            self.send_response(206)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(MINIMAL_PNG)))
            self.send_header("Content-Range", f"bytes 20-{20 + len(MINIMAL_PNG) - 1}/10000")
            self.end_headers()
            self.wfile.write(MINIMAL_PNG)
        elif self.path == "/dem_png/14/600/600.png":
            # 206 claiming a bigger interval (bytes 0-9999) than the body it actually
            # sends (truncated to len(MINIMAL_PNG)), with Content-Length matching the
            # truncated body -- the case from MINECRAFT-571 55874.
            self.send_response(206)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(MINIMAL_PNG)))
            self.send_header("Content-Range", "bytes 0-9999/10000")
            self.end_headers()
            self.wfile.write(MINIMAL_PNG)
        else:
            body = b"<Error><Code>NoSuchKey</Code></Error>"
            self.send_response(404)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _png(self, body: bytes):
        self.send_response(206)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Range", f"bytes 0-{len(body)-1}/{len(body)}")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


class ProxyTest(unittest.TestCase):
    def setUp(self):
        FakeUpstream.hits = []
        self.upstream_server = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        self.upstream_port = self.upstream_server.server_address[1]
        threading.Thread(target=self.upstream_server.serve_forever, daemon=True).start()

        self.db_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.db_dir, "gsi.sqlite3")
        self.server, self.proxy = gsi.run_server(
            self.db_path, 0, upstream_base=f"http://127.0.0.1:{self.upstream_port}/"
        )
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.proxy.close()
        self.upstream_server.shutdown()
        self.upstream_server.server_close()

    def _get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body

    def test_path_validation_rejects_unknown_product(self):
        self.assertIsNone(gsi.parse_tile_path("other_png/14/1/1.png"))
        self.assertIsNone(gsi.parse_tile_path("dem_png/15/1/1.png"))  # dem_png caps at 14
        self.assertIsNone(gsi.parse_tile_path("dem5a_png/14/1/1.png"))  # dem5a starts at 15
        self.assertIsNone(gsi.parse_tile_path("../../etc/passwd"))
        self.assertEqual(gsi.parse_tile_path("dem_png/14/1/1.png"), "dem_png/14/1/1.png")

    def test_real_tile_is_cached_and_served(self):
        resp, body = self._get("/dem_png/14/100/100.png")
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, MINIMAL_PNG)
        self.assertEqual(len(FakeUpstream.hits), 1)

        resp2, body2 = self._get("/dem_png/14/100/100.png")
        self.assertEqual(resp2.status, 200)
        self.assertEqual(body2, MINIMAL_PNG)
        self.assertEqual(len(FakeUpstream.hits), 1, "cache hit must not re-fetch upstream")
        self.assertEqual(self.proxy.stats.as_dict()["cache_hits"], 1)

    def test_genuine_404_is_cached_as_negative_not_success(self):
        resp, body = self._get("/dem_png/14/999/999.png")
        self.assertEqual(resp.status, 404)
        self.assertEqual(body, b"")

        resp2, _ = self._get("/dem_png/14/999/999.png")
        self.assertEqual(resp2.status, 404)
        self.assertEqual(len(FakeUpstream.hits), 1, "negative result must be cached too, not re-fetched")
        self.assertEqual(self.proxy.stats.as_dict()["negative_hits"], 1)

    def test_malformed_200_is_never_cached(self):
        resp, _ = self._get("/dem_png/14/200/200.png")
        self.assertEqual(resp.status, 502)
        resp2, _ = self._get("/dem_png/14/200/200.png")
        self.assertEqual(resp2.status, 502)
        self.assertGreaterEqual(len(FakeUpstream.hits), 2, "a malformed success must be retried, never cached")

    def test_dropped_connection_is_never_cached_as_success(self):
        resp, _ = self._get("/dem_png/14/300/300.png")
        self.assertEqual(resp.status, 502)
        self.assertIsNone(self.proxy.cache.get("dem_png/14/300/300.png"))

    def test_origin_ignoring_range_is_rejected_not_downloaded_unbounded(self):
        resp, _ = self._get("/dem_png/14/400/400.png")
        self.assertEqual(resp.status, 502)
        self.assertIsNone(self.proxy.cache.get("dem_png/14/400/400.png"))

    def test_206_with_wrong_offset_content_range_is_rejected(self):
        resp, _ = self._get("/dem_png/14/500/500.png")
        self.assertEqual(resp.status, 502, "a 206 whose Content-Range doesn't start at 0 must never be trusted as this tile's own bytes")
        self.assertIsNone(self.proxy.cache.get("dem_png/14/500/500.png"))

    def test_206_truncated_body_with_matching_content_length_is_rejected(self):
        # Content-Length matches the (already-truncated) body, but Content-Range's
        # own interval claims a much larger span -- a short body that lies about
        # being the whole thing it claims to be must never be cached as success.
        resp, _ = self._get("/dem_png/14/600/600.png")
        self.assertEqual(resp.status, 502)
        self.assertIsNone(self.proxy.cache.get("dem_png/14/600/600.png"))

    def test_range_request_served_as_206_slice(self):
        self._get("/dem_png/14/100/100.png")
        resp, body = self._get("/dem_png/14/100/100.png", {"Range": "bytes=0-7"})
        self.assertEqual(resp.status, 206)
        self.assertEqual(body, MINIMAL_PNG[:8])
        self.assertEqual(resp.getheader("Content-Range"), f"bytes 0-7/{len(MINIMAL_PNG)}")

    def test_conditional_request_answers_304(self):
        resp, body = self._get("/dem_png/14/100/100.png")
        etag = resp.getheader("ETag")
        resp2, body2 = self._get("/dem_png/14/100/100.png", {"If-None-Match": etag})
        self.assertEqual(resp2.status, 304)
        self.assertEqual(body2, b"")

    def test_concurrent_miss_deduplication_single_flight(self):
        barrier = threading.Barrier(5)
        results = []

        def worker():
            barrier.wait()
            resp, _ = self._get("/dem_png/14/100/100.png")
            results.append(resp.status)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, [200] * 5)
        self.assertEqual(len(FakeUpstream.hits), 1, "5 concurrent misses for the same tile must cause exactly 1 upstream fetch")

    def test_restart_persists_cache_with_no_time_expiry(self):
        self._get("/dem_png/14/100/100.png")
        self.proxy.close()
        self.server.shutdown()

        # Simulate a large clock advance: John's decision is TTL -1 (no
        # time-based expiration) for validated data, not "expires eventually".
        with mock.patch("time.time", return_value=time.time() + 10 * 365 * 24 * 3600):
            server2, proxy2 = gsi.run_server(self.db_path, 0, upstream_base=f"http://127.0.0.1:{self.upstream_port}/")
            try:
                cached = proxy2.cache.get("dem_png/14/100/100.png")
                self.assertIsNotNone(cached)
                self.assertEqual(cached[0], "hit")
            finally:
                proxy2.close()
                server2.server_close()  # never served; shutdown() would hang (see test_bytes_cap_evicts_lru)
        # No new upstream hit from the restart/clock-advance check itself.
        self.assertEqual(len(FakeUpstream.hits), 1)

    def test_explicit_invalidation_forces_refetch(self):
        self._get("/dem_png/14/100/100.png")
        self.assertEqual(len(FakeUpstream.hits), 1)

        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("DELETE", "/dem_png/14/100/100.png")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)

        self._get("/dem_png/14/100/100.png")
        self.assertEqual(len(FakeUpstream.hits), 2, "invalidated entry must be re-fetched on next request")

    def test_bytes_cap_evicts_lru(self):
        small_db = os.path.join(self.db_dir, "small.sqlite3")
        # 1.5x one tile's size: adding a 2nd tile forces eviction, but the cap
        # leaves room for exactly one tile after trimming to 90% of the cap.
        max_bytes = int(len(MINIMAL_PNG) * 1.5)
        # server.shutdown() would hang forever here: it waits on an event that
        # only serve_forever()'s loop sets, and this server's loop is never
        # started -- cache operations below go straight through proxy.cache.
        server, proxy = gsi.run_server(
            small_db, 0, upstream_base=f"http://127.0.0.1:{self.upstream_port}/", max_bytes=max_bytes
        )
        try:
            proxy.cache.put("dem_png/14/1/1.png", MINIMAL_PNG)
            proxy.cache.evict_to_fit(proxy.stats)
            proxy.cache.put("dem_png/14/2/2.png", MINIMAL_PNG)
            proxy.cache.evict_to_fit(proxy.stats)
            self.assertIsNone(proxy.cache.get("dem_png/14/1/1.png"), "oldest entry must be evicted once over the byte cap")
            self.assertIsNotNone(proxy.cache.get("dem_png/14/2/2.png"))
        finally:
            proxy.close()
            server.server_close()

    def test_default_upstream_unchanged_when_unset(self):
        proxy = gsi.GsiCacheProxy(os.path.join(self.db_dir, "defaults.sqlite3"))
        try:
            self.assertEqual(proxy.upstream_base, gsi.DEFAULT_UPSTREAM)
        finally:
            proxy.close()

    def test_env_override_changes_upstream(self):
        with mock.patch.dict(os.environ, {"GSI_CACHE_UPSTREAM": "http://example.invalid/"}):
            proxy = gsi.GsiCacheProxy(os.path.join(self.db_dir, "override.sqlite3"))
            try:
                self.assertEqual(proxy.upstream_base, "http://example.invalid/")
            finally:
                proxy.close()

    def test_health_endpoint_is_fresh_and_bypasses_the_tile_cache(self):
        with mock.patch.object(gsi.GsiCacheProxy, "HEALTH_PROBE_TILE", "dem_png/14/100/100.png"):
            # Prime the ordinary cache so a stale hit there can't masquerade as a live probe.
            self._get("/dem_png/14/100/100.png")
            hits_before = len(FakeUpstream.hits)

            resp, body = self._get("/__health")
            self.assertEqual(resp.status, 200)
            self.assertEqual(body, b"")
            self.assertEqual(len(FakeUpstream.hits), hits_before + 1, "/__health must always hit upstream fresh, never the tile cache")

            resp2, _ = self._get("/__health")
            self.assertEqual(resp2.status, 200)
            self.assertEqual(len(FakeUpstream.hits), hits_before + 2, "a second /__health call must probe again, not reuse the first result")
            self.assertEqual(self.proxy.stats.as_dict()["probe_requests"], 2)

    def test_health_endpoint_reports_failure_without_poisoning_tile_cache(self):
        with mock.patch.object(gsi.GsiCacheProxy, "HEALTH_PROBE_TILE", "dem_png/14/999/999.png"):
            resp, _ = self._get("/__health")
            self.assertEqual(resp.status, 502)
            self.assertEqual(self.proxy.stats.as_dict()["probe_failures"], 1)
        # The failed probe must not have written a negative result into the ordinary tile cache.
        self.assertIsNone(self.proxy.cache.get("dem_png/14/999/999.png"))

    def test_head_on_ordinary_tile_goes_through_the_normal_cache(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.request("HEAD", "/dem_png/14/100/100.png")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, b"")  # HEAD never carries a body
        self.assertEqual(resp.getheader("Content-Length"), str(len(MINIMAL_PNG)))
        self.assertEqual(len(FakeUpstream.hits), 1, "a HEAD on a tile must still go through (and populate) the ordinary cache")


if __name__ == "__main__":
    unittest.main()
