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

# 引入 Supabase 與 Cloudinary
from supabase import create_client, Client
import cloudinary
import cloudinary.uploader

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "").lower() == "true",
)

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")

# 初始化 Supabase 連線
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY) if SUPABASE_URL and SUPABASE_SECRET_KEY else None

# 初始化 Cloudinary 連線
cloudinary.config(
    cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key = os.environ.get("CLOUDINARY_API_KEY"),
    api_secret = os.environ.get("CLOUDINARY_API_SECRET")
)

# PayPal 設定
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET = os.environ.get("PAYPAL_SECRET", "")
PAYPAL_CURRENCY = os.environ.get("PAYPAL_CURRENCY", "USD")
PAYPAL_API = ("https://api-m.paypal.com" if os.environ.get("PAYPAL_ENV", "sandbox") == "live"
              else "https://api-m.sandbox.paypal.com")
PENDING_PAYPAL = {}

# --- 資料庫輔助函式（改用 Supabase 替代原本的 database.json） ---
def load_db():
    # 如果還沒設定 Supabase，回傳預設結構避免報錯
    if not supabase:
        return {
            "users": [], "orders": [],
            "posts": [{"title": "歡迎來到蠍瓜網", "date": "2026-06-07", "content": "這是蠍瓜網的第一篇公告，歡迎所有玩家與訪客！"}],
            "services": [{"name": "蠍瓜伺服器客戶端工具", "note": "內含優化模組與快捷連線設定，請直接下載執行。", "url": "server.exe"}],
            "products": {"mc-firework": [], "mc-other": [], "shop-goods": [], "other-games": []}
        }
    
    try:
        # 從 Supabase 的 kv_store 或對應表格抓取資料
        res = supabase.table("app_data").select("data").eq("id", 1).execute()
        if res.data and len(res.data) > 0:
            return res.data[0]["data"]
        else:
            # 初始資料
            initial_data = {
                "users": [],
                "orders": [],
                "posts": [{"title": "歡迎來到蠍瓜網", "date": "2026-06-07", "content": "這是蠍瓜網的第一篇公告，歡迎所有玩家與訪客！"}],
                "services": [{"name": "蠍瓜伺服器客戶端工具", "note": "內含優化模組與快捷連線設定，請直接下載執行。", "url": "server.exe"}],
                "products": {
                    "mc-firework": [
                        {"name": "飛行 III 煙火（大包裝）", "desc": "Minecraft 內使用，鞘翅長途飛行必備！", "price": "23000 伺服幣"},
                        {"name": "飛行 III 煙火（中包裝）", "desc": "Minecraft 內使用，鞘翅飛行加速煙火", "price": "2300 伺服幣"},
                        {"name": "飛行 III 煙火（基礎包）", "desc": "Minecraft 內使用，飛行時間 III", "price": "500 伺服幣"}
                    ],
                    "mc-other": [], "shop-goods": [], "other-games": []
                }
            }
            supabase.table("app_data").upsert({"id": 1, "data": initial_data}).execute()
            return initial_data
    except Exception as e:
        print(f"載入 Supabase 資料失敗: {e}")
        return {"users": [], "orders": [], "posts": [], "services": [], "products": {}}

def save_db(data):
    if not supabase:
        return
    try:
        supabase.table("app_data").upsert({"id": 1, "data": data}).execute()
    except Exception as e:
        print(f"儲存至 Supabase 失敗: {e}")

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

# --- 圖片上傳路由（透過 Cloudinary） ---
@app.route('/api/upload-image', methods=['POST'])
@admin_required
def upload_image():
    if 'image' not in request.files:
        return jsonify({"message": "沒有上傳檔案"}), 400
    file = request.files['image']
    if file.filename == '':
        return jsonify({"message": "沒有選擇檔案"}), 400
    try:
        upload_result = cloudinary.uploader.upload(file)
        image_url = upload_result.get("secure_url")
        return jsonify({"status": "success", "url": image_url})
    except Exception as e:
        return jsonify({"message": f"上傳失敗：{str(e)}"}), 500

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
    elif request.method == 'PUT':
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
    db = load_db()
    for order in pending['orders']:
        db['orders'].append({**order, "buyer": pending['buyer'], "status": "已付款 (PayPal)",
                             "fulfillmentStatus": "待處理", "paypalId": order_id,
                             "time": datetime.now().strftime("%Y-%m-%d %H:%M")})
    save_db(db)
    return jsonify({"status": "success"})

if __name__ == '__main__':
    app.run(
        host=os.environ.get('HOST', '0.0.0.0'),
        port=int(os.environ.get('PORT', '8000')),
        debug=os.environ.get('FLASK_DEBUG', '').lower() == 'true',
    )
