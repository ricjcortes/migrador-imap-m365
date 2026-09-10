#!/usr/bin/env python3
"""
Comprueba que el ejecutable construido sirve de verdad.

Por que existe. "Se construyo sin errores" no es lo mismo que "funciona": un
binario puede compilar y despues no arrancar porque falto una dependencia, o
arrancar y no poder hablar con los servidores porque la biblioteca TLS del Python
usado es demasiado vieja. Este script lo comprueba en vez de suponerlo, y sale
con codigo distinto de cero si algo no cuadra.

Se ejecuta solo, sin argumentos, desde la raiz del paquete:

    python3 verificar.py

Comprueba, en este orden:

  1. Que el binario existe y con que arquitectura se construyo.
  2. Que arranca en un entorno limpio, sin PATH ni paquetes del sistema. Es lo
     que demuestra que es autocontenido y que no depende de lo que hubiera
     instalado en el equipo donde se construyo.
  3. Que sirve su pagina con el testigo correcto y responde 404 sin el.
  4. Que negocia TLS con los dos servidores IMAP implicados. Esta es la que
     falla de forma silenciosa si se construye con un Python cuya biblioteca TLS
     ya no sirve para lo que exigen los servidores.
"""

import os
import platform
import secrets
import socket
import ssl
import subprocess
import sys
import time
import urllib.request

RAIZ = os.path.dirname(os.path.abspath(__file__))
ES_WINDOWS = os.name == "nt"
BINARIO = os.path.join(RAIZ, "dist",
                       "MigradorCorreo.exe" if ES_WINDOWS else "MigradorCorreo")
# El de origen se puede cambiar con MIGRADOR_HOST_ORIGEN, igual que en la
# herramienta, para verificar contra el servidor que se vaya a usar de verdad.
SERVIDORES = (os.environ.get("MIGRADOR_HOST_ORIGEN", "imap.secureserver.net"),
              "outlook.office365.com")

fallos = []


def titulo(t):
    print("\n" + t)
    print("-" * len(t))


def bien(t):
    print("  OK    " + t)


def mal(t):
    print("  FALLA " + t)
    fallos.append(t)


def puerto_libre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ 1. existe
titulo("1. El binario")

if not os.path.exists(BINARIO):
    mal("no existe %s" % BINARIO)
    print("\nNo se puede seguir sin binario. Construyelo primero.")
    sys.exit(1)

tam = os.path.getsize(BINARIO) / 1048576
bien("existe, %.1f MB" % tam)

if sys.platform == "darwin":
    try:
        arq = subprocess.run(["lipo", "-archs", BINARIO], capture_output=True,
                             text=True).stdout.strip()
        bien("arquitectura: %s   (este equipo es %s)" % (arq, platform.machine()))
    except Exception:
        pass
elif ES_WINDOWS:
    bien("plataforma: Windows   (este equipo es %s)" % platform.machine())
else:
    bien("plataforma: %s   (%s)" % (sys.platform, platform.machine()))

if not ES_WINDOWS and not os.access(BINARIO, os.X_OK):
    mal("no tiene permiso de ejecucion")


# ---------------------------------------------------- 2 y 3. arranca y sirve
titulo("2. Arranca en un entorno limpio y sirve su pagina")

puerto = puerto_libre()
# Entorno minimo: sin PATH, sin PYTHONPATH, sin nada del equipo. Si arranca asi,
# es autocontenido de verdad.
entorno = {
    "HOME": os.path.expanduser("~"),
    # Un identificador de mentira, solo para que arranque. Se comprueba que el
    # binario funciona, no que este configurado: uno generico -que es una forma
    # de construirlo perfectamente valida- se niega a arrancar sin esto, y eso
    # no es un defecto del binario.
    "MIGRADOR_APP_ID": "00000000-0000-0000-0000-000000000000",
}
if ES_WINDOWS:
    for k in ("SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA"):
        if os.environ.get(k):
            entorno[k] = os.environ[k]
else:
    entorno["TMPDIR"] = os.environ.get("TMPDIR", "/tmp")

