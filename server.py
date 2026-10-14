import os
import time
import json
import hashlib
import uuid
import requests
from datetime import datetime, timezone, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from flask_socketio import SocketIO, join_room, emit

app = Flask(__name__, static_folder='.', static_url_path='')
socketio = SocketIO(app, cors_allowed_origins="*")

DB_URI = os.getenv("DATABASE_URL")

if DB_URI and DB_URI.startswith("postgres://"):
    DB_URI = DB_URI.replace("postgres://", "postgresql://", 1)
    
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "supersecreto123")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8905492002:AAHGxqlBtlTXRcso66at_cMjShQECGQpbwA")
WOMPI_EVENTS_SECRET = os.getenv("WOMPI_EVENTS_SECRET", "test_events_YCoAJd13MYktS6RQGpt1kpZfEIgeZwQV")
WOMPI_PRV_KEY = os.getenv("WOMPI_PRV_KEY", "prv_test_dFZ8LK0A0cYV8brLyHjtQ5KSHBjB86e9")

# --- AÑADE ESTO (Es tu Secreto de Integridad de Wompi) ---
WOMPI_INTEGRITY_SECRET = os.getenv("WOMPI_INTEGRITY_SECRET", "test_integrity_wsgH1421JNMDMEVUxvrMD9T9ExaJAmuW")

@app.route('/api/wompi/signature', methods=['POST'])
def generate_wompi_signature():
    data = request.json
    reference = data.get('reference')
    amount_in_cents = str(data.get('amount_in_cents'))
    currency = data.get('currency', 'COP')
    
    if not reference or not amount_in_cents:
        return jsonify({"error": "Faltan parámetros"}), 400
        
    # La firma de Wompi requiere juntar: referencia + monto + moneda + secreto
    raw_string = f"{reference}{amount_in_cents}{currency}{WOMPI_INTEGRITY_SECRET}"
    signature = hashlib.sha256(raw_string.encode('utf-8')).hexdigest()
    
    return jsonify({"signature": signature}), 200

def get_db_connection():
    for _ in range(6):
        try:
            return psycopg2.connect(DB_URI, cursor_factory=RealDictCursor)
        except psycopg2.OperationalError:
            time.sleep(0.5)
    return psycopg2.connect(DB_URI, cursor_factory=RealDictCursor)

@app.route('/api/webhooks/wompi', methods=['POST'])
def wompi_webhook():
    data = request.json
    event_type = data.get('event')
    transaction = data.get('data', {}).get('transaction', {})
    
    if event_type != 'transaction.updated':
        return jsonify({"status": "ignored"}), 200

    status = transaction.get('status')
    reference = transaction.get('reference')
    amount_in_cents = transaction.get('amount_in_cents')
    signature = data.get('signature', {}).get('checksum')

    timestamp = data.get('timestamp', '')
    raw_signature = f"{transaction.get('id')}{status}{amount_in_cents}{timestamp}{WOMPI_EVENTS_SECRET}"
    expected_signature = hashlib.sha256(raw_signature.encode('utf-8')).hexdigest()

    if signature != expected_signature:
        return jsonify({"error": "Firma inválida"}), 403

    if status == 'APPROVED' and reference.startswith('REC-'):
        short_id = reference.split('-')[1] 
        # IMPORTANTE: Descontamos el 5% de mantenimiento en el servidor antes de sumarlo
        gross_amount = amount_in_cents / 100
        net_amount = gross_amount * 0.95

        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("SELECT id FROM financial_ledger WHERE external_reference = %s", (reference,))
        if not cur.fetchone():
            cur.execute("UPDATE users SET balance = balance + %s WHERE short_id = %s RETURNING id", (net_amount, short_id))
            user = cur.fetchone()
            
            if user:
                cur.execute(
                    "INSERT INTO financial_ledger (user_id, transaction_type, amount_cop, external_reference, status) VALUES (%s, %s, %s, %s, %s)",
                    (user['id'], 'deposit', net_amount, reference, 'completed')
                )
                conn.commit()
                
        cur.close(); conn.close()

    return jsonify({"status": "ok"}), 200

@app.route('/api/wallet/payout', methods=['POST'])
def dispatch_nequi_payout():
    payload = request.get_json() or {}
    token = payload.get('session_token')
    amount_cop = float(payload.get('amount_cop', 0))
    phone_nequi = payload.get('phone_nequi')
    
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
        
    cur.execute("UPDATE users SET phone_nequi = %s, balance = balance - %s WHERE id = %s", (phone_nequi, amount_cop, user['id']))
    payout_ref = f"PO-{user['id']}-{int(time.time())}"
    
    headers = {
        "Authorization": f"Bearer {WOMPI_PRV_KEY}",
        "Content-Type": "application/json"
    }
    
    wompi_payload = {
        "amount_in_cents": int(amount_cop * 100),
        "currency": "COP",
        "reference": payout_ref,
        "recipient_type": "NEQUI",
        "recipient_number": phone_nequi 
    }
    
    try:
        response = requests.post("https://sandbox.wompi.co/v1/transfers", json=wompi_payload, headers=headers)
        wompi_data = response.json()
        
        if response.status_code == 201:
            cur.execute("INSERT INTO financial_ledger (user_id, transaction_type, amount_cop, external_reference, status) VALUES (%s, 'payout', %s, %s, 'pending')", (user['id'], amount_cop, payout_ref))
            conn.commit()
            msg = "Retiro en proceso. Llegará a tu Nequi en 24h hábiles."
        else:
            conn.rollback()
            return jsonify({"error": "Error del banco: " + str(wompi_data.get('error', {}).get('messages', ''))}), 500
            
    except Exception as e:
        conn.rollback()
        return jsonify({"error": "Error de conexión con el banco"}), 500
        
    cur.close(); conn.close()
    return jsonify({"message": msg, "reference": payout_ref, "new_balance": float(user['balance']) - amount_cop}), 200

