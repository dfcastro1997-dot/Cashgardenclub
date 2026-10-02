import os
import hashlib
from datetime import datetime, timezone
import requests
from flask import Flask, request, jsonify, send_from_directory
import psycopg2
from psycopg2.extras import RealDictCursor
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, static_folder='.', static_url_path='')

DB_URI = os.getenv("DATABASE_URL")
WOMPI_PUB_KEY = os.getenv("WOMPI_PUBLIC_KEY", "pub_test_...")
WOMPI_PRV_KEY = os.getenv("WOMPI_PRIVATE_KEY", "prv_test_...")
WOMPI_INTEGRITY_SECRET = os.getenv("WOMPI_INTEGRITY_SECRET", "secret_...")

def get_db_connection():
    return psycopg2.connect(DB_URI, cursor_factory=RealDictCursor)

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(50) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            balance NUMERIC(12, 2) DEFAULT 0.00,
            real_balance_cop NUMERIC(12, 2) DEFAULT 0.00,
            phone_nequi VARCHAR(15),
            game_state TEXT
        );
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS tournaments (
            id SERIAL PRIMARY KEY,
            title VARCHAR(100) NOT NULL,
            entry_fee_cop NUMERIC(10, 2) NOT NULL,
            rake_percentage NUMERIC(5, 2) DEFAULT 15.00,
            prize_pool_cop NUMERIC(12, 2) DEFAULT 0.00,
            status VARCHAR(20) DEFAULT 'open'
        );
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS tournament_entries (
            id SERIAL PRIMARY KEY,
            tournament_id INTEGER REFERENCES tournaments(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            final_score NUMERIC(10, 2) DEFAULT 0.00,
            reward_cop NUMERIC(10, 2) DEFAULT 0.00,
            CONSTRAINT unique_entry_per_tournament UNIQUE(tournament_id, user_id)
        );
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS financial_ledger (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
            transaction_type VARCHAR(30) NOT NULL,
            amount_cop NUMERIC(12, 2) NOT NULL,
            external_reference VARCHAR(120) UNIQUE NOT NULL,
            status VARCHAR(20) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );
    ''')
    conn.commit()
    cur.close()
    conn.close()

init_db()

@app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

@app.route('/views/<path:path>')
def serve_views(path):
    return send_from_directory('views', path)

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username')
    password = data.get('password')

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username = %s", (username,))
    if cur.fetchone():
        return jsonify({"error": "El usuario ya existe"}), 400

    hashed_pw = generate_password_hash(password)
    cur.execute("INSERT INTO users (username, password_hash, real_balance_cop) VALUES (%s, %s, 0) RETURNING id, username, real_balance_cop as balance", (username, hashed_pw))
    new_user = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"message": "Registro exitoso", "user": new_user})

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username = %s", (username,))
    user = cur.fetchone()
    cur.close()
    conn.close()

    if user and check_password_hash(user['password_hash'], password):
        del user['password_hash']
        user['balance'] = float(user['real_balance_cop'] or 0)
        return jsonify({"message": "Login exitoso", "user": user})
    return jsonify({"error": "Usuario o contraseña incorrectos"}), 401

@app.route('/api/save_state', methods=['POST'])
def save_state():
    data = request.json
    username = data.get('username')
    game_state = data.get('game_state')
    balance = data.get('balance', 0)

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET game_state = %s, real_balance_cop = %s WHERE username = %s", (game_state, balance, username))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"message": "Progreso guardado"})

@app.route('/api/sync/<username>', methods=['GET'])
def sync_user(username):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT real_balance_cop as balance, game_state FROM users WHERE username = %s", (username,))
    user_data = cur.fetchone()
    cur.close()
    conn.close()

    if user_data:
        user_data['balance'] = float(user_data['balance'] or 0)
        return jsonify({"status": "success", "user": user_data}), 200
    return jsonify({"error": "Usuario no encontrado"}), 404

# --- NUEVOS ENDPOINTS DE MONETIZACIÓN (WOMPI & TORNEOS) ---

@app.route('/api/wallet/payout', methods=['POST'])
def dispatch_nequi_payout():
    payload = request.get_json() or {}
    username = payload.get('username')
    amount_cop = float(payload.get('amount_cop', 0))
    phone_nequi = payload.get('phone_nequi')

    if amount_cop < 10000:
        return jsonify({"error": "El retiro mínimo a Nequi es de $10.000 COP"}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, real_balance_cop FROM users WHERE username = %s FOR UPDATE", (username,))
    user = cur.fetchone()

    if not user or float(user['real_balance_cop']) < amount_cop:
        cur.close(); conn.close()
        return jsonify({"error": "Fondos insuficientes"}), 400

    # Actualizar cuenta Nequi
    cur.execute("UPDATE users SET phone_nequi = %s, real_balance_cop = real_balance_cop - %s WHERE id = %s", (phone_nequi, amount_cop, user['id']))

    payout_ref = f"PO-{user['id']}-{int(datetime.now(timezone.utc).timestamp())}"
    cur.execute("""
        INSERT INTO financial_ledger (user_id, transaction_type, amount_cop, external_reference, status)
        VALUES (%s, 'payout', %s, %s, 'pending')
    """, (user['id'], amount_cop, payout_ref))

    conn.commit()
    cur.close(); conn.close()

    # Integración con API de Pagos a Terceros Wompi (Descomentar en Producción)
    # headers = {"Authorization": f"Bearer {WOMPI_PRV_KEY}"}
    # wompi_data = {"target_type": "NEQUI", "target_number": phone_nequi, "amount_in_cents": int(amount_cop * 100)}
    # requests.post("https://production.wompi.co/v1/payouts", json=wompi_data, headers=headers)

    return jsonify({"message": "Retiro tramitado correctamente hacia Nequi", "reference": payout_ref, "new_balance": float(user['real_balance_cop']) - amount_cop}), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)