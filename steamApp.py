from flask import Flask, request, jsonify
import requests, pymysql, os, time, random
from datetime import datetime, timedelta

app = Flask(__name__)

# ----------------------------
# Configuration
# ----------------------------
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASS = os.getenv("MYSQL_PASS", "")
MYSQL_DB   = os.getenv("MYSQL_DB", "steamapp")
CSFLOAT_API_KEY = os.getenv("CSFLOAT_API_KEY")  # Optional

# ----------------------------
# Database
# ----------------------------
def get_db():
    return pymysql.connect(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASS,
        database=MYSQL_DB,
        autocommit=True
    )

def get_cached_value(db, steamid):
    with db.cursor() as c:
        c.execute("SELECT value FROM inventory_values WHERE steamid=%s AND expires_at > NOW()", (steamid,))
        row = c.fetchone()
        return row[0] if row else None

def set_cached_value(db, steamid, value, ttl=300):
    expires = datetime.utcnow() + timedelta(seconds=ttl)
    with db.cursor() as c:
        c.execute("""
            REPLACE INTO inventory_values (steamid, value, expires_at)
            VALUES (%s, %s, %s)
        """, (steamid, value, expires))

def get_item_cache(db, market_hash):
    with db.cursor() as c:
        c.execute("SELECT price FROM item_prices WHERE market_hash=%s AND expires_at > NOW()", (market_hash,))
        row = c.fetchone()
        return row[0] if row else None

def set_item_cache(db, market_hash, price, ttl=3600):
    expires = datetime.utcnow() + timedelta(seconds=ttl)
    with db.cursor() as c:
        c.execute("""
            REPLACE INTO item_prices (market_hash, price, expires_at)
            VALUES (%s, %s, %s)
        """, (market_hash, price, expires))

# ----------------------------
# Utilities
# ----------------------------
def extract_steamid(url):
    try:
        if "/profiles/" in url:
            return url.split("/profiles/")[1].split("/")[0]
        elif "/id/" in url:
            return url.split("/id/")[1].split("/")[0]
    except Exception:
        return None

def safe_request(url, headers=None, retries=3, backoff=1.5):
    """Handles retries and rate limiting gracefully."""
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                time.sleep(backoff * (attempt + 1))
            else:
                break
        except requests.RequestException:
            time.sleep(backoff * (attempt + 1))
    return None

# ----------------------------
# Price Functions
# ----------------------------
def get_csfloat_price(market_hash):
    """Fetch price from CSFloat if API key is available."""
    headers = {"Authorization": f"Bearer {CSFLOAT_API_KEY}"} if CSFLOAT_API_KEY else {}
    url = f"https://api.csfloat.com/api/v1/listings?search={requests.utils.quote(market_hash)}"
    data = safe_request(url, headers=headers)
    if data and isinstance(data, list) and len(data) > 0:
        price = data[0].get("price", 0)
        try:
            return float(price) / 100.0  # Convert cents to USD
        except Exception:
            return 0.0
    return 0.0

def get_steam_market_price(market_hash):
    """Fetch price from Steam Community Market (no API key required)."""
    url = f"https://steamcommunity.com/market/priceoverview/?appid=730&currency=1&market_hash_name={requests.utils.quote(market_hash)}"
    data = safe_request(url)
    if data and data.get("success"):
        price_str = data.get("lowest_price") or data.get("median_price")
        if price_str:
            price = price_str.replace("$", "").replace(",", "").strip()
            try:
                return float(price)
            except ValueError:
                return 0.0
    return 0.0

def get_item_price(market_hash, db):
    """Decide which pricing source to use (CSFloat if available, else Steam Market)."""
    price = get_item_cache(db, market_hash)
    if price is not None:
        return price

    if CSFLOAT_API_KEY:
        price = get_csfloat_price(market_hash)
    else:
        price = get_steam_market_price(market_hash)

    set_item_cache(db, market_hash, price)
    time.sleep(random.uniform(0.3, 0.8))  # avoid rate limits
    return price

# ----------------------------
# Routes
# ----------------------------
@app.route("/value", methods=["POST"])
def value():
    trade_url = request.form.get("trade_url") or (request.json and request.json.get("trade_url"))
    steamid = extract_steamid(trade_url)
    if not steamid:
        return jsonify({"error": "Invalid URL"}), 400

    detailed = request.args.get("detailed", "false").lower() == "true"
    db = get_db()

    # Check cached total
    cached = get_cached_value(db, steamid)
    if cached is not None and not detailed:
        return jsonify({"steamid": steamid, "total": float(cached), "cached": True})

    # Fetch Steam inventory
    inv_url = f"https://steamcommunity.com/inventory/{steamid}/730/2?l=english&count=5000"
    inv = safe_request(inv_url)
    if not inv or "descriptions" not in inv:
        return jsonify({"error": "Failed to fetch inventory"}), 502

    items = inv.get("descriptions", [])
    if not items:
        return jsonify({"error": "No items found"}), 404

    total_value = 0.0
    item_details = []

    for item in items:
        market_hash = item.get("market_hash_name")
        if not market_hash:
            continue
        price = get_item_price(market_hash, db)
        total_value += price

        if detailed:
            item_details.append({
                "name": market_hash,
                "price_usd": round(price, 2)
            })

    set_cached_value(db, steamid, total_value)

    response = {
        "steamid": steamid,
        "total": round(total_value, 2),
        "cached": False,
        "source": "CSFloat" if CSFLOAT_API_KEY else "Steam Market"
    }

    if detailed:
        response["items"] = sorted(item_details, key=lambda x: x["price_usd"], reverse=True)

    return jsonify(response)

@app.route("/health")
def health():
    return "OK", 200

# ----------------------------
# Run
# ----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
