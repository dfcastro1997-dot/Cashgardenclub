import os
import time
import json
import hashlib
import uuid # <-- Añadido para la generación de tokens
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
            game_state TEXT,
            session_token VARCHAR(120) -- <-- NUEVA COLUMNA PARA TOKEN ÚNICO
        );
    ''')
    # Inyectar columna si la tabla ya existía de antes:
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS session_token VARCHAR(120);")
    
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

            any_plant_infected = any(
                p.get('status') == 'planted' and p.get('hasCrow') 
                for p in state.get('plots', [])
            )

            for plot in state.get('plots', []):
                if plot.get('status') == 'planted':
                    plant_id = plot.get('plant', {}).get('id') if plot.get('plant') else None
                    
                    min_safe = 20
                    max_safe = 100
                    if plant_id == 'flower_wheat': min_safe, max_safe = 60, 100
                    elif plant_id == 'flower_small': min_safe, max_safe = 30, 90
                    elif plant_id == 'flower_big': min_safe, max_safe = 50, 70
                    elif plant_id == 'flower_cactus': min_safe, max_safe = 20, 80

                    if not plot.get('isReady'):
                        if is_auto_watering:
                            plot['water'] = max_safe if plant_id != 'flower_wheat' else 100
                            plot['hasCrow'] = False
                        else:
                            evap_rate = 10.0 if plant_id == 'flower_cactus' else 40.0
                            
                            if plot.get('water', 0) > 0:
                                plot['water'] = max(0, plot['water'] - (evap_rate * delta_hours))
                            
                            if plot.get('hasCrow') and plot.get('crowLeavingAt', 0) > 0 and now >= plot.get('crowLeavingAt'):
                                plot['hasCrow'] = False
                                plot['crowLeavingAt'] = 0
                                if plot.get('water', 0) > max_safe:
                                    plot['water'] = max_safe

                        if plot.get('scarecrowEndTime', 0) > now:
                            plot['hasCrow'] = False
                        elif not plot.get('hasCrow') and not is_auto_watering:
                            if plot.get('water', 0) < min_safe:
                                plot['hasCrow'] = True
                            elif plot.get('water', 0) > max_safe:
                                plot['hasCrow'] = True
                            elif any_plant_infected:
                                plot['hasCrow'] = True

                        if plot.get('hasCrow'):
                            crow_arrived = plot.get('crowArrivedAt', now)
                            plot['crowArrivedAt'] = crow_arrived
                            if plant_id == 'flower_wheat' and (now - crow_arrived) > 900000:
                                plot['status'] = 'empty'
                                plot['potUses'] = max(0, plot.get('potUses', 1) - 1)
                                plot['plant'] = None
                                plot['hasCrow'] = False
                                plot['crowArrivedAt'] = 0
                                plot['crowLeavingAt'] = 0
                        else:
                            plot['crowArrivedAt'] = 0

                        if plot.get('water', 0) <= 0 or plot.get('hasCrow'):
                            plot['harvestAt'] = plot.get('harvestAt', now) + delta_ms

                    if now >= plot.get('harvestAt', now):
                        plot['isReady'] = True
                        
                        growth_time = plot.get('harvestAt', now) - plot.get('plantedAt', now)
                        if growth_time > 0 and now >= (plot.get('harvestAt', now) + growth_time):
                            plot['isSpoiled'] = True
                            plot['hasCrow'] = True

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
    token = str(uuid.uuid4()) # GENERAR TOKEN ÚNICO
    
    cur.execute("INSERT INTO users (username, password_hash, balance, session_token) VALUES (%s, %s, 0, %s) RETURNING id, username, balance, session_token", (username, hashed_pw, token))
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
    if user and check_password_hash(user['password_hash'], data.get('password')):
        token = str(uuid.uuid4()) # NUEVO TOKEN (INVALIDA OTROS DISPOSITIVOS)
        cur.execute("UPDATE users SET session_token = %s WHERE id = %s", (token, user['id']))
        conn.commit()
        
        del user['password_hash']
        user['balance'] = float(user['balance'] or 0)
        user['session_token'] = token
        
        cur.close(); conn.close()
        return jsonify({"message": "Login exitoso", "user": user})
    
    cur.close(); conn.close()
    return jsonify({"error": "Usuario o contraseña incorrectos"}), 401

@app.route('/api/sync/<username>', methods=['GET'])
def sync_user(username):
    token = request.args.get('token')
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT balance, game_state, session_token FROM users WHERE username = %s", (username,))
    user_data = cur.fetchone()
    if user_data:
        # VALIDACIÓN DE SESIÓN ANTI-DUPLICADOS
        if user_data.get('session_token') and user_data['session_token'] != token:
            cur.close(); conn.close()
            return jsonify({"error": "Sesión expirada"}), 401
            
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
    token = data.get('session_token')
    incoming_state_str = data.get('game_state')
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT game_state, session_token FROM users WHERE username = %s FOR UPDATE", (username,))
    user = cur.fetchone()
    
    if user:
        # EVITA SOBREESCRITURA DESDE DISPOSITIVO INVÁLIDO
        if user.get('session_token') and user['session_token'] != token:
            cur.close(); conn.close()
            return jsonify({"error": "Múltiples sesiones detectadas"}), 401
            
        cur.execute("UPDATE users SET game_state = %s WHERE username = %s", (incoming_state_str, username))
        conn.commit()
    
    cur.close(); conn.close()
    return jsonify({"message": "Progreso guardado y validado"})

@app.route('/api/store/buy_practice', methods=['POST'])
def buy_practice():
    data = request.json
    username = data.get('username')
    token = data.get('session_token')
    cost = int(data.get('cost', 500))
    amount = int(data.get('amount', 5))
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, game_state, session_token FROM users WHERE username = %s FOR UPDATE", (username,))
        user = cur.fetchone()
        
        if not user: return jsonify({"error": "Usuario no encontrado"}), 404
        if user.get('session_token') and user['session_token'] != token:
            cur.close(); conn.close()
            return jsonify({"error": "Sesión inválida"}), 401
            
        state = json.loads(user['game_state'] or '{}')
        seeds_balance = state.get('seedsBalance', 0)
        
        if seeds_balance < cost:
            return jsonify({"error": "No tienes Semillas (🌱) suficientes para comprar el pase."}), 400
            
        state['seedsBalance'] = seeds_balance - cost
        state['practiceTokens'] = state.get('practiceTokens', 0) + amount
        
        cur.execute("UPDATE users SET game_state = %s WHERE id = %s", (json.dumps(state), user['id']))
        conn.commit()
        
        return jsonify({
            "message": "Prácticas adquiridas con éxito", 
            "new_seeds": state['seedsBalance'],
            "new_tokens": state['practiceTokens']
        })
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()

@app.route('/api/store/buy_seeds', methods=['POST'])
def buy_seeds():
    data = request.json
    username = data.get('username')
    token = data.get('session_token')
    package = data.get('package_id')
    
    costs = {
        "iap_sp_12h": {"seeds": 0, "cop": 2000},
        "iap_sp_24h": {"seeds": 0, "cop": 3500},
        "iap_sp_7d": {"seeds": 0, "cop": 15000},
        "exc_10k": {"seeds": 4250, "cop": 10000},
        "exc_25k": {"seeds": 11000, "cop": 25000},
        "exc_50k": {"seeds": 22500, "cop": 50000}
    }
    
    if package not in costs: return jsonify({"error": "Paquete inválido"}), 400
        
    cost_cop = costs[package]['cop']
    seeds_amount = costs[package]['seeds']
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, balance, game_state, session_token FROM users WHERE username = %s FOR UPDATE", (username,))
        user = cur.fetchone()
        
        if not user or float(user['balance']) < cost_cop:
            return jsonify({"error": "Saldo COP insuficiente. Completa ofertas o torneos para recargar."}), 400
        if user.get('session_token') and user['session_token'] != token:
            cur.close(); conn.close()
            return jsonify({"error": "Sesión inválida"}), 401
            
        state = json.loads(user['game_state'] or '{}')
        if seeds_amount > 0:
            state['seedsBalance'] = state.get('seedsBalance', 0) + seeds_amount
        
        cur.execute("UPDATE users SET balance = balance - %s, game_state = %s WHERE id = %s", (cost_cop, json.dumps(state), user['id']))
        cur.execute("INSERT INTO financial_ledger (user_id, transaction_type, amount_cop, external_reference, status) VALUES (%s, 'iap_purchase', %s, %s, 'completed')", (user['id'], cost_cop, f"IAP-{user['id']}-{int(time.time())}"))
        conn.commit()
        
        return jsonify({
            "message": "Transacción exitosa", 
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
    token = data.get('session_token')
    fee = float(data.get('fee', 0))
    t_id = int(data.get('tournament_id', 1))

    if fee not in [0, 5000, 10000, 30000, 50000]: return jsonify({"error": "Tarifa de inscripción inválida"}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, balance, game_state, session_token FROM users WHERE username = %s FOR UPDATE", (username,))
        user = cur.fetchone()
        if not user: return jsonify({"error": "Usuario no encontrado"}), 404
        if user.get('session_token') and user['session_token'] != token:
            cur.close(); conn.close()
            return jsonify({"error": "Sesión inválida"}), 401

        cur.execute("SELECT id FROM tournaments WHERE id = %s", (t_id,))
        if not cur.fetchone():
            cur.execute("INSERT INTO tournaments (id, title, entry_fee_cop) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (t_id, f"Arena {fee}", fee))
        
        seed_cost = 0
        if fee == 0: seed_cost = 5000
        elif fee == 5000: seed_cost = 5000
        elif fee == 10000: seed_cost = 15000
        elif fee == 30000: seed_cost = 30000
        elif fee == 50000: seed_cost = 50000

        state = json.loads(user['game_state'] or '{}')
        if state.get('seedsBalance', 0) < seed_cost:
            return jsonify({"error": f"Requiere {seed_cost} 🌱 para participar en esta liga"}), 400

        if fee == 0:
            state['seedsBalance'] -= seed_cost
            cur.execute("UPDATE users SET game_state = %s WHERE id = %s", (json.dumps(state), user['id']))
        else:
            state['seedsBalance'] -= seed_cost
            prize_addition = fee * 0.85 
            cur.execute("UPDATE users SET game_state = %s WHERE id = %s", (json.dumps(state), user['id']))
            cur.execute("UPDATE tournaments SET prize_pool_cop = prize_pool_cop + %s WHERE id = %s", (prize_addition, t_id))
            cur.execute("INSERT INTO financial_ledger (user_id, transaction_type, amount_cop, external_reference, status) VALUES (%s, 'tournament_entry_wompi', %s, %s, 'completed')", (user['id'], fee, f"WOMPI-{user['id']}-{int(time.time())}"))
            
        cur.execute("INSERT INTO tournament_entries (tournament_id, user_id) VALUES (%s, %s)", (t_id, user['id']))
        
        cur.execute("SELECT balance FROM users WHERE id = %s", (user['id'],))
        new_balance_cop = cur.fetchone()['balance']
        conn.commit()
        
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
    token = payload.get('session_token')
    amount_cop = float(payload.get('amount_cop', 0))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, balance, session_token FROM users WHERE username = %s FOR UPDATE", (payload.get('username'),))
    user = cur.fetchone()
    if not user or float(user['balance']) < amount_cop or amount_cop < 10000:
        cur.close(); conn.close()
        return jsonify({"error": "Fondos insuficientes o menores a $10.000"}), 400
    if user.get('session_token') and user['session_token'] != token:
        cur.close(); conn.close()
        return jsonify({"error": "Sesión inválida"}), 401
        
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