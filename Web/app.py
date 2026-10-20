import json
import os
import threading
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import urlsplit

import requests
from flask import Flask, jsonify, request, render_template, session
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "").lower() == "true",
)
DB_FILE = "database.json"
DB_LOCK = threading.Lock()
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")

# PayPal 設定：請以環境變數提供，勿寫死在程式裡
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET = os.environ.get("PAYPAL_SECRET", "")
PAYPAL_CURRENCY = os.environ.get("PAYPAL_CURRENCY", "USD")
PAYPAL_API = ("https://api-m.paypal.com" if os.environ.get("PAYPAL_ENV", "sandbox") == "live"
              else "https://api-m.sandbox.paypal.com")
# PayPal 訂單 ID -> 待捕獲的訂單資料
PENDING_PAYPAL = {}

# 初始化資料庫檔案
def load_db():
    if not os.path.exists(DB_FILE):
        initial_data = {
            "users": [],
            "orders": [],
            "posts": [
                {"title": "歡迎來到蠍瓜網", "date": "2026-06-07", "content": "這是蠍瓜網的第一篇公告，歡迎所有玩家與訪客！"}
            ],
            "services": [
                {"name": "蠍瓜伺服器客戶端工具", "note": "內含優化模組與快捷連線設定，請直接下載執行。", "url": "server.exe"}
            ],
            "products": {
                "mc-firework": [
                    {"name": "飛行 III 煙火（大包裝）", "desc": "Minecraft 內使用，鞘翅長途飛行必備！", "price": "23000 伺服幣"},
                    {"name": "飛行 III 煙火（中包裝）", "desc": "Minecraft 內使用，鞘翅飛行加速煙火", "price": "2300 伺服幣"},
                    {"name": "飛行 III 煙火（基礎包）", "desc": "Minecraft 內使用，飛行時間 III", "price": "500 伺服幣"}
                ],
                "mc-other": [],
                "shop-goods": [],
                "other-games": []
            }
        }
        save_db(initial_data)
        return initial_data
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            return jsonify({"message": "請先登入"}), 401
        return view(*args, **kwargs)
    return wrapped

def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("role") != "admin":
            return jsonify({"message": "需要管理員權限"}), 403
        return view(*args, **kwargs)
    return wrapped

def is_paypal_url(value):
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        valid_host = (host == "paypal.com" or host.endswith(".paypal.com")
                      or host == "paypal.me" or host.endswith(".paypal.me"))
        return parsed.scheme == "https" and valid_host and not parsed.username and not parsed.password
    except (TypeError, ValueError):
        return False

@app.route('/')
def index():
    return render_template('index.html')

# --- API 路由 ---

@app.route('/api/data', methods=['GET'])
def get_all_data():
    db = load_db()
    return jsonify({key: db.get(key, default) for key, default in {
        "posts": [], "services": [], "products": {}
    }.items()})

@app.route('/api/users', methods=['POST'])
def handle_users():
    db = load_db()
    user = request.get_json(silent=True) or {}
    name = str(user.get('name', '')).strip()
    email = str(user.get('email', '')).strip()
    password = user.get('password', '')
    if len(name) < 2 or len(name) > 32 or not isinstance(password, str) or len(password) < 8:
        return jsonify({"message": "名稱需為 2 到 32 字元，密碼至少 8 個字元"}), 400
    if name.lower() == ADMIN_USERNAME.lower():
        return jsonify({"message": "此名稱無法註冊"}), 400
    if any(u.get('name', '').lower() == name.lower() for u in db['users']):
        return jsonify({"message": "此名稱已經被註冊過了！"}), 400
    db['users'].append({"name": name, "email": email, "password": generate_password_hash(password)})
    save_db(db)
    return jsonify({"status": "success"}), 201

@app.route('/api/auth/login', methods=['POST'])
def login():
    body = request.get_json(silent=True) or {}
    name = str(body.get('name', '')).strip()
    password = body.get('password', '')
    if not isinstance(password, str):
        return jsonify({"message": "帳號或密碼錯誤"}), 401
    if name == ADMIN_USERNAME:
        if not ADMIN_PASSWORD:
            return jsonify({"message": "尚未設定管理員密碼，請設定 ADMIN_PASSWORD"}), 503
        if password != ADMIN_PASSWORD:
            return jsonify({"message": "帳號或密碼錯誤"}), 401
        session.clear()
        session.update(user=ADMIN_USERNAME, role="admin")
        return jsonify({"user": ADMIN_USERNAME, "role": "admin"})
    db = load_db()
    user = next((u for u in db['users'] if u.get('name', '').lower() == name.lower()), None)
    if not user:
        return jsonify({"message": "帳號或密碼錯誤"}), 401
    stored_password = user.get('password', '')
    try:
        valid = check_password_hash(stored_password, password)
    except (ValueError, TypeError):
        valid = False
    if not valid and stored_password == password:
        user['password'] = generate_password_hash(password)
        save_db(db)
        valid = True
    if not valid:
        return jsonify({"message": "帳號或密碼錯誤"}), 401
    session.clear()
    session.update(user=user['name'], role="user")
    return jsonify({"user": user['name'], "role": "user"})

