import json, os, time, threading
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    requests = None

from app import db

SYNC_URL = os.environ.get("MI_NEGOCIO_SYNC_URL", "").strip().rstrip("/")
SYNC_KEY = os.environ.get("MI_NEGOCIO_SYNC_KEY", "").strip()
DEVICE_ID = (os.environ.get("MI_NEGOCIO_DEVICE_ID") or os.environ.get("MI_NEGOCIO_CLOUD_DEVICE_ID") or "ANDROID-WEB").strip() or "ANDROID-WEB"


_SYNC_LOCK = threading.Lock()

def _log(msg):
    print("[sync] " + str(msg), flush=True)   # aparece en Render -> Logs

def _save_result(text):
    """Guarda el resultado del último intento para mostrarlo en la pantalla Sincronización."""
    try:
        c = db(); ensure_tables(c)
        c.execute("INSERT INTO sync_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  ("last_result", datetime.now().strftime("%d/%m %H:%M:%S") + " · " + str(text)[:300]))
        c.commit(); c.close()
    except Exception as e:
        _log("no se pudo guardar el resultado: %r" % (e,))

def request_retry(method, url, **kwargs):
    """Reintenta ante 429/502/503, típicos cuando el servidor gratis de Render está despertando
    o cuando el borde de Render limita pedidos. Deja en el log quién respondió, para diagnosticar."""
    last = None
    for delay in (0, 4, 10):
        if delay: time.sleep(delay)
        try:
            rr = requests.request(method, url, **kwargs)
            if rr.status_code not in (429, 502, 503): return rr
            last = rr
            _log("respuesta %s de %s (Server=%s, Retry-After=%s) cuerpo: %r" % (
                rr.status_code, url, rr.headers.get("Server"), rr.headers.get("Retry-After"), (rr.text or "")[:120]))
            ra = rr.headers.get("Retry-After")
            if ra and ra.isdigit() and int(ra) <= 15: time.sleep(int(ra))
        except Exception as exc:
            last = exc
            _log("fallo de red hacia %s: %r" % (url, exc))
    if isinstance(last, Exception): raise last
    return last

def _check(rr, what):
    """Como raise_for_status pero con un mensaje entendible."""
    if rr.status_code >= 400:
        raise Exception("El servidor central respondió %s en '%s' (Server=%s): %s" % (
            rr.status_code, what, rr.headers.get("Server"), (rr.text or "")[:100].replace("\n", " ")))

def enabled():
    return bool(requests and SYNC_URL and SYNC_KEY)


def ensure_tables(c):
    c.execute("CREATE TABLE IF NOT EXISTS sync_outbox(op_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, synced INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE IF NOT EXISTS sync_state(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.commit()


def pending_count():
    c = db(); ensure_tables(c)
    n = c.execute("SELECT COUNT(*) FROM sync_outbox WHERE synced=0").fetchone()[0]
    c.close()
    return int(n)


def snapshot():
    c = db(); ensure_tables(c)
    tables = ("users","products","customers","suppliers","purchases","purchase_items","sales","sale_items","account_movements","cash_sessions","cash_movements","app_settings","audit_log","stock_movements")
    data = {k: [dict(r) for r in c.execute("SELECT * FROM " + k).fetchall()] for k in tables}
    c.close()
    return data


def bootstrap_needed(c):
    row = c.execute("SELECT value FROM sync_state WHERE key='bootstrapped'").fetchone()
    return not row or row[0] != "1"


def mark_bootstrapped(c):
    c.execute("INSERT INTO sync_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("bootstrapped", "1"))


def sync_once():
    if not enabled():
        return {"ok": False, "configured": False, "message": "Servidor de sincronización no configurado."}
    if not _SYNC_LOCK.acquire(blocking=False):
        return {"ok": True, "configured": True, "message": "Ya hay una sincronización en curso.", "pending": pending_count()}
    try:
        _log("inicio de sincronización")
        result = _sync_once_locked()
        _log("fin: ok=%s · %s" % (result.get("ok"), result.get("message")))
        _save_result(("OK" if result.get("ok") else "ERROR") + " — " + str(result.get("message")) +
                     (" (traídas %s, aplicadas %s, omitidas %s)" % (result.get("remote"), result.get("applied"), result.get("skipped")) if result.get("ok") else ""))
        return result
    finally:
        _SYNC_LOCK.release()


def _shared_mode():
    """
    Si existe DATABASE_URL, esta instancia usa la base central.
    No depende de que la tabla operations tenga registros.
    """
    return bool(os.environ.get("DATABASE_URL"))

def _push_direct(c, ops):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    accepted = []
    for o in ops:
        c.execute("INSERT INTO operations(op_id,device_id,type,payload,created_at,received_at) VALUES(?,?,?,?,?,?) ON CONFLICT(op_id) DO NOTHING",
                  (o["op_id"], o["device_id"], o["type"], json.dumps(o["payload"], ensure_ascii=False, separators=(",", ":")), str(o.get("created_at") or now), now))
        accepted.append(o["op_id"])
    return accepted


def _pull_direct(c, cur):
    rows = c.execute("SELECT seq,op_id,device_id,type,payload,created_at FROM operations WHERE seq>? ORDER BY seq LIMIT 500", (cur,)).fetchall()
    ops = []
    for r in rows:
        if r[2] == DEVICE_ID: continue
        ops.append({"seq": int(r[0]), "op_id": r[1], "device_id": r[2], "type": r[3], "payload": json.loads(r[4]), "created_at": r[5]})
    return {"ok": True, "ops": ops, "next_cursor": int(rows[-1][0]) if rows else cur}


def sync_async():
    """Para el botón: arranca la sincronización en segundo plano y responde enseguida
    (si tardara más de 30 s dentro del pedido web, gunicorn reiniciaría el servicio)."""
    if not enabled():
        return False, "Servidor de sincronización no configurado."
    if _SYNC_LOCK.locked():
        return False, "Ya hay una sincronización en curso. Esperá un momento y recargá esta pantalla."
    threading.Thread(target=sync_once, daemon=True, name="mi-negocio-sync-manual").start()
    return True, "Sincronización iniciada. Recargá esta pantalla en unos segundos para ver el resultado."


def _sync_once_locked():
    c = db(); ensure_tables(c)
    rows = c.execute("SELECT * FROM sync_outbox WHERE synced=0 ORDER BY created_at, op_id LIMIT 100").fetchall()
    ops = [{"op_id": r["op_id"], "device_id": r["device_id"], "type": r["type"], "payload": json.loads(r["payload"]), "created_at": r["created_at"]} for r in rows]

    try:
        headers = {"X-MiNegocio-Key": SYNC_KEY}

        # La nube no necesita publicar una copia inicial en el servidor central: la publica la PC
        # y la nube nunca la descarga. Enviarla era un pedido pesado que además recibía 429.
        if bootstrap_needed(c):
            mark_bootstrapped(c); c.commit()

        shared = _shared_mode(c)
        if shared: _log("base compartida con el servidor central: se sincroniza directo en la base de datos")

        if ops:
            if shared:
                data = {"accepted": _push_direct(c, ops)}
            else:
                r = request_retry("POST", SYNC_URL + "/v1/push", json={"device_id": DEVICE_ID, "ops": ops}, headers=headers, timeout=20)
                _check(r, "push")
                data = r.json()
            for oid in data.get("accepted", []):
                c.execute("UPDATE sync_outbox SET synced=1 WHERE op_id=?", (oid,))
            if data.get("rejected"):
                c.commit(); c.close()
                return {"ok": False, "configured": True, "message": "El servidor rechazó una o más operaciones.", "pending": pending_count()}
        else:
            data = {"accepted": []}

        row = c.execute("SELECT value FROM sync_state WHERE key='cursor'").fetchone()
        cur = int(row[0]) if row else 0

        if shared:
            pdata = _pull_direct(c, cur)
        else:
            pr = request_retry("GET", SYNC_URL + "/v1/pull", params={"after": cur, "device_id": DEVICE_ID}, headers=headers, timeout=20)
            _check(pr, "pull")
            pdata = pr.json()
        _log("traídas %d operaciones (desde cursor %s)" % (len(pdata.get("ops", [])), cur))

        # cloud_launcher exposes apply_op for the central PostgreSQL-backed app.
        from cloud_launcher import apply_op, fix_sequences
        applied = 0
        skipped = 0
        for op in pdata.get("ops", []):
            # SAVEPOINT por operación: en PostgreSQL un solo error aborta toda la transacción.
            # Así una operación con conflicto se descarta sola y el resto sigue aplicándose.
            c.execute("SAVEPOINT op_sp")
            try:
                if apply_op(c, op): applied += 1
                c.execute("RELEASE SAVEPOINT op_sp")
            except Exception as e:
                skipped += 1
                _log("operación omitida %s (%s): %r" % (op.get("op_id"), op.get("type"), str(e)[:200]))
                c.execute("ROLLBACK TO SAVEPOINT op_sp")
                c.execute("RELEASE SAVEPOINT op_sp")
        fix_sequences(c)

        next_cur = int(pdata.get("next_cursor", cur))
        c.execute("INSERT INTO sync_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("cursor", str(next_cur)))
        c.execute("INSERT INTO sync_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("last_sync", datetime.now().isoformat(timespec="seconds")))
        c.commit(); c.close()
        return {"ok": True, "configured": True, "accepted": len(data.get("accepted", [])), "remote": len(pdata.get("ops", [])), "applied": applied, "skipped": skipped, "pending": pending_count(), "message": "Sincronización realizada." if not skipped else f"Sincronización realizada. Se omitieron {skipped} operación(es) con conflicto."}
    except Exception as e:
        import traceback; _log("ERROR: " + traceback.format_exc()[-800:])
        try: c.rollback(); c.close()
        except Exception: pass
        return {"ok": False, "configured": True, "message": str(e), "pending": len(ops)}


def worker_loop(stop_event, interval=60):
    _log("hilo de sincronización automática iniciado (cada %s s)" % interval)
    while not stop_event.is_set():
        try:
            sync_once()
        except Exception as e:
            _log("error inesperado en el hilo: %r" % (e,))
        stop_event.wait(interval)
