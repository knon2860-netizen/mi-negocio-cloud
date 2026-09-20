import os,re,sqlite3,json

DATABASE_URL=os.environ.get('DATABASE_URL','').strip()
SYNC_KEY=os.environ.get('MI_NEGOCIO_SYNC_KEY','').strip()
CLOUD_DEVICE_ID=os.environ.get('MI_NEGOCIO_CLOUD_DEVICE_ID','ANDROID-WEB').strip() or 'ANDROID-WEB'
REAL_CONNECT=sqlite3.connect
DB_PATH=os.path.abspath(os.environ.get('MI_NEGOCIO_DB','mi_negocio.db'))

class PGCursor:
    def __init__(self,conn): self.conn=conn; self.cur=conn.pg.cursor(); self._last_id=None
    @property
    def lastrowid(self): return self.conn.last_id
    def execute(self,sql,params=None):
        raw=sql.strip(); low=raw.lower()
        if low.startswith('pragma table_info'):
            m=re.search(r'table_info\(([^)]+)\)',raw,re.I); table=(m.group(1).strip(" '\"") if m else '')
            self.cur.execute("SELECT column_name AS name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",(table,)); return self
        if low.startswith('pragma '): return self
        if 'last_insert_rowid()' in low:
            if 'where id=last_insert_rowid()' in low:
                sql=re.sub(r'last_insert_rowid\(\)','%s',sql,flags=re.I); params=(self.conn.last_id,)
            elif re.match(r'select\s+last_insert_rowid',low): self.cur.execute('SELECT %s AS last_insert_rowid',(self.conn.last_id,)); return self
            else: sql=re.sub(r'last_insert_rowid\(\)','%s',sql,flags=re.I); params=tuple(params or ())+(self.conn.last_id,)
        sql=sql.replace('?','%s')
        try:
            self.cur.execute(sql,params or ())
            if low.startswith('insert into '):
                m=re.search(r'insert\s+into\s+([a-z_]\w*)',raw,re.I)
                if m:
                    table=m.group(1)
                    try:
                        q=self.conn.pg.cursor(); q.execute("SELECT currval(pg_get_serial_sequence(%s,'id'))",(table,)); r=q.fetchone(); self.conn.last_id=r[0] if r else None; q.close()
                    except Exception: self.conn.last_id=None
            return self
        except Exception as e:
            try:
                from psycopg.errors import UniqueViolation
                if isinstance(e,UniqueViolation): raise sqlite3.IntegrityError(str(e))
            except ImportError: pass
            raise
    def executemany(self,sql,seq): self.cur.executemany(sql.replace('?','%s'),seq); return self
    def fetchone(self): return self.cur.fetchone()
    def fetchall(self): return self.cur.fetchall()
    def close(self): self.cur.close()

class PGConn:
    def __init__(self,url):
        import psycopg
        from psycopg.rows import dict_row
        self.pg=psycopg.connect(url,row_factory=dict_row); self.last_id=None
    def execute(self,sql,params=None): return PGCursor(self).execute(sql,params)
    def executescript(self,script):
        s=re.sub(r'INTEGER PRIMARY KEY AUTOINCREMENT','BIGSERIAL PRIMARY KEY',script,flags=re.I)
        s=s.replace('REAL','DOUBLE PRECISION')
        s=re.sub(r',\s*FOREIGN KEY\([^)]*\) REFERENCES [a-z_]+\([^)]*\)', '', s, flags=re.I)
        self.pg.execute(s)
    def commit(self): self.pg.commit()
    def rollback(self): self.pg.rollback()
    def close(self): self.pg.close()

def connect_proxy(path,*args,**kwargs):
    p=os.path.abspath(str(path))
    if DATABASE_URL and p==DB_PATH: return PGConn(DATABASE_URL)
    return REAL_CONNECT(path,*args,**kwargs)

if DATABASE_URL: sqlite3.connect=connect_proxy
from app import app, init_db
init_db()

# Mantiene sincronizada la base central con el servidor de sincronización cada 60 segundos.
# No cambia el intervalo de Windows ni agrega otra frecuencia.
import threading
_sync_stop = threading.Event()
def _cloud_sync_worker():
    try:
        from sync_client import worker_loop
        worker_loop(_sync_stop, interval=60)
    except Exception:
        pass
threading.Thread(target=_cloud_sync_worker, daemon=True).start()

