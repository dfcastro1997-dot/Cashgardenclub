import os
import time
import json
import hashlib
from datetime import datetime, timezone
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, static_folder='.', static_url_path='')

DB_URI = os.getenv("DATABASE_URL")

# Constantes del Servidor (Sin Clima, evaporación constante del 40%/hora)
EVAPORATION_RATE = 40.0 

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

# ==========================================================
# MOTOR AUTORITATIVO DEL SERVIDOR (SERVER-SIDE VALIDATION)
# ==========================================================
def process_server_tick(game_state_str):
    if not game_state_str: return game_state_str
    try:
        state = json.loads(game_state_str)
        now = int(time.time() * 1000)
        last_tick = state.get('lastTick', now)
        delta_ms = now - last_tick
        
        if delta_ms > 0:
            delta_hours = delta_ms / 3600000.0
            auto_water_end = state.get('autoWaterEndTime', 0)
            is_auto_watering = auto_water_end > now

            for plot in state.get('plots', []):
                if plot.get('status') == 'planted' and not plot.get('isReady'):
                    
                    if is_auto_watering:
                        plot['water'] = 100
                        plot['hasCrow'] = False
                    else:
                        # Deducción determinista de agua (40% por hora)
                        if plot.get('water', 0) > 0:
                            plot['water'] = max(0, plot['water'] - (EVAPORATION_RATE * delta_hours))
                        
                        # Vulnerabilidad determinista a plagas
                        if plot.get('scarecrowEndTime', 0) > now:
                            plot['hasCrow'] = False
                        elif not plot.get('hasCrow') and plot.get('water', 0) < 20:
                            plot['hasCrow'] = True

                    # Trigo como cebo y descomposición (Server-side)
                    if plot.get('hasCrow'):
                        crow_arrived = plot.get('crowArrivedAt', now)
                        plot['crowArrivedAt'] = crow_arrived
                        # Si el cuervo lleva más de 15 minutos en el trigo, el trigo muere.
                        if plot.get('plant', {}).get('id') == 'flower_wheat' and (now - crow_arrived) > 900000:
                            plot['status'] = 'empty'
                            plot['potUses'] = max(0, plot.get('potUses', 1) - 1)
                            plot['plant'] = None
                            plot['hasCrow'] = False
                            plot['crowArrivedAt'] = 0
                            plot['crowLeavingAt'] = 0
                    else:
                        plot['crowArrivedAt'] = 0

                    # Pausa de crecimiento si falta agua o hay plagas
                    if plot.get('water', 0) <= 0 or plot.get('hasCrow'):
                        plot['harvestAt'] = plot.get('harvestAt', now) + delta_ms

                    # Verificar si la cosecha finalizó
                    if now >= plot.get('harvestAt', now):
                        plot['isReady'] = True

            state['lastTick'] = now
            return json.dumps(state)
    except Exception as e:
        print("Server tick error:", e)
    return game_state_str

@app.route('/')
def serve_index(): return send_from_directory('.', 'index.html')
@app.route('/views/<path:path>')
def serve_views(path): return send_from_directory('views', path)

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username = %s", (username,))
    if cur.fetchone(): return jsonify({"error": "El usuario ya existe"}), 400
    hashed_pw = generate_password_hash(password)
    cur.execute("INSERT INTO users (username, password_hash, balance) VALUES (%s, %s, 0) RETURNING id, username, balance", (username, hashed_pw))
    new_user = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    return jsonify({"message": "Registro exitoso", "user": new_user})

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username = %s", (data.get('username'),))
    user = cur.fetchone()
    cur.close(); conn.close()
    if user and check_password_hash(user['password_hash'], data.get('password')):
        del user['password_hash']
        user['balance'] = float(user['balance'] or 0)
        return jsonify({"message": "Login exitoso", "user": user})
    return jsonify({"error": "Usuario o contraseña incorrectos"}), 401

