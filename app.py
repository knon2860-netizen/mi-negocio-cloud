
import os, sqlite3, json, socket, secrets, sys
from functools import wraps
from datetime import datetime, date
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RESOURCE_DIR = getattr(sys, "_MEIPASS", APP_DIR)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = APP_DIR
DB = os.environ.get("MI_NEGOCIO_DB", os.path.join(APP_DIR, "mi_negocio.db"))
app = Flask(__name__, template_folder=os.path.join(RESOURCE_DIR, "templates"), static_folder=os.path.join(RESOURCE_DIR, "static"))
app.secret_key = os.environ.get("MI_NEGOCIO_SECRET", secrets.token_hex(32))
app.permanent_session_lifetime = __import__("datetime").timedelta(days=3650)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

def ars_money(value):
    """Formato de pesos argentinos: $16.000 o $16.000,50."""
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        n = 0.0
    text = f"{n:,.2f}"
    text = text.replace(",", "X").replace(".", ",").replace("X", ".")
    if text.endswith(",00"):
        text = text[:-3]
    return text

app.jinja_env.filters["ars"] = ars_money

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def now():
    return datetime.now().isoformat(timespec="seconds")

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
      password TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','employee')),
      active INTEGER DEFAULT 1, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS products(
      id INTEGER PRIMARY KEY AUTOINCREMENT, barcode TEXT UNIQUE, name TEXT NOT NULL,
      category TEXT, buy_price REAL DEFAULT 0, sell_price REAL DEFAULT 0,
      stock REAL DEFAULT 0, min_stock REAL DEFAULT 0, fractional INTEGER DEFAULT 0, active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS customers(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT,
      address TEXT, balance REAL DEFAULT 0, active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS suppliers(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT, notes TEXT, active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS purchases(
      id INTEGER PRIMARY KEY AUTOINCREMENT, supplier_id INTEGER, user_id INTEGER,
      total REAL NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(supplier_id) REFERENCES suppliers(id), FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS purchase_items(
      id INTEGER PRIMARY KEY AUTOINCREMENT, purchase_id INTEGER, product_id INTEGER,
      qty REAL NOT NULL, unit_cost REAL NOT NULL, subtotal REAL NOT NULL,
      FOREIGN KEY(purchase_id) REFERENCES purchases(id), FOREIGN KEY(product_id) REFERENCES products(id)
    );
    CREATE TABLE IF NOT EXISTS sales(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, customer_id INTEGER,
      cash_session_id INTEGER, total REAL NOT NULL, payment TEXT NOT NULL,
      created_at TEXT NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(customer_id) REFERENCES customers(id),
      FOREIGN KEY(cash_session_id) REFERENCES cash_sessions(id)
    );
    CREATE TABLE IF NOT EXISTS sale_items(
      id INTEGER PRIMARY KEY AUTOINCREMENT, sale_id INTEGER, product_id INTEGER,
      qty REAL NOT NULL, unit_price REAL NOT NULL, subtotal REAL NOT NULL,
      FOREIGN KEY(sale_id) REFERENCES sales(id), FOREIGN KEY(product_id) REFERENCES products(id)
    );
    CREATE TABLE IF NOT EXISTS account_movements(
      id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER, sale_id INTEGER,
      type TEXT NOT NULL, amount REAL NOT NULL, note TEXT, created_at TEXT NOT NULL,
      FOREIGN KEY(customer_id) REFERENCES customers(id), FOREIGN KEY(sale_id) REFERENCES sales(id)
    );
    CREATE TABLE IF NOT EXISTS cash_sessions(
      id INTEGER PRIMARY KEY AUTOINCREMENT, opened_by INTEGER NOT NULL, opened_at TEXT NOT NULL,
      opening_amount REAL NOT NULL, closed_by INTEGER, closed_at TEXT, closing_amount REAL,
      status TEXT NOT NULL DEFAULT 'open', note TEXT,
      FOREIGN KEY(opened_by) REFERENCES users(id), FOREIGN KEY(closed_by) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS cash_movements(
      id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
      type TEXT NOT NULL, amount REAL NOT NULL, note TEXT, created_at TEXT NOT NULL,
      FOREIGN KEY(session_id) REFERENCES cash_sessions(id), FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS app_settings(
      key TEXT PRIMARY KEY, value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS audit_log(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT NOT NULL,
      detail TEXT, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS sync_outbox(
      op_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, type TEXT NOT NULL,
      payload TEXT NOT NULL, created_at TEXT NOT NULL, synced INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS sync_state(
      key TEXT PRIMARY KEY, value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS stock_movements(
      id INTEGER PRIMARY KEY AUTOINCREMENT, op_id TEXT UNIQUE NOT NULL, product_id INTEGER NOT NULL,
      qty REAL NOT NULL, reason TEXT NOT NULL, note TEXT, user_id INTEGER NOT NULL,
      device_id TEXT NOT NULL, created_at TEXT NOT NULL, stock_before REAL NOT NULL, stock_after REAL NOT NULL,
      FOREIGN KEY(product_id) REFERENCES products(id), FOREIGN KEY(user_id) REFERENCES users(id)
    );    CREATE TABLE IF NOT EXISTS applied_operations(
      op_id TEXT PRIMARY KEY,
      applied_at TEXT NOT NULL
    );
    """)
    # Migration: productos que pueden venderse fraccionados.
    pcols = {r["name"] for r in c.execute("PRAGMA table_info(products)").fetchall()}
    if "fractional" not in pcols:
        c.execute("ALTER TABLE products ADD COLUMN fractional INTEGER DEFAULT 0")

    # Lightweight migration for databases created by V1/V2.
    cols = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
    if "created_at" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN created_at TEXT")
        c.execute("UPDATE users SET created_at=? WHERE created_at IS NULL", (now(),))
   user_count = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
if user_count == 0:
        c.execute("INSERT INTO users(username,password,role,active,created_at) VALUES(?,?,?,?,?)",
                  ("admin", generate_password_hash("admin123"), "admin", 1, now()))
        c.execute("INSERT INTO users(username,password,role,active,created_at) VALUES(?,?,?,?,?)",
                  ("empleado", generate_password_hash("empleado123"), "employee", 1, now()))
    c.commit(); c.close()

def sync_queue(c, op_type, payload):
    import uuid
    device_id=os.environ.get("MI_NEGOCIO_DEVICE_ID", "WINDOWS-001")
    op_id=uuid.uuid4().hex
    c.execute("INSERT INTO sync_outbox(op_id,device_id,type,payload,created_at,synced) VALUES(?,?,?,?,?,?)",
              (op_id, device_id, op_type, json.dumps(payload, ensure_ascii=False), now(), 0))
    return op_id

def sync_status_data():
    try:
        c=db(); n=c.execute("SELECT COUNT(*) FROM sync_outbox WHERE synced=0").fetchone()[0]
        last=c.execute("SELECT value FROM sync_state WHERE key='last_sync'").fetchone()
        c.close(); return n, (last[0] if last else "Nunca")
    except Exception:
        return 0, "Nunca"

def company_settings(c=None):
    own=False
    if c is None:
        c=db(); own=True
    vals={
        "company_name": get_setting(c, "company_name", "DonArturo Forrajería"),
        "cuit": get_setting(c, "company_cuit", ""),
        "address": get_setting(c, "company_address", ""),
        "phone": get_setting(c, "company_phone", ""),
        "ticket_footer": get_setting(c, "ticket_footer", "Gracias por su compra!"),
    }
    if own: c.close()
    return vals

def audit(action, detail=""):
    if "user_id" not in session: return
    c=db()
    created=now(); uid=session["user_id"]
    c.execute("INSERT INTO audit_log(user_id,action,detail,created_at) VALUES(?,?,?,?)",
              (uid, action, detail, created))
    try:
        sync_queue(c, "AUDIT", {"user_id":uid,"action":action,"detail":detail,"created_at":created})
    except Exception:
        pass
    c.commit(); c.close()

def login_required(f):
    @wraps(f)
    def w(*a, **kw):
        if "user_id" not in session: return redirect(url_for("login"))
        return f(*a, **kw)
    return w

def admin_required(f):
    @wraps(f)
    def w(*a, **kw):
        if session.get("role") != "admin":
            flash("Esta sección es solo para el administrador.", "error")
            return redirect(url_for("dashboard"))
        return f(*a, **kw)
    return w

def get_setting(c, key, default=""):
    row=c.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def set_setting(c, key, value):
    c.execute("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

def ensure_daily_cash(c):
    """Mantiene una caja automática por día, sin apertura/cierre manual."""
    today=date.today().isoformat()
    current=c.execute("SELECT * FROM cash_sessions WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone()
    if current and current["opened_at"][:10] != today:
        # El día anterior se cierra automáticamente al comenzar el nuevo día.
        c.execute("UPDATE cash_sessions SET closed_at=?, closing_amount=NULL, status='closed', note=COALESCE(note,'') || ? WHERE id=?",
                  (today + "T00:00:00", " | Cierre automático por cambio de día", current["id"]))
        current=None
    if not current:
        amount=float(get_setting(c, "daily_start_cash", "0") or 0)
        # El usuario administrador que configura el fondo queda como responsable de la sesión.
        admin=c.execute("SELECT id FROM users WHERE role='admin' AND active=1 ORDER BY id LIMIT 1").fetchone()
        opened_by=admin["id"] if admin else 1
        c.execute("INSERT INTO cash_sessions(opened_by,opened_at,opening_amount,status,note) VALUES(?,?,?,?,?)",
                  (opened_by, today + "T00:00:00", amount, "open", "Caja diaria automática"))
        current=c.execute("SELECT * FROM cash_sessions WHERE id=last_insert_rowid()").fetchone()
    return current

def open_cash(c):
    return ensure_daily_cash(c)

@app.context_processor
def inject_globals():
    c=db()
    cash = open_cash(c)
    c.close()
    return {"cash_open": bool(cash), "today": date.today().isoformat()}

@app.route("/")
def index():
    return redirect(url_for("dashboard") if "user_id" in session else url_for("login"))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method=="POST":
        u=request.form.get("username","").strip()
        p=request.form.get("password","")
        c=db(); user=c.execute("SELECT * FROM users WHERE username=? AND active=1",(u,)).fetchone(); c.close()
        remember = request.form.get("remember") == "1"
        if user and check_password_hash(user["password"], p):
            session.clear()
            session.permanent = remember
            session.update(user_id=user["id"], username=user["username"], role=user["role"])
            audit("LOGIN", "Ingreso al sistema")
            return redirect(url_for("dashboard"))
        flash("Usuario o contraseña incorrectos.", "error")
    return render_template("login.html")

@app.get("/logout")
def logout():
    if "user_id" in session: audit("LOGOUT", "Cierre de sesión")
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    c=db()
    t=date.today().isoformat()
    sales_today=c.execute("SELECT COUNT(*) n,COALESCE(SUM(total),0) total FROM sales WHERE substr(created_at,1,10)=?",(t,)).fetchone()
    low=c.execute("SELECT COUNT(*) n FROM products WHERE active=1 AND stock<=min_stock").fetchone()["n"]
    negative=c.execute("SELECT COUNT(*) n FROM products WHERE active=1 AND stock<0").fetchone()["n"]
    debt=c.execute("SELECT COALESCE(SUM(balance),0) total FROM customers WHERE balance>0").fetchone()["total"]
    profit=c.execute("""SELECT COALESCE(SUM((si.unit_price-p.buy_price)*si.qty),0)
                       FROM sale_items si JOIN products p ON p.id=si.product_id
                       JOIN sales s ON s.id=si.sale_id WHERE substr(s.created_at,1,10)=?""",(t,)).fetchone()[0]
    cash=open_cash(c)
    c.close()
    return render_template("dashboard.html", sales_today=sales_today, low=low, negative=negative, debt=debt, profit=profit, cash=cash)

# Products / stock
@app.route("/products", methods=["GET","POST"])
@login_required
@admin_required
def products():
    c=db()
    if request.method=="POST":
        d=request.form
        try:
            c.execute("""INSERT INTO products(barcode,name,category,buy_price,sell_price,stock,min_stock,fractional)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (d.get("barcode") or None,d["name"].strip(),d.get("category","").strip(),
                       float(d.get("buy_price") or 0),float(d.get("sell_price") or 0),
                       float(d.get("stock") or 0),float(d.get("min_stock") or 0),1 if d.get("fractional") else 0))
            c.commit(); sync_queue(c, "PRODUCT_UPSERT", {"id": c.execute("SELECT last_insert_rowid()").fetchone()[0], "barcode": d.get("barcode") or "", "name": d["name"].strip(), "category": d.get("category","").strip(), "buy_price": float(d.get("buy_price") or 0), "sell_price": float(d.get("sell_price") or 0), "stock": float(d.get("stock") or 0), "min_stock": float(d.get("min_stock") or 0), "fractional": 1 if d.get("fractional") else 0, "active": 1}); c.commit(); audit("PRODUCT_CREATE", d["name"].strip()); flash("Producto agregado.","ok")
        except sqlite3.IntegrityError: c.rollback(); flash("El código de barras ya existe.","error")
    rows=c.execute("SELECT * FROM products WHERE active=1 ORDER BY name").fetchall()
    c.close()
    return render_template("products.html", products=rows)

@app.post("/products/<int:pid>/edit")
@login_required
@admin_required
def edit_product(pid):
    d=request.form; c=db()
    try:
        c.execute("""UPDATE products SET barcode=?,name=?,category=?,buy_price=?,sell_price=?,min_stock=?,fractional=? WHERE id=?""",
                  (d.get("barcode") or None,d["name"].strip(),d.get("category","").strip(),
                   float(d.get("buy_price") or 0),float(d.get("sell_price") or 0),
                   float(d.get("min_stock") or 0),1 if d.get("fractional") else 0,pid))
        c.commit(); row=c.execute("SELECT * FROM products WHERE id=?",(pid,)).fetchone(); sync_queue(c, "PRODUCT_UPSERT", {k: row[k] for k in row.keys() if k != "stock"} if row else {"id":pid}); c.commit(); audit("PRODUCT_EDIT", f"Producto #{pid}"); flash("Producto actualizado. El stock se modifica desde Ajustar stock.","ok")
    except sqlite3.IntegrityError:
        c.rollback(); flash("El código de barras ya existe.","error")
    c.close()
    return redirect(url_for("products"))

@app.post("/products/<int:pid>/delete")
@login_required
@admin_required
def delete_product(pid):
    c=db(); c.execute("UPDATE products SET active=0 WHERE id=?",(pid,)); sync_queue(c, "PRODUCT_UPSERT", {"id":pid,"active":0}); c.commit(); c.close()
    audit("PRODUCT_DISABLE", f"Producto #{pid}"); flash("Producto desactivado.","ok")
    return redirect(url_for("products"))

@app.get("/api/product")
@login_required
def api_product():
    q=request.args.get("barcode","").strip()
    c=db(); p=c.execute("SELECT * FROM products WHERE barcode=? AND active=1",(q,)).fetchone(); c.close()
    if not p: return jsonify({"ok":False})
    return jsonify({"ok":True,"id":p["id"],"name":p["name"],"price":p["sell_price"],"stock":p["stock"],"barcode":p["barcode"]})

# Customers / credit
@app.route("/customers", methods=["GET","POST"])
@login_required
def customers():
    c=db()
    if request.method=="POST":
        d=request.form
        c.execute("INSERT INTO customers(name,phone,address) VALUES(?,?,?)",
                  (d["name"].strip(),d.get("phone","").strip(),d.get("address","").strip()))
        c.commit(); row=c.execute("SELECT * FROM customers WHERE id=last_insert_rowid()").fetchone(); sync_queue(c, "CUSTOMER_UPSERT", dict(row) if row else {}); c.commit(); c.close(); audit("CUSTOMER_CREATE", d["name"].strip()); flash("Cliente agregado.","ok")
        return redirect(url_for("customers"))
    rows=c.execute("SELECT * FROM customers WHERE active=1 ORDER BY name").fetchall()
    c.close()
    return render_template("customers.html", customers=rows)

@app.post("/customers/<int:cid>/payment")
@login_required
def payment(cid):
    try: amount=float(request.form["amount"])
    except: amount=0
    if amount<=0: flash("Importe inválido.","error"); return redirect(url_for("customers"))
    c=db(); cust=c.execute("SELECT * FROM customers WHERE id=? AND active=1",(cid,)).fetchone()
    if not cust: c.close(); flash("Cliente inexistente.","error"); return redirect(url_for("customers"))
    amount=min(amount,max(cust["balance"],0))
    c.execute("UPDATE customers SET balance=balance-? WHERE id=?",(amount,cid))
    c.execute("""INSERT INTO account_movements(customer_id,type,amount,note,created_at)
                 VALUES(?,?,?,?,?)""",(cid,"payment",-amount,"Pago de cuenta",now()))
    cash=open_cash(c)
    if cash:
        c.execute("""INSERT INTO cash_movements(session_id,user_id,type,amount,note,created_at)
                     VALUES(?,?,?,?,?,?)""",(cash["id"],session["user_id"],"debt_payment",amount,f"Pago de {cust['name']}",now()))
    sync_queue(c, "CUSTOMER_PAYMENT", {"customer_id":cid,"amount":amount,"created_at":now()}); c.commit(); c.close()
    audit("CUSTOMER_PAYMENT", f"Cliente #{cid}: ${amount:.2f}")
    flash("Pago registrado.","ok"); return redirect(url_for("customers"))

@app.get("/account/<int:cid>")
@login_required
def account(cid):
    c=db()
    cust=c.execute("SELECT * FROM customers WHERE id=?",(cid,)).fetchone()
    if not cust:
        c.close(); flash("Cliente inexistente.","error"); return redirect(url_for("customers"))
    rows=c.execute("""SELECT am.*, s.total AS sale_total, s.payment AS sale_payment,
                            s.created_at AS sale_created_at, u.username AS sale_user
                     FROM account_movements am
                     LEFT JOIN sales s ON s.id=am.sale_id
                     LEFT JOIN users u ON u.id=s.user_id
                     WHERE am.customer_id=? ORDER BY am.id DESC""",(cid,)).fetchall()
    movements=[]
    for m in rows:
        item_details=[]
        if m["sale_id"]:
            items=c.execute("""SELECT si.qty, si.unit_price, si.subtotal, p.name
                             FROM sale_items si JOIN products p ON p.id=si.product_id
                             WHERE si.sale_id=? ORDER BY si.id""",(m["sale_id"],)).fetchall()
            item_details=[dict(i) for i in items]
        movements.append({**dict(m), "items": item_details})
    c.close()
    return render_template("account.html", customer=cust, movements=movements)

# Sales
@app.get("/sales")
@login_required
def sales():
    c=db()
    rows=c.execute("""SELECT s.*,u.username,COALESCE(c.name,'Consumidor final') customer
                     FROM sales s JOIN users u ON u.id=s.user_id
                     LEFT JOIN customers c ON c.id=s.customer_id ORDER BY s.id DESC LIMIT 500""").fetchall()
    c.close(); return render_template("sales.html", sales=rows)

@app.route("/new-sale", methods=["GET","POST"])
@login_required
def new_sale():
    c=db()
    products=c.execute("SELECT * FROM products WHERE active=1 ORDER BY name").fetchall()
    customers=c.execute("SELECT * FROM customers WHERE active=1 ORDER BY name").fetchall()
    if request.method=="POST":
        try:
            payload=json.loads(request.form["items"])
            if not payload: raise ValueError("El carrito está vacío.")
            payment_type=request.form.get("payment")
            cid=request.form.get("customer_id") or None
            if payment_type not in ("efectivo","transferencia","fiado"): raise ValueError("Forma de pago inválida.")
            cash=open_cash(c)
            if payment_type=="efectivo" and not cash: raise ValueError("La caja está cerrada. El administrador debe abrirla.")
            total=0; validated=[]
            for item in payload:
                p=c.execute("SELECT * FROM products WHERE id=? AND active=1",(int(item["id"]),)).fetchone()
                qty=float(item["qty"])
                if not p or qty<=0: raise ValueError("Producto o cantidad inválida.")
                if not p["fractional"] and abs(qty-round(qty)) > 1e-9:
                    raise ValueError(f"{p['name']} no está configurado para venderse fraccionado.")
                if p["fractional"] and abs(qty*1000-round(qty*1000)) > 1e-8:
                    raise ValueError(f"La cantidad fraccionada de {p['name']} debe ser de gramos enteros (por ejemplo 0,700 kg).")
                try:
                    unit_price=float(item.get("unit_price", p["sell_price"]))
                except (TypeError, ValueError):
                    raise ValueError(f"Precio de venta inválido para {p['name']}.")
                if unit_price <= 0:
                    raise ValueError(f"El precio de venta de {p['name']} debe ser mayor a 0.")
                # El empleado puede aplicar un precio especial solo para esta venta.
                # Nunca modifica el precio maestro del producto.
                if session.get("role") == "employee" and unit_price > float(p["sell_price"]) + 1e-9:
                    raise ValueError(f"El empleado no puede aumentar el precio de lista de {p['name']}.")
                subtotal=round(qty*unit_price,2); total+=subtotal
                validated.append((p,qty,unit_price,subtotal))
            if payment_type=="fiado":
                if not cid: raise ValueError("Para fiado debe seleccionar un cliente.")
                cust=c.execute("SELECT * FROM customers WHERE id=? AND active=1",(cid,)).fetchone()
                if not cust: raise ValueError("Cliente inválido.")
            cur=c.execute("""INSERT INTO sales(user_id,customer_id,cash_session_id,total,payment,created_at)
                             VALUES(?,?,?,?,?,?)""",
                          (session["user_id"],cid,cash["id"] if payment_type=="efectivo" and cash else None,total,payment_type,now()))
            sid=cur.lastrowid
            for p,qty,unit_price,subtotal in validated:
                c.execute("""INSERT INTO sale_items(sale_id,product_id,qty,unit_price,subtotal)
                             VALUES(?,?,?,?,?)""",(sid,p["id"],qty,unit_price,subtotal))
                c.execute("UPDATE products SET stock=stock-? WHERE id=?",(qty,p["id"]))
            if payment_type=="fiado":
                c.execute("UPDATE customers SET balance=balance+? WHERE id=?",(total,cid))
                c.execute("""INSERT INTO account_movements(customer_id,sale_id,type,amount,note,created_at)
                             VALUES(?,?,?,?,?,?)""",(cid,sid,"sale",total,f"Venta fiada #{sid}",now()))
            elif payment_type=="efectivo":
                c.execute("""INSERT INTO cash_movements(session_id,user_id,type,amount,note,created_at)
                             VALUES(?,?,?,?,?,?)""",(cash["id"],session["user_id"],"sale_cash",total,f"Venta #{sid}",now()))
            sync_queue(c, "SALE", {"sale_id":sid,"user_id":session["user_id"],"customer_id":cid,"total":total,"payment":payment_type,"items":[{"product_id":p["id"],"barcode":p["barcode"],"qty":qty,"base_price":float(p["sell_price"]),"unit_price":unit_price,"subtotal":subtotal} for p,qty,unit_price,subtotal in validated],"created_at":now()}); c.commit()
            price_notes=[]
            for p,qty,unit_price,subtotal in validated:
                base_price=float(p["sell_price"])
                if abs(unit_price-base_price) > 1e-9:
                    discount_pct=(1-(unit_price/base_price))*100 if base_price else 0
                    price_notes.append(f"{p['name']}: lista ${base_price:.2f} -> aplicado ${unit_price:.2f} ({discount_pct:.2f}% descuento)")
            detail=f"Venta #{sid}: ${total:.2f} {payment_type}"
            if price_notes:
                detail += " | " + " | ".join(price_notes)
            audit("SALE", detail)
            flash(f"Venta #{sid} registrada correctamente.","ok")
            return redirect(url_for("new_sale"))
        except Exception as e:
            c.rollback(); flash(str(e),"error")
    c.close(); return render_template("new_sale.html", products=products, customers=customers)

@app.get("/sale/<int:sid>")
@login_required
def sale_detail(sid):
    c=db()
    s=c.execute("""SELECT s.*,u.username,COALESCE(c.name,'Consumidor final') customer,
                   c.phone,c.address FROM sales s JOIN users u ON u.id=s.user_id
                   LEFT JOIN customers c ON c.id=s.customer_id WHERE s.id=?""",(sid,)).fetchone()
    items=c.execute("""SELECT si.*,p.name,p.barcode FROM sale_items si JOIN products p ON p.id=si.product_id
                       WHERE si.sale_id=? ORDER BY si.id""",(sid,)).fetchall()
    if not s:
        c.close()
        flash("Venta inexistente.","error")
        return redirect(url_for("sales"))
    company=company_settings(c)
    c.close()
    return render_template("sale_detail.html", sale=s, items=items, company=company)

# Cash
@app.route("/cash", methods=["GET","POST"])
@login_required
@admin_required
def cash():
    c=db(); current=ensure_daily_cash(c)
    c.commit()
    if request.method=="POST":
        # La caja ya no se abre ni se cierra manualmente.
        if request.form.get("action") in ("open", "close"):
            flash("La caja ahora funciona automáticamente por día. No hace falta abrirla ni cerrarla.", "error")
        c.close(); return redirect(url_for("cash"))
    summary=c.execute("""SELECT
      COALESCE(SUM(CASE WHEN type='sale_cash' THEN amount ELSE 0 END),0) sales_cash,
      COALESCE(SUM(CASE WHEN type='debt_payment' THEN amount ELSE 0 END),0) debt_payments,
      COALESCE(SUM(CASE WHEN type='in' THEN amount ELSE 0 END),0) cash_in,
      COALESCE(SUM(CASE WHEN type='out' THEN amount ELSE 0 END),0) cash_out
      FROM cash_movements WHERE session_id=?""",(current["id"],)).fetchone()
    expected=current["opening_amount"]+summary["sales_cash"]+summary["debt_payments"]+summary["cash_in"]-summary["cash_out"]
    moves=c.execute("SELECT cm.*,u.username FROM cash_movements cm JOIN users u ON u.id=cm.user_id WHERE session_id=? ORDER BY cm.id DESC",(current["id"],)).fetchall()
    history=c.execute("""SELECT cs.*,ou.username opened_name,cu.username closed_name
                         FROM cash_sessions cs JOIN users ou ON ou.id=cs.opened_by
                         LEFT JOIN users cu ON cu.id=cs.closed_by ORDER BY cs.id DESC LIMIT 30""").fetchall()
    fixed=float(get_setting(c, "daily_start_cash", "0") or 0)
    c.close()
    return render_template("cash.html", current=current, summary=summary, expected=expected, moves=moves, history=history, fixed=fixed)

@app.post("/cash/movement")
@login_required
@admin_required
def cash_movement():
    c=db(); cashs=ensure_daily_cash(c)
    typ=request.form.get("type")
    amount=float(request.form.get("amount") or 0)
    if typ not in ("in","out") or amount<=0: c.close(); flash("Movimiento inválido.","error"); return redirect(url_for("cash"))
    c.execute("""INSERT INTO cash_movements(session_id,user_id,type,amount,note,created_at)
                 VALUES(?,?,?,?,?,?)""",(cashs["id"],session["user_id"],typ,amount,request.form.get("note",""),now()))
    sync_queue(c, "CASH_MOVEMENT", {"session_id":cashs["id"],"user_id":session["user_id"],"type":typ,"amount":amount,"note":request.form.get("note","")}); c.commit(); c.close(); audit("CASH_MOVEMENT", f"{typ} ${amount:.2f}"); flash("Movimiento de caja registrado.","ok")
    return redirect(url_for("cash"))

# Purchases / suppliers
@app.route("/suppliers", methods=["GET","POST"])
@login_required
@admin_required
def suppliers():
    c=db()
    if request.method=="POST":
        d=request.form
        c.execute("INSERT INTO suppliers(name,phone,notes) VALUES(?,?,?)",(d["name"].strip(),d.get("phone",""),d.get("notes","")))
        c.commit(); row=c.execute("SELECT * FROM suppliers WHERE id=last_insert_rowid()").fetchone(); sync_queue(c, "SUPPLIER_UPSERT", dict(row) if row else {}); c.commit(); c.close(); audit("SUPPLIER_CREATE",d["name"].strip()); flash("Proveedor agregado.","ok")
        return redirect(url_for("suppliers"))
    rows=c.execute("SELECT * FROM suppliers WHERE active=1 ORDER BY name").fetchall()
    c.close(); return render_template("suppliers.html", suppliers=rows)

@app.route("/purchases", methods=["GET","POST"])
@login_required
@admin_required
def purchases():
    c=db()
    suppliers=c.execute("SELECT * FROM suppliers WHERE active=1 ORDER BY name").fetchall()
    products=c.execute("SELECT * FROM products WHERE active=1 ORDER BY name").fetchall()
    if request.method=="POST":
        try:
            payload=json.loads(request.form["items"])
            if not payload: raise ValueError("La compra está vacía.")
            sid=request.form.get("supplier_id") or None
            total=0; validated=[]
            for item in payload:
                p=c.execute("SELECT * FROM products WHERE id=? AND active=1",(int(item["id"]),)).fetchone()
                qty=float(item["qty"]); cost=float(item["cost"])
                if not p or qty<=0 or cost<0: raise ValueError("Producto, cantidad o costo inválido.")
                sub=round(qty*cost,2); total+=sub; validated.append((p,qty,cost,sub))
            cur=c.execute("INSERT INTO purchases(supplier_id,user_id,total,created_at) VALUES(?,?,?,?)",
                          (sid,session["user_id"],total,now()))
            pid=cur.lastrowid
            for p,qty,cost,sub in validated:
                c.execute("""INSERT INTO purchase_items(purchase_id,product_id,qty,unit_cost,subtotal)
                             VALUES(?,?,?,?,?)""",(pid,p["id"],qty,cost,sub))
                c.execute("UPDATE products SET stock=stock+?, buy_price=? WHERE id=?",(qty,cost,p["id"]))
            sync_queue(c, "PURCHASE", {"purchase_id":pid,"supplier_id":sid,"user_id":session["user_id"],"total":total,"items":[{"product_id":p["id"],"barcode":p["barcode"],"qty":qty,"unit_cost":cost,"subtotal":sub} for p,qty,cost,sub in validated],"created_at":now()}); c.commit(); audit("PURCHASE",f"Compra #{pid}: ${total:.2f}"); flash(f"Compra #{pid} registrada y stock actualizado.","ok")
            return redirect(url_for("purchases"))
        except Exception as e:
            c.rollback(); flash(str(e),"error")
    rows=c.execute("""SELECT pu.*,COALESCE(s.name,'Sin proveedor') supplier,u.username
                      FROM purchases pu LEFT JOIN suppliers s ON s.id=pu.supplier_id
                      JOIN users u ON u.id=pu.user_id ORDER BY pu.id DESC LIMIT 200""").fetchall()
    c.close(); return render_template("purchases.html", suppliers=suppliers, products=products, purchases=rows)

# Stock adjustments / audit
STOCK_REASONS = {
    "perdida": "Pérdida", "rotura": "Rotura", "vencimiento": "Vencimiento",
    "robo_faltante": "Robo / faltante", "correccion": "Corrección de inventario",
    "ingreso_manual": "Ingreso manual", "otro": "Otro",
}

def stock_device_id():
    return os.environ.get("MI_NEGOCIO_DEVICE_ID", "WINDOWS-001").strip() or "WINDOWS-001"

@app.route("/stock/ajuste", methods=["GET", "POST"])
@login_required
@admin_required
def stock_adjustment():
    c=db(); products=c.execute("SELECT * FROM products WHERE active=1 ORDER BY name").fetchall()
    if request.method == "POST":
        try:
            pid=int(request.form.get("product_id") or 0); reason=request.form.get("reason", "").strip()
            direction=request.form.get("direction", "decrease").strip(); qty=float(request.form.get("quantity") or 0)
            note=request.form.get("note", "").strip(); p=c.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
            if not p: raise ValueError("Producto inválido.")
            if reason not in STOCK_REASONS: raise ValueError("Motivo de ajuste inválido.")
            if qty <= 0: raise ValueError("La cantidad debe ser mayor que 0.")
            if not p["fractional"] and abs(qty-round(qty)) > 1e-9: raise ValueError(f"{p['name']} no admite cantidades decimales.")
            if p["fractional"] and abs(qty*1000-round(qty*1000)) > 1e-8: raise ValueError(f"La cantidad fraccionada de {p['name']} debe ser de gramos enteros (por ejemplo 0,700 kg).")
            if reason in ("perdida","rotura","vencimiento","robo_faltante") and not note: raise ValueError("La observación es obligatoria para este motivo.")
            delta=qty if direction=="increase" else -qty; before=float(p["stock"] or 0); after=before+delta
            import uuid; op_id=uuid.uuid4().hex; created=now()
            c.execute("INSERT INTO stock_movements(op_id,product_id,qty,reason,note,user_id,device_id,created_at,stock_before,stock_after) VALUES(?,?,?,?,?,?,?,?,?,?)", (op_id,pid,delta,reason,note,session["user_id"],stock_device_id(),created,before,after))
            c.execute("UPDATE products SET stock=stock+? WHERE id=?", (delta,pid))
            sync_queue(c,"STOCK_ADJUSTMENT",{"op_id":op_id,"product_id":pid,"qty":delta,"reason":reason,"note":note,"user_id":session["user_id"],"device_id":stock_device_id(),"created_at":created})
            c.commit(); audit("STOCK_ADJUSTMENT",f"{p['name']}: {delta:+g}; motivo={STOCK_REASONS[reason]}; stock {before:g} -> {after:g}")
            flash(f"Ajuste registrado. Stock: {before:g} → {after:g}.","ok"); return redirect(url_for("stock_history"))
        except Exception as e: c.rollback(); flash(str(e),"error")
    c.close(); return render_template("stock_adjustment.html", products=products, reasons=STOCK_REASONS)

@app.get("/stock/historial")
@login_required
@admin_required
def stock_history():
    c=db(); where=[]; args=[]
    date_from=request.args.get("date_from","").strip(); date_to=request.args.get("date_to","").strip(); product=request.args.get("product","").strip(); reason=request.args.get("reason","").strip(); user=request.args.get("user","").strip(); direction=request.args.get("direction","").strip()
    if date_from: where.append("substr(sm.created_at,1,10)>=?"); args.append(date_from)
    if date_to: where.append("substr(sm.created_at,1,10)<=?"); args.append(date_to)
    if product: where.append("(p.name LIKE ? OR COALESCE(p.barcode,'') LIKE ?)"); args.extend([f"%{product}%",f"%{product}%"])
    if reason in STOCK_REASONS: where.append("sm.reason=?"); args.append(reason)
    if user.isdigit(): where.append("sm.user_id=?"); args.append(int(user))
    if direction=="increase": where.append("sm.qty>0")
    elif direction=="decrease": where.append("sm.qty<0")
    clause="WHERE "+" AND ".join(where) if where else ""
    rows=c.execute(f"""SELECT sm.*,p.name product_name,p.barcode,u.username,CASE WHEN sm.qty>0 THEN 'Aumento' ELSE 'Disminución' END direction_label,CASE WHEN so.synced=1 THEN 'Sincronizado' ELSE 'Pendiente' END sync_status FROM stock_movements sm JOIN products p ON p.id=sm.product_id JOIN users u ON u.id=sm.user_id LEFT JOIN sync_outbox so ON so.op_id=sm.op_id {clause} ORDER BY sm.id DESC LIMIT 1000""",args).fetchall()
    users=c.execute("SELECT id,username FROM users ORDER BY username").fetchall(); summary={"count":len(rows),"decreased":sum(abs(float(r["qty"])) for r in rows if r["qty"]<0),"increased":sum(float(r["qty"]) for r in rows if r["qty"]>0)}; negative=c.execute("SELECT COUNT(*) FROM products WHERE active=1 AND stock<0").fetchone()[0]
    c.close(); return render_template("stock_history.html",rows=rows,users=users,reasons=STOCK_REASONS,summary=summary,negative=negative,filters=request.args)

@app.get("/stock/historial/<int:mid>")
@login_required
@admin_required
def stock_movement_detail(mid):
    c=db(); row=c.execute("""SELECT sm.*,p.name product_name,p.barcode,u.username,CASE WHEN sm.qty>0 THEN 'Aumento' ELSE 'Disminución' END direction_label FROM stock_movements sm JOIN products p ON p.id=sm.product_id JOIN users u ON u.id=sm.user_id WHERE sm.id=?""",(mid,)).fetchone(); c.close()
    if not row: flash("Movimiento inexistente.","error"); return redirect(url_for("stock_history"))
    return render_template("stock_movement_detail.html",row=row)

# Employees / passwords
@app.route("/employees", methods=["GET","POST"])
@login_required
@admin_required
def employees():
    c=db()
    if request.method=="POST":
        d=request.form
        try:
            c.execute("INSERT INTO users(username,password,role,active,created_at) VALUES(?,?,?,?,?)",
                      (d["username"].strip(),generate_password_hash(d["password"]),"employee",1,now()))
            c.commit(); audit("EMPLOYEE_CREATE",d["username"].strip()); flash("Empleado creado.","ok")
        except sqlite3.IntegrityError: c.rollback(); flash("Ese usuario ya existe.","error")
    rows=c.execute("SELECT id,username,role,active,created_at FROM users ORDER BY role,username").fetchall()
    c.close(); return render_template("employees.html", employees=rows)

@app.post("/employees/<int:uid>/toggle")
@login_required
@admin_required
def toggle_employee(uid):
    if uid==session["user_id"]: flash("No podés desactivar tu propio usuario.","error"); return redirect(url_for("employees"))
    c=db(); c.execute("UPDATE users SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=? AND role='employee'",(uid,)); c.commit(); c.close()
    audit("EMPLOYEE_TOGGLE",f"Usuario #{uid}"); flash("Estado del empleado actualizado.","ok"); return redirect(url_for("employees"))

@app.post("/employees/<int:uid>/delete")
@login_required
@admin_required
def delete_employee(uid):
    if uid==session["user_id"]:
        flash("No podés eliminar tu propio usuario.","error"); return redirect(url_for("employees"))
    c=db()
    emp=c.execute("SELECT id,username FROM users WHERE id=? AND role='employee'",(uid,)).fetchone()
    if not emp:
        c.close(); flash("El empleado no existe.","error"); return redirect(url_for("employees"))
    refs=[]
    checks=[("ventas", "SELECT COUNT(*) FROM sales WHERE user_id=?"),
            ("compras", "SELECT COUNT(*) FROM purchases WHERE user_id=?"),
            ("movimientos de caja", "SELECT COUNT(*) FROM cash_movements WHERE user_id=?"),
            ("auditoría", "SELECT COUNT(*) FROM audit_log WHERE user_id=?") ]
    for label,q in checks:
        if c.execute(q,(uid,)).fetchone()[0] > 0: refs.append(label)
    if refs:
        c.close()
        flash("No se puede borrar definitivamente a este empleado porque tiene historial de " + ", ".join(refs) + ". Podés desactivarlo para que no pueda volver a ingresar.","error")
        return redirect(url_for("employees"))
    username=emp["username"]
    c.execute("DELETE FROM users WHERE id=? AND role='employee'",(uid,)); c.commit(); c.close()
    audit("EMPLOYEE_DELETE",f"Usuario #{uid} ({username})")
    flash("Empleado eliminado.","ok")
    return redirect(url_for("employees"))

@app.route("/my-user", methods=["GET","POST"])
@login_required
@admin_required
def my_user():
    c=db()
    u=c.execute("SELECT id,username,password FROM users WHERE id=? AND role='admin'",(session["user_id"],)).fetchone()
    if not u:
        c.close(); flash("No se encontró el usuario administrador.","error"); return redirect(url_for("dashboard"))
    if request.method=="POST":
        username=request.form.get("username","").strip()
        current=request.form.get("current_password","")
        new_password=request.form.get("new_password","")
        if not username:
            flash("El nombre de usuario no puede quedar vacío.","error")
        elif not check_password_hash(u["password"], current):
            flash("La contraseña actual no coincide.","error")
        elif new_password and len(new_password)<6:
            flash("La nueva contraseña debe tener al menos 6 caracteres.","error")
        else:
            try:
                password_hash=generate_password_hash(new_password) if new_password else u["password"]
                c.execute("UPDATE users SET username=?, password=? WHERE id=? AND role='admin'",
                          (username,password_hash,u["id"]))
                c.commit()
                session["username"]=username
                audit("ADMIN_PROFILE_CHANGE", f"Usuario administrador actualizado: {username}")
                c.close()
                flash("Datos del administrador actualizados.","ok")
                return redirect(url_for("my_user"))
            except sqlite3.IntegrityError:
                c.rollback(); flash("Ese nombre de usuario ya existe.","error")
    c.close()
    return render_template("my_user.html", user=u)

@app.route("/change-password", methods=["GET","POST"])
@login_required
def change_password():
    if request.method=="POST":
        old=request.form.get("old_password",""); new=request.form.get("new_password","")
        if len(new)<6: flash("La nueva contraseña debe tener al menos 6 caracteres.","error")
        else:
            c=db(); u=c.execute("SELECT password FROM users WHERE id=?",(session["user_id"],)).fetchone()
            if u and check_password_hash(u["password"],old):
                c.execute("UPDATE users SET password=? WHERE id=?",(generate_password_hash(new),session["user_id"])); c.commit(); c.close()
                audit("PASSWORD_CHANGE"); flash("Contraseña actualizada.","ok"); return redirect(url_for("dashboard"))
            c.close(); flash("La contraseña actual no coincide.","error")
    return render_template("change_password.html")

# Statistics
@app.get("/statistics")
@login_required
@admin_required
def statistics():
    c=db()
    t=request.args.get("date") or date.today().isoformat()
    totals=c.execute("""SELECT COUNT(*) n,COALESCE(SUM(total),0) total FROM sales WHERE substr(created_at,1,10)=?""",(t,)).fetchone()
    cost=c.execute("""SELECT COALESCE(SUM(si.qty*p.buy_price),0) cost
                      FROM sale_items si JOIN products p ON p.id=si.product_id JOIN sales s ON s.id=si.sale_id
                      WHERE substr(s.created_at,1,10)=?""",(t,)).fetchone()["cost"]
    profit=totals["total"]-cost
    markup=(profit/cost*100) if cost else 0
    margin=(profit/totals["total"]*100) if totals["total"] else 0
    payments=c.execute("""SELECT
        COALESCE(SUM(CASE WHEN payment='efectivo' THEN total ELSE 0 END),0) cash,
        COALESCE(SUM(CASE WHEN payment='transferencia' THEN total ELSE 0 END),0) transfer,
        COALESCE(SUM(CASE WHEN payment='fiado' THEN total ELSE 0 END),0) credit
        FROM sales WHERE substr(created_at,1,10)=?""",(t,)).fetchone()
    cash_pct=(payments["cash"]/totals["total"]*100) if totals["total"] else 0
    transfer_pct=(payments["transfer"]/totals["total"]*100) if totals["total"] else 0
    credit_pct=(payments["credit"]/totals["total"]*100) if totals["total"] else 0
    c.close()
    return render_template("statistics.html", day=t, totals=totals, cost=cost, profit=profit, markup=markup, margin=margin, payments=payments, cash_pct=cash_pct, transfer_pct=transfer_pct, credit_pct=credit_pct)

# Reports
@app.get("/reports")
@login_required
@admin_required
def reports():
    c=db()
    t=request.args.get("date") or date.today().isoformat()
    totals=c.execute("""SELECT COUNT(*) n,COALESCE(SUM(total),0) total FROM sales WHERE substr(created_at,1,10)=?""",(t,)).fetchone()
    cost=c.execute("""SELECT COALESCE(SUM(si.qty*p.buy_price),0) cost
                      FROM sale_items si JOIN products p ON p.id=si.product_id JOIN sales s ON s.id=si.sale_id
                      WHERE substr(s.created_at,1,10)=?""",(t,)).fetchone()["cost"]
    payment_rows=c.execute("""SELECT payment,COUNT(*) n,COALESCE(SUM(total),0) total
                              FROM sales WHERE substr(created_at,1,10)=? GROUP BY payment""",(t,)).fetchall()
    top=c.execute("""SELECT p.name,SUM(si.qty) qty,SUM(si.subtotal) total
                     FROM sale_items si JOIN products p ON p.id=si.product_id
                     JOIN sales s ON s.id=si.sale_id WHERE substr(s.created_at,1,10)=?
                     GROUP BY p.id ORDER BY qty DESC LIMIT 15""",(t,)).fetchall()
    c.close()
    return render_template("reports.html", day=t, totals=totals, cost=cost, profit=totals["total"]-cost, payments=payment_rows, top=top)

# Backup / restore
@app.get("/backup")
@login_required
@admin_required
def backup():
    c=db(); c.commit(); c.close()
    audit("BACKUP", "Copia de base de datos")
    return send_file(DB, as_attachment=True, download_name=f"mi_negocio_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")

@app.route("/restore", methods=["GET","POST"])
@login_required
@admin_required
def restore():
    if request.method=="POST":
        f=request.files.get("backup")
        if not f or not f.filename.endswith(".db"):
            flash("Seleccioná un archivo .db de respaldo.","error")
            return redirect(url_for("restore"))
        tmp=os.path.join(APP_DIR,"restore_tmp.db")
        f.save(tmp)
        try:
            test=sqlite3.connect(tmp); test.execute("PRAGMA integrity_check").fetchone(); test.close()
            # Keep current DB as a safety copy.
            safety=os.path.join(APP_DIR,f"antes_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
            import shutil
            shutil.copy2(DB,safety); shutil.copy2(tmp,DB); os.remove(tmp)
            flash("Respaldo restaurado. Cerrá y volvé a abrir la aplicación si Windows mantiene una conexión anterior.","ok")
            audit("RESTORE","Restauración de base de datos")
        except Exception as e:
            if os.path.exists(tmp): os.remove(tmp)
            flash("No se pudo restaurar el respaldo: "+str(e),"error")
        return redirect(url_for("restore"))
    return render_template("restore.html")

@app.get("/audit")
@login_required
@admin_required
def audit_page():
    c=db()
    rows=c.execute("""SELECT a.*,COALESCE(u.username,'-') username FROM audit_log a
                      LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 500""").fetchall()
    c.close(); return render_template("audit.html", logs=rows)

@app.route("/settings", methods=["GET","POST"])
@login_required
@admin_required
def settings():
    c=db()
    if request.method=="POST":
        try:
            amount=float(request.form.get("daily_start_cash") or 0)
            if amount < 0: raise ValueError
            set_setting(c, "daily_start_cash", f"{amount:.2f}")
            set_setting(c, "company_name", request.form.get("company_name", "").strip() or "Mi Negocio")
            set_setting(c, "company_cuit", request.form.get("company_cuit", "").strip())
            set_setting(c, "company_address", request.form.get("company_address", "").strip())
            set_setting(c, "company_phone", request.form.get("company_phone", "").strip())
            set_setting(c, "ticket_footer", request.form.get("ticket_footer", "").strip())
            c.commit(); audit("SETTINGS_UPDATE", "Datos de empresa y ticket actualizados")
            flash("Configuración guardada correctamente.", "ok")
        except ValueError:
            c.rollback(); flash("El fondo inicial debe ser un número mayor o igual a 0.", "error")
    fixed=float(get_setting(c, "daily_start_cash", "0") or 0)
    company=company_settings(c)
    c.close()
    return render_template("settings.html", fixed=fixed, company=company)

@app.route("/sync", methods=["GET","POST"])
@login_required
@admin_required
def sync_page():
    if request.method=="POST":
        try:
            from sync_client import sync_once
            result=sync_once()
            flash(result.get("message","Sincronización terminada."), "ok" if result.get("ok") else "error")
        except Exception as e:
            flash("No se pudo sincronizar: "+str(e), "error")
        return redirect(url_for("sync_page"))
    pending,last=sync_status_data()
    configured=bool(os.environ.get("MI_NEGOCIO_SYNC_URL") and os.environ.get("MI_NEGOCIO_SYNC_KEY"))
    return render_template("sync.html", pending=pending, last=last, configured=configured, url=os.environ.get("MI_NEGOCIO_SYNC_URL", ""))


init_db()

if __name__=="__main__":
    try: ip=socket.gethostbyname(socket.gethostname())
    except: ip="IP-DE-LA-PC"
    print("\nMI NEGOCIO V3")
    print("PC del dueño: http://127.0.0.1:5050")
    print("Android ya no depende de la IP local 192.168.x.x; usa el servidor de sincronización por Internet.\n")
    app.run(host="0.0.0.0", port=5050, debug=False)
