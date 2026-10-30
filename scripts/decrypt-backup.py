#!/usr/bin/env python3
"""Decrypt a MOKS backup without MOKS itself, for use when everything else is gone.

    pip install cryptography
    MOKS_BACKUP_ENCRYPTION_KEY=... python3 decrypt-backup.py in.dump.enc out.dump
    pg_restore --list out.dump

Keep this file with the encryption key. The format is described in backend/moks/backup.py.
"""
import base64
import os
import struct
import sys

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"MOKSBAK1"


def main(source: str, target: str) -> None:
    key = base64.b64decode(os.environ["MOKS_BACKUP_ENCRYPTION_KEY"], validate=True)
    with open(source, "rb") as src, open(target, "wb") as dst:
        if src.read(len(MAGIC)) != MAGIC:
            sys.exit("not a MOKS backup")
        aes = AESGCM(HKDF(algorithm=hashes.SHA256(), length=32, salt=src.read(16), info=b"moks-backup-v1").derive(key))
        counter, head = 0, src.read(4)
        while head:
            sealed = src.read(struct.unpack(">I", head)[0])
            following = src.read(4)
            dst.write(aes.decrypt(struct.pack(">QI", counter, 0 if following else 1), sealed, MAGIC))
            counter, head = counter + 1, following


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
