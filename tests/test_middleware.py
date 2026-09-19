import asyncio
import unittest

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.api.error_handlers import http_exception_handler, validation_exception_handler
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_id import RequestIdMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.middleware.timeout import RequestTimeoutMiddleware


class _Payload(BaseModel):
    value: int


class MiddlewareTests(unittest.TestCase):
    def test_security_headers_are_present(self):
        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware, production=True)

        @app.get("/ping")
        async def ping():
            return {"ok": True}

        response = TestClient(app).get("/ping")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertIn("max-age=31536000", response.headers["strict-transport-security"])

    def test_rate_limit_returns_retryable_error(self):
        app = FastAPI()
        app.add_middleware(RequestIdMiddleware)
        app.add_middleware(RateLimitMiddleware, max_requests=1, window_seconds=60)

        @app.get("/limited")
        async def limited():
            return {"ok": True}

        client = TestClient(app)
        self.assertEqual(client.get("/limited").status_code, 200)
        response = client.get("/limited", headers={"x-request-id": "req-rate"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "rate_limit_exceeded")
        self.assertEqual(response.headers["retry-after"], "60")
        self.assertEqual(response.headers["x-request-id"], "req-rate")

    def test_timeout_returns_bounded_error(self):
        app = FastAPI()
        app.add_middleware(RequestIdMiddleware)
        app.add_middleware(RequestTimeoutMiddleware, timeout_seconds=0.01)

        @app.get("/slow")
        async def slow():
            await asyncio.sleep(0.05)
            return {"ok": True}

        response = TestClient(app).get("/slow", headers={"x-request-id": "req-timeout"})
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["error"]["code"], "request_timeout")
        self.assertEqual(response.headers["x-request-id"], "req-timeout")

    def test_http_and_validation_errors_share_safe_envelope(self):
        app = FastAPI()
        app.add_exception_handler(HTTPException, http_exception_handler)
        app.add_exception_handler(RequestValidationError, validation_exception_handler)

        @app.get("/denied")
        async def denied():
            raise HTTPException(status_code=403, detail="not allowed")

        @app.post("/payload")
        async def payload(body: _Payload):
            return body

        client = TestClient(app)
        denied_response = client.get("/denied")
        self.assertEqual(denied_response.status_code, 403)
        self.assertEqual(denied_response.json()["error"]["code"], "forbidden")
        invalid = client.post("/payload", json={"value": "not-an-int"})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["error"]["code"], "request_validation_failed")
        self.assertNotIn("input", str(invalid.json()))


if __name__ == "__main__":
    unittest.main()
