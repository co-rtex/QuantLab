"""TLS settings in QUANTLAB_DATABASE_URL reach libpq unchanged (sslmode=verify-full, #19)."""

from __future__ import annotations

from sqlalchemy import create_engine

BUNDLE = "/etc/ssl/certs/rds-global-bundle.pem"  # added by services/api/Dockerfile
URL = (
    "postgresql+psycopg://app:p%40ss%2Fword@db.example.internal:5432/quantlab"
    f"?sslmode=verify-full&sslrootcert={BUNDLE}"
)


def test_sslmode_and_root_certificate_pass_through_to_the_driver():
    # The application and alembic both hand the URL to create_engine as is (db.py, env.py).
    engine = create_engine(URL)  # no connection is made
    _, kwargs = engine.dialect.create_connect_args(engine.url)
    assert kwargs["sslmode"] == "verify-full"
    assert kwargs["sslrootcert"] == BUNDLE
    assert kwargs["host"] == "db.example.internal" and kwargs["dbname"] == "quantlab"
    assert kwargs["password"] == "p@ss/word"  # URL-encoded characters are decoded
    engine.dispose()


def test_the_app_engine_keeps_the_tls_parameters(monkeypatch):
    from quantlab_api import db

    engine = db._engine(URL)
    _, kwargs = engine.dialect.create_connect_args(engine.url)
    assert (kwargs["sslmode"], kwargs["sslrootcert"]) == ("verify-full", BUNDLE)
