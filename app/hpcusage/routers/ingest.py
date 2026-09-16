"""Collector upload endpoint. Authenticated with a per-cluster bearer token, not CAS."""

import hmac
import logging
import zlib

import orjson
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..db import get_db
from ..ingest.schemas import Envelope
from ..ingest.service import ingest_envelope
from ..settings import Settings, get_settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/ingest", tags=["ingest"])


def cluster_for_token(request: Request, settings: Settings = Depends(get_settings)) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    presented = auth[7:].strip()
    for cluster, token in settings.collector_token_map.items():
        if token and hmac.compare_digest(presented, token):
            return cluster
    raise HTTPException(403, "invalid token")


async def read_envelope(request: Request, settings: Settings) -> Envelope:
    raw = await request.body()
    if request.headers.get("content-encoding", "").lower() == "gzip":
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = d.decompress(raw, settings.max_ingest_bytes + 1)
        if d.unconsumed_tail:
            raise HTTPException(413, f"payload exceeds {settings.max_ingest_bytes} bytes")
    elif len(raw) > settings.max_ingest_bytes:
        raise HTTPException(413, "payload too large")
    try:
        data = orjson.loads(raw)
    except orjson.JSONDecodeError as e:
        raise HTTPException(400, f"invalid JSON: {e}") from e
    try:
        return Envelope.model_validate(data)
    except ValidationError as e:
        raise HTTPException(422, f"invalid envelope: {e.errors()[:3]}") from e


@router.post("/{kind}")
async def ingest(kind: str, request: Request, token_cluster: str = Depends(cluster_for_token),
                 db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    env = await read_envelope(request, settings)
    if env.kind != kind:
        raise HTTPException(400, f"envelope kind {env.kind!r} does not match URL {kind!r}")
    if env.cluster != token_cluster:
        raise HTTPException(403, f"token is for cluster {token_cluster!r}, envelope says {env.cluster!r}")
    try:
        result = ingest_envelope(db, env, settings)
    except (ValidationError, ValueError) as e:
        raise HTTPException(422, str(e)[:1000]) from e
    log.info("ingest %s/%s: %s", env.cluster, env.kind, {k: v for k, v in result.items() if k != "rollup_days"})
    return result

