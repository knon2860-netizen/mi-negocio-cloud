import json, os
from datetime import datetime

try:
    import requests
except ImportError:
    requests = None

from app import db

SYNC_URL = os.environ.get("MI_NEGOCIO_SYNC_URL", "").strip().rstrip("/")
SYNC_KEY = os.environ.get("MI_NEGOCIO_SYNC_KEY", "").strip()
DEVICE_ID = (os.environ.get("MI_NEGOCIO_DEVICE_ID") or os.environ.get("MI_NEGOCIO_CLOUD_DEVICE_ID") or "ANDROID-WEB").strip() or "ANDROID-WEB"


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

    c = db(); ensure_tables(c)
    rows = c.execute("SELECT * FROM sync_outbox WHERE synced=0 ORDER BY created_at, op_id LIMIT 100").fetchall()
    ops = [{"op_id": r["op_id"], "device_id": r["device_id"], "type": r["type"], "payload": json.loads(r["payload"]), "created_at": r["created_at"]} for r in rows]

    try:
        headers = {"X-MiNegocio-Key": SYNC_KEY}

        if bootstrap_needed(c):
            br = requests.post(SYNC_URL + "/v1/bootstrap", json={"source": "cloud", "device_id": DEVICE_ID, "snapshot": snapshot()}, headers=headers, timeout=15)
            br.raise_for_status()
            mark_bootstrapped(c)
            c.commit()

        if ops:
            r = requests.post(SYNC_URL + "/v1/push", json={"device_id": DEVICE_ID, "ops": ops}, headers=headers, timeout=15)
            r.raise_for_status()
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

        pr = requests.get(SYNC_URL + "/v1/pull", params={"after": cur, "device_id": DEVICE_ID}, headers=headers, timeout=15)
        pr.raise_for_status()
        pdata = pr.json()

        # cloud_launcher exposes apply_op for the central PostgreSQL-backed app.
        from cloud_launcher import apply_op
        applied = 0
        for op in pdata.get("ops", []):
            if apply_op(c, op):
                applied += 1

        next_cur = int(pdata.get("next_cursor", cur))
        c.execute("INSERT INTO sync_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("cursor", str(next_cur)))
        c.execute("INSERT INTO sync_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("last_sync", datetime.now().isoformat(timespec="seconds")))
        c.commit(); c.close()
        return {"ok": True, "configured": True, "accepted": len(data.get("accepted", [])), "remote": len(pdata.get("ops", [])), "applied": applied, "pending": pending_count(), "message": "Sincronización realizada."}
    except Exception as e:
        c.rollback(); c.close()
        return {"ok": False, "configured": True, "message": str(e), "pending": len(ops)}


def worker_loop(stop_event, interval=60):
    while not stop_event.is_set():
        try:
            sync_once()
        except Exception:
            pass
        stop_event.wait(interval)
