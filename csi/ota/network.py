"""The maintenance network: the access point the host provides, named and keyed from the kit key.
"""
from __future__ import annotations

import hashlib
import hmac

from ..round_auth import kit_key_check

NETWORK_LABEL = b"shadowcast maintenance network"
SSID_PREFIX = "shadowcast-"


def maintenance_network(kit_key: bytes) -> tuple[str, str]:
    """(SSID, WPA2 passphrase) of the access point the host provides for maintenance mode. Both follow from the
    kit key (the node derives the same), so no node needs a second secret."""
    passphrase = hmac.new(kit_key, NETWORK_LABEL, hashlib.sha256).digest()[:12].hex()
    return SSID_PREFIX + kit_key_check(kit_key), passphrase
