#!/usr/bin/env python3
"""Print a random collector bearer token (URL-safe, 43 chars ≈ 256 bits)."""
import secrets
import sys

n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
for _ in range(n):
    print(secrets.token_urlsafe(32))
