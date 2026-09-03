# citador_ar_mcp

[![PyPI](https://img.shields.io/pypi/v/citador-ar-mcp)](https://pypi.org/project/citador-ar-mcp/)
[![CI](https://github.com/LautaroMoya25/citador-ar-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/LautaroMoya25/citador-ar-mcp/actions/workflows/ci.yml)

Un servidor MCP que responde la pregunta que ningún buscador jurídico argentino
abierto responde: **¿este fallo de la Corte sigue siendo buen derecho?**

No es un buscador de jurisprudencia. Ya hay once. Este construye el **grafo de
citas** entre fallos de la CSJN y clasifica *cómo* cada fallo posterior trató a
los anteriores: si los aplicó, los distinguió, los criticó o los abandonó.

> **No es asesoramiento legal.** Es una herramienta de investigación. Toda
> afirmación que hace es auditable contra el pasaje que la generó, y toda
> respuesta lleva su nivel de confianza. Verificá el texto antes de usarla.

---

## El problema

En Estados Unidos la pregunta la resuelve un *citador*: Shepard's en Lexis,
KeyCite en Westlaw, BCite en Bloomberg. Te dicen qué fallos posteriores citaron
al tuyo y, sobre todo, **cómo** lo citaron. Un semáforo rojo significa que no lo
cites.

En Argentina eso no existe en abierto. Los servidores MCP jurídicos que hay
buscan y devuelven documentos; ninguno construye la red de citas.

## El demo: la cadena de la tenencia para consumo personal

Cuatro fallos que se dan vuelta entre sí a lo largo de treinta años. Ningún
buscador actual muestra esta cadena.

| Año | Fallo | Cita | Qué hizo |
|---|---|---|---|
| 1978 | Colavini | `Fallos: 300:254` | Declaró constitucional penar la tenencia para consumo |
| 1986 | Bazterrica | `Fallos: 308:1392` | Se apartó de Colavini: inconstitucional (ver *Limitaciones*) |
| 1990 | Montalvo | `Fallos: 313:1333` | **Abandonó** Bazterrica: volvió a la criminalización |
| 2009 | Arriola | `Fallos: 332:1963` | **Abandonó** Montalvo, **siguió** Bazterrica |

Un pasaje real de Arriola, extraído por el pipeline, que sostiene dos de esas
aristas a la vez:

> Así en "Colavini" (Fallos: 300:254) se pronunció a favor de la
> criminalización; en "Bazterrica" y "Capalbo", se apartó de tal doctrina
> (Fallos: 308:1392); y en 1990, en "Montalvo" vuelve nuevamente sobre sus pasos
> a favor de la criminalización de la tenencia para consumo personal
> (Fallos: 313:1333), y como lo adelantáramos en las consideraciones previas,
> hoy el Tribunal decide volver a "Bazterrica".
>
> — CSJN, *Arriola*, considerando 12

Esa cadena es también el criterio de aceptación del proyecto:
`tests/test_golden_chain.py`. Si no la reconstruye, nada más importa.

## Las tools

| Tool | Qué hace |
|---|---|
| `citador_lookup_ruling` | Resuelve cualquier forma de cita a un fallo: `Fallos 332:1963`, `"Arriola"`, `A. 891. XLIV` |
| `citador_check_status` | La tool central: señal agregada de vigencia con los tratamientos que la sustentan |
| `citador_citing_rulings` | Qué fallos posteriores lo citaron, filtrable por tratamiento |
| `citador_cited_rulings` | En qué se apoyó este fallo |
| `citador_trace_doctrine` | Reconstruye la cadena completa de un tema a lo largo del tiempo |

### El vocabulario de tratamientos

| Señal | Significado |
|---|---|
| `applied` | Lo aplica como fundamento |
| `followed` | Lo sigue expresamente |
| `distinguished` | Lo reconoce pero lo aparta por los hechos |
| `limited` | Restringe su alcance |
| `criticized` | Lo cuestiona sin abandonarlo |
| `abandoned` | Se aparta de la doctrina anterior |
| `mentioned` | Cita sin tomar postura |

`abandoned` es el semáforo rojo y es el más conservador de todos: ante duda, la
clasificación es `mentioned` con confianza baja.

## Cómo se clasifica

En dos etapas, y la segunda sólo corre sobre lo que la primera no resuelve.

**Reglas.** Fórmulas fijas del castellano jurídico (`corresponde apartarse de`,
`han sido resueltas acertadamente en`, `no es compatible con el criterio`), con
tres guardas, porque la prosa de un fallo es adversarial por construcción:

- **la unidad es la cláusula, no el pasaje.** El considerando 12 de Arriola cita
  tres precedentes en una sola oración con tres tratamientos distintos; un
  clasificador que lee el párrafo entero pinta los tres de rojo.
- **negación y atribución.** `no corresponde apartarse` es lo contrario de
  `corresponde apartarse`, y `el recurrente sostiene que corresponde apartarse`
  es el argumento de una parte, no la postura del tribunal.
- **relato histórico.** En `en "Bazterrica" se apartó de tal doctrina`, el
  precedente citado es el *lugar* del apartamiento, no su objeto. Sin esta
  guarda el pipeline lee que Arriola abandonó a Bazterrica, que es exactamente lo
  contrario de lo que pasó.

Medido contra la cadena dorada: **4 de 5, con cero rojos falsos**. CI falla si
aparece uno.

```bash
uv run python scripts/eval_treatment.py
```

**LLM.** Para lo que requiere criterio a través de cláusulas. Que Arriola se
aparta de Montalvo no está dicho en ninguna parte: se sigue de que vuelve al
precedente que Montalvo había desplazado. Dos guardas:

- el modelo tiene que devolver la frase textual en la que se apoya, y esa frase
  tiene que estar en el pasaje; una inventada se descarta;
- su confianza tiene tope por debajo del umbral que necesita un tratamiento
  negativo para encender la señal roja. El modelo puede poner `abandoned` en el
  detalle con su pasaje; no puede, por sí solo, decirle a un abogado que
  descarte un precedente.

Es opcional y opt-in (`CITADOR_LLM=1`, extra `llm`), porque cuesta plata por
pasaje. Sin credencial el pipeline sigue con las reglas solas.

## Alcance

**Solo Corte Suprema de Justicia de la Nación.** Un citador nacional completo es
un proyecto de años; el recorte tiene razones, no excusas:

- La Corte tiene **cita canónica** (`Fallos tomo:página`), un identificador
  estable y parseable que no existe en el resto del sistema.
- El corpus es **acotado**: 349 tomos, verificado contra la fuente.
- Es el tribunal ápice, donde la pregunta de vigencia vale más.

### Lo que NO hace

- **No es asesoramiento legal.** Es una herramienta de investigación.
- **No busca personas ni partes.** El eje es el precedente.
- **No cubre expedientes del PJN.** Verificación anti-bot antes de cada búsqueda,
  fueros restringidos y nombres de partes en litigio: es un problema de datos
  personales, no una limitación técnica.
- **No compite con Westlaw en cobertura.** Compite en ser abierto y auditable.

## Cobertura del corpus

Medido contra la fuente el 1 de septiembre de 2026:

- **349 tomos** en la colección Fallos (tomo 349 = 2026).
- Los fallos modernos traen texto extraíble limpio (calidad 0,77–0,89).
- **Una parte de los fallos viejos no.** No son escaneos: tienen capa de texto,
  pero con fuentes sin mapa Unicode usable, de modo que lo que se extrae es una
  sustitución monoalfabética del texto real. El problema es **por documento, no
  por tomo**: en el tomo 300 conviven PDFs limpios y cifrados. Se detectan con un
  filtro de calidad y se recuperan por OCR; los tres fallos viejos de la cadena
  pasaron de 0,42–0,61 a 0,87–0,92.
- Todo texto recuperado por OCR se marca `text_status='ocr'` y las tools lo
  informan. **El OCR se equivoca**, sobre todo con impresiones de los años 70 y
  80, así que un pasaje puede traer erratas. Está declarado justamente para que
  quien verifique la cita sepa contra qué la está verificando.

### Qué trae el corpus publicado

El `.db` del release se crawleó el 2 de septiembre de 2026 sobre los **tomos
330-349** (2007-2026). Los números de portada halagan al archivo, así que van
con su letra chica:

| | |
|---|---|
| Nodos | 7.953 — de los cuales **5.325** son fallos crawleados y **2.628** son *stubs* |
| Aristas de cita | 73.611 |
| Con postura distinta de `mentioned` | **1.513 (2,1 %)** |
| Atribuidas a un voto | 10.444 (14,2 %), de las cuales 7.157 a la mayoría |
| Con texto propio | 391 (387 extraídos del PDF, 4 por OCR) |

Tres cosas que conviene entender antes de confiar en una respuesta:

- **Un *stub* no es un fallo.** Es una posición real en la colección, citada
  desde adentro del rango pero ubicada afuera, de la que sólo se conoce la cita.
  No tiene texto ni carátula completa. Contarlos junto a los fallos crawleados
  infla el corpus a la mitad, y por eso van separados.
- **Las aristas las publica la propia Corte**, en el campo `linksCitantes` de
  cada sumario. El grafo es ancho porque no depende de encontrar la cita en el
  texto; la clasificación es fina porque sí.
- **El 97,9 % de las aristas quedó en `mentioned`.** Eso no significa "citado sin
  tomar postura": significa *no clasificado*. La distinción es la diferencia
  entre una señal gris que informa y una que tranquiliza sin motivo, y las tools
  la dicen en cada respuesta con esa palabra.

Todo esto está grabado en `corpus_meta` dentro del propio archivo, lo calcula el
pipeline y lo devuelve la resource `citador://corpus`. No es un dato de folleto:
si regenerás el grafo, los números se recalculan solos.

## Limitaciones del método

Estas no son bugs pendientes. Son límites de lo que un citador construido sobre
el grafo de citas puede hacer, y conviene tenerlos a la vista.

**El abandono implícito no se detecta.** Cuando la Corte se aparta de un
precedente sin citarlo, no hay cita que clasificar. Pasa en la propia cadena de
demostración: doctrinariamente Bazterrica abandona a Colavini, pero el voto de la
mayoría de Bazterrica cita a Colavini una sola vez y **a favor**, aceptando su
valoración del problema; el apartamiento llega dos considerandos después y no lo
nombra. Este citador registra esa arista como `mentioned`, que es lo que el
pasaje sostiene. Marcarla `abandoned` sería afirmar algo que el texto citado no
dice, y eso es exactamente lo que el proyecto no hace.

**El precedente se mueve en las dos direcciones.** Un fallo abandonado y después
retomado no es doctrina muerta. Bazterrica fue abandonado por Montalvo en 1990 y
retomado por Arriola en 2009: la señal es amarilla, no roja ni verde, y la
advertencia cuenta la historia.

**Una cita no es la doctrina del tribunal por estar en el documento.** Los tomos
impresos traen el dictamen del Procurador General antes del fallo. En Bazterrica
el Procurador pedía mantener Colavini y la Corte resolvió al revés. El dictamen,
los votos propios y las disidencias se segmentan aparte y no mueven la señal.

## Instalación

```bash
uv tool install citador-ar-mcp
```

O `pip install citador-ar-mcp`. Para trabajar sobre el repo, `uv sync --extra ingest`.

El servidor necesita el grafo antes de responder nada. Generalo con la cadena
dorada, que no requiere red:

```bash
uv run python -m citador_ar_mcp.ingest.build_graph --from-fixture
```

Eso son cuatro fallos: alcanza para probar el servidor y correr las evals, no
para investigar. El corpus completo se baja del release (41 MB):

```bash
gh release download --repo LautaroMoya25/citador-ar-mcp --pattern corpus.db --dir data
```

También está en [la página de releases](https://github.com/LautaroMoya25/citador-ar-mcp/releases).
Como el nombre no coincide con el default, apuntá el servidor al archivo:

```bash
export CITADOR_DB="$PWD/data/corpus.db"
```

Si falta, el servidor levanta igual y lista sus tools: el error de cada llamada
trae las instrucciones para generarlo, en lugar de que el servidor no arranque.

### Conectarlo a un cliente MCP

En Claude Code:

```bash
claude mcp add citador --env CITADOR_DB=/ruta/al/corpus.db -- uvx citador-ar-mcp
```

En Claude Desktop, en `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "citador": {
      "command": "uvx",
      "args": ["citador-ar-mcp"],
      "env": { "CITADOR_DB": "/ruta/al/corpus.db" }
    }
  }
}
```

Si en cambio lo corrés desde el repo, reemplazá el comando por
`uv --directory /ruta/a/citador-ar-mcp run citador-ar-mcp`.

`CITADOR_DB` es opcional sólo si trabajás dentro del repo, donde por defecto usa
`data/citador.db`. Instalado desde PyPI no hay tal directorio, así que declaralo
apuntando al `.db` que bajaste del release.

### Variables de entorno

| Variable | Para qué |
|---|---|
| `CITADOR_DB` | Dónde está el grafo. Por defecto `data/citador.db` |
| `CITADOR_LOG_LEVEL` | Nivel de log a stderr. Por defecto `INFO` |
| `CITADOR_TESSERACT` | Ruta al binario de Tesseract si no está en el `PATH` |
| `TESSDATA_PREFIX` | Dónde está `spa.traineddata`, si no está en el directorio estándar |
| `CITADOR_LLM` | `1` para habilitar la etapa de clasificación por LLM. Apagada por defecto |
| `ANTHROPIC_API_KEY` | Credencial para la etapa de LLM. El SDK también acepta `ANTHROPIC_AUTH_TOKEN` o un perfil de `ant auth login` |
| `ANTHROPIC_WORKSPACE_ID` | Obligatoria si la organización usa claves *identity-linked*: la API rechaza el pedido con un **400**, no con un 401, así que es fácil leerlo como un problema de credencial cuando no lo es |

La etapa de LLM consume créditos de la API, que se cargan aparte y **no** vienen
con una suscripción a Claude. Si falta crédito, `diagnose()` lo dice con todas
las letras en lugar de fallar en silencio:

```bash
uv run python -c "from citador_ar_mcp.ingest import llm; print(llm.diagnose() or 'lista')"
```

Para recuperar fallos viejos hace falta Tesseract con el paquete de español:

```bash
winget install UB-Mannheim.TesseractOCR
```

En Linux, `apt install tesseract-ocr tesseract-ocr-spa`; en macOS,
`brew install tesseract tesseract-lang`. El paquete `spa.traineddata` conviene
bajarlo de [tessdata_best](https://github.com/tesseract-ocr/tessdata_best), que
rinde bastante mejor sobre impresiones viejas. Si el binario no está en el
`PATH`, definí `CITADOR_TESSERACT`; si los datos de idioma están fuera del
directorio estándar, `TESSDATA_PREFIX`.

## Uso

El servidor lee un grafo SQLite que produce el pipeline de ingesta. La ingesta
**no corre dentro del servidor**: es un proceso batch que deja un archivo.

Construir el grafo mínimo, con la cadena dorada y sin red:

```bash
uv run python -m citador_ar_mcp.ingest.build_graph --from-fixture
```

Correr el pipeline completo sobre un fallo (baja el PDF, extrae, pasa OCR si hace
falta, encuentra las citas, las atribuye a su voto y clasifica el tratamiento):

```bash
uv run --extra ingest python -m citador_ar_mcp.ingest.build_graph --ruling 332:1963
```

Levantar el servidor:

```bash
uv run mcp dev src/citador_ar_mcp/server.py
```

### Prompts

| Prompt | Para qué |
|---|---|
| `auditar_citas` | Revisar todas las citas de un escrito antes de presentarlo |
| `verificar_precedente` | Comprobar si conviene apoyarse en un fallo |
| `rastrear_doctrina` | Reconstruir cómo evolucionó una línea jurisprudencial |

`auditar_citas` es el caso de uso que justifica un citador: encontrar, antes que
la contraparte, que uno de los precedentes del escrito fue abandonado.

### Evals

Diez preguntas en `evals/evaluation.xml`, cuatro de ellas midiendo clasificación
de tratamiento y no sólo recuperación. Corren contra el grafo del fixture, que es
el mismo que arma CI, así que son estables.

## Licencia

MIT. El corpus derivado se publica con atribución a la Corte Suprema de Justicia
de la Nación, Secretaría de Jurisprudencia.