VALID_OPS={'PRODUCT_UPSERT','CUSTOMER_UPSERT','SUPPLIER_UPSERT','SALE','PURCHASE','CUSTOMER_PAYMENT','CASH_MOVEMENT','STOCK_ADJUSTMENT','AUDIT'}
def key_ok(req): return bool(SYNC_KEY) and req.headers.get('X-MiNegocio-Key','')==SYNC_KEY

def apply_op(c,op):
    oid=op.get('op_id'); typ=op.get('type'); p=op.get('payload') or {}
    if not oid or typ not in VALID_OPS: return False
    if c.execute('SELECT 1 FROM applied_operations WHERE op_id=?',(oid,)).fetchone(): return False
    if typ=='PRODUCT_UPSERT':
        if not p.get('id'): return False
        # Para un borrado/desactivación, Windows envía id + active=0.
        # No hace falta exigir el nombre para aplicar el borrado.
        if int(p.get('active', 1) or 0) == 0:
            c.execute('UPDATE products SET active=0 WHERE id=?',(int(p['id']),))
            if p.get('barcode'):
                c.execute('UPDATE products SET active=0 WHERE barcode=?',(str(p.get('barcode')),))
        else:
            if not p.get('name'): return False
            c.execute('''INSERT INTO products(id,barcode,name,category,buy_price,sell_price,stock,min_stock,fractional,active) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET barcode=excluded.barcode,name=excluded.name,category=excluded.category,buy_price=excluded.buy_price,sell_price=excluded.sell_price,min_stock=excluded.min_stock,fractional=excluded.fractional,active=excluded.active''',tuple(p.get(x) for x in ['id','barcode','name','category','buy_price','sell_price','stock','min_stock','fractional','active']))
    elif typ=='CUSTOMER_UPSERT':
        if not p.get('id') or not p.get('name'): return False
        c.execute('''INSERT INTO customers(id,name,phone,address,balance,active) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,phone=excluded.phone,address=excluded.address,active=excluded.active''',tuple(p.get(x) for x in ['id','name','phone','address','balance','active']))
    elif typ=='SUPPLIER_UPSERT':
        if not p.get('id') or not p.get('name'): return False
        c.execute('''INSERT INTO suppliers(id,name,phone,notes,active) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,phone=excluded.phone,notes=excluded.notes,active=excluded.active''',tuple(p.get(x) for x in ['id','name','phone','notes','active']))
    elif typ=='SALE':
        sid=int(p.get('sale_id') or 0); items=p.get('items') or []
        if not sid or c.execute('SELECT 1 FROM sales WHERE id=?',(sid,)).fetchone(): return False
        uid=int(p.get('user_id') or 1); cid=p.get('customer_id'); cid=int(cid) if cid and c.execute('SELECT 1 FROM customers WHERE id=?',(int(cid),)).fetchone() else None; pay=str(p.get('payment') or 'efectivo')
        cash=None
        if pay=='efectivo':
            cash=c.execute("SELECT * FROM cash_sessions WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone()
        c.execute('INSERT INTO sales(id,user_id,customer_id,cash_session_id,total,payment,created_at) VALUES(?,?,?,?,?,?,?)',(sid,uid,cid,cash['id'] if cash else None,float(p.get('total') or 0),pay,p.get('created_at') or __import__('datetime').datetime.now().isoformat(timespec='seconds'),before,after))
        for it in items:
            pid=int(it.get('product_id') or 0)
            if not pid: continue
            qty=float(it.get('qty') or 0); price=float(it.get('unit_price') or 0); sub=float(it.get('subtotal') or qty*price); c.execute('INSERT INTO sale_items(sale_id,product_id,qty,unit_price,subtotal) VALUES(?,?,?,?,?)',(sid,pid,qty,price,sub)); c.execute('UPDATE products SET stock=stock-? WHERE id=?',(qty,pid))
        if pay=='fiado' and cid:
            total=float(p.get('total') or 0); c.execute('UPDATE customers SET balance=balance+? WHERE id=?',(total,cid)); c.execute('INSERT INTO account_movements(customer_id,sale_id,type,amount,note,created_at) VALUES(?,?,?,?,?,?)',(cid,sid,'sale',total,f'Venta fiada #{sid}',p.get('created_at') or __import__('datetime').datetime.now().isoformat(timespec='seconds')))
        if pay=='efectivo' and cash: c.execute('INSERT INTO cash_movements(session_id,user_id,type,amount,note,created_at) VALUES(?,?,?,?,?,?)',(cash['id'],uid,'sale_cash',float(p.get('total') or 0),f'Venta #{sid} sincronizada',p.get('created_at') or __import__('datetime').datetime.now().isoformat(timespec='seconds')))
    elif typ=='PURCHASE':
        pid=int(p.get('purchase_id') or 0); items=p.get('items') or []
        if not pid or c.execute('SELECT 1 FROM purchases WHERE id=?',(pid,)).fetchone(): return False
        sup=p.get('supplier_id'); sup=int(sup) if sup and c.execute('SELECT 1 FROM suppliers WHERE id=?',(int(sup),)).fetchone() else None; uid=int(p.get('user_id') or 1)
        c.execute('INSERT INTO purchases(id,supplier_id,user_id,total,created_at) VALUES(?,?,?,?,?)',(pid,sup,uid,float(p.get('total') or 0),p.get('created_at') or __import__('datetime').datetime.now().isoformat(timespec='seconds')))
        for it in items:
            prod=int(it.get('product_id') or 0)
            if not prod: continue
            qty=float(it.get('qty') or 0); cost=float(it.get('unit_cost') or 0); sub=float(it.get('subtotal') or qty*cost); c.execute('INSERT INTO purchase_items(purchase_id,product_id,qty,unit_cost,subtotal) VALUES(?,?,?,?,?)',(pid,prod,qty,cost,sub)); c.execute('UPDATE products SET stock=stock+?,buy_price=? WHERE id=?',(qty,cost,prod))
    elif typ=='CUSTOMER_PAYMENT':
        cid=int(p.get('customer_id') or 0); amount=float(p.get('amount') or 0)
        if not cid or amount<=0: return False
        c.execute('UPDATE customers SET balance=GREATEST(0,balance-?) WHERE id=?',(amount,cid)); c.execute('INSERT INTO account_movements(customer_id,sale_id,type,amount,note,created_at) VALUES(?,?,?,?,?,?)',(cid,None,'payment',-amount,'Pago sincronizado',p.get('created_at') or __import__('datetime').datetime.now().isoformat(timespec='seconds')))
    elif typ=='CASH_MOVEMENT':
        cash=c.execute("SELECT * FROM cash_sessions WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone(); amount=float(p.get('amount') or 0)
        if not cash or amount==0: return False
        c.execute('INSERT INTO cash_movements(session_id,user_id,type,amount,note,created_at) VALUES(?,?,?,?,?,?)',(cash['id'],int(p.get('user_id') or 1),str(p.get('type') or 'manual'),amount,str(p.get('note') or 'Movimiento sincronizado'),p.get('created_at') or __import__('datetime').datetime.now().isoformat(timespec='seconds')))
    elif typ=='STOCK_ADJUSTMENT':
        pid=int(p.get('product_id') or 0); qty=float(p.get('qty') or 0); row=c.execute('SELECT stock FROM products WHERE id=?',(pid,)).fetchone()
        if not pid or qty==0 or not row: return False
        before=float(row['stock'] or 0); after=before+qty; c.execute('UPDATE products SET stock=? WHERE id=?',(after,pid)); c.execute('INSERT INTO stock_movements(op_id,product_id,qty,reason,note,user_id,device_id,created_at,stock_before,stock_after) VALUES(?,?,?,?,?,?,?,?,?,?)',(oid,pid,qty,str(p.get('reason') or 'otro'),str(p.get('note') or ''),int(p.get('user_id') or 1),str(p.get('device_id') or op.get('device_id') or ''),p.get('created_at') or __import__('datetime').datetime.now().isoformat(timespec='seconds'),before,after))
    elif typ=='AUDIT': c.execute('INSERT INTO audit_log(user_id,action,detail,created_at) VALUES(?,?,?,?)',(int(p.get('user_id') or 1),str(p.get('action') or 'SYNC'),str(p.get('detail') or ''),p.get('created_at') or __import__('datetime').datetime.now().isoformat(timespec='seconds')))
    c.execute('INSERT INTO applied_operations(op_id,applied_at) VALUES(?,?) ON CONFLICT(op_id) DO NOTHING',(oid,__import__('datetime').datetime.now().isoformat(timespec='seconds'))); return True

# Ensure central sync tables exist after app schema.
c=app.view_functions and None
conn=__import__('app').db(); conn.execute('''CREATE TABLE IF NOT EXISTS operations(seq BIGSERIAL PRIMARY KEY,op_id TEXT UNIQUE NOT NULL,device_id TEXT NOT NULL,type TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL,received_at TEXT NOT NULL)''') if DATABASE_URL else None
conn.execute('''CREATE TABLE IF NOT EXISTS applied_operations(op_id TEXT PRIMARY KEY,applied_at TEXT NOT NULL)''') if DATABASE_URL else None
conn.commit(); conn.close()

from flask import request,jsonify
@app.get('/health')
def health(): return jsonify(ok=True,service='Mi Negocio Web + Sync Server',database='postgres' if DATABASE_URL else 'sqlite')
@app.post('/v1/push')
def push():
    if not key_ok(request): return jsonify(ok=False,error='unauthorized'),401
    data=request.get_json(silent=True) or {}; ops=data.get('ops') or []
    if not isinstance(ops,list) or len(ops)>200: return jsonify(ok=False,error='invalid_batch'),400
    c=__import__('app').db(); accepted=[]; rejected=[]
    try:
        for op in ops:
            if not isinstance(op,dict) or op.get('type') not in VALID_OPS or not op.get('op_id'): rejected.append(str(op.get('op_id',''))); continue
            if not c.execute('SELECT 1 FROM operations WHERE op_id=?',(op['op_id'],)).fetchone():
                c.execute('INSERT INTO operations(op_id,device_id,type,payload,created_at,received_at) VALUES(?,?,?,?,?,?)',(op['op_id'],op.get('device_id',''),op['type'],json.dumps(op.get('payload') or {},ensure_ascii=False),op.get('created_at') or __import__('datetime').datetime.now().isoformat(timespec='seconds'),__import__('datetime').datetime.now().isoformat(timespec='seconds'))); apply_op(c,op)
            accepted.append(op['op_id'])
        c.commit(); c.close(); return jsonify(ok=True,accepted=accepted,rejected=rejected)
    except Exception: c.rollback(); c.close(); return jsonify(ok=False,error='server_database_error'),500
@app.get('/v1/pull')
def pull():
    if not key_ok(request): return jsonify(ok=False,error='unauthorized'),401
    try: after=max(0,int(request.args.get('after','0')))
    except: after=0
    device=request.args.get('device_id',''); c=__import__('app').db(); rows=c.execute('SELECT seq,op_id,device_id,type,payload,created_at FROM operations WHERE seq>? ORDER BY seq LIMIT 500',(after,)).fetchall(); ops=[{'seq':int(r['seq']),'op_id':r['op_id'],'device_id':r['device_id'],'type':r['type'],'payload':json.loads(r['payload']),'created_at':r['created_at']} for r in rows if r['device_id']!=device]; nxt=int(rows[-1]['seq']) if rows else after; c.close(); return jsonify(ok=True,ops=ops,next_cursor=nxt)
@app.post('/v1/bootstrap')
def bootstrap():
    if not key_ok(request): return jsonify(ok=False,error='unauthorized'),401
    data=request.get_json(silent=True) or {}; snap=data.get('snapshot') or {}; c=__import__('app').db()
    try:
        if c.execute("SELECT 1 FROM metadata WHERE key='snapshot'").fetchone(): c.close(); return jsonify(ok=True,created=False)
        order=('users','products','customers','suppliers','app_settings','cash_sessions','purchases','purchase_items','sales','sale_items','account_movements','cash_movements','audit_log','stock_movements')
        for t in order:
            for r in (snap.get(t) or []):
                cols=list(r.keys()); c.execute(f'INSERT INTO {t}({",".join(cols)}) VALUES({",".join(["?"]*len(cols))}) ON CONFLICT DO NOTHING',[r[k] for k in cols])
        c.execute('INSERT INTO metadata(key,value) VALUES(?,?)',('snapshot',json.dumps({'source':data.get('source',''),'device_id':data.get('device_id','')}))); c.commit(); c.close(); return jsonify(ok=True,created=True)
    except Exception: c.rollback(); c.close(); return jsonify(ok=False,error='server_database_error'),500

# Sincronización automática entre la base web y el servidor central.
# Se inicia después de cargar todas las rutas para evitar ciclos de importación.
if os.environ.get("MI_NEGOCIO_SYNC_URL") and os.environ.get("MI_NEGOCIO_SYNC_KEY"):
    import threading
    from sync_client import worker_loop
    _sync_stop = threading.Event()
    _sync_thread = threading.Thread(target=worker_loop, args=(_sync_stop, 60), daemon=True, name="mi-negocio-sync")
    _sync_thread.start()

if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT','8080')),debug=False)