@app.route('/api/auth/me')
def current_session():
    return jsonify({"user": session.get("user"), "role": session.get("role")})

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({"status": "success"})

@app.route('/api/products/<category>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def handle_products(category):
    if request.method != 'GET' and session.get('role') != 'admin':
        return jsonify({"message": "需要管理員權限"}), 403
    db = load_db()
    if request.method == 'GET':
        return jsonify(db['products'].get(category, []))
    
    data = request.get_json(silent=True) or {}
    if category not in db['products']:
        db['products'][category] = []
    product = data.get('item') if request.method == 'PUT' else data
    if request.method in {'POST', 'PUT'}:
        if not isinstance(product, dict):
            return jsonify({"message": "商品資料格式錯誤"}), 400
        paypal_url = str(product.get('paypalUrl', '')).strip()
        if paypal_url and not is_paypal_url(paypal_url):
            return jsonify({"message": "付款連結必須是 PayPal 官方 HTTPS 網址"}), 400
        if paypal_url:
            product['paypalUrl'] = paypal_url
        else:
            product.pop('paypalUrl', None)
    if request.method == 'POST':
        db['products'][category].append(product)
    elif request.method == 'PUT': # 編輯
        idx = data.get('index')
        if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < len(db['products'][category]):
            return jsonify({"message": "商品不存在"}), 404
        db['products'][category][idx] = data.get('item')
    elif request.method == 'DELETE':
        idx = data.get('index')
        if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < len(db['products'][category]):
            return jsonify({"message": "商品不存在"}), 404
        db['products'][category].pop(idx)
        
    save_db(db)
    return jsonify({"status": "success"})

@app.route('/api/posts', methods=['GET', 'POST', 'PUT', 'DELETE'])
def handle_posts():
    if request.method != 'GET' and session.get('role') != 'admin':
        return jsonify({"message": "需要管理員權限"}), 403
    db = load_db()
    if request.method == 'GET':
        return jsonify(db['posts'])
    
    data = request.json
    if request.method == 'POST':
        db['posts'].insert(0, data)
    elif request.method == 'PUT':
        idx = data.get('index')
        db['posts'][idx] = data.get('item')
    elif request.method == 'DELETE':
        idx = data.get('index')
        db['posts'].pop(idx)
        
    save_db(db)
    return jsonify({"status": "success"})

@app.route('/api/services', methods=['GET', 'POST', 'PUT', 'DELETE'])
def handle_services():
    if request.method != 'GET' and session.get('role') != 'admin':
        return jsonify({"message": "需要管理員權限"}), 403
    db = load_db()
    if request.method == 'GET':
        return jsonify(db['services'])
    
    data = request.json
    if request.method == 'POST':
        db['services'].append(data)
    elif request.method == 'PUT':
        idx = data.get('index')
        db['services'][idx] = data.get('item')
    elif request.method == 'DELETE':
        idx = data.get('index')
        db['services'].pop(idx)
        
    save_db(db)
    return jsonify({"status": "success"})

@app.route('/api/orders', methods=['GET', 'POST', 'PUT', 'DELETE'])
def handle_orders():
    db = load_db()
    if request.method == 'GET':
        if session.get('role') != 'admin':
            return jsonify({"message": "需要管理員權限"}), 403
        return jsonify(db['orders'])
    if request.method == 'POST' and not session.get('user'):
        return jsonify({"message": "請先登入"}), 401
    if request.method in {'PUT', 'DELETE'} and session.get('role') != 'admin':
        return jsonify({"message": "需要管理員權限"}), 403
    data = request.get_json(silent=True) or {}
    if request.method == 'POST':
        category, index = data.get('category'), data.get('index')
        products = db['products'].get(category, [])
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(products):
            return jsonify({"message": "商品不存在"}), 404
        item = products[index]
        order = {
            "buyer": session['user'],
            "mcId": str(data.get('mcId', ''))[:64],
            "itemName": item.get('name', ''),
            "price": item.get('price', ''),
            "status": "待付款",
            "fulfillmentStatus": "待處理",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        if category.startswith('mc-') and not order['mcId'].strip():
            return jsonify({"message": "請填寫 Minecraft ID"}), 400
        db['orders'].append(order)
    elif request.method == 'PUT':
        idx = data.get('index')
        status = data.get('fulfillmentStatus')
        allowed_statuses = {'待處理', '處理中', '已完成', '已取消'}
        if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < len(db['orders']):
            return jsonify({"message": "訂單不存在"}), 404
        if status not in allowed_statuses:
            return jsonify({"message": "無效的出貨狀態"}), 400
        db['orders'][idx]['fulfillmentStatus'] = status
    elif request.method == 'DELETE':
        idx = data.get('index')
        db['orders'].pop(idx)
        
    save_db(db)
    return jsonify({"status": "success"})

@app.route('/api/my/orders')
@login_required
def my_orders():
    orders = load_db()['orders']
    return jsonify([order for order in orders if order.get('buyer') == session['user']])

@app.route('/api/cart/checkout', methods=['POST'])
@login_required
def checkout_cart():
    body = request.get_json(silent=True) or {}
    requested_items = body.get('items')
    mc_id = str(body.get('mcId', '')).strip()[:64]
    if not isinstance(requested_items, list) or not requested_items or len(requested_items) > 20:
        return jsonify({"message": "購物車需包含 1 到 20 項商品"}), 400
    with DB_LOCK:
        db = load_db()
        validated = []
        needs_mc_id = False
        for entry in requested_items:
            if not isinstance(entry, dict):
                return jsonify({"message": "購物車資料格式錯誤"}), 400
            category, index = entry.get('category'), entry.get('index')
            if not isinstance(category, str) or not isinstance(index, int) or isinstance(index, bool):
                return jsonify({"message": "購物車商品資料錯誤"}), 400
            products = db.get('products', {}).get(category, [])
            if not 0 <= index < len(products):
                return jsonify({"message": "商品已不存在，請更新購物車"}), 409
            product = products[index]
            if entry.get('name') != product.get('name') or entry.get('price') != product.get('price'):
                return jsonify({"message": "商品資料已更新，請更新購物車後再送出"}), 409
            needs_mc_id = needs_mc_id or category.startswith('mc-')
            validated.append((product, category))
        if needs_mc_id and not mc_id:
            return jsonify({"message": "Minecraft 商品請填寫遊戲 ID"}), 400
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        for product, category in validated:
            db['orders'].append({
                "buyer": session['user'],
                "mcId": mc_id if category.startswith('mc-') else f"(一般會員: {session['user']})",
                "itemName": product.get('name', ''),
                "price": product.get('price', ''),
                "status": "待付款",
                "fulfillmentStatus": "待處理",
                "time": created_at,
            })
        save_db(db)
    return jsonify({"status": "success", "count": len(validated)}), 201

# --- PayPal ---

def paypal_token():
    r = requests.post(f"{PAYPAL_API}/v1/oauth2/token", data={"grant_type": "client_credentials"},
                      auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET), timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]

@app.route('/api/paypal/config')
def paypal_config():
    return jsonify({"enabled": bool(PAYPAL_CLIENT_ID and PAYPAL_SECRET),
                    "clientId": PAYPAL_CLIENT_ID, "currency": PAYPAL_CURRENCY})

@app.route('/api/paypal/create-order', methods=['POST'])
@login_required
def paypal_create_order():
    if not (PAYPAL_CLIENT_ID and PAYPAL_SECRET):
        return jsonify({"message": "PayPal 尚未設定"}), 503
    body = request.json or {}
    category, index = body.get('category'), body.get('index')
    items = load_db()['products'].get(category, [])
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(items):
        return jsonify({"message": "商品不存在"}), 404
    item = items[index]
    try:
        amount = Decimal(str(item.get('paypalPrice', ''))).quantize(Decimal('0.01'))
        if amount <= 0:
            raise InvalidOperation
    except InvalidOperation:
        return jsonify({"message": "此商品不支援 PayPal 付款"}), 400
    mc_id = str(body.get('mcId', '')).strip()[:64]
    if category.startswith('mc-') and not mc_id:
        return jsonify({"message": "Minecraft 商品請填寫遊戲 ID"}), 400
    try:
        r = requests.post(f"{PAYPAL_API}/v2/checkout/orders",
                          headers={"Authorization": f"Bearer {paypal_token()}"},
                          json={"intent": "CAPTURE", "purchase_units": [{
                              "description": item['name'][:127],
                              "amount": {"currency_code": PAYPAL_CURRENCY, "value": str(amount)}}]},
                          timeout=15)
        r.raise_for_status()
    except requests.RequestException:
        return jsonify({"message": "無法連線 PayPal"}), 502
    order_id = r.json()["id"]
    PENDING_PAYPAL[order_id] = {
        "buyer": session['user'],
        "orders": [{"mcId": mc_id or f"(一般會員: {session['user']})", "itemName": item['name'],
                    "price": f"{amount} {PAYPAL_CURRENCY}"}],
    }
    return jsonify({"id": order_id})

@app.route('/api/paypal/create-cart-order', methods=['POST'])
@login_required
def paypal_create_cart_order():
    if not (PAYPAL_CLIENT_ID and PAYPAL_SECRET):
        return jsonify({"message": "PayPal 尚未設定"}), 503
    body = request.get_json(silent=True) or {}
    requested_items = body.get('items')
    mc_id = str(body.get('mcId', '')).strip()[:64]
    if not isinstance(requested_items, list) or not requested_items or len(requested_items) > 20:
        return jsonify({"message": "購物車需包含 1 到 20 件商品"}), 400
    if sum(entry.get('quantity', 1) for entry in requested_items if isinstance(entry, dict) and isinstance(entry.get('quantity', 1), int)) > 20:
        return jsonify({"message": "購物車最多 20 件商品"}), 400
    db = load_db()
    validated = []
    paypal_items = []
    total = Decimal('0.00')
    for entry in requested_items:
        if not isinstance(entry, dict):
            return jsonify({"message": "購物車資料格式錯誤"}), 400
        category, index, quantity = entry.get('category'), entry.get('index'), entry.get('quantity', 1)
        if (not isinstance(category, str) or not isinstance(index, int) or isinstance(index, bool)
                or not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= 20):
            return jsonify({"message": "購物車商品資料錯誤"}), 400
        products = db.get('products', {}).get(category, [])
        if not 0 <= index < len(products):
            return jsonify({"message": "商品已不存在，請更新購物車"}), 409
        product = products[index]
        if entry.get('name') != product.get('name') or entry.get('price') != product.get('price'):
            return jsonify({"message": "商品資料已更新，請更新購物車後再送出"}), 409
        try:
            amount = Decimal(str(product.get('paypalPrice', ''))).quantize(Decimal('0.01'))
            if amount <= 0:
                raise InvalidOperation
        except InvalidOperation:
            return jsonify({"message": "購物車商品尚未設定 PayPal 價格"}), 400
        total += amount * quantity
        paypal_items.append({
            "name": product['name'][:127],
            "quantity": str(quantity),
            "unit_amount": {"currency_code": PAYPAL_CURRENCY, "value": str(amount)},
        })
        validated.extend([{"itemName": product['name'], "mcId": mc_id if category.startswith('mc-') else f"(一般會員: {session['user']})",
                           "price": f"{amount} {PAYPAL_CURRENCY}"}] * quantity)
    if any(item.startswith('mc-') for item in [entry['category'] for entry in requested_items]) and not mc_id:
        return jsonify({"message": "Minecraft 商品請填寫遊戲 ID"}), 400
    try:
        r = requests.post(f"{PAYPAL_API}/v2/checkout/orders",
                          headers={"Authorization": f"Bearer {paypal_token()}"},
                          json={"intent": "CAPTURE", "purchase_units": [{
                              "items": paypal_items,
                              "amount": {"currency_code": PAYPAL_CURRENCY, "value": str(total),
                                         "breakdown": {"item_total": {"currency_code": PAYPAL_CURRENCY, "value": str(total)}}}}]},
                          timeout=15)
        r.raise_for_status()
    except requests.RequestException:
        return jsonify({"message": "無法連線 PayPal"}), 502
    PENDING_PAYPAL[r.json()["id"]] = {"buyer": session['user'], "orders": validated}
    return jsonify({"id": r.json()["id"]})

@app.route('/api/paypal/capture-order', methods=['POST'])
@login_required
def paypal_capture_order():
    order_id = (request.json or {}).get('orderID')
    pending = PENDING_PAYPAL.get(order_id)
    if not pending:
        return jsonify({"message": "找不到訂單"}), 404
    if pending['buyer'] != session['user']:
        return jsonify({"message": "無法存取此訂單"}), 403
    try:
        r = requests.post(f"{PAYPAL_API}/v2/checkout/orders/{order_id}/capture",
                          headers={"Authorization": f"Bearer {paypal_token()}"}, timeout=15)
        r.raise_for_status()
    except requests.RequestException:
        return jsonify({"message": "付款確認失敗"}), 502
    if r.json().get("status") != "COMPLETED":
        return jsonify({"message": "付款未完成"}), 400
    PENDING_PAYPAL.pop(order_id, None)
    with DB_LOCK:
        db = load_db()
        for order in pending['orders']:
            db['orders'].append({**order, "buyer": pending['buyer'], "status": "已付款 (PayPal)",
                                 "fulfillmentStatus": "待處理", "paypalId": order_id,
                                 "time": datetime.now().strftime("%Y-%m-%d %H:%M")})
        save_db(db)
    PENDING_PAYPAL.pop(order_id, None)
    return jsonify({"status": "success"})

if __name__ == '__main__':
    load_db()
    app.run(
        host=os.environ.get('HOST', '0.0.0.0'),
        port=int(os.environ.get('PORT', '80')),
        debug=os.environ.get('FLASK_DEBUG', '').lower() == 'true',
    )