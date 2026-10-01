# Contrato común de eventos — cómo se sostiene con el equipo

Este documento acompaña a `schemas/` y `ejemplos/` (el contrato en sí) con el **proceso** para que las 5 áreas de servicio (Usuarios, Reportes, Geoespacial, Multimedia, Obras) lo adopten y lo mantengan sin que cada una reinterprete el sobre a su manera. Sin un proceso, el JSON Schema es solo un archivo que nadie vuelve a mirar después de la primera semana.

## 1. Dónde vive el contrato

Una sola carpeta compartida, no una copia por servicio:

```
validar/
├── CONTRATO_EVENTOS.md      (este documento)
├── servicio_reportes_diseno.md
├── schemas/                 (la fuente de verdad — JSON Schema por eventType+version)
├── ejemplos/                (un ejemplo real por eventType+version)
└── validar_contratos.py     (la prueba de contrato)
```

Esta carpeta vive dentro del repo monolítico actual; si en el futuro cada servicio termina en su propio repo, puede extraerse como paquete instalable (`pip`/`npm`) versionado. Lo que no debe pasar es que `ServicioReportes` tenga su copia del esquema de `reporte.creado` y `ServicioAuditoria` tenga otra: **un esquema, una ubicación, todos lo referencian.**

## 2. Cómo se agrega o cambia un evento (mini-RFC)

Cualquier `eventType` nuevo, o un cambio de `version` sobre uno existente, se propone con esta plantilla — en un PR o un documento corto, no hace falta una reunión larga:

```
### RFC: <eventType> v<version>

- Propone: <nombre>, equipo de <ServicioX>
- Motivación: <qué hecho de negocio dispara este evento>
- ¿Aditivo o rompe consumidores existentes?: <...>
- Esquema propuesto: <link a schemas/<eventType>.v<version>.schema.json>
- Ejemplo: <link a ejemplos/<eventType>.v<version>.example.json>
- Consumidores conocidos que deben revisar: <lista de equipos/servicios>
- Fecha límite para comentarios: <fecha>
```

Regla de aprobación: **al menos un representante de cada equipo consumidor conocido** confirma el RFC antes de mergear el esquema a `validar/schemas/`. Los consumidores conocidos son:

| Evento | Productor | Consumidores |
|---|---|---|
| `reporte.creado` v1 | Reportes | Geoespacial (projector), Auditoría, Notificaciones |
| `reporte.validado` v1 | Reportes | Obras, Auditoría |
| `reporte.rechazado` v1 | Reportes | Auditoría, Notificaciones |
| `reporte.resuelto` v1 | Reportes | Auditoría, Notificaciones |
| `obra.asignada` v1 | Obras | Notificaciones, Auditoría |
| `multimedia.upload` v1 | Multimedia | `fn_multimedia` |
| `usuario.rol_cambiado` v1 | Usuarios | Auditoría, (opcional) Obras |

Esta tabla es el catálogo actual de eventos y se actualiza en el mismo PR que agrega o cambia un evento, así nadie tiene que hacer *grep* en 5 repos para saber qué eventos existen.

## 3. Regla de versionado (recordatorio operativo)

- Agregar un campo opcional a `data` → **no** sube `version`, es compatible hacia atrás.
- Quitar, renombrar, o cambiar el tipo de un campo existente → **sí** sube `version`; el evento viejo sigue publicándose (o se sostiene por un período de transición acordado en el RFC) mientras los consumidores migran.
- Un consumidor que reciba un `eventType`/`version` que no reconoce **lo ignora con un log de advertencia**, nunca lo rechaza con error — así un productor puede publicar una versión nueva antes de que todos los consumidores la entiendan.

## 4. Cómo se hace cumplir (no solo se documenta)

Documentar la regla no basta; lo que la sostiene es que romperla falle una prueba automatizada:

1. **El productor** agrega, en su propio pipeline de CI, un paso que corre `validar_contratos.py` (o el equivalente en su stack) contra sus propios ejemplos antes de cada release. Si el productor cambia el payload real sin actualizar `schemas/`, esta prueba lo detecta porque el ejemplo deja de coincidir con lo que el esquema dice que debería ser — o, mejor, el productor genera el ejemplo directamente desde una corrida real de su código (no lo escribe a mano), así el archivo de ejemplo es HONESTO sobre lo que el servicio realmente publica.
2. **Cada consumidor**, en sus pruebas de integración, valida los mensajes que efectivamente recibe de la cola contra el mismo esquema, antes de procesarlos — un mensaje que no valida se trata como error de contrato (va a la DLQ con un motivo explícito), no se intenta parsear "a lo mejor funciona".
3. **CI del repo compartido** (`validar/`) corre `python validar/validar_contratos.py` en cada PR: falla si algún esquema no tiene ejemplo, o si algún ejemplo no valida contra su esquema — incluye chequeo de formato UUID/fecha-hora con `jsonschema[format]`.

## 5. Ceremonia mínima con el equipo

No hace falta un comité permanente. Alcanza con:

- **Revisión async por PR**: el RFC se comenta en el PR; se mergea cuando los consumidores listados aprobaron (aunque sea con un 👍) o pasó la fecha límite sin objeción.
- **Una reunión corta solo si hay desacuerdo real** sobre compatibilidad (p. ej. alguien necesita romper un campo que otro equipo ya consume en producción) — no una reunión recurrente por defecto.
- Dueño de cada evento = el equipo que lo produce; cualquier otro equipo puede proponer un cambio, pero el dueño decide y firma el PR.

## 6. Qué hacer primero con esto

1. Mantener los esquemas en `validar/schemas/`, los ejemplos en `validar/ejemplos/` y ejecutar `python validar/validar_contratos.py` desde la raíz del repo.
2. Cada equipo (Usuarios, Reportes, Geoespacial, Multimedia, Obras) revisa el esquema del evento que a él le toca producir y lo confirma o pide cambios via RFC — no se asume que lo que hay aquí es definitivo, es el punto de partida.
3. Se agrega `validar_contratos.py` (o su puerto al lenguaje de cada servicio) al pipeline de CI de cada uno.
