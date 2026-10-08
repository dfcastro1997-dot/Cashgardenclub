import os
import psycopg2

DB_URI = os.getenv("DATABASE_URL") # Asegúrate de que la variable de entorno esté cargada o pega aquí la URL de tu base de datos entre comillas

def wipe_database():
    try:
        conn = psycopg2.connect(DB_URI)
        cur = conn.cursor()
        print("Borrando todas las tablas...")
        
        # Elimina todas las tablas en cascada
        cur.execute("DROP SCHEMA public CASCADE;")
        cur.execute("CREATE SCHEMA public;")
        
        conn.commit()
        cur.close()
        conn.close()
        print("¡Base de datos limpiada exitosamente! Ya puedes reiniciar server.py")
    except Exception as e:
        print(f"Error limpiando la base de datos: {e}")

if __name__ == "__main__":
    wipe_database()