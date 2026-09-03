# Changelog

Formato de [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).

## [0.1.2] - 2026-09-03

### Corregido

- **Los siete tratamientos `criticized` del corpus eran falsos, sin excepción.**
  La regla enganchaba "es objetable" en la cláusula y le atribuía la crítica al
  precedente citado, cuando lo objetado era la conclusión del tribunal *a quo*,
  una ley, o la aplicación ilegal de una norma válida — y el precedente estaba
  invocado **a favor**, a veces con un "conf. doctrina de" delante. Producían
  seis señales amarillas falsas, cada una diciéndole a un abogado que
  desconfiara de autoridad que la Corte estaba aplicando. Dos de esas aristas se
  reclasificaron como `applied`, que es lo que el pasaje sostiene.
- Nueva guarda de **cita de apoyo**: una cita introducida por `conf.`, `cfr.`,
  `arg.` o `(doctrina de` está invocada en respaldo, así que ninguna etiqueta
  negativa o cautelar se le adhiere. Se limita a la cláusula de la cita: un
  fallo puede citar un precedente a favor y distinguirlo en el considerando
  siguiente, y eso sigue registrándose.

### Agregado

- `reclassify_stored(include_classified=True)` permite **retirar** un veredicto
  cuando la regla que lo produjo resultó equivocada. Por defecto sigue sin pisar
  clasificaciones existentes, y nunca toca las anotadas a mano.

## [0.1.1] - 2026-09-03

### Cambiado

- Se quitaron del repositorio los dos documentos de trabajo internos, junto con
  las veinticinco referencias que el código, el esquema y los tests hacían a
  ellos. Donde la referencia era sólo un puntero, se fue el puntero; donde
  cargaba contenido, el contenido quedó escrito en su lugar.

### Corregido

- Los errores accionables no llegaban al cliente. El SDK conserva el texto de un
  `ToolError` y reemplaza cualquier otra excepción por `Error executing tool`,
  así que toda la mensajería útil —incluida la que explica cómo generar el
  grafo, que es lo primero que ve quien instala— se perdía antes de llegar al
  modelo. Alcanzaba la clase base equivocada para provocarlo.
- Las URLs del paquete apuntaban a un usuario de GitHub inexistente.
- El CI disparaba en `main`, rama que no existe en este repositorio, y por eso
  nunca se había ejecutado.

## [0.1.0] - 2026-09-03

Primera implementación completa. Retirada de PyPI y reemplazada por 0.1.1.

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
