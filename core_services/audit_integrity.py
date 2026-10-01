import hashlib
import json

GENESIS_HASH = "0" * 64


def calcular_hash_evento(evento, hash_previo):
    contenido = json.dumps(
        evento,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(f"{hash_previo}:{contenido}".encode("utf-8")).hexdigest()