# Función helper para Telegram (Soporta Imágenes)
def send_telegram_msg(chat_id, text, image_url=None):
    if not TELEGRAM_BOT_TOKEN or not chat_id: return
    try:
        if image_url:
            # Si hay imagen, usa el endpoint sendPhoto
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            payload = {"chat_id": chat_id, "photo": image_url, "caption": text}
        else:
            # Si no hay imagen, envía solo texto
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": chat_id, "text": text}
            
        requests.post(url, json=payload, timeout=3)
    except Exception as e:
        print("Telegram error:", e)

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
            session_token VARCHAR(120),
            telegram_chat_id VARCHAR(50)
        );
    ''')
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS session_token VARCHAR(120);")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_chat_id VARCHAR(50);")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS short_id VARCHAR(4) UNIQUE;")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ref_code VARCHAR(5) UNIQUE;")
    
    # --- NUEVA COLUMNA PARA GUARDAR QUIÉN LO INVITÓ ---
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by VARCHAR(5);")

    cur.execute('''
        CREATE TABLE IF NOT EXISTS pvp_matches (
            id SERIAL PRIMARY KEY,
            challenger_id INTEGER REFERENCES users(id),
            target_id INTEGER REFERENCES users(id),
            status VARCHAR(20) DEFAULT 'pending', 
            bet_seeds INTEGER DEFAULT 0,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );
    ''')
    cur.execute("ALTER TABLE pvp_matches ADD COLUMN IF NOT EXISTS match_data TEXT DEFAULT '{}';")
    cur.execute("ALTER TABLE pvp_matches ADD COLUMN IF NOT EXISTS winner_username VARCHAR(50);")

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
    cur.execute('''
        CREATE TABLE IF NOT EXISTS tournament_instances (
            id SERIAL PRIMARY KEY,
            template_id INTEGER NOT NULL,
            entry_fee_cop NUMERIC(10, 2) NOT NULL,
            status VARCHAR(20) DEFAULT 'waiting',
            players_count INTEGER DEFAULT 0,
            max_players INTEGER DEFAULT 10,
            prize_pool_cop NUMERIC(12, 2) DEFAULT 0.00,
            start_time BIGINT DEFAULT 0,
            end_time BIGINT DEFAULT 0
        );
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS tournament_players (
            id SERIAL PRIMARY KEY,
            instance_id INTEGER REFERENCES tournament_instances(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            username VARCHAR(50) NOT NULL,
            current_score NUMERIC(10, 2) DEFAULT 10000.00,
            CONSTRAINT unique_player_instance UNIQUE(instance_id, user_id)
        );
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS tree_state (
            id SERIAL PRIMARY KEY,
            date_str VARCHAR(10) UNIQUE,
            total_donated INTEGER DEFAULT 0,
            daily_goal INTEGER DEFAULT 100000,
            rewards_json TEXT,
            status VARCHAR(20) DEFAULT 'active'
        );
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS tree_donations (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            username VARCHAR(50),
            amount INTEGER DEFAULT 0,
            date_str VARCHAR(10),
            CONSTRAINT unique_user_tree UNIQUE(user_id, date_str)
        );
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS dynamic_plants (
            id VARCHAR(50) PRIMARY KEY,
            category VARCHAR(50),
            name VARCHAR(100),
            image_url VARCHAR(255),
            hp INTEGER,
            atk INTEGER,
            def INTEGER,
            price INTEGER,
            reward INTEGER,
            grow_time_minutes INTEGER
        );
    ''')

    cur.execute("ALTER TABLE tournament_players ADD COLUMN IF NOT EXISTS alchemy_precision NUMERIC(6,3) DEFAULT 0.000;")
    cur.execute("ALTER TABLE tournament_players ADD COLUMN IF NOT EXISTS alchemy_time NUMERIC(5,2) DEFAULT 0.00;")
    cur.execute("ALTER TABLE tree_state ADD COLUMN IF NOT EXISTS donation_count INTEGER DEFAULT 0;")

    cur.execute('''
        CREATE TABLE IF NOT EXISTS tree_config (
            id SERIAL PRIMARY KEY,
            vip_prizes_json TEXT,
            milestones_json TEXT
        );
    ''')
    cur.execute("SELECT id FROM tree_config LIMIT 1")
    if not cur.fetchone():
        default_vip = json.dumps([
            {"rank": 1, "items": [{"id": "flower_moon", "qty": 2}]},
            {"rank": 2, "items": [{"id": "flower_big", "qty": 2}]},
            {"rank": 3, "items": [{"id": "tool_pot_big", "qty": 2}]}
        ])
        default_miles = json.dumps([
            {"pct": 25, "id": "tool_pot_small", "qty": 5},
            {"pct": 50, "id": "tool_water", "qty": 10},
            {"pct": 75, "id": "flower_wheat", "qty": 3},
            {"pct": 100, "id": "flower_bamboo", "qty": 2}
        ])
        cur.execute("INSERT INTO tree_config (id, vip_prizes_json, milestones_json) VALUES (1, %s, %s)", (default_vip, default_miles))

    cur.execute('''
        CREATE TABLE IF NOT EXISTS tournament_config (
            id SERIAL PRIMARY KEY,
            config_json TEXT
        );
    ''')
    cur.execute("SELECT id FROM tournament_config LIMIT 1")
    if not cur.fetchone():
        cur.execute("INSERT INTO tournament_config (id, config_json) VALUES (1, '{}')")

    conn.commit()
    cur.close()
    conn.close()


def get_db_connection():
    # Sistema de reintento para evitar que el servidor se caiga por el límite estricto de Aiven
    for _ in range(6):
        try:
            return psycopg2.connect(DB_URI, cursor_factory=RealDictCursor)
        except psycopg2.OperationalError:
            time.sleep(0.5)
    # Último intento
    return psycopg2.connect(DB_URI, cursor_factory=RealDictCursor)


init_db()

# ==========================================================
# MOTOR AUTORITATIVO DEL SERVIDOR (SERVER-SIDE VALIDATION)
# ==========================================================

def get_season_multiplier(now_ms):
    dt = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
    day = dt.day
    if 1 <= day <= 9: return 0.5   
    elif 10 <= day <= 19: return 1.5 
    elif 20 <= day <= 26: return 1.0 
    else: return 2.0                 

def process_server_tick(game_state_str, chat_id=None):
    import random
    
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
            
            auto_harvest_end = state.get('autoHarvestEndTime', 0)
            is_auto_harvesting = auto_harvest_end > now
            
            season_multiplier = get_season_multiplier(now)

            any_plant_infected = any(
                p.get('status') == 'planted' and p.get('hasCrow') 
                for p in state.get('plots', [])
            )

            contagion_triggered = False

            for plot in state.get('plots', []):
                # Inicialización de banderas para evitar spam de notificaciones en Telegram
                if 'notifyFlags' not in plot: 
                    plot['notifyFlags'] = {'crow': False, 'ready': False, 'water': False, 'spoiled': False}
                flags = plot['notifyFlags']

                # Omitir daño a la planta del torneo si aún está "en espera" (waiting)
                if plot.get('status') in ['tournament_waiting']:
                    continue

                if plot.get('status') == 'tournament':
                    if any_plant_infected and not plot.get('hasCrow') and plot.get('crowLeavingAt', 0) <= now:
                        plot['hasCrow'] = True
                    
                    if plot.get('hasCrow') and plot.get('crowLeavingAt', 0) > 0 and now >= plot.get('crowLeavingAt'):
                        plot['hasCrow'] = False
                        plot['crowLeavingAt'] = 0

                elif plot.get('status') in ['empty', 'pot']:
                    if plot.get('water', 0) > 0:
                        accelerated_evap = 40.0 * 4.0 * season_multiplier # 4 veces más rápido sin planta
                        plot['water'] = max(0, plot['water'] - (accelerated_evap * delta_hours))

                elif plot.get('status') == 'planted':
                    plant_id = plot.get('plant', {}).get('id') if plot.get('plant') else None
                    
                    min_safe = 20
                    max_safe = 100
                    if plant_id == 'flower_wheat': min_safe, max_safe = 60, 100
                    elif plant_id == 'flower_bamboo': min_safe, max_safe = 50, 90
                    elif plant_id == 'flower_small': min_safe, max_safe = 30, 90
                    elif plant_id == 'flower_big': min_safe, max_safe = 50, 70
                    elif plant_id == 'flower_cactus': min_safe, max_safe = 20, 80
                    elif plant_id == 'flower_crystal': min_safe, max_safe = 40, 80
                    elif plant_id == 'flower_moon': min_safe, max_safe = 40, 90
                    elif plant_id == 'flower_solar': min_safe, max_safe = 30, 70
                    elif plant_id == 'flower_neon': min_safe, max_safe = 40, 60

                    # === CORRECCIÓN AUTO-RIEGO ===
                    # Ancla el agua al centro seguro exacto de la planta, asegurando que nunca se ahogue ni se seque
                    if is_auto_watering:
                        safe_mid = min_safe + ((max_safe - min_safe) / 2.0)
                        plot['water'] = safe_mid
                        plot['hasCrow'] = False
                        plot['crowLeavingAt'] = 0

                    if not plot.get('isReady'):
                        if not is_auto_watering:
                            evap_rate = 800.0 if plant_id == 'flower_bamboo' else (10.0 if plant_id == 'flower_cactus' else 40.0)
                            evap_rate = evap_rate * season_multiplier 
                            
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

                        # Periodo de gracia de 10 minutos (600,000 ms) antes de pausar el crecimiento
                        time_with_crow = now - plot.get('crowArrivedAt', now)
                        is_crow_landed = plot.get('hasCrow') and time_with_crow > 600000
                        
                        if plot.get('water', 0) <= 0 or is_crow_landed:
                            plot['harvestAt'] = plot.get('harvestAt', now) + delta_ms

                    # FASE DE COSECHA O ABANDONO
                    if now >= plot.get('harvestAt', now):
                        plot['isReady'] = True
                        
                        if is_auto_harvesting and not plot.get('isSpoiled'):
                            reward = plot.get('plant', {}).get('reward', 0)
                            if reward == 0:
                                if plant_id == 'flower_wheat': reward = 1500
                                elif plant_id == 'flower_bamboo': reward = 6500
                                elif plant_id == 'flower_cactus': reward = 3200
                                elif plant_id == 'flower_small': reward = 4500
                                elif plant_id == 'flower_big': reward = 26000
                                elif plant_id == 'flower_crystal': reward = 10000
                                elif plant_id == 'flower_moon': reward = 55000
                                elif plant_id == 'flower_solar': reward = 5000
                                elif plant_id == 'flower_neon': reward = 12000
                            
                            state['seedsBalance'] = state.get('seedsBalance', 0) + reward
                            plot['potUses'] = plot.get('potUses', 0) + 1
                            max_uses = 5 if plot.get('pot') == 'small' else 10
                            
                            if plot['potUses'] >= max_uses:
                                plot['status'] = 'empty'
                                plot['pot'] = None
                                plot['potUses'] = 0
                            else:
                                plot['status'] = 'pot'
                                
                            plot['plant'] = None
                            # El agua se conserva en la tierra para que se evapore al sol
                            plot['hasCrow'] = False
                            plot['crowLeavingAt'] = 0
                            plot['scarecrowEndTime'] = 0
                            plot['plantedAt'] = 0
                            plot['harvestAt'] = 0
                            plot['isReady'] = False
                            plot['isSpoiled'] = False
                        else:
                            # === CORRECCIÓN: SOLO PENALIZAR POR ABANDONO SI NO TIENE AUTO-RIEGO ===
                            if not is_auto_watering:
                                growth_time = plot.get('harvestAt', now) - plot.get('plantedAt', now)
                                grace_period = growth_time / 2.0  # 50% del tiempo de vida
                                time_overdue = now - (plot.get('harvestAt', now) + grace_period)
                                
                                if time_overdue > 0:
                                    plot['isSpoiled'] = True
                                    plot['hasCrow'] = True
                                    
                                    # Tramos de 15 minutos = 900,000 ms
                                    if time_overdue > 2700000: # 45 mins: 3 cuervos + Colapso
                                        plot['crowsCount'] = 3
                                        plot['rewardMultiplier'] = 0.0 # Pierde el 100% de la ganancia
                                        
                                        # Contagio al siguiente terreno (se procesa fuera del for)
                                        if not plot.get('contagionTriggered'):
                                            plot['contagionTriggered'] = True
                                            contagion_triggered = True
                                            
                                    elif time_overdue > 1800000: # 30 mins: 3 cuervos (-5%/min extra)
                                        plot['crowsCount'] = 3
                                        mins_w_three = (time_overdue - 1800000) / 60000.0
                                        penalty = 0.30 + 0.45 + (mins_w_three * 0.05)
                                        plot['rewardMultiplier'] = max(0.0, 1.0 - penalty)
                                        
                                    elif time_overdue > 900000: # 15 mins: 2 cuervos (-3%/min extra)
                                        plot['crowsCount'] = 2
                                        mins_w_two = (time_overdue - 900000) / 60000.0
                                        penalty = 0.30 + (mins_w_two * 0.03)
                                        plot['rewardMultiplier'] = max(0.0, 1.0 - penalty)
                                        
                                    else: # 0 a 15 mins: 1 cuervo (-1%/min)
                                        plot['crowsCount'] = 1
                                        mins_w_one = time_overdue / 60000.0
                                        penalty = mins_w_one * 0.01
                                        plot['rewardMultiplier'] = max(0.0, 1.0 - penalty)
                    
                    # --- LÓGICA DE NOTIFICACIONES CARISMÁTICAS TELEGRAM ---
                    if chat_id and plot.get('status') == 'planted':
                        plant_name = plot.get('plant', {}).get('name', 'tu plantita')
                        plant_img = plot.get('plant', {}).get('icon', None)
                        p_id = plot['id'] + 1
                        
                        # MENSAJES SUAVES Y TIERNOS PARA CUERVO (Ataque)
                        crow_msgs = [
                            f"🦅 ¡Cua, cua! (Bueno, los cuervos no hacen así, pero entiendes la idea). ¡Un pajarraco me está picoteando en el Terreno {p_id}! 😭 ¡Ven a asustarlo antes de que me quede sin hojitas, por favor! 🥺",
                            f"🦅 ¡Auxilio, granjero/a! 🚨 Un cuervo gruñón aterrizó en la parcela {p_id} y me está mirando con cara de ensalada. ¡Sálvame, soy tu {plant_name} favorita! 🛡️🌱",
                            f"🦅 ¡Uy, qué miedo! 🫣 Hay un intruso con plumas en el Terreno {p_id} molestándome. Necesito a mi héroe (sí, ¡tú!) para que lo espante. 🥺"
                        ]
                        
                        # MENSAJES SUAVES PARA COSECHA LISTA (Felicidad)
                        ready_msgs = [
                            f"✨ ¡Tachán! 🎉 He crecido grande, fuerte y sanita en el Terreno {p_id}. Ya estoy lista para que me coseches y ganes tus semillitas. ¡Ven a verme, me veo genial! 🥰🌻",
                            f"✨ ¡Misión botánica cumplida! 🫡 Soy tu {plant_name} del Terreno {p_id} y ya llegué a mi mejor etapa. ¡Apúrate a recogerme para celebrar juntos! 🥳🌱",
                            f"✨ ¡Brillo más que el sol! 😍 Estoy lista en la parcela {p_id} y llena de recompensas para ti. ¡Ven rapidito a cosecharme, que huelo delicioso! 🧺💖"
                        ]
                        
                        # MENSAJES SUAVES PARA SEQUÍA (Empatía)
                        water_msgs = [
                            f"💧 ¡Tengo mucha sed! 🥺 Estoy en el Terreno {p_id} soñando con un chapuzón. ¿Me regalarías un poquito de agua de tu regadera mágica? 🚿🪴",
                            f"💧 ¡Ay, qué calorcito hace por aquí! 🥵 Tu {plant_name} en la parcela {p_id} se está secando un poquito. ¡Ven a refrescarme para seguir creciendo feliz! 🏜️🌱",
                            f"💧 Glu, glu... oh, espera, ¡no hay agüita! 😢 Mis raíces en el Terreno {p_id} están buscando humedad. ¡Un chorrito me haría la planta más feliz del mundo! 🚰💖"
                        ]
                        
                        # MENSAJES SUAVES PARA PUDRICIÓN (Tristeza tierna)
                        spoiled_msgs = [
                            f"🦠 Sniff, sniff... me he enfermado en el Terreno {p_id}. 🤧 Tengo un hongo molesto porque hubo un problemita con el riego. Ven a limpiarme, ¡te prometo ser más fuerte la próxima vez! 🥺🩹",
                            f"🦠 ¡Achoo! 🤒 Ups, creo que me pasé de humedad en la parcela {p_id} y ahora estoy marchita. ¿Me ayudarías con el espantapájaros? Quiero volver a sonreír. 🪴✨",
                            f"🦠 Oh no, me siento pachuchita. 🥀 Estoy un poco enfermita en el Terreno {p_id}. Tomará 15 minutitos limpiarme, pero te juro que valdrá la pena. ¡No me dejes así, porfis! 🗑️💔"
                        ]
                        
                        if plot.get('hasCrow') and not flags.get('crow'):
                            send_telegram_msg(chat_id, random.choice(crow_msgs), plant_img)
                            flags['crow'] = True
                        elif plot.get('isReady') and not flags.get('ready') and not is_auto_harvesting:
                            send_telegram_msg(chat_id, random.choice(ready_msgs), plant_img)
                            flags['ready'] = True
                        elif plot.get('water', 100) < min_safe and not plot.get('isReady') and not plot.get('hasCrow') and not flags.get('water'):
                            send_telegram_msg(chat_id, random.choice(water_msgs), plant_img)
                            flags['water'] = True
                        elif plot.get('isSpoiled') and not flags.get('spoiled'):
                            send_telegram_msg(chat_id, random.choice(spoiled_msgs), plant_img)
                            flags['spoiled'] = True

                        if not plot.get('hasCrow'): flags['crow'] = False
                        if not plot.get('isReady'): flags['ready'] = False
                        if plot.get('water', 100) >= min_safe: flags['water'] = False
                        if not plot.get('isSpoiled'): flags['spoiled'] = False

            # --- PROCESAR CONTAGIO DEL ÉXODO ---
            # Si una planta colapsó (pasaron 45 min extra), contagia a la siguiente parcela viva.
            if contagion_triggered:
                for p in state.get('plots', []):
                    if p.get('status') == 'planted' and not p.get('hasCrow') and not p.get('isSpoiled'):
                        p['hasCrow'] = True
                        p['crowArrivedAt'] = now
                        break # Solo propaga a 1 planta sana a la vez

            state['lastTick'] = now
            return json.dumps(state)
    except Exception as e:
        print("Server tick error:", e)
    return game_state_str



@app.route('/')
def serve_index(): return send_from_directory('.', 'index.html')
@app.route('/views/<path:path>')
def serve_views(path): return send_from_directory('views', path)
@app.route('/admin.html')
def serve_admin(): return send_from_directory('.', 'admin.html')



@app.route('/api/login', methods=['POST'])
def login():
    import random
    import string
    data = request.json
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username = %s", (data.get('username'),))
    user = cur.fetchone()
    if user and check_password_hash(user['password_hash'], data.get('password')):
        token = str(uuid.uuid4())
        short_id = user.get('short_id')
        ref_code = user.get('ref_code')
        
        # Generar ref_code si es una cuenta antigua que no lo tiene
        if not ref_code:
            while True:
                ref_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
                cur.execute("SELECT id FROM users WHERE ref_code = %s", (ref_code,))
                if not cur.fetchone():
                    break
            cur.execute("UPDATE users SET ref_code = %s WHERE id = %s", (ref_code, user['id']))
            user['ref_code'] = ref_code

        # Reparación para cuentas antiguas sin ID
        if not short_id:
            while True:
                short_id = str(random.randint(1000, 9999))
                cur.execute("SELECT id FROM users WHERE short_id = %s", (short_id,))
                if not cur.fetchone():
                    break
            cur.execute("UPDATE users SET session_token = %s, short_id = %s WHERE id = %s", (token, short_id, user['id']))
        else:
            cur.execute("UPDATE users SET session_token = %s WHERE id = %s", (token, user['id']))
        conn.commit()
        
        del user['password_hash']
        user['balance'] = float(user['balance'] or 0)
        user['session_token'] = token
        user['short_id'] = short_id
        
        cur.close(); conn.close()
        return jsonify({"message": "Login exitoso", "user": user})
    
    cur.close(); conn.close()
    return jsonify({"error": "Usuario o contraseña incorrectos"}), 401


@app.route('/api/sync/<username>', methods=['GET'])
def sync_user(username):
    token = request.args.get('token')
    conn = get_db_connection()
    cur = conn.cursor()

    # --- AÑADIDO: ref_code en la consulta SQL ---
    cur.execute("SELECT id, balance, game_state, session_token, telegram_chat_id, short_id, ref_code FROM users WHERE username = %s", (username,))
    user_data = cur.fetchone()
    
    if user_data:
        import random
        import string
        
        # Generar ref_code si es cuenta antigua sincronizando
        if not user_data.get('ref_code'):
            while True:
                new_ref = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
                cur.execute("SELECT id FROM users WHERE ref_code = %s", (new_ref,))
                if not cur.fetchone():
                    break
            cur.execute("UPDATE users SET ref_code = %s WHERE id = %s", (new_ref, user_data['id']))
            conn.commit()
            user_data['ref_code'] = new_ref

        # Reparación para cuentas antiguas sin ID
        if not user_data.get('short_id'):
            while True:
                new_id = str(random.randint(1000, 9999))
                cur.execute("SELECT id FROM users WHERE short_id = %s", (new_id,))
                if not cur.fetchone():
                    break
            cur.execute("UPDATE users SET short_id = %s WHERE id = %s", (new_id, user_data['id']))
            conn.commit()
            user_data['short_id'] = new_id

        # Buscar si alguien lo está retando en este momento
        cur.execute('''
            SELECT p.id as match_id, u.username as challenger_name, u.short_id as challenger_short_id, p.bet_seeds 
            FROM pvp_matches p JOIN users u ON p.challenger_id = u.id 
            WHERE p.target_id = %s AND p.status = 'pending' LIMIT 1
        ''', (user_data['id'],))
        challenge = cur.fetchone()
        if challenge:
            user_data['pending_challenge'] = challenge

        # Validar token de sesión (Múltiples ventanas)
        if user_data.get('session_token') and user_data['session_token'] != token:
            cur.close()
            conn.close()
            return jsonify({"error": "Sesión expirada"}), 401
            
        # Procesar crecimiento de plantas y plagas
        validated_state = process_server_tick(user_data['game_state'], user_data.get('telegram_chat_id'))
        if validated_state != user_data['game_state']:
            cur.execute("UPDATE users SET game_state = %s WHERE username = %s", (validated_state, username))
            conn.commit()
            user_data['game_state'] = validated_state

        user_data['balance'] = float(user_data['balance'] or 0)
        cur.close()
        conn.close()
        return jsonify({"status": "success", "user": user_data}), 200
    
    cur.close()
    conn.close()
    return jsonify({"error": "Usuario no encontrado"}), 404

@app.route('/api/save_state', methods=['POST'])
def save_state():
    data = request.json
    username = data.get('username')
    token = data.get('session_token')
    incoming_state_str = data.get('game_state')
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, game_state, session_token, telegram_chat_id FROM users WHERE username = %s FOR UPDATE", (username,))
    user = cur.fetchone()
    
    if user:
        if user.get('session_token') and user['session_token'] != token:
            cur.close(); conn.close()
            return jsonify({"error": "Múltiples sesiones detectadas"}), 401
            
        try:
            incoming_state = json.loads(incoming_state_str)
            db_state_raw = user['game_state']
            db_state = json.loads(db_state_raw) if db_state_raw else {}
            
            db_tick = db_state.get('lastTick', 0)
            in_tick = incoming_state.get('lastTick', 0)
            
            if db_tick > in_tick:
                incoming_state['seedsBalance'] = db_state.get('seedsBalance', 0)
                incoming_state['lastTick'] = db_tick
                if 'inventory' in db_state: incoming_state['inventory'] = db_state['inventory']
                if 'arsenal' in db_state: incoming_state['arsenal'] = db_state['arsenal']
                if 'plots' in db_state: incoming_state['plots'] = db_state['plots']
                if 'pending_gift' in db_state: incoming_state['pending_gift'] = db_state['pending_gift']
                # --- SOLUCIÓN: PROTEGER LA FOTO DE PERFIL ---
                if 'profilePic' in db_state: incoming_state['profilePic'] = db_state['profilePic']

            incoming_seeds = incoming_state.get('seedsBalance', 0)
            db_seeds = db_state.get('seedsBalance', 35000 if not db_state_raw else 0)
            
            if incoming_state.get('skipSeedsSave'):
                incoming_state['seedsBalance'] = db_seeds
                del incoming_state['skipSeedsSave']
            else:
                if incoming_seeds > db_seeds + 500000:
                    incoming_state['seedsBalance'] = db_seeds
                
            incoming_state_str = json.dumps(incoming_state)
            validated_state_str = process_server_tick(incoming_state_str, user.get('telegram_chat_id'))
        except Exception:
            validated_state_str = process_server_tick(incoming_state_str, user.get('telegram_chat_id'))
            
        cur.execute("UPDATE users SET game_state = %s WHERE username = %s", (validated_state_str, username))
        conn.commit()
    
    cur.close(); conn.close()
    return jsonify({"message": "Progreso guardado y validado"})


@app.route('/api/telegram/link', methods=['POST'])
def link_telegram():
    data = request.json
    username = data.get('username')
    token = data.get('session_token')
    chat_id = data.get('telegram_chat_id')
    
    if not chat_id or not chat_id.replace('-', '').isdigit():
        return jsonify({"error": "Debes ingresar tu ID Numérico (Ej: 123456789), no tu @usuario. Usa @userinfobot en Telegram."}), 400
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET telegram_chat_id = %s WHERE username = %s AND session_token = %s RETURNING id", (chat_id, username, token))
    user_updated = cur.fetchone()
    
    if user_updated:
        conn.commit()
        cur.close()
        conn.close()
        # MENSAJE DE BIENVENIDA ACTUALIZADO
        send_telegram_msg(chat_id, "🌱 ¡Yupi, conexión exitosa! Soy el asistente de CashGarden. Prometo cuidar tus plantitas desde aquí y avisarte rápidamente si necesitan mimos o ayuda. 👩‍🌾✨")
        return jsonify({"message": "Telegram vinculado con éxito."})
        
    cur.close()
    conn.close()
    return jsonify({"error": "Sesión no válida o no autorizada"}), 401

@app.route('/api/cron/check_plants', methods=['GET'])
def cron_check_plants():
    # Este endpoint simula el paso del tiempo para usuarios offline y dispara las alertas
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, username, game_state, telegram_chat_id FROM users WHERE telegram_chat_id IS NOT NULL")
    users = cur.fetchall()
    
    for u in users:
        new_state = process_server_tick(u['game_state'], u['telegram_chat_id'])
        if new_state != u['game_state']:
            cur.execute("UPDATE users SET game_state = %s WHERE id = %s", (new_state, u['id']))
            
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"message": "Cron procesado. Alertas enviadas si era necesario."})



@app.route('/api/plants', methods=['GET'])
def get_dynamic_plants():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM dynamic_plants")
    plants = cur.fetchall()
    cur.close(); conn.close()
    return jsonify(plants)

@app.route('/api/admin/plants', methods=['POST', 'DELETE'])
def admin_plants():
    data = request.json
    if data.get('admin_token') != ADMIN_SECRET: return jsonify({"error": "No autorizado"}), 403
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    if request.method == 'POST':
        plant_id = data.get('id')
        if not plant_id:
            plant_id = 'dyn_' + data['name'].lower().replace(' ', '_')
            
        cur.execute("""
            INSERT INTO dynamic_plants (id, category, name, image_url, hp, atk, def, price, reward, grow_time_minutes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
            category=EXCLUDED.category, name=EXCLUDED.name, image_url=EXCLUDED.image_url,
            hp=EXCLUDED.hp, atk=EXCLUDED.atk, def=EXCLUDED.def, price=EXCLUDED.price,
            reward=EXCLUDED.reward, grow_time_minutes=EXCLUDED.grow_time_minutes
        """, (plant_id, data['category'], data['name'], data['image_url'], int(data['hp']), int(data['atk']), int(data['def']), int(data['price']), int(data['reward']), int(data['grow_time_minutes'])))
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"message": "Planta guardada correctamente en el sistema."})
        
    elif request.method == 'DELETE':
        cur.execute("DELETE FROM dynamic_plants WHERE id = %s", (data['id'],))
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"message": "Planta eliminada."})










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
    incoming_inventory = data.get('inventory') 
    
    costs = {
        "iap_combo_24h": {"seeds": 0, "cop": 7500},
        "iap_combo_72h": {"seeds": 0, "cop": 18000},
        "iap_combo_7d": {"seeds": 0, "cop": 30000},
        "iap_nursery_72h": {"seeds": 0, "cop": 8000},
        "iap_nursery_7d": {"seeds": 0, "cop": 15000},
        "iap_elite_7d": {"seeds": 0, "cop": 39000},
        "exc_10k": {"seeds": 4250, "cop": 10000},
        "exc_25k": {"seeds": 11000, "cop": 25000},
        "exc_50k": {"seeds": 22500, "cop": 50000}
    }
    
    standard_costs = {
        "tool_pot_small": 5000,
        "tool_pot_big": 15000,
        "tool_water": 500,
        "tool_scarecrow": 6000
    }

    # Inyectar los costos de las plantas dinámicas en el diccionario de precios
    conn2 = get_db_connection()
    cur2 = conn2.cursor()
    cur2.execute("SELECT id, price FROM dynamic_plants")
    for dp in cur2.fetchall():
        standard_costs[dp['id']] = dp['price']
    cur2.close(); conn2.close()




    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, balance, game_state, session_token FROM users WHERE username = %s FOR UPDATE", (username,))
        user = cur.fetchone()
        
        if not user: return jsonify({"error": "Usuario no encontrado"}), 404
        if user.get('session_token') and user['session_token'] != token:
            cur.close(); conn.close()
            return jsonify({"error": "Sesión inválida"}), 401
            
        state = json.loads(user['game_state'] or '{}')
        
        if package in costs:
            cost_cop = costs[package]['cop']
            seeds_amount = costs[package]['seeds']
            
            if float(user['balance']) < cost_cop:
                return jsonify({"error": "Saldo COP insuficiente. Completa ofertas o torneos para recargar."}), 400
                
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
            
        elif package in standard_costs and incoming_inventory:
            qty = data.get('qty', 1)
            total_seed_cost = standard_costs[package] * qty
            
            if state.get('seedsBalance', 0) < total_seed_cost:
                return jsonify({"error": "Semillas insuficientes"}), 400
                
            state['seedsBalance'] -= total_seed_cost
            state['inventory'] = incoming_inventory 
            
            cur.execute("UPDATE users SET game_state = %s WHERE id = %s", (json.dumps(state), user['id']))
            conn.commit()
            
            return jsonify({
                "message": "Compra exitosa",
                "new_seeds": state['seedsBalance']
            })
            
        else:
            return jsonify({"error": "Paquete inválido"}), 400
            
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()

# ==========================================================
# ENDPOINTS DE TORNEOS (SCHEDULED & SIT&GO HÍBRIDO)
# ==========================================================

def get_next_schedule_ms(t_id):
    now = datetime.now(timezone.utc)
    # Desfase para que no todos inicien a la vez: Bonsái(0h), Orquídea(1h), Rosa(2h), Loto(3h)
    offset_hours = {1: 0, 3: 1, 4: 2, 5: 3}.get(t_id, 0)
    
    # Bloque base de 4 horas
    base_hour = ((now.hour // 4) + 1) * 4
    import datetime as dt
    next_time = now.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(hours=base_hour + offset_hours)
    
    # Si la hora calculada ya quedó atrás en el tiempo actual, saltar al siguiente ciclo
    if int(next_time.timestamp() * 1000) <= int(now.timestamp() * 1000):
        next_time += dt.timedelta(hours=4)
        
    return int(next_time.timestamp() * 1000)

def process_tournament_lifecycle(inst, conn, cur):
    now = int(time.time() * 1000)
    if not inst: return None
    
    t_id = inst.get('template_id')
    
    # MOTOR DE INICIO
    if inst['status'] == 'waiting' and now >= inst['start_time']:
        if t_id == 1 or inst['players_count'] >= 3:
            dur_hours = {1: 12, 3: 6, 4: 8, 5: 12}.get(t_id, 6)
            end_time = inst['start_time'] + (dur_hours * 3600000)
            cur.execute("UPDATE tournament_instances SET status = 'running', end_time = %s WHERE id = %s", (end_time, inst['id']))
            inst['status'] = 'running'
            inst['end_time'] = end_time
        else:
            new_start = get_next_schedule_ms(t_id)
            cur.execute("UPDATE tournament_instances SET start_time = %s WHERE id = %s", (new_start, inst['id']))
            inst['start_time'] = new_start
        conn.commit()
        
    # MOTOR DE CIERRE Y PAGOS (Top 3 gana COP, resto Semillas)
    if inst['status'] == 'running' and now >= inst['end_time']:
        # Bloqueo atómico: solo el primer request que detecte el fin ejecutará los pagos
        cur.execute("UPDATE tournament_instances SET status = 'completed' WHERE id = %s AND status = 'running' RETURNING id", (inst['id'],))
        updated = cur.fetchone()
        
        if updated:
            prize_pool = float(inst['prize_pool_cop'] or 0)
            if t_id == 1 and prize_pool < 10000: prize_pool = 10000.0
            
            cur.execute('''
                SELECT user_id, username FROM tournament_players 
                WHERE instance_id = %s 
                ORDER BY current_score DESC, alchemy_precision DESC, alchemy_time ASC
            ''', (inst['id'],))
            players = cur.fetchall()
            
            # Distribución Dinero Real (Top 3)
            prizes_cop = [prize_pool * 0.50, prize_pool * 0.30, prize_pool * 0.20]
            
            # Compensación Semillas (Posiciones indexadas: 0 a 9)
            seeds_compensation = {
                1: [0, 0, 0, 3000, 3000, 2000, 2000, 2000, 1000, 1000],
                3: [0, 0, 0, 12000, 12000, 8000, 8000, 8000, 4000, 4000],
                4: [0, 0, 0, 25000, 25000, 15000, 15000, 15000, 8000, 8000],
                5: [0, 0, 0, 40000, 40000, 25000, 25000, 25000, 12000, 12000]
            }
            comp_array = seeds_compensation.get(t_id, [0] * 10)
            
            for i, p in enumerate(players):
                uid = p['user_id']
                
                # Pago COP
                if i < 3 and prizes_cop[i] > 0:
                    amt = prizes_cop[i]
                    cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s", (amt, uid))
                    cur.execute("INSERT INTO financial_ledger (user_id, transaction_type, amount_cop, external_reference, status) VALUES (%s, 'tournament_prize', %s, %s, 'completed')", (uid, amt, f"PRIZE-{inst['id']}-{uid}"))
                    
                # Pago Semillas
                seed_amt = comp_array[i] if i < len(comp_array) else comp_array[-1]
                if seed_amt > 0:
                    cur.execute("SELECT game_state FROM users WHERE id = %s FOR UPDATE", (uid,))
                    u_state = cur.fetchone()
                    if u_state and u_state['game_state']:
                        import json
                        try:
                            st = json.loads(u_state['game_state'])
                            st['seedsBalance'] = st.get('seedsBalance', 0) + seed_amt
                            cur.execute("UPDATE users SET game_state = %s WHERE id = %s", (json.dumps(st), uid))
                        except Exception:
                            pass
                            
            conn.commit()
        inst['status'] = 'completed'
        
    return inst

@app.route('/api/tournaments/join', methods=['POST'])
def join_tournament():
    data = request.json
    username = data.get('username')
    token = data.get('session_token')
    fee = float(data.get('fee', 0))
    t_id = int(data.get('tournament_id', 1))
    
    # Nuevas variables capturadas del frontend
    precision = float(data.get('precision', 0.000))
    time_used = float(data.get('time_used', 0.00))
    score = float(data.get('score', 10000.00)) # <-- NUEVO: Captura el puntaje inicial con descuento

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, balance, game_state, session_token FROM users WHERE username = %s FOR UPDATE", (username,))
        user = cur.fetchone()
        if not user or (user.get('session_token') and user['session_token'] != token):
            return jsonify({"error": "Sesión inválida"}), 401

        # Buscar sala en espera, si no existe, crearla con horario programado
        cur.execute("SELECT id FROM tournament_config WHERE id = 1 FOR UPDATE") # Bloqueo atómico para evitar duplicidad de instancias
        cur.execute("SELECT id, players_count, max_players, start_time FROM tournament_instances WHERE template_id = %s AND status = 'waiting' LIMIT 1", (t_id,))
        instance = cur.fetchone()
        
        if not instance:
            start_time = get_next_schedule_ms(t_id) # <-- Modificado para usar el ID
            cur.execute("INSERT INTO tournament_instances (template_id, entry_fee_cop, max_players, start_time, prize_pool_cop) VALUES (%s, %s, 1000, %s, %s) RETURNING id", (t_id, fee, start_time, 10000 if t_id == 1 else 0))
            instance_id = cur.fetchone()['id']
            players_count = 0
            tourney_status = 'waiting'
            end_time = 0
        else:
            instance_id = instance['id']
            players_count = instance['players_count']
            start_time = instance['start_time']
            tourney_status = 'waiting'
            end_time = 0

        seed_cost = {0: 5000, 5000: 5000, 10000: 15000, 30000: 30000, 50000: 50000}.get(fee, 5000)
        state = json.loads(user['game_state'] or '{}')
        if state.get('seedsBalance', 0) < seed_cost:
            return jsonify({"error": f"Requiere {seed_cost} 🌱 para participar"}), 400

        # Anti-Duplicado (Si cambia de torneo, debemos removerlo del anterior. Por seguridad, aquí limpiamos instancias pasadas en estado 'waiting')
        cur.execute("DELETE FROM tournament_players WHERE user_id = %s AND instance_id IN (SELECT id FROM tournament_instances WHERE status = 'waiting')", (user['id'],))

        # Cobro de Semillas y Dinero Real
        state['seedsBalance'] -= seed_cost
        if fee > 0:
            prize_addition = fee * 0.85 
            cur.execute("UPDATE tournament_instances SET prize_pool_cop = prize_pool_cop + %s WHERE id = %s", (prize_addition, instance_id))
            cur.execute("INSERT INTO financial_ledger (user_id, transaction_type, amount_cop, external_reference, status) VALUES (%s, 'tournament_entry', %s, %s, 'completed')", (user['id'], fee, f"WOMPI-{user['id']}-{int(time.time())}"))

        # NUEVO: Insertamos el current_score calculado en el frontend junto con la precisión
        cur.execute("INSERT INTO tournament_players (instance_id, user_id, username, alchemy_precision, alchemy_time, current_score) VALUES (%s, %s, %s, %s, %s, %s)", (instance_id, user['id'], username, precision, time_used, score))
        cur.execute("UPDATE tournament_instances SET players_count = players_count + 1 WHERE id = %s", (instance_id,))
        cur.execute("UPDATE users SET game_state = %s WHERE id = %s", (json.dumps(state), user['id']))
        conn.commit()
        
        return jsonify({
            "message": "Inscripción exitosa",
            "instance_id": instance_id,
            "tourney_status": tourney_status,
            "start_time": start_time,
            "end_time": end_time,
            "new_balance_cop": float(user['balance']),
            "new_game_state": state
        })
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close(); conn.close()


@app.route('/api/tournaments/global_leaderboard/<int:t_id>', methods=['GET'])
def global_leaderboard(t_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tournament_instances WHERE template_id = %s AND status IN ('waiting', 'running') ORDER BY id DESC LIMIT 1", (t_id,))
    inst = cur.fetchone()
    
    if inst:
        inst = process_tournament_lifecycle(inst, conn, cur)
        
    # Si se completó o no hay activos, buscamos los resultados del campeón reciente
    if not inst or inst['status'] == 'completed':
        cur.execute("SELECT * FROM tournament_instances WHERE template_id = %s AND status = 'completed' ORDER BY id DESC LIMIT 1", (t_id,))
        last_inst = cur.fetchone()
        if last_inst:
            cur.execute('''SELECT username, current_score, alchemy_precision, alchemy_time FROM tournament_players WHERE instance_id = %s ORDER BY current_score DESC, alchemy_precision DESC, alchemy_time ASC LIMIT 10''', (last_inst['id'],))
            players = cur.fetchall()
            cur.close(); conn.close()
            return jsonify({
                "status": "completed",
                "leaderboard": players,
                "start_time": last_inst['start_time'],
                "end_time": last_inst['end_time'],
                "prize_pool": last_inst['prize_pool_cop']
            })
        cur.close(); conn.close()
        return jsonify({"status": "no_instance", "prize_pool": 10000 if t_id == 1 else 0})
        
    cur.execute('''
        SELECT username, current_score, alchemy_precision, alchemy_time 
        FROM tournament_players 
        WHERE instance_id = %s 
        ORDER BY current_score DESC, alchemy_precision DESC, alchemy_time ASC LIMIT 10
    ''', (inst['id'],))
    players = cur.fetchall()
    cur.close(); conn.close()
    
    return jsonify({
        "prize_pool": inst['prize_pool_cop'],
        "status": inst['status'],
        "start_time": inst['start_time'],
        "end_time": inst['end_time'],
        "players_count": inst['players_count'],
        "max_players": inst['max_players'],
        "leaderboard": players
    })



@app.route('/api/tournaments/status/<int:instance_id>', methods=['GET'])
def check_tournament_status(instance_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tournament_instances WHERE id = %s", (instance_id,))
    inst = cur.fetchone()
    
    if inst:
        inst = process_tournament_lifecycle(inst, conn, cur)
        # FIX: Convertimos Decimal a Float para que JSON lo pueda parsear sin error 500
        if 'prize_pool_cop' in inst and inst['prize_pool_cop'] is not None:
            inst['prize_pool_cop'] = float(inst['prize_pool_cop'])
        if 'entry_fee_cop' in inst and inst['entry_fee_cop'] is not None:
            inst['entry_fee_cop'] = float(inst['entry_fee_cop'])
        
    cur.close(); conn.close()
    return jsonify(inst if inst else {})

@app.route('/api/tournaments/leaderboard/<int:instance_id>', methods=['GET'])
def get_leaderboard(instance_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT prize_pool_cop, status, players_count, max_players FROM tournament_instances WHERE id = %s", (instance_id,))
    instance = cur.fetchone()
    
    cur.execute('''
        SELECT username, current_score, alchemy_precision, alchemy_time 
        FROM tournament_players 
        WHERE instance_id = %s 
        ORDER BY current_score DESC, alchemy_precision DESC, alchemy_time ASC LIMIT 10
    ''', (instance_id,))  # <-- CORRECCIÓN: Debe ser instance_id
    players = cur.fetchall()
    cur.close(); conn.close()
    
    return jsonify({
        "prize_pool": instance['prize_pool_cop'] if instance else 0,
        "status": instance['status'] if instance else 'unknown',
        "players_count": instance['players_count'] if instance else 0,
        "max_players": instance['max_players'] if instance else 10,
        "leaderboard": players
    })

@app.route('/api/dev/add_cop', methods=['POST'])
def dev_add_cop():
    data = request.json
    if data.get('admin_token') != ADMIN_SECRET:
        return jsonify({"error": "No autorizado"}), 403
    
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



@app.route('/api/admin/tree_config', methods=['GET', 'POST'])
def admin_tree_config():
    if request.method == 'POST':
        data = request.json
        if data.get('admin_token') != ADMIN_SECRET: return jsonify({"error": "No autorizado"}), 403
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE tree_config SET vip_prizes_json = %s, milestones_json = %s WHERE id = 1", (json.dumps(data['vip_prizes']), json.dumps(data['milestones'])))
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"message": "Configuración del Árbol guardada exitosamente."})
    else:
        admin_token = request.args.get('admin_token')
        if admin_token != ADMIN_SECRET: return jsonify({"error": "No autorizado"}), 403
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM tree_config WHERE id = 1")
        conf = cur.fetchone()
        cur.close(); conn.close()
        if conf:
            return jsonify({
                "vip_prizes": json.loads(conf['vip_prizes_json']) if conf['vip_prizes_json'] else [],
                "milestones": json.loads(conf['milestones_json']) if conf['milestones_json'] else []
            })
        return jsonify({"vip_prizes": [], "milestones": []})


# --- NUEVA RUTA PARA GUARDAR Y LEER LOS GUARDIANES DEL TORNEO ---
@app.route('/api/admin/tournament_config', methods=['GET', 'POST'])
def admin_tournament_config():
    if request.method == 'POST':
        data = request.json
        if data.get('admin_token') != ADMIN_SECRET: 
            return jsonify({"error": "No autorizado"}), 403
        
        # Filtramos el token para no guardarlo en el JSON de la BD
        config_data = {k: v for k, v in data.items() if k != 'admin_token'}
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE tournament_config SET config_json = %s WHERE id = 1", (json.dumps(config_data),))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"message": "Configuración de Guardianes guardada exitosamente."})
    else:
        # El GET es público (no exige token) para que nutrients.html pueda leerlo al armar las batallas
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT config_json FROM tournament_config WHERE id = 1")
        conf = cur.fetchone()
        cur.close()
        conn.close()
        
        if conf and conf['config_json']:
            return jsonify(json.loads(conf['config_json']))
        return jsonify({})



@app.route('/api/register', methods=['POST'])
def register():
    import random
    import string
    data = request.json
    username = data.get('username')
    password = data.get('password')
    referred_by = data.get('referred_by')
    if referred_by:
        referred_by = referred_by.strip().upper()
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username = %s", (username,))
    if cur.fetchone(): 
        cur.close()
        conn.close()
        return jsonify({"error": "El usuario ya existe"}), 400
    
    hashed_pw = generate_password_hash(password)
    token = str(uuid.uuid4())
    
    while True:
        short_id = str(random.randint(1000, 9999))
        cur.execute("SELECT id FROM users WHERE short_id = %s", (short_id,))
        if not cur.fetchone():
            break

    while True:
        ref_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
        cur.execute("SELECT id FROM users WHERE ref_code = %s", (ref_code,))
        if not cur.fetchone():
            break
    
    cur.execute(
        "INSERT INTO users (username, password_hash, balance, session_token, short_id, ref_code, referred_by) VALUES (%s, %s, 0, %s, %s, %s, %s) RETURNING id, username, balance, session_token, short_id, ref_code, referred_by", 
        (username, hashed_pw, token, short_id, ref_code, referred_by)
    )
    new_user = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    
    return jsonify({"message": "Registro exitoso", "user": new_user})

@app.route('/api/admin/users', methods=['POST'])
def admin_get_users():
    data = request.json
    if data.get('admin_token') != ADMIN_SECRET: return jsonify({"error": "No autorizado"}), 403
    
    conn = get_db_connection()
    cur = conn.cursor()
    # AÑADIDO: ref_code, referred_by
    cur.execute("SELECT id, username, short_id, balance, game_state, phone_nequi, ref_code, referred_by FROM users ORDER BY id DESC")
    users = cur.fetchall()
    
    for u in users:
        u['seeds'] = 0
        if u.get('game_state'):
            try:
                st = json.loads(u['game_state'])
                u['seeds'] = st.get('seedsBalance', 0)
            except:
                pass
        del u['game_state']

    cur.close()
    conn.close()
    return jsonify({"status": "success", "users": users})






@app.route('/api/admin/user_farm/<short_id>', methods=['POST'])
def admin_get_farm(short_id):
    data = request.json
    if data.get('admin_token') != ADMIN_SECRET: return jsonify({"error": "No autorizado"}), 403
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT username, balance, game_state, telegram_chat_id FROM users WHERE short_id = %s", (short_id,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    
    if user: 
        return jsonify({"status": "success", "user": user})
    return jsonify({"error": "Usuario no encontrado"}), 404

@app.route('/api/admin/add_items', methods=['POST'])
def admin_add_items():
    data = request.json
    if data.get('admin_token') != ADMIN_SECRET: return jsonify({"error": "No autorizado"}), 403
    
    conn = get_db_connection()
    cur = conn.cursor()

    # 1. ACELERADOR DE TIEMPO GLOBAL
    if data.get('global_time_skip'):
        skip_ms = int(data.get('global_time_skip')) * 3600000
        cur.execute("SELECT id, game_state FROM users FOR UPDATE")
        users = cur.fetchall()
        for u in users:
            if u['game_state']:
                try:
                    st = json.loads(u['game_state'])
                    st['autoWaterEndTime'] = max(0, st.get('autoWaterEndTime', 0) - skip_ms)
                    st['autoHarvestEndTime'] = max(0, st.get('autoHarvestEndTime', 0) - skip_ms)
                    for p in st.get('plots', []):
                        if p.get('plantedAt'): p['plantedAt'] -= skip_ms
                        if p.get('harvestAt'): p['harvestAt'] -= skip_ms
                        if p.get('scarecrowEndTime'): p['scarecrowEndTime'] = max(0, p['scarecrowEndTime'] - skip_ms)
                        if p.get('crowLeavingAt'): p['crowLeavingAt'] = max(0, p['crowLeavingAt'] - skip_ms)
                        if p.get('tournamentEndTime'): p['tournamentEndTime'] = max(0, p['tournamentEndTime'] - skip_ms)
                    cur.execute("UPDATE users SET game_state = %s WHERE id = %s", (json.dumps(st), u['id']))
                except: pass
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"message": f"Se ha adelantado el tiempo {data['global_time_skip']} horas en TODAS las granjas."})

    # 2. INYECCIÓN POR LOTES (CARRITO / REGALOS) A UNO O TODOS
    target_all = data.get('target_all', False)
    short_id = data.get('short_id')

    if not target_all and not short_id:
        cur.close(); conn.close()
        return jsonify({"error": "ID o selección global requerida"}), 400

    if target_all:
        cur.execute("SELECT id, balance, game_state FROM users FOR UPDATE")
        users_to_update = cur.fetchall()
    else:
        cur.execute("SELECT id, balance, game_state FROM users WHERE short_id = %s FOR UPDATE", (short_id,))
        u = cur.fetchone()
        if not u:
            cur.close(); conn.close()
            return jsonify({"error": "Usuario no encontrado"}), 404
        users_to_update = [u]

    cart = data.get('cart', [])
    gift_msg = data.get('gift_msg', '').strip()
    now = int(time.time() * 1000)

    icons = {
        'flower_wheat': '🌾', 'flower_bamboo': '🎋', 'flower_cactus': '🌵', 'flower_small': '🌸',
        'flower_crystal': '💎', 'flower_big': '🌺', 'flower_moon': '🌙', 'flower_solar': '☀️', 'flower_neon': '🟣',
        'tool_pot_small': '🪴', 'tool_pot_big': '🏺', 'tool_water': '🚿', 'tool_scarecrow': '🦉', 'tool_duel_ticket': '🎟️'
    }
    
    names = {
        'flower_wheat': 'Trigo Rápido', 'flower_bamboo': 'Bambú Tryhard', 'flower_cactus': 'Cactus de Cuarzo',
        'flower_small': 'Flor Pequeña', 'flower_crystal': 'Helecho Cristal', 'flower_big': 'Flor Grande',
        'flower_moon': 'Lirio Lunar', 'flower_solar': 'Girasol Solar', 'flower_neon': 'Orquídea Neón'
    }
    img_icons = {
        'flower_wheat': 'assets/Plantas/trigo_rapido.png', 
        'flower_bamboo': 'assets/Plantas/bambu_tryhard.png', 
        'flower_cactus': 'assets/Plantas/cactus_cuarzo.png', 
        'flower_small': 'assets/Plantas/flor_pequena.png', 
        'flower_crystal': 'assets/Plantas/helecho_cristal.png', 
        'flower_big': 'assets/Plantas/flor_grande.png', 
        'flower_moon': 'assets/Plantas/lirio_lunar.png'
    }

    # --- AÑADIDO: Cargar atributos de plantas dinámicas ---
    dyn_stats = {}
    for dp in data.get('dynamic_plants_data', []):
        icons[dp['id']] = '✨'
        names[dp['id']] = dp['name']
        img_icons[dp['id']] = dp['image_url']
        dyn_stats[dp['id']] = dp

    for user in users_to_update:
        state = json.loads(user['game_state'] or '{}')
        gift_items = []
        total_cop_added = 0.0

        for item in cart:
            t = item.get('type')
            raw = item.get('rawData')
            lbl = item.get('label', '')
            
            if t == 'seeds':
                qty = int(raw)
                state['seedsBalance'] = state.get('seedsBalance', 0) + qty
                gift_items.append({"icon": "🌱", "text": f"+{qty} Semillas"})
                
            elif t == 'cop':
                qty = float(raw)
                total_cop_added += qty
                gift_items.append({"icon": "💵", "text": f"+${int(qty)} COP"})
                
            elif t == 'item':
                item_id = raw['id']
                qty = int(raw['qty'])
                if 'inventory' not in state: state['inventory'] = {}
                if item_id not in state['inventory']:
                        state['inventory'][item_id] = {
                            'qty': 0, 
                            'type': 'seed' if ('flower' in item_id or 'dyn_' in item_id) else 'pot' if 'pot' in item_id else 'water' if 'water' in item_id else 'defense' if 'scarecrow' in item_id else 'ticket',
                            'name': names.get(item_id, item_id.replace('_', ' ').title()), 
                            'image': img_icons.get(item_id, 'assets/Fondos/logo.png')
                        }
                state['inventory'][item_id]['qty'] = state['inventory'][item_id].get('qty', 0) + qty
                gift_items.append({"icon": icons.get(item_id, '📦'), "text": lbl})
                
            elif t == 'arsenal':
                if 'arsenal' not in state: state['arsenal'] = []
                pid = raw['id']
                qty = int(raw['qty'])
                patk, pdef, php = 40, 20, 100
                if pid == 'flower_wheat': patk, pdef, php = 34, 15, 100
                elif pid == 'flower_cactus': patk, pdef, php = 34, 35, 150
                elif pid == 'flower_bamboo': patk, pdef, php = 50, 20, 100
                elif pid == 'flower_crystal': patk, pdef, php = 100, 8, 60
                elif pid == 'flower_moon': patk, pdef, php = 60, 25, 200
                elif pid in dyn_stats:
                    patk = dyn_stats[pid].get('atk', 40)
                    pdef = dyn_stats[pid].get('def', 20)
                    php = dyn_stats[pid].get('hp', 100)
                
                for _ in range(qty):
                    state['arsenal'].append({
                        'id': 'ars_' + str(int(time.time())) + '_' + str(uuid.uuid4())[:4],
                        'plantId': pid, 
                        'name': names.get(pid, pid.replace('_', ' ').title()), 
                        'icon': img_icons.get(pid, 'assets/Fondos/logo.png'),
                        'hp': php, 'maxHp': php, 'atk': patk, 'def': pdef, 'wins': 0, 'lastRecover': int(time.time() * 1000)
                    })
                gift_items.append({"icon": icons.get(pid, '⚔️'), "text": lbl})

            elif t == 'vip':
                unlock_vip = raw
                if unlock_vip.startswith('plot_'):
                    plot_idx = int(unlock_vip.split('_')[1])
                    if 'plots' in state and len(state['plots']) > plot_idx:
                        if state['plots'][plot_idx]['status'] in ['locked', 'arena_locked']:
                            state['plots'][plot_idx]['status'] = 'empty'
                elif unlock_vip.startswith('vip_'):
                    parts = unlock_vip.split('_')
                    ms_to_add = int(parts[2]) * 3600000
                    if parts[1] in ['water', 'combo']: state['autoWaterEndTime'] = max(state.get('autoWaterEndTime', 0), now) + ms_to_add
                    if parts[1] in ['harvest', 'combo']: state['autoHarvestEndTime'] = max(state.get('autoHarvestEndTime', 0), now) + ms_to_add
                gift_items.append({"icon": "⭐", "text": lbl})

        if gift_msg and len(gift_items) > 0:
            state['pending_gift'] = { "message": gift_msg, "items": gift_items }

        state['lastTick'] = now + 10000 

        cur.execute("UPDATE users SET game_state = %s, balance = balance + %s WHERE id = %s", (json.dumps(state), total_cop_added, user['id']))

    for user in users_to_update:
        state = json.loads(user['game_state'] or '{}')
        gift_items = []
        total_cop_added = 0.0

        for item in cart:
            t = item.get('type')
            raw = item.get('rawData')
            lbl = item.get('label', '')
            
            if t == 'seeds':
                qty = int(raw)
                state['seedsBalance'] = state.get('seedsBalance', 0) + qty
                gift_items.append({"icon": "🌱", "text": f"+{qty} Semillas"})
                
            elif t == 'cop':
                qty = float(raw)
                total_cop_added += qty
                gift_items.append({"icon": "💵", "text": f"+${int(qty)} COP"})
                
            elif t == 'item':
                item_id = raw['id']
                qty = int(raw['qty'])
                if 'inventory' not in state: state['inventory'] = {}
                if item_id not in state['inventory']:
                        state['inventory'][item_id] = {
                            'qty': 0, 
                            'type': 'seed' if ('flower' in item_id or 'dyn_' in item_id) else 'pot' if 'pot' in item_id else 'water' if 'water' in item_id else 'defense' if 'scarecrow' in item_id else 'ticket',
                            'name': item_id.replace('_', ' ').title(), 
                            'image': 'assets/Fondos/logo.png'
                        }
                state['inventory'][item_id]['qty'] = state['inventory'][item_id].get('qty', 0) + qty
                gift_items.append({"icon": icons.get(item_id, '📦'), "text": lbl})
                
            elif t == 'arsenal':
                if 'arsenal' not in state: state['arsenal'] = []
                pid = raw['id']
                qty = int(raw['qty'])
                patk, pdef, php = 40, 20, 100
                if pid == 'flower_wheat': patk, pdef, php = 34, 15, 100
                elif pid == 'flower_cactus': patk, pdef, php = 34, 35, 150
                elif pid == 'flower_bamboo': patk, pdef, php = 50, 20, 100
                elif pid == 'flower_crystal': patk, pdef, php = 100, 8, 60
                elif pid == 'flower_moon': patk, pdef, php = 60, 25, 200
                
                for _ in range(qty):
                    state['arsenal'].append({
                        'id': 'ars_' + str(int(time.time())) + '_' + str(uuid.uuid4())[:4],
                        'plantId': pid, 'name': names.get(pid, pid.replace('_', ' ').title()), 'icon': img_icons.get(pid, 'assets/Fondos/logo.png'),
                        'hp': php, 'maxHp': php, 'atk': patk, 'def': pdef, 'wins': 0, 'lastRecover': int(time.time() * 1000)
                    })
                gift_items.append({"icon": icons.get(pid, '⚔️'), "text": lbl})

            elif t == 'vip':
                unlock_vip = raw
                if unlock_vip.startswith('plot_'):
                    plot_idx = int(unlock_vip.split('_')[1])
                    if 'plots' in state and len(state['plots']) > plot_idx:
                        if state['plots'][plot_idx]['status'] in ['locked', 'arena_locked']:
                            state['plots'][plot_idx]['status'] = 'empty'
                elif unlock_vip.startswith('vip_'):
                    parts = unlock_vip.split('_')
                    ms_to_add = int(parts[2]) * 3600000
                    if parts[1] in ['water', 'combo']: state['autoWaterEndTime'] = max(state.get('autoWaterEndTime', 0), now) + ms_to_add
                    if parts[1] in ['harvest', 'combo']: state['autoHarvestEndTime'] = max(state.get('autoHarvestEndTime', 0), now) + ms_to_add
                gift_items.append({"icon": "⭐", "text": lbl})

        if gift_msg and len(gift_items) > 0:
            state['pending_gift'] = { "message": gift_msg, "items": gift_items }

        # --- SOLUCIÓN: FORZAR MARCA DE TIEMPO AL FUTURO ---
        # Garantiza que el frontend del usuario reconozca que esta inyección del Admin es la más reciente
        state['lastTick'] = now + 10000 

        cur.execute("UPDATE users SET game_state = %s, balance = balance + %s WHERE id = %s", (json.dumps(state), total_cop_added, user['id']))

    conn.commit()
    cur.close(); conn.close()
    
    return jsonify({"message": f"Inyección procesada a {'TODOS los jugadores' if target_all else '1 jugador'}."})


@app.route('/api/admin/tree_control', methods=['POST'])
def admin_tree_control():
    data = request.json
    if data.get('admin_token') != ADMIN_SECRET: 
        return jsonify({"error": "No autorizado"}), 403
    
    action = data.get('action')
    amount = int(data.get('amount', 0))
    date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    if action == 'reset':
        # CORRECCIÓN: Ahora también resetea el donation_count a 0
        cur.execute("UPDATE tree_state SET total_donated = 0, donation_count = 0, status = 'active' WHERE date_str = %s", (date_str,))
        cur.execute("DELETE FROM tree_donations WHERE date_str = %s", (date_str,))
        msg = "El Árbol de la Vida ha sido reseteado a 0."
    elif action == 'add':
        cur.execute("UPDATE tree_state SET total_donated = total_donated + %s WHERE date_str = %s", (amount, date_str))
        msg = f"Se inyectaron {amount:,} semillas al Árbol de la Vida exitosamente."
        
        # Opcional: Re-evaluar si llegó a la meta con esta inyección (simplificado)
        cur.execute("SELECT id, total_donated, daily_goal FROM tree_state WHERE date_str = %s", (date_str,))
        tree = cur.fetchone()
        if tree and tree['total_donated'] >= tree['daily_goal']:
            cur.execute("UPDATE tree_state SET status = 'completed' WHERE id = %s", (tree['id'],))
            msg += " ¡La meta del 100% ha sido alcanzada!"
    else:
        cur.close(); conn.close()
        return jsonify({"error": "Acción inválida"}), 400
        
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"message": msg})









@app.route('/api/pvp/challenge', methods=['POST'])
def pvp_challenge():
    data = request.json
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT id, game_state FROM users WHERE short_id = %s", (data['target_short_id'],))
    target = cur.fetchone()
    if not target: 
        cur.close(); conn.close()
        return jsonify({"error": "ID de oponente no encontrado"}), 404
    
    cur.execute("SELECT id, game_state FROM users WHERE username = %s", (data['username'],))
    challenger = cur.fetchone()
    
    if challenger['id'] == target['id']: 
        cur.close(); conn.close()
        return jsonify({"error": "No puedes retarte a ti mismo"}), 400
    
    bet = int(data['bet'])
    
    # Validar saldo del retador
    c_state = json.loads(challenger['game_state'] or '{}')
    if c_state.get('seedsBalance', 0) < bet:
        cur.close(); conn.close()
        return jsonify({"error": "No tienes suficientes semillas."}), 400
        
    # Validar saldo del objetivo (Rival)
    t_state = json.loads(target['game_state'] or '{}')
    t_seeds = t_state.get('seedsBalance', 0)
    if t_seeds < bet:
        cur.close(); conn.close()
        return jsonify({"error": f"El oponente no tiene suficientes semillas para apostar (Máximo que posee: {t_seeds} 🌱)"}), 400
    
    cur.execute("INSERT INTO pvp_matches (challenger_id, target_id, bet_seeds, status) VALUES (%s, %s, %s, 'pending') RETURNING id", (challenger['id'], target['id'], bet))
    match_id = cur.fetchone()['id']
    conn.commit(); cur.close(); conn.close()
    return jsonify({"message": "Reto enviado", "match_id": match_id})

@app.route('/api/pvp/combat_sync', methods=['POST'])
def pvp_combat_sync():
    data = request.json
    match_id = data.get('match_id')
    username = data.get('username')
    update_data = data.get('update_data')
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    if update_data:
        cur.execute("SELECT * FROM pvp_matches WHERE id = %s FOR UPDATE", (match_id,))
    else:
        cur.execute("SELECT * FROM pvp_matches WHERE id = %s", (match_id,))
        
    match = cur.fetchone()
    
    if not match: 
        cur.close(); conn.close()
        return jsonify({"error": "Match not found"}), 404
    
    state = json.loads(match['match_data']) if match['match_data'] else {}
    
    cur.execute("SELECT username FROM users WHERE id = %s", (match['challenger_id'],))
    challenger_name = cur.fetchone()['username']
    role = 'challenger' if username == challenger_name else 'target'
    
    if update_data:
        if 'team' in update_data:
            state[role + '_team'] = update_data['team']
        if 'actionQueue' in update_data:
            state['last_action'] = {'role': role, 'actions': update_data['actionQueue']}
            state['turn'] = 'target' if role == 'challenger' else 'challenger'
            
        if 'rps_choice' in update_data:
            state['rps_' + role] = update_data['rps_choice']
        if 'rps_clear' in update_data:
            state.pop('rps_challenger', None)
            state.pop('rps_target', None)
        if 'initial_turn' in update_data:
            state['turn'] = update_data['initial_turn']
            
        if 'winner' in update_data and not match.get('winner_username'):
            winner_name = update_data['winner']
            # Determinar ganador real si fue por abandono
            if winner_name == 'opponent_abandoned':
                cur.execute("SELECT username FROM users WHERE id = %s", (match['challenger_id'],))
                c_name = cur.fetchone()['username']
                cur.execute("SELECT username FROM users WHERE id = %s", (match['target_id'],))
                t_name = cur.fetchone()['username']
                winner_name = t_name if username == c_name else c_name

            cur.execute("UPDATE pvp_matches SET winner_username = %s WHERE id = %s RETURNING bet_seeds", (winner_name, match_id))
            updated_match = cur.fetchone()
            
            # INYECCIÓN DEL PREMIO DIRECTAMENTE EN LA BD
            # INYECCIÓN DEL PREMIO DIRECTAMENTE EN LA BD
            if updated_match:
                bet_amount = updated_match.get('bet_seeds', 0)
                if bet_amount > 0:
                    gross_reward = bet_amount * 2
                    tax = int(gross_reward * 0.05)
                    net_reward = gross_reward - tax
                    
                    cur.execute("SELECT id, game_state FROM users WHERE username = %s FOR UPDATE", (winner_name,))
                    w_user = cur.fetchone()
                    if w_user and w_user['game_state']:
                        try:
                            w_state = json.loads(w_user['game_state'])
                            w_state['seedsBalance'] = w_state.get('seedsBalance', 0) + net_reward
                            
                            # Se eliminó el forzado de 'lastTick' al futuro.
                            # El frontend ya utiliza 'skipSeedsSave' para no sobreescribir este premio, 
                            # lo que permite que el cliente devuelva las plantas al arsenal sin que el servidor rechace el guardado.
                            
                            cur.execute("UPDATE users SET game_state = %s WHERE id = %s", (json.dumps(w_state), w_user['id']))
                        except Exception:
                            pass
            
            conn.commit()
            cur.close(); conn.close()
            return jsonify({"status": "game_over", "winner": winner_name})
            
        cur.execute("UPDATE pvp_matches SET match_data = %s WHERE id = %s", (json.dumps(state), match_id))
        conn.commit()
        
    cur.close(); conn.close()
    return jsonify({"state": state, "role": role, "winner": match.get('winner_username')})

@app.route('/api/pvp/accept', methods=['POST'])
def pvp_accept():
    data = request.json
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE pvp_matches SET status = %s WHERE id = %s", (data['action'], data['match_id']))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"message": f"Reto {data['action']}"})

@app.route('/api/pvp/status/<int:match_id>', methods=['GET'])
def pvp_status(match_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT status FROM pvp_matches WHERE id = %s", (match_id,))
    match = cur.fetchone()
    cur.close(); conn.close()
    return jsonify(match)



@app.route('/api/dev/reset_tournaments', methods=['POST'])
def dev_reset_tournaments():
    data = request.json
    if data.get('admin_token') != ADMIN_SECRET:
        return jsonify({"error": "No autorizado"}), 403
        
    username = data.get('username')
    clear_all = data.get('clear_all', False)
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
        if user:
            cur.execute("DELETE FROM tournament_players WHERE user_id = %s", (user['id'],))
            if clear_all:
                cur.execute("UPDATE users SET balance = 0.00 WHERE id = %s", (user['id'],))
            conn.commit()
            return jsonify({"message": "Datos de desarrollo reseteados en BD"}), 200
        return jsonify({"error": "Usuario no encontrado"}), 404
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


import threading





# ==========================================================
# WEBSOCKETS (SOCKET.IO) PARA PVP SIN LAG
# ==========================================================
@socketio.on('register_user')
def on_register_user(data):
    if data.get('short_id'): join_room(f"user_{data['short_id']}")

@socketio.on('join_match')
def on_join_match(data):
    if data.get('match_id'): join_room(f"match_{data['match_id']}")

@socketio.on('pvp_challenge')
def on_pvp_challenge(data):
    emit('incoming_challenge', data, room=f"user_{data.get('target_short_id')}")

@socketio.on('pvp_accept')
def on_pvp_accept(data):
    emit('challenge_accepted', data, room=f"user_{data.get('challenger_short_id')}")

@socketio.on('pvp_reject')
def on_pvp_reject(data):
    emit('challenge_rejected', data, room=f"user_{data.get('challenger_short_id')}")

@socketio.on('rps_action')
def on_rps_action(data):
    emit('rps_update', data, room=f"match_{data.get('match_id')}", include_self=False)

@socketio.on('combat_action')
def on_combat_action(data):
    emit('combat_update', data, room=f"match_{data.get('match_id')}", include_self=False)


# --- NUEVO: SISTEMA DE MATCHMAKING CON NEGOCIACIÓN ---
matchmaking_queue = {'global': []}

@socketio.on('join_matchmaking')
def on_join_matchmaking(data):
    short_id = data.get('short_id')
    username = data.get('username')
    sid = request.sid
    
    # Limpiar si el usuario ya estaba en la fila para evitar clones
    matchmaking_queue['global'] = [p for p in matchmaking_queue['global'] if p.get('short_id') != short_id]
        
    if len(matchmaking_queue['global']) > 0:
        # Hay alguien esperando, hacemos match
        opponent = matchmaking_queue['global'].pop(0)
        
        # El jugador que acaba de llegar propondrá la apuesta, el que estaba esperando la recibirá
        emit('match_found_negotiation', {'role': 'proposer', 'opponent_id': opponent['short_id'], 'opponent_name': opponent['username']}, room=f"user_{short_id}")
        emit('match_found_negotiation', {'role': 'waiter', 'opponent_id': short_id, 'opponent_name': username}, room=f"user_{opponent['short_id']}")
    else:
        # No hay nadie, lo metemos a la fila
        matchmaking_queue['global'].append({'short_id': short_id, 'username': username, 'sid': sid})
        
    broadcast_matchmaking_count()

@socketio.on('cancel_matchmaking')
def on_cancel_matchmaking(data):
    short_id = data.get('short_id')
    matchmaking_queue['global'] = [p for p in matchmaking_queue['global'] if p.get('short_id') != short_id]
    broadcast_matchmaking_count()

@socketio.on('get_matchmaking_count')
def on_get_mm_count():
    count = len(matchmaking_queue['global'])
    emit('matchmaking_count', {'count': count})

@socketio.on('disconnect')
def test_disconnect():
    sid = request.sid
    matchmaking_queue['global'] = [p for p in matchmaking_queue['global'] if p.get('sid') != sid]
    broadcast_matchmaking_count()

def broadcast_matchmaking_count():
    count = len(matchmaking_queue['global'])
    socketio.emit('matchmaking_count', {'count': count})



def init_daily_tree(cur, date_str):
    cur.execute("SELECT COUNT(id) as c FROM users WHERE balance > 0 OR game_state IS NOT NULL")
    active_users = max(1, cur.fetchone()['c'])
    daily_goal = active_users * 15000  
    
    cur.execute("SELECT milestones_json FROM tree_config WHERE id = 1")
    conf = cur.fetchone()
    chosen_pool = json.loads(conf['milestones_json']) if conf and conf['milestones_json'] else []
    
    items_ref = {
        'flower_wheat': ('Trigo Rápido', '🌾'), 'flower_bamboo': ('Bambú Tryhard', '🎋'),
        'flower_cactus': ('Cactus Cuarzo', '🌵'), 'flower_small': ('Flor Pequeña', '🌸'),
        'flower_crystal': ('Helecho Cristal', '💎'), 'flower_big': ('Flor Grande', '🌺'),
        'flower_moon': ('Lirio Lunar', '🌙'), 'tool_pot_small': ('Maceta Peq.', '🪴'),
        'tool_pot_big': ('Maceta Gde.', '🏺'), 'tool_water': ('Agua', '🚿'),
        'tool_scarecrow': ('Espantapájaros', '🦉'), 'tool_duel_ticket': ('Ticket Duelo', '🎟️')
    }
    cur.execute("SELECT id, name FROM dynamic_plants")
    for dp in cur.fetchall():
        items_ref[dp['id']] = (dp['name'], '✨')

    for m in chosen_pool:
        name, icon = items_ref.get(m['id'], ('Objeto', '📦'))
        m['name'] = f"{m['qty']}x {name}"
        m['icon'] = icon

    cur.execute("INSERT INTO tree_state (date_str, daily_goal, rewards_json, status) VALUES (%s, %s, %s, 'active') ON CONFLICT DO NOTHING", (date_str, daily_goal, json.dumps(chosen_pool)))

@app.route('/api/tree', methods=['GET'])
def get_tree_state():
    date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tree_state WHERE date_str = %s", (date_str,))
    tree = cur.fetchone()
    if not tree:
        init_daily_tree(cur, date_str)
        conn.commit()
        cur.execute("SELECT * FROM tree_state WHERE date_str = %s", (date_str,))
        tree = cur.fetchone()
        
    cur.execute("SELECT username, amount FROM tree_donations WHERE date_str = %s ORDER BY amount DESC LIMIT 10", (date_str,))
    top_donators = cur.fetchall()
    donors_count = tree.get('donation_count', 0)
            
    user_donation = 0
    username = request.args.get('username')
    if username:
        cur.execute("SELECT amount FROM tree_donations WHERE username = %s AND date_str = %s", (username, date_str))
        u_don = cur.fetchone()
        if u_don: user_donation = u_don['amount']

    # --- FORZAR LECTURA DE CONFIGURACIÓN DEL ADMIN PARA REFLEJO INMEDIATO ---
    cur.execute("SELECT vip_prizes_json, milestones_json FROM tree_config WHERE id = 1")
    conf = cur.fetchone()
    vip_prizes = json.loads(conf['vip_prizes_json']) if conf and conf['vip_prizes_json'] else []
    milestones = json.loads(conf['milestones_json']) if conf and conf['milestones_json'] else []
    
    items_ref = {
        'flower_wheat': ('Trigo Rápido', '🌾'), 'flower_bamboo': ('Bambú Tryhard', '🎋'),
        'flower_cactus': ('Cactus Cuarzo', '🌵'), 'flower_small': ('Flor Pequeña', '🌸'),
        'flower_crystal': ('Helecho Cristal', '💎'), 'flower_big': ('Flor Grande', '🌺'),
        'flower_moon': ('Lirio Lunar', '🌙'), 'tool_pot_small': ('Maceta Peq.', '🪴'),
        'tool_pot_big': ('Maceta Gde.', '🏺'), 'tool_water': ('Agua', '🚿'),
        'tool_scarecrow': ('Espantapájaros', '🦉'), 'tool_duel_ticket': ('Ticket Duelo', '🎟️')
    }
    cur.execute("SELECT id, name FROM dynamic_plants")
    for dp in cur.fetchall():
        items_ref[dp['id']] = (dp['name'], '✨')

    enriched_vip = []
    for vp in vip_prizes:
        enriched_items = []
        for item in vp.get('items', []):
            name, icon = items_ref.get(item['id'], ('Objeto', '📦'))
            enriched_items.append({"id": item['id'], "qty": item['qty'], "name": f"{item['qty']}x {name}", "icon": icon})
        enriched_vip.append({"rank": vp['rank'], "items": enriched_items})

    enriched_milestones = []
    for m in milestones:
        name, icon = items_ref.get(m['id'], ('Objeto', '📦'))
        enriched_milestones.append({
            "pct": m['pct'],
            "id": m['id'],
            "qty": m['qty'],
            "name": f"{m['qty']}x {name}",
            "icon": icon
        })
        
    cur.close(); conn.close()
    return jsonify({
        "goal": tree['daily_goal'], "donated": tree['total_donated'], "rewards": enriched_milestones, 
        "top": top_donators, "user_donation": user_donation, "status": tree['status'], "donors_count": donors_count,
        "vip_prizes": enriched_vip
    })

@app.route('/api/tree/donate', methods=['POST'])
def donate_tree():
    data = request.json
    username = data.get('username')
    amount = int(data.get('amount', 0))
    
    date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        cur.execute("SELECT id, status, total_donated, daily_goal, rewards_json FROM tree_state WHERE date_str = %s FOR UPDATE", (date_str,))
        t_stat = cur.fetchone()
        
        if not t_stat:
            init_daily_tree(cur, date_str)
            cur.execute("SELECT id, status, total_donated, daily_goal, rewards_json FROM tree_state WHERE date_str = %s FOR UPDATE", (date_str,))
            t_stat = cur.fetchone()

        if t_stat and t_stat['status'] == 'completed':
            cur.close(); conn.close()
            return jsonify({"error": "El árbol ya ha florecido hoy. ¡Vuelve mañana!"}), 400
        
        # Validación de faltante exacto para evitar cobros de más
        remaining = max(0, t_stat['daily_goal'] - t_stat['total_donated'])
        if remaining > 0 and amount > remaining:
            amount = remaining 
            
        min_allowed = remaining if (0 < remaining < 1000) else 1000
        if amount < min_allowed:
            cur.close(); conn.close()
            return jsonify({"error": f"La donación mínima en este momento es de {min_allowed} 🌱."}), 400
        
        cur.execute("SELECT id, game_state FROM users WHERE username = %s FOR UPDATE", (username,))
        user = cur.fetchone()
        if not user: 
            cur.close(); conn.close()
            return jsonify({"error": "Usuario no encontrado"}), 404
            
        state = json.loads(user['game_state'] or '{}')
        if state.get('seedsBalance', 0) < amount: 
            cur.close(); conn.close()
            return jsonify({"error": "Semillas insuficientes en tu balance."}), 400
            
        cur.execute("SELECT amount FROM tree_donations WHERE user_id = %s AND date_str = %s", (user['id'], date_str))
        is_new_donor = cur.fetchone() is None

        # Descontar donación inicial
        state['seedsBalance'] -= amount
        
        cur.execute("UPDATE tree_state SET total_donated = total_donated + %s, donation_count = COALESCE(donation_count, 0) + 1 WHERE date_str = %s RETURNING id, total_donated, daily_goal, rewards_json, donation_count", (amount, date_str))
        tree = cur.fetchone()
        
        cur.execute("INSERT INTO tree_donations (user_id, username, amount, date_str) VALUES (%s, %s, %s, %s) ON CONFLICT (user_id, date_str) DO UPDATE SET amount = tree_donations.amount + EXCLUDED.amount", (user['id'], username, amount, date_str))
        
        # Marca de tiempo en el futuro para forzar la sincronización en vivo en cualquier ventana
        future_tick = int(time.time() * 1000) + 2000

        # --- PREMIO SORPRESA POR ORDEN DE LLEGADA (1, 10, 25, 50, 75, 99) ---
        total_donors_now = tree['donation_count']
        if total_donors_now in [1, 10, 25, 50, 75, 99]:
            donor_reward = int(t_stat['daily_goal'] * 0.02)
            state['seedsBalance'] = int(state.get('seedsBalance', 0)) + donor_reward
            pg = state.get('pending_gift')
            new_msg = f"¡Felicidades! Eres el Donador #{total_donors_now} en unirse a la meta de hoy. Has recibido un premio sorpresa del 2% del botín total."
            if pg and isinstance(pg, dict):
                pg['message'] = pg.get('message', '') + f"\n\n{new_msg}"
                if 'items' not in pg: pg['items'] = []
                pg['items'].append({"icon": "🎁", "text": f"+{donor_reward:,} Semillas"})
            else:
                state['pending_gift'] = {
                    "message": new_msg,
                    "items": [{"icon": "🎁", "text": f"+{donor_reward:,} Semillas"}]
                }
        # --- EVALUADOR INMEDIATO DEL 100% PARA TODOS LOS JUGADORES ---
        if t_stat['status'] == 'active' and tree['total_donated'] >= tree['daily_goal']:
            cur.execute("UPDATE tree_state SET status = 'completed' WHERE id = %s", (tree['id'],))
            
            # CORRECCIÓN 1: Bajamos la donación mínima a 1 para que nadie se quede sin premio en las pruebas
            min_donation = 1 
            
            cur.execute("SELECT user_id, username, amount FROM tree_donations WHERE date_str = %s ORDER BY amount DESC", (date_str,))
            donators = cur.fetchall()
            
            # --- LEER LA CONFIGURACIÓN DEL ADMIN (VIP Y MILESTONES) ---
            cur.execute("SELECT vip_prizes_json, milestones_json FROM tree_config WHERE id = 1")
            conf = cur.fetchone()
            vip_prizes = json.loads(conf['vip_prizes_json']) if conf and conf['vip_prizes_json'] else []
            milestones = json.loads(conf['milestones_json']) if conf and conf['milestones_json'] else []

            items_ref = {
                'flower_wheat': ('Trigo Rápido', 'assets/Plantas/trigo_rapido.gif'), 'flower_bamboo': ('Bambú Tryhard', 'assets/Plantas/bambu_tryhard.gif'),
                'flower_cactus': ('Cactus Cuarzo', 'assets/Plantas/cactus_cuarzo.gif'), 'flower_small': ('Flor Pequeña', 'assets/Plantas/flor_pequena.gif'),
                'flower_crystal': ('Helecho Cristal', 'assets/Plantas/helecho_cristal.gif'), 'flower_big': ('Flor Grande', 'assets/Plantas/flor_grande.gif'),
                'flower_moon': ('Lirio Lunar', 'assets/Plantas/lirio_lunar.gif'), 'tool_pot_small': ('Maceta Pequeña', 'assets/Herramientas_y_Servicios/maceta_pequena.png'),
                'tool_pot_big': ('Maceta Grande', 'assets/Herramientas_y_Servicios/maceta_grande.png'), 'tool_water': ('Regadera', 'assets/Herramientas_y_Servicios/regadera.png'),
                'tool_scarecrow': ('Espantapájaros', 'assets/Herramientas_y_Servicios/espantapajaros.png'), 'tool_duel_ticket': ('Ticket de Duelo', 'assets/Herramientas_y_Servicios/ticket_duelo.png')
            }
            cur.execute("SELECT id, name, image_url FROM dynamic_plants")
            for dp in cur.fetchall():
                items_ref[dp['id']] = (dp['name'], dp['image_url'])
                
            base_gift_items = []
            for m in milestones:
                name, icon = items_ref.get(m['id'], ('Premio', '🎁'))
                emoji_ref = {
                    'flower_wheat': '🌾', 'flower_bamboo': '🎋', 'flower_cactus': '🌵', 'flower_small': '🌸',
                    'flower_crystal': '💎', 'flower_big': '🌺', 'flower_moon': '🌙', 'tool_pot_small': '🪴',
                    'tool_pot_big': '🏺', 'tool_water': '🚿', 'tool_scarecrow': '🦉', 'tool_duel_ticket': '🎟️'
                }
                emoji = emoji_ref.get(m['id'], '✨')
                base_gift_items.append({"icon": emoji, "text": f"{m['qty']}x {name}"})

            for rank, w in enumerate(donators):
                if w['amount'] < min_donation: continue
                
                extra_items = []
                rank_text = ""
                seed_reward = 0
                prize = next((p for p in vip_prizes if p['rank'] == rank + 1), None)
                
                if rank == 0:
                    seed_reward = int(tree['daily_goal'] * 0.10)
                    rank_text = "\n\nPor asegurar el 1er Puesto 🥇, recibes recompensas exclusivas y el 10% del botín total."
                elif rank == 1:
                    seed_reward = int(tree['daily_goal'] * 0.05)
                    rank_text = "\n\nPor quedar en 2do Puesto 🥈, recibes recompensas exclusivas y el 5% del botín total."
                elif rank == 2:
                    seed_reward = int(tree['daily_goal'] * 0.02)
                    rank_text = "\n\nPor quedar en 3er Puesto 🥉, recibes recompensas exclusivas y el 2% del botín total."
                
                target_state = state if w['user_id'] == user['id'] else None
                
                if not target_state:
                    cur.execute("SELECT game_state FROM users WHERE id = %s FOR UPDATE", (w['user_id'],))
                    u_row = cur.fetchone()
                    if u_row and u_row['game_state']:
                        target_state = json.loads(u_row['game_state'])
                
                if target_state:
                    if prize and 'items' in prize:
                        for item in prize['items']:
                            p_id = item['id']
                            p_qty = item['qty']
                            p_name, p_img = items_ref.get(p_id, ('Premio', 'assets/Fondos/logo.png'))
                            
                            emoji = emoji_ref.get(p_id, '✨')
                            extra_items.append({"icon": emoji, "text": f"{p_qty}x {p_name}"})
                            
                            if 'inventory' not in target_state: target_state['inventory'] = {}
                            if p_id not in target_state['inventory']:
                                typ = 'seed' if ('flower' in p_id or 'dyn_' in p_id) else 'pot' if 'pot' in p_id else 'defense' if 'scarecrow' in p_id else 'water'
                                req = 'small' if ('small' in p_id or 'wheat' in p_id) else 'big'
                                target_state['inventory'][p_id] = {'qty': 0, 'type': typ, 'reqSize': req, 'name': p_name, 'image': p_img}
                            target_state['inventory'][p_id]['qty'] = int(target_state['inventory'][p_id].get('qty', 0)) + p_qty
                    
                    # CORRECCIÓN 2: Este bloque for 'm in milestones:' ahora está alineado fuera del 'if prize'
                    for m in milestones:
                        rid = m['id']
                        if 'inventory' not in target_state: target_state['inventory'] = {}
                        if rid not in target_state['inventory']:
                            r_name, r_img = items_ref.get(rid, ('Premio', 'assets/Fondos/logo.png'))
                            typ = 'seed' if ('flower' in rid or 'dyn_' in rid) else 'pot' if 'pot' in rid else 'defense' if 'scarecrow' in rid else 'water'
                            req = 'small' if ('small' in rid or 'wheat' in rid) else 'big'
                            target_state['inventory'][rid] = {'qty': 0, 'type': typ, 'reqSize': req, 'name': r_name, 'image': r_img}
                        target_state['inventory'][rid]['qty'] = int(target_state['inventory'][rid].get('qty', 0)) + int(m.get('qty', 1))

                    target_state['seedsBalance'] = int(target_state.get('seedsBalance', 0)) + seed_reward
                    extra_items.append({"icon": "🌱", "text": f"+{seed_reward:,} Semillas"})
                    
                    msg = f"¡El Árbol de la Vida ha culminado con éxito! Por tu valiosa donación de {w['amount']:,} 🌱, tu premio es el siguiente.{rank_text}"
                    gift_payload = {"message": msg, "items": base_gift_items + extra_items}

                    pg = target_state.get('pending_gift')
                    if pg and isinstance(pg, dict):
                        if 'items' not in pg: pg['items'] = []
                        pg['items'].extend(gift_payload['items'])
                        pg['message'] = pg.get('message', '') + "\n\n" + msg
                    else:
                        target_state['pending_gift'] = gift_payload

                    target_state['lastTick'] = future_tick
                    if w['user_id'] != user['id']:
                        cur.execute("UPDATE users SET game_state = %s WHERE id = %s", (json.dumps(target_state), w['user_id']))

        socketio.emit('tree_completed_event', {})
        state['lastTick'] = future_tick
        cur.execute("UPDATE users SET game_state = %s WHERE id = %s", (json.dumps(state), user['id']))
        conn.commit()
        
        return jsonify({
            "message": "Donación exitosa", 
            "new_seeds": state['seedsBalance'],
            "gift": state.get('pending_gift')
        })

    except Exception as e:
        conn.rollback()
        return jsonify({"error": f"Error interno: {str(e)}"}), 500
    finally:
        cur.close()
        conn.close()


def background_cron_worker():
    while True:
        time.sleep(60) 
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            # Procesar evaporación y plagas
            cur.execute("SELECT id, username, game_state, telegram_chat_id FROM users WHERE telegram_chat_id IS NOT NULL")
            users = cur.fetchall()
            for u in users:
                try:
                    new_state = process_server_tick(u['game_state'], u['telegram_chat_id'])
                    if new_state != u['game_state']:
                        cur.execute("UPDATE users SET game_state = %s WHERE id = %s", (new_state, u['id']))
                except Exception: pass
            
            # --- EVALUADOR DIARIO DEL ÁRBOL A LAS 00:00 UTC ---
            yesterday_str = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')
            cur.execute("SELECT * FROM tree_state WHERE date_str = %s AND status = 'active'", (yesterday_str,))
            old_tree = cur.fetchone()
            
            if old_tree:
                # Si llegó a medianoche estando activo, significa que NO se llenó. Se declara fallido.
                cur.execute("UPDATE tree_state SET status = 'failed' WHERE id = %s", (old_tree['id'],))
            conn.commit()
            cur.close()
            conn.close()
        except Exception: pass


# Iniciamos el motor automático justo antes de que arranque la app
threading.Thread(target=background_cron_worker, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    # REEMPLAZA app.run POR socketio.run
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)