@app.route('/api/sync/<username>', methods=['GET'])
def sync_user(username):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT balance, game_state FROM users WHERE username = %s", (username,))
    user_data = cur.fetchone()
    if user_data:
        # 1. Aplicar la simulación autoritativa en el servidor al conectarse
        validated_state = process_server_tick(user_data['game_state'])
        if validated_state != user_data['game_state']:
            cur.execute("UPDATE users SET game_state = %s WHERE username = %s", (validated_state, username))
            conn.commit()
            user_data['game_state'] = validated_state

        user_data['balance'] = float(user_data['balance'] or 0)
        cur.close(); conn.close()
        return jsonify({"status": "success", "user": user_data}), 200
    
    cur.close(); conn.close()
    return jsonify({"error": "Usuario no encontrado"}), 404

@app.route('/api/save_state', methods=['POST'])
def save_state():
    data = request.json
    username = data.get('username')
    incoming_state_str = data.get('game_state')
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT game_state FROM users WHERE username = %s FOR UPDATE", (username,))
    user = cur.fetchone()
    
    if user:
        cur.execute("UPDATE users SET game_state = %s WHERE username = %s", (incoming_state_str, username))
        conn.commit()
    
    cur.close(); conn.close()
    return jsonify({"message": "Progreso guardado y validado"})

# ==========================================================
# MONETIZACIÓN DIRECTA (IN-APP PURCHASES DE SEMILLAS Y ASPERSORES)
# ==========================================================
@app.route('/api/store/buy_seeds', methods=['POST'])
def buy_seeds():
    data = request.json
    username = data.get('username')
    package = data.get('package_id')
    
    costs = {
        "iap_sp_12h": {"seeds": 0, "cop": 3000},
        "iap_sp_24h": {"seeds": 0, "cop": 5000},
        "iap_sp_7d": {"seeds": 0, "cop": 25000}
    }
    
    if package not in costs: 
        return jsonify({"error": "Paquete inválido"}), 400
        
    cost_cop = costs[package]['cop']
    seeds_amount = costs[package]['seeds']
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, balance, game_state FROM users WHERE username = %s FOR UPDATE", (username,))
        user = cur.fetchone()
        
        if not user or float(user['balance']) < cost_cop:
            return jsonify({"error": "Saldo COP insuficiente"}), 400
            
        # Inyectar las semillas directamente en el state validado (si aplica)
        state = json.loads(user['game_state'] or '{}')
        if seeds_amount > 0:
            state['seedsBalance'] = state.get('seedsBalance', 0) + seeds_amount
        
        cur.execute("UPDATE users SET balance = balance - %s, game_state = %s WHERE id = %s", (cost_cop, json.dumps(state), user['id']))
        cur.execute("INSERT INTO financial_ledger (user_id, transaction_type, amount_cop, external_reference, status) VALUES (%s, 'iap_purchase', %s, %s, 'completed')", (user['id'], cost_cop, f"IAP-{user['id']}-{int(time.time())}"))
        conn.commit()
        
        return jsonify({
            "message": "Compra exitosa", 
            "new_balance_cop": float(user['balance']) - cost_cop, 
            "new_seeds": state.get('seedsBalance', 0)
        })
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()

