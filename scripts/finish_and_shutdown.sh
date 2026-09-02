#!/usr/bin/env bash
# Espera a que termine el crawl, recupera los tomos que perdieron páginas,
# deja un informe y apaga la máquina.
#
# El apagado tiene una cuenta regresiva cancelable:
#
#     shutdown /a
#
set -uo pipefail
cd "$(dirname "$0")/.."

LOG=data/crawl.log
REPORT=data/INFORME-FINAL.txt
DB=data/corpus.db
SHUTDOWN=/c/Windows/System32/shutdown.exe
COUNTDOWN=300  # cinco minutos para cancelar

export PYTHONIOENCODING=utf-8

say() { echo "[$(date '+%H:%M:%S')] $*" | tee -a data/finish.log; }

say "esperando a que termine el crawl..."
STALE_LIMIT=900  # si el log no se mueve en 15 min, el proceso murió sin dejar rastro
while ! tail -5 "$LOG" 2>/dev/null | grep -q "exit="; do
    sleep 60
    quieto=$(( $(date +%s) - $(stat -c %Y "$LOG" 2>/dev/null || date +%s) ))
    if [ "$quieto" -gt "$STALE_LIMIT" ]; then
        say "el log lleva ${quieto}s sin moverse: doy el crawl por muerto y sigo"
        break
    fi
done
say "crawl terminado"

# Los tomos que perdieron alguna página quedaron marcados como relevados por la
# versión anterior del código. Se desmarcan para que la nueva corrida --que sí
# reintenta-- los vuelva a pedir. Es barato: sólo esos tomos.
say "recuperando tomos con páginas perdidas..."
uv run python - <<'PY' 2>&1 | tee -a data/finish.log
import re, sqlite3, pathlib
log = pathlib.Path("data/crawl.log").read_text(encoding="utf-8", errors="replace")
lossy = sorted({
    int(t) for t, got, total in re.findall(r"tomo (\d+): (\d+)/(\d+) sumarios", log)
    if got != total
})
print(f"  tomos incompletos: {lossy or 'ninguno'}")
if lossy:
    c = sqlite3.connect("data/corpus.db")
    row = c.execute("SELECT value FROM corpus_meta WHERE key='crawled_tomos'").fetchone()
    done = {int(v) for v in (row[0] if row else "").split(",") if v.strip().isdigit()}
    keep = sorted(done - set(lossy))
    c.execute("UPDATE corpus_meta SET value=? WHERE key='crawled_tomos'",
              (",".join(map(str, keep)),))
    c.commit(); c.close()
    print(f"  desmarcados, se reintentan: {lossy}")
PY

uv run python -m citador_ar_mcp.ingest.build_graph \
    --db "$DB" --tomos 330-349 --delay 0.5 >> data/crawl-recovery.log 2>&1
say "recuperación terminada (exit=$?)"

say "generando informe..."
uv run python - > "$REPORT" 2>&1 <<'PY'
import sqlite3, datetime, re, pathlib

c = sqlite3.connect("file:data/corpus.db?mode=ro", uri=True)
q = lambda s: c.execute(s).fetchone()[0]

print("INFORME DEL CRAWL — tomos 330-349")
print(f"generado {datetime.datetime.now():%Y-%m-%d %H:%M}")
print()
print(f"  fallos relevados      : {q('SELECT count(*) FROM rulings WHERE decided_year > 0')}")
print(f"  fallos citados sin relevar : {q('SELECT count(*) FROM rulings WHERE decided_year = 0')}")
print(f"  aristas de cita       : {q('SELECT count(*) FROM citations')}")
print(f"  alias                 : {q('SELECT count(*) FROM aliases')}")
row = c.execute("SELECT value FROM corpus_meta WHERE key='crawled_tomos'").fetchone()
print(f"  tomos relevados       : {row[0] if row else '(ninguno)'}")
print()

print("  Los 15 fallos más citados del corpus:")
for cid, n in c.execute("""SELECT cited_id, count(DISTINCT citing_id) n FROM citations
                           GROUP BY cited_id ORDER BY n DESC LIMIT 15"""):
    cap = c.execute("SELECT caption FROM rulings WHERE id=?", (cid,)).fetchone()
    print(f"    {n:>4} citantes  {cid:<20} {(cap[0] if cap else '')[:52]}")
print()

print("  Tratamientos: el crawl no clasifica, así que todo queda en 'mentioned'.")
for t, n in c.execute("SELECT treatment, count(*) FROM citations GROUP BY treatment"):
    print(f"    {t:<12} {n}")
print()
print("  Para clasificar hace falta el texto de cada fallo:")
print("    uv run --extra ingest python -m citador_ar_mcp.ingest.build_graph \\")
print("        --db data/corpus.db --ruling <tomo>:<pagina>")
print()

log = pathlib.Path("data/crawl.log").read_text(encoding="utf-8", errors="replace")
rec = pathlib.Path("data/crawl-recovery.log")
if rec.exists():
    log += rec.read_text(encoding="utf-8", errors="replace")
incompletos = [(t, g, tt) for t, g, tt in re.findall(r"tomo (\d+): (\d+)/(\d+) sumarios", log) if g != tt]
print(f"  504 del servidor durante el crawl: {log.count('falló la página')}")
print(f"  tomos que quedaron incompletos   : {incompletos or 'ninguno'}")
PY

cat "$REPORT" | tee -a data/finish.log

say "apagando en $((COUNTDOWN / 60)) minutos — cancelar con:  shutdown /a"

# MSYS_NO_PATHCONV es obligatorio, no una precaución. Git Bash convierte un
# argumento que empieza con barra en una ruta de Windows, así que `/s /t 300`
# le llega a shutdown.exe como `C:/Program Files/Git/s ...`: imprime el uso, no
# apaga nada, y devuelve éxito. El fallo es silencioso y en la dirección
# cómoda -- la máquina queda prendida -- pero es un fallo igual.
MSYS_NO_PATHCONV=1 "$SHUTDOWN" /s /t "$COUNTDOWN" \
    /c "Crawl del citador terminado. Cancelar con: shutdown /a"
rc=$?
if [ "$rc" -ne 0 ]; then
    say "shutdown.exe devolvió $rc: la máquina NO se va a apagar"
fi