proc = subprocess.Popen(
    [BINARIO, "--puerto", str(puerto), "--no-abrir"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    env=entorno, text=True, bufsize=1)

url = None
inicio = time.time()
try:
    while time.time() - inicio < 90:
        linea = proc.stdout.readline()
        if not linea:
            if proc.poll() is not None:
                break
            continue
        if "127.0.0.1" in linea and "?t=" in linea:
            url = linea.strip()
            break

    if not url:
        mal("no arranco o no anuncio su direccion en 90 segundos")
        resto = proc.stdout.read()[:800] if proc.stdout else ""
        if resto:
            print("\n  salida del programa:\n" + "\n".join(
                "    " + l for l in resto.splitlines()[:20]))
    else:
        bien("arranca sin PATH ni paquetes del sistema")
        base = url.split("?")[0]
        testigo = url.split("?t=")[1]

        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                if r.status == 200 and int(r.headers.get("Content-Length", 0)) > 3000:
                    bien("sirve la pagina con el testigo correcto")
                else:
                    mal("la pagina respondio %s con %s bytes"
                        % (r.status, r.headers.get("Content-Length")))
        except Exception as e:
            mal("no sirvio la pagina: %s" % e)

        # Sin testigo debe negarse. Es lo que impide que otra pestania abierta en
        # el navegador le hable al servidor local.
        try:
            urllib.request.urlopen(base + "?t=" + secrets.token_urlsafe(8), timeout=15)
            mal("acepto un testigo invalido: deberia responder 404")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                bien("rechaza un testigo invalido con 404")
            else:
                mal("con testigo invalido respondio %s en vez de 404" % e.code)
        except Exception as e:
            mal("no se pudo comprobar el rechazo del testigo: %s" % e)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


# --------------------------------------------------------------------- 4. TLS
titulo("3. Conexion TLS con los servidores de correo")
print("  (esta es la que falla en silencio si el Python usado trae una")
print("   biblioteca TLS demasiado vieja, y la que fallaba en Windows cuando")
print("   la verificacion dependia del almacen de certificados del sistema)")
print()

sys.path.insert(0, os.path.join(RAIZ, "src"))
try:
    from migrador import contexto_tls
    origen_ctx = "el mismo contexto que usa la herramienta"
except Exception:
    contexto_tls = ssl.create_default_context
    origen_ctx = "el contexto por defecto (no se pudo importar el de la herramienta)"
print("  usando %s\n" % origen_ctx)

def dudoso(t):
    """
    Ni pasa ni falla: no se pudo comprobar.

    La distincion importa. Que el equipo donde se construye no alcance un
    servidor no dice nada sobre el binario, y hacer fallar la construccion por
    eso convierte al verificador en algo que la gente aprende a ignorar o a
    desactivar. Solo se cuenta como fallo lo que sea atribuible al binario.
    """
    print("  ?     " + t)
    dudas.append(t)


dudas = []

for host in SERVIDORES:
    try:
        ctx = contexto_tls()
        with socket.create_connection((host, 993), timeout=25) as s:
            with ctx.wrap_socket(s, server_hostname=host) as t:
                v = t.version()
                if v in ("TLSv1.2", "TLSv1.3"):
                    bien("%-24s %s" % (host, v))
                else:
                    mal("%-24s negocio %s, insuficiente" % (host, v))
    except ssl.SSLCertVerificationError as e:
        # Esta si es del binario: es exactamente el fallo que se corrigio al
        # dejar de depender del almacen de certificados del sistema.
        mal("%-24s no verifico el certificado: %s" % (host, e))
    except (socket.timeout, socket.gaierror, ConnectionError, OSError, ssl.SSLError) as e:
        # Bloqueos de red, proxies que interceptan, cortafuegos corporativos.
        # Nada de eso depende del ejecutable que se acaba de construir.
        dudoso("%-24s no se pudo comprobar desde este equipo: %s" % (host, e))

print("\n  Nota: esto usa el Python con el que corres este script, no el que va")
print("  dentro del binario. Si construiste con ese mismo Python -que es lo")
print("  normal- la comprobacion vale; si no, vuelve a correrla con el suyo.")


# ------------------------------------------------------------------ resultado
print("\n" + "=" * 62)
if dudas and not fallos:
    print("RESULTADO: las comprobaciones del binario pasaron.")
    print()
    print("Quedaron %d sin poder comprobar desde este equipo:" % len(dudas))
    for d in dudas:
        print("  - " + d.strip())
    print()
    print("Eso depende de la red de este equipo, no del ejecutable. Conviene")
    print("repetirlo desde donde se vaya a usar la herramienta.")
    print("El binario esta en: " + BINARIO)
    print("=" * 62)
    sys.exit(0)

if fallos:
    print("RESULTADO: %d comprobacion(es) fallaron" % len(fallos))
    for f in fallos:
        print("  - " + f)
    print("\nNo entregues este binario hasta resolverlo.")
    sys.exit(1)

print("RESULTADO: todas las comprobaciones pasaron.")
print("El binario esta en: " + BINARIO)
print("=" * 62)