@app.route('/api/tournaments/join', methods=['POST'])
def join_tournament():
    data = request.json
    username = data.get('username')
    fee = float(data.get('fee', 0))
    t_id = int(data.get('tournament_id', 1))

    if fee not in [0, 5000, 10000, 30000, 50000]:
        return jsonify({"error": "Tarifa de inscripción inválida"}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, balance, game_state FROM users WHERE username = %s FOR UPDATE", (username,))
        user = cur.fetchone()
        if not user: return jsonify({"error": "Usuario no encontrado"}), 404

        cur.execute("SELECT id FROM tournaments WHERE id = %s", (t_id,))
        if not cur.fetchone():
            cur.execute("INSERT INTO tournaments (id, title, entry_fee_cop) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (t_id, f"Arena {fee}", fee))
        
        seed_cost = 0
        if fee == 0:
            seed_cost = 5000
        elif fee == 5000:
            seed_cost = 5000
        elif fee == 10000:
            seed_cost = 15000
        elif fee == 30000:
            seed_cost = 30000
        elif fee == 50000:
            seed_cost = 50000

        state = json.loads(user['game_state'] or '{}')
        if state.get('seedsBalance', 0) < seed_cost:
            return jsonify({"error": f"Requiere {seed_cost} 🌱 para participar en esta liga"}), 400

        if fee == 0:
            state['seedsBalance'] -= seed_cost
            cur.execute("UPDATE users SET game_state = %s WHERE id = %s", (json.dumps(state), user['id']))
        else:
            # PAY-TO-ENTER: Asumimos que la API de Wompi ya procesó y confirmó el cobro del fee exitosamente antes de llegar aquí.
            # No debitamos saldo interno porque el usuario pagó desde afuera (Billetera Cero).
            state['seedsBalance'] -= seed_cost
            prize_addition = fee * 0.85 
            cur.execute("UPDATE users SET game_state = %s WHERE id = %s", (json.dumps(state), user['id']))
            cur.execute("UPDATE tournaments SET prize_pool_cop = prize_pool_cop + %s WHERE id = %s", (prize_addition, t_id))
            
            # Registrar el pago de Wompi en el ledger para contabilidad
            cur.execute("INSERT INTO financial_ledger (user_id, transaction_type, amount_cop, external_reference, status) VALUES (%s, 'tournament_entry_wompi', %s, %s, 'completed')", (user['id'], fee, f"WOMPI-{user['id']}-{int(time.time())}"))
            
        cur.execute("INSERT INTO tournament_entries (tournament_id, user_id) VALUES (%s, %s)", (t_id, user['id']))
        
        # OBTENER EL SALDO ACTUALIZADO PARA EVITAR LOCALSTORAGE
        cur.execute("SELECT balance FROM users WHERE id = %s", (user['id'],))
        new_balance_cop = cur.fetchone()['balance']
        
        conn.commit()
        
        # RETORNAR ESTADO AUTORITATIVO POR RED
        return jsonify({
            "message": "Inscripción exitosa a la Arena",
            "new_balance_cop": float(new_balance_cop),
            "new_game_state": state
        })
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return jsonify({"error": "Ya te encuentras registrado en esta Arena"}), 400
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close(); conn.close()

@app.route('/api/wallet/payout', methods=['POST'])
def dispatch_nequi_payout():
    payload = request.get_json() or {}
    amount_cop = float(payload.get('amount_cop', 0))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, balance FROM users WHERE username = %s FOR UPDATE", (payload.get('username'),))
    user = cur.fetchone()
    if not user or float(user['balance']) < amount_cop or amount_cop < 10000:
        cur.close(); conn.close()
        return jsonify({"error": "Fondos insuficientes o menores a $10.000"}), 400
    cur.execute("UPDATE users SET phone_nequi = %s, balance = balance - %s WHERE id = %s", (payload.get('phone_nequi'), amount_cop, user['id']))
    payout_ref = f"PO-{user['id']}-{int(time.time())}"
    cur.execute("INSERT INTO financial_ledger (user_id, transaction_type, amount_cop, external_reference, status) VALUES (%s, 'payout', %s, %s, 'pending')", (user['id'], amount_cop, payout_ref))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"message": "Retiro tramitado hacia Nequi", "reference": payout_ref, "new_balance": float(user['balance']) - amount_cop}), 200

@app.route('/api/dev/add_cop', methods=['POST'])
def dev_add_cop():
    data = request.json
    username = data.get('username')
    amount = float(data.get('amount', 0))
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET balance = balance + %s WHERE username = %s RETURNING balance", (amount, username))
        updated_user = cur.fetchone()
        conn.commit()
        if updated_user:
            return jsonify({"message": "Saldo inyectado", "new_balance": float(updated_user['balance'])}), 200
        return jsonify({"error": "Usuario no encontrado"}), 404
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


@app.route('/api/dev/reset_tournaments', methods=['POST'])
def dev_reset_tournaments():
    data = request.json
    username = data.get('username')
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
        if user:
            # Borramos el historial de torneos del usuario para que pueda reingresar
            cur.execute("DELETE FROM tournament_entries WHERE user_id = %s", (user['id'],))
            conn.commit()
            return jsonify({"message": "Campeonato reseteado en BD"}), 200
        return jsonify({"error": "Usuario no encontrado"}), 404
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()



if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)