from flask import Flask, request, jsonify, send_from_directory
import psycopg2
from psycopg2.extras import RealDictCursor
from werkzeug.security import generate_password_hash, check_password_hash
import os
import hmac
import hashlib

app = Flask(__name__, static_folder='.', static_url_path='')

# Variables de entorno
DB_URI = os.getenv("DATABASE_URL")
# Llave secreta de BitLabs extraída de tu panel
BITLABS_SECRET = os.getenv("BITLABS_SECRET_KEY", "glrhMlnWAzlo5eOYb2hUcNnEniiG4fnG")
# Llave secreta de TapResearch (Secret Key del Postback)
TAPRESEARCH_SECRET = os.getenv("TAPRESEARCH_SECRET_KEY", "tu_llave_secreta_tapresearch_aqui")
# Llave secreta de CPX Research (Secure Hash)
CPX_SECRET = os.getenv("CPX_SECRET_KEY", "tu_hash_secret_cpx_aqui")

def get_db_connection():
    return psycopg2.connect(DB_URI, cursor_factory=RealDictCursor)

# Inicializar tabla de usuarios si no existe
def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(50) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            balance INTEGER DEFAULT 150
        );
    ''')
    cur.execute('''
        DO $$ 
        BEGIN 
            BEGIN
                ALTER TABLE users ADD COLUMN game_state TEXT;
            EXCEPTION
                WHEN duplicate_column THEN RAISE NOTICE 'column game_state already exists';
            END;
        END;
        $$
    ''')
    conn.commit()
    cur.close()
    conn.close()

init_db()

# Rutas para servir el Frontend
@app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

@app.route('/views/<path:path>')
def serve_views(path):
    return send_from_directory('views', path)

# Endpoints de la API
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
    cur.execute("INSERT INTO users (username, password_hash) VALUES (%s, %s) RETURNING id, username, balance, game_state", (username, hashed_pw))
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
        return jsonify({"message": "Login exitoso", "user": user})
    
    return jsonify({"error": "Usuario o contraseña incorrectos"}), 401

@app.route('/api/save_state', methods=['POST'])
def save_state():
    data = request.json
    username = data.get('username')
    game_state = data.get('game_state')
    balance = data.get('balance', 150)

    if not username:
        return jsonify({"error": "Usuario requerido"}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET game_state = %s, balance = %s WHERE username = %s", (game_state, balance, username))
    conn.commit()
    cur.close()
    conn.close()
    
    return jsonify({"message": "Progreso guardado en la nube"})

# --- WEBHOOK PARA BITLABS (S2S POSTBACK) ---
@app.route('/api/webhook/bitlabs', methods=['GET', 'POST'])
def bitlabs_webhook():
    try:
        data = request.args if request.method == 'GET' else (request.json or request.form or request.args)
        
        uid = data.get('uid') or data.get('user_id') or data.get('user')
        val = data.get('val') or data.get('amount') or data.get('reward')
        
        if not uid or not val:
            return jsonify({"status": "OK", "message": "Webhook activo y funcional."}), 200
            
        reward_amount = int(float(val))

        signature = request.headers.get('X-Bitlabs-Signature')
        if signature:
            computed_sig = hmac.new(
                BITLABS_SECRET.encode('utf-8'),
                request.data or request.query_string,
                hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(computed_sig, signature):
                return jsonify({"error": "Firma inválida"}), 403

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE users SET balance = balance + %s WHERE username = %s", (reward_amount, uid))
        conn.commit()
        cur.close()
        conn.close()

        return jsonify({"status": "SUCCESS", "message": f"Acreditados {reward_amount} a {uid}"}), 200

    except Exception as e:
        print(f"Error procesando webhook de BitLabs: {e}")
        return jsonify({"error": "Error interno"}), 500

# --- WEBHOOK PARA TAPRESEARCH (POSTBACK) ---
@app.route('/api/webhook/tapresearch', methods=['GET'])
def tapresearch_webhook():
    try:
        uid = request.args.get('user_identifier')
        reward = request.args.get('reward')
        tx_id = request.args.get('transaction_identifier')
        hash_signature = request.args.get('hash')

        if not uid or not reward:
             return jsonify({"status": "OK", "message": "Webhook de TapResearch activo"}), 200

        if hash_signature:
            message = f"{tx_id}:{reward}:{TAPRESEARCH_SECRET}"
            computed_hash = hashlib.md5(message.encode('utf-8')).hexdigest()
            if computed_hash != hash_signature:
                return jsonify({"error": "Firma inválida. Hash no coincide."}), 403

        reward_amount = int(float(reward))

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE users SET balance = balance + %s WHERE username = %s", (reward_amount, uid))
        conn.commit()
        cur.close()
        conn.close()

        return jsonify({"status": "SUCCESS", "message": f"TapResearch: Acreditados {reward_amount} a {uid}"}), 200

    except Exception as e:
        print(f"Error procesando webhook de TapResearch: {e}")
        return jsonify({"error": "Error interno"}), 500

# --- NUEVO: WEBHOOK PARA CPX RESEARCH (POSTBACK) ---
@app.route('/api/webhook/cpx', methods=['GET'])
def cpx_webhook():
    try:
        status = request.args.get('status')
        trans_id = request.args.get('trans_id')
        user_id = request.args.get('ext_user_id')
        amount = request.args.get('amount_local')
        hash_signature = request.args.get('hash')

        if not user_id or not amount:
            return jsonify({"status": "OK", "message": "Webhook de CPX activo"}), 200

        if hash_signature:
            message = f"{trans_id}-{CPX_SECRET}"
            computed_hash = hashlib.md5(message.encode('utf-8')).hexdigest()
            if computed_hash != hash_signature:
                return jsonify({"error": "Firma inválida CPX"}), 403

        if status == '1':
            reward_amount = int(float(amount))
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE users SET balance = balance + %s WHERE username = %s", (reward_amount, user_id))
            conn.commit()
            cur.close()
            conn.close()

        return jsonify({"status": "success", "message": f"CPX: Acreditados {amount} a {user_id}"}), 200

    except Exception as e:
        print(f"Error procesando webhook de CPX: {e}")
        return jsonify({"error": "Error interno"}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)