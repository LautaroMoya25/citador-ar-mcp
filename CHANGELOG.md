# Changelog

Formato de [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).

## [No publicado]

Primera implementación completa. Todavía sin publicar en PyPI ni en el MCP
Registry, y sin release del `.db`.

### Agregado

- Cinco tools (`citador_lookup_ruling`, `citador_check_status`,
  `citador_citing_rulings`, `citador_cited_rulings`, `citador_trace_doctrine`),
  tres prompts y el recurso `citador://corpus`.
- Cliente de la Secretaría de Jurisprudencia de la CSJN, mapeado a mano: no hay
  especificación publicada.
- Recuperación por OCR de los fallos cuyo PDF trae una capa de texto con fuente
  sin mapa Unicode. No son escaneos, y el problema es por documento, no por tomo.
- Clasificación de tratamiento en dos etapas: reglas sobre fórmulas del
  castellano jurídico, y una etapa opcional con Claude para lo que las reglas no
  resuelven. 4 de 5 contra la cadena dorada, con cero rojos falsos.
- Segmentación por voto, incluido el dictamen del Procurador: sólo la mayoría
  mueve la señal.
- Fixture dorado Colavini → Bazterrica → Montalvo → Arriola, con las cinco
  aristas reconstruidas de texto real.

### Seguridad

- Tope de salida en `citador_trace_doctrine`. Sin él, un fallo muy citado
  producía 2.020 eslabones y unos 445.000 tokens en una sola llamada.
- Los pasajes se renderizan defangueados y enmarcados como material de origen,
  no como instrucciones: el `.db` se distribuye como archivo y su contenido
  llega al contexto de un modelo.
- Conexión al grafo en modo sólo lectura, con la ruta percent-encodeada antes de
  volverse URI.
- Límites de entrada en las tools y tope de tamaño en la descarga de PDFs.

### Notas

- El diseño inicial usaba `Fallos 331:2691` como identificador de Arriola. Es
  `Fallos: 332:1963`, verificado contra la fuente. Corregido.
