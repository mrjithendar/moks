"""Sealing a revealed secret to one browser (docs/SECRETS_SYNC.md, "Viewing values").

The browser makes an ECDH P-256 key pair whose private half never leaves it (WebCrypto, not extractable) and sends
the public half with the request. The executor reads the values, makes its own one-time key pair, and encrypts
with AES-256-GCM under HKDF-SHA256(ECDH shared secret, salt = the job id, info = INFO), with the job id as
associated data. Postgres and the API only ever hold the ciphertext and the executor's public key, so neither can
read the values, and the result opens only in the tab that asked, only for this job.
"""
from __future__ import annotations

import base64
import os
import uuid
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ALG = "ECDH-ES+HKDF-SHA256+A256GCM"
INFO = b"moks-secret-reveal-v1"


class SealError(ValueError):
    pass


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def public_key(jwk: Any) -> ec.EllipticCurvePublicKey:
    """The browser's key, checked: an EC P-256 public JWK and nothing else (no private part)."""
    if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256" or "d" in jwk:
        raise SealError("public_key must be an EC P-256 public JWK")
    try:
        x, y = _unb64(str(jwk["x"])), _unb64(str(jwk["y"]))
        if len(x) != 32 or len(y) != 32:
            raise SealError("public_key coordinates must be 32 bytes")
        return ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()).public_key()
    except SealError:
        raise
    except Exception:
        raise SealError("public_key is not a point on P-256") from None


def _key(shared: bytes, job_id: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=uuid.UUID(job_id).bytes, info=INFO).derive(shared)


def seal(jwk: dict[str, Any], plaintext: bytes, job_id: str) -> dict[str, str | dict[str, str]]:
    peer = public_key(jwk)
    mine = ec.generate_private_key(ec.SECP256R1())
    key = _key(mine.exchange(ec.ECDH(), peer), job_id)
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, plaintext, job_id.encode())
    n = mine.public_key().public_numbers()
    return {"alg": ALG, "epk": {"kty": "EC", "crv": "P-256", "x": _b64(n.x.to_bytes(32, "big")), "y": _b64(n.y.to_bytes(32, "big"))}, "iv": _b64(iv), "ct": _b64(ct)}


def open_sealed(private: ec.EllipticCurvePrivateKey, sealed: dict[str, Any], job_id: str) -> bytes:
    """What the browser does, for tests."""
    key = _key(private.exchange(ec.ECDH(), public_key(sealed["epk"])), job_id)
    return AESGCM(key).decrypt(_unb64(sealed["iv"]), _unb64(sealed["ct"]), job_id.encode())
