#!/usr/bin/env python3
"""Prueba de contrato del sobre común de eventos.

Uso:
    python validar_contratos.py

Qué hace:
    - Por cada archivo en ejemplos/<eventType>.v<version>.example.json,
      busca el esquema schemas/<eventType>.v<version>.schema.json y valida
      el ejemplo contra él.
    - Falla (exit code != 0) si un ejemplo no cumple su esquema, o si un
      ejemplo no tiene esquema (evento no formalizado), o si un esquema no
      tiene al menos un ejemplo (esquema sin caso de prueba).

Cada equipo agrega este script (o su equivalente en su stack) a su propio
pipeline de CI: el productor valida los ejemplos que él mismo genera, y en
sus pruebas de consumidor valida los mensajes reales que llegan a la cola
contra el mismo esquema, en vez de asumir que el payload viene bien formado.
"""
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

BASE = Path(__file__).parent
SCHEMAS_DIR = BASE / "schemas"
EJEMPLOS_DIR = BASE / "ejemplos"


def cargar_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    esquemas = {
        p.name.removesuffix(".schema.json"): p
        for p in SCHEMAS_DIR.glob("*.schema.json")
    }
    esquemas.pop("envelope", None)  # el sobre no se prueba solo; se prueba dentro de cada evento concreto
    ejemplos = {
        p.name.removesuffix(".example.json"): p
        for p in EJEMPLOS_DIR.glob("*.example.json")
    }

    errores = []

    # Todo esquema debe tener al menos un ejemplo (si no, nadie probó que sea usable).
    for clave in esquemas:
        if clave not in ejemplos:
            errores.append(f"[SIN EJEMPLO] {clave}.schema.json no tiene ejemplo en ejemplos/")

    # Todo ejemplo debe validar contra su esquema declarado.
    for clave, ejemplo_path in ejemplos.items():
        schema_path = esquemas.get(clave)
        if schema_path is None:
            errores.append(f"[SIN ESQUEMA] {ejemplo_path.name} no tiene esquema formal en schemas/")
            continue

        schema = cargar_json(schema_path)
        instancia = cargar_json(ejemplo_path)
        validador = Draft202012Validator(schema, format_checker=FormatChecker())

        fallos = sorted(validador.iter_errors(instancia), key=lambda e: e.path)
        if fallos:
            for f in fallos:
                ruta = "/".join(str(p) for p in f.path) or "(raíz)"
                errores.append(f"[INVÁLIDO] {ejemplo_path.name} en '{ruta}': {f.message}")
        else:
            print(f"OK  {clave}")

    if errores:
        print("\nFallos encontrados:")
        for e in errores:
            print(f"  - {e}")
        return 1

    print(f"\n{len(ejemplos)} eventos validados correctamente contra su esquema.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
