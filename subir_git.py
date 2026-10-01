import tkinter as tk
from tkinter import messagebox
import os
import subprocess

# Archivo que guardará el contador y URL de tu repositorio
ARCHIVO_CONTADOR = "contador.txt"
REPO_URL = "https://github.com/dfcastro1997-dot/Cashgardenclub.git"

def obtener_siguiente_numero():
    """Lee el número del archivo contador.txt o lo crea en 0."""
    if not os.path.exists(ARCHIVO_CONTADOR):
        with open(ARCHIVO_CONTADOR, "w") as f:
            f.write("0")
        return 0
    
    with open(ARCHIVO_CONTADOR, "r") as f:
        try:
            return int(f.read())
        except ValueError:
            with open(ARCHIVO_CONTADOR, "w") as f_write:
                f_write.write("0")
            return 0

def guardar_siguiente_numero(numero):
    """Guarda el siguiente número en el archivo contador."""
    with open(ARCHIVO_CONTADOR, "w") as f:
        f.write(str(numero))

def configurar_repositorio():
    """Verifica si la carpeta es un repo Git, de lo contrario la inicializa."""
    if not os.path.isdir(".git"):
        try:
            print("Inicializando repositorio Git...")
            subprocess.run(["git", "init"], check=True)
            subprocess.run(["git", "remote", "add", "origin", REPO_URL], check=True)
            subprocess.run(["git", "branch", "-M", "main"], check=True)
        except subprocess.CalledProcessError as e:
            messagebox.showerror("Error", f"No se pudo inicializar Git:\n{e}")
            return False
    return True

def ejecutar_git():
    """Ejecuta los comandos de Git usando subprocess para manejar errores."""
    # 1. Asegurar que estamos en un repositorio de Git válido
    if not configurar_repositorio():
        return

    numero_commit = obtener_siguiente_numero()
    mensaje_commit = f"{numero_commit:02d}"
    
    print(f"--- Iniciando proceso para el commit: {mensaje_commit} ---")

    try:
        # 2. git add .
        subprocess.run(["git", "add", "."], check=True)
        
        # 3. git status para ver si hay cambios (para no hacer commits vacíos)
        estado = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        if not estado.stdout.strip():
            messagebox.showinfo("Aviso", "No hay cambios nuevos para subir.")
            return

        # 4. git commit
        subprocess.run(["git", "commit", "-m", mensaje_commit], check=True)
        
        # 5. git push
        # Usamos -u origin main para setear el upstream la primera vez
        subprocess.run(["git", "push", "-u", "origin", "main"], check=True)
        
        print("--- Proceso completado exitosamente ---")
        
        # 6. Incrementar solo si todo lo anterior funcionó
        guardar_siguiente_numero(numero_commit + 1)
        label_contador.config(text=f"Próximo commit será: {numero_commit + 1:02d}")
        messagebox.showinfo("Éxito", f"¡Archivos subidos con éxito!\nCommit: {mensaje_commit}")

    except subprocess.CalledProcessError as e:
        messagebox.showerror("Error de Git", f"El comando falló:\n{' '.join(e.cmd)}")
        print(f"Error al ejecutar: {e.cmd}")
    except Exception as e:
        messagebox.showerror("Error Inesperado", f"Ocurrió un error: {e}")

# --- Configuración de la Interfaz Gráfica (GUI) ---
ventana = tk.Tk()
ventana.title("Asistente de Git")
ventana.geometry("350x200")

titulo = tk.Label(ventana, text="Subir Cambios a Git", font=("Helvetica", 16))
titulo.pack(pady=10)

boton_subir = tk.Button(ventana, text="Añadir, Comitear y Subir", command=ejecutar_git, bg="lightblue", fg="black", font=("Helvetica", 12))
boton_subir.pack(pady=20, ipadx=10, ipady=5)

proximo_numero = obtener_siguiente_numero()
label_contador = tk.Label(ventana, text=f"Próximo commit será: {proximo_numero:02d}", font=("Helvetica", 10))
label_contador.pack(pady=5)

ventana.mainloop()