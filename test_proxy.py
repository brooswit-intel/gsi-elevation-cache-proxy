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
    malformed-but-200 tile, one that drops the connection, and 404s the rest."""

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


if __name__ == "__main__":
    unittest.main()
