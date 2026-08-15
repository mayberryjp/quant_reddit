"""Bottle application entry point for the quant_reddit API."""

from __future__ import annotations

import json
import logging
import os
import sys

from bottle import Bottle, abort, request, response

from app.routes import health, reddit

SERVICE_NAME = "quant-reddit-api"
log = logging.getLogger(SERVICE_NAME)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
    force=True,
)

_DEFAULT_ERRORS = {
    404: "not found",
    405: "method not allowed",
    500: "internal server error",
}


def create_app() -> Bottle:
    """Assemble the Bottle application from its route modules."""
    app = Bottle()

    @app.hook("after_request")
    def _allow_cross_origin_requests():
        response.set_header("Access-Control-Allow-Origin", "*")
        response.set_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        response.set_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    @app.route(
        "/<:re:.*>",
        method=["OPTIONS", "GET", "HEAD"],
    )
    def _cors_preflight():
        if request.method != "OPTIONS":
            abort(404)
        response.status = 204
        return ""

    app.merge(health.sub)
    app.merge(reddit.sub)

    def _json_error(error):
        response.content_type = "application/json"
        return json.dumps({"detail": _DEFAULT_ERRORS.get(error.status_code, "error")})

    for _code in _DEFAULT_ERRORS:
        app.error(_code)(_json_error)

    return app


app = create_app()


if __name__ == "__main__":
    from waitress import serve

    host = os.environ.get("API_LISTEN_ADDRESS", "0.0.0.0")
    port = int(os.environ.get("API_PORT", "8018"))
    log.info("Starting quant_reddit API on %s:%d ...", host, port)
    serve(app, host=host, port=port, threads=20)
