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
# Agrega BITLABS_SECRET_KEY en las variables de entorno de Render
BITLABS_SECRET = os.getenv("BITLABS_SECRET_KEY", "glrhMlnWAzlo5eOYb2hUcNnEniiG4fnG")

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

# --- NUEVO WEBHOOK PARA BITLABS (S2S POSTBACK) ---
@app.route('/api/webhook/bitlabs', methods=['GET', 'POST'])
def bitlabs_webhook():
    try:
        # Obtener parámetros (BitLabs los puede enviar vía GET o POST según configuración)
        data = request.args if request.method == 'GET' else (request.json or request.form)
        
        uid = data.get('uid')
        val = data.get('val') # Valor / Tréboles acreditados
        
        if not uid or not val:
            return jsonify({"error": "Parámetros faltantes"}), 400
            
        reward_amount = int(float(val))

        # Opcional: Validación de Firma HMAC-SHA256 (Si la activas en el panel de BitLabs)
        signature = request.headers.get('X-Bitlabs-Signature')
        if signature:
            computed_sig = hmac.new(
                BITLABS_SECRET.encode('utf-8'),
                request.data or request.query_string,
                hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(computed_sig, signature):
                return jsonify({"error": "Firma inválida"}), 403

        # Actualizar saldo en la base de datos
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

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)