import unittest

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.dependencies import _build_store
from app.config.settings import AppSettings
from app.middleware.request_size import RequestSizeLimitMiddleware
from app.persistence.database import PostgresControlStore


class HardeningTests(unittest.TestCase):
    def test_production_rejects_sqlite_control_plane(self):
        settings = AppSettings(
            environment="production",
            dev_bearer_token="not-the-default-token",
            allowed_origins=("https://guru.example.test",),
            control_database_url="sqlite:///./data/guru_ji.db",
        )
        with self.assertRaisesRegex(ValueError, "production requires CONTROL_DATABASE_URL"):
            settings.ensure_safe_for_production()

    def test_production_rejects_missing_origins(self):
        settings = AppSettings(
            environment="production",
            dev_bearer_token="not-the-default-token",
            allowed_origins=(),
            control_database_url="postgresql://user:pass@localhost/guru",
        )
        with self.assertRaisesRegex(ValueError, "GURU_ALLOWED_ORIGINS"):
            settings.ensure_safe_for_production()

    def test_backend_selection_keeps_memory_and_sqlite_explicit(self):
        self.assertEqual(_build_store(AppSettings(control_database_url=None)).backend_name, "memory")
        sqlite = _build_store(AppSettings(control_database_url=":memory:"))
        try:
            self.assertEqual(sqlite.backend_name, "sqlite")
        finally:
            sqlite.close()
        with self.assertRaisesRegex(ValueError, "CONTROL_DATABASE_URL"):
            _build_store(AppSettings(control_database_url="mysql://localhost/guru"))

    def test_postgres_store_rejects_non_postgres_url_before_connecting(self):
        with self.assertRaisesRegex(ValueError, "requires a postgresql"):
            PostgresControlStore("sqlite:///tmp/not-postgres.db")

    def test_request_size_middleware_rejects_oversized_payloads(self):
        app = FastAPI()
        app.add_middleware(RequestSizeLimitMiddleware, max_bytes=10)

        @app.post("/echo")
        async def echo(request: Request):
            return {"size": len(await request.body())}

        client = TestClient(app)
        response = client.post("/echo", content=b"01234567890", headers={"x-request-id": "req-test"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.headers["x-request-id"], "req-test")
        self.assertEqual(response.json()["error"]["code"], "payload_too_large")

    def test_request_size_middleware_allows_bounded_payloads(self):
        app = FastAPI()
        app.add_middleware(RequestSizeLimitMiddleware, max_bytes=10)

        @app.post("/echo")
        async def echo(request: Request):
            return {"size": len(await request.body())}

        response = TestClient(app).post("/echo", content=b"0123456789")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"size": 10})


if __name__ == "__main__":
    unittest.main()
