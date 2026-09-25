#!/usr/bin/env python3
"""
Migrador de buzones: servidor IMAP -> Microsoft 365.

Que es y que no es. Es el motor, en linea de comandos. No trae interfaz grafica
todavia a proposito: el diseno de la interfaz depende de la velocidad real, y
este prototipo es justamente lo que la mide. Si un buzon de 16 GB proyecta dos
horas, la interfaz es una barra de progreso; si proyecta treinta, es un proceso
que sobrevive a que cierren la laptop, y eso es otro producto.

Decisiones apoyadas en lo que anuncian los servidores (verificado 2026-09-08):

  Origen  AUTH=PLAIN  UIDPLUS CHILDREN NAMESPACE IDLE UNSELECT
  EXO     AUTH=XOAUTH2 LOGINDISABLED UIDPLUS LITERAL+ MOVE SASL-IR

  - El origen solo habla PLAIN: necesita la contrasena del buzon, no OAuth.
  - EXO tiene LOGINDISABLED: OAuth es obligatorio, no opcional.
  - UIDPLUS en los dos lados permite reanudacion exacta. Cada APPEND devuelve
    APPENDUID con el UID que quedo en destino, asi que el registro de avance
    guarda la correspondencia real y no una heuristica por Message-ID (que
    falla en los mensajes que no lo traen).

Garantias del prototipo:
  - El origen se abre SIEMPRE readonly y se lee con BODY.PEEK[], asi que no
    marca como leido ni altera banderas.
  - Reanudable: se puede interrumpir con Ctrl-C y continuar donde iba.
  - Idempotente: correrlo dos veces no duplica nada.
  - Si cambia el UIDVALIDITY de una carpeta en origen, invalida el tramo de esa
    carpeta y la vuelve a migrar. Ignorar esto es la causa clasica de
    migraciones con huecos.

Uso:
    python3 migrador.py inventario --buzon <origen>
    python3 migrador.py migrar --buzon <origen> --upn <destino> --app-id <GUID> \
                               --destino "Migracion Titan" [--solo INBOX] [--limite-mb 50]
    python3 migrador.py estado
"""

import argparse
import base64
import getpass
import imaplib
import os
import platform
import re
import signal
import sqlite3
import ssl
import sys
import time

# Servidor IMAP de origen. Por defecto el de GoDaddy y Titan, que es el caso
# para el que se escribio; se cambia con MIGRADOR_HOST_ORIGEN o con config.json
# para migrar desde cualquier otro servidor IMAP.
EXO_HOST = "outlook.office365.com"
PUERTO = 993

# Tiempo maximo que una operacion de socket puede quedarse esperando.
#
# Sin esto, imaplib bloquea para siempre. Si la conexion muere en silencio -sin
# FIN ni RST, que es lo que hace un cortafuegos o un NAT al descartar un flujo
# inactivo- la lectura no vuelve nunca y no se lanza ninguna excepcion. Toda la
# reconexion, que solo actua ante errores, queda fuera de juego: el proceso
# parece vivo, no consume CPU, no tiene conexiones abiertas y no avanza.
#
# Ocurrio de verdad: una migracion se quedo parada mas de dos horas al 69% sin
# un solo mensaje de error. Cinco minutos es de sobra para el mensaje mas pesado
# y corta el bloqueo indefinido.
TIEMPO_ESPERA = 300
SCOPE_IMAP = ["https://outlook.office.com/IMAP.AccessAsUser.All"]

def _config(clave, variable):
    """
    Identificadores de la aplicacion de Entra y del tenant.

    No van escritos en el codigo a proposito. Este repositorio es publico, y el
    identificador de un cliente publico que ya tiene consentido el acceso a
    buzones para toda una organizacion le ahorra la mitad del trabajo a quien
    quiera montar un phishing por codigo de dispositivo contra ese tenant. El
    codigo es generico; los identificadores se ponen al construir o al lado del
    ejecutable.

    Se buscan en tres sitios, en este orden:

      1. Variable de entorno. Util para desarrollo y para apuntar a otro tenant
         sin reconstruir.
      2. Modulo generado durante la construccion, que es como viajan dentro de
         los binarios que se entregan.
      3. config.json junto al ejecutable, para cuando el binario es generico y la
         configuracion se distribuye aparte.
    """
    v = os.environ.get(variable)
    if v:
        return v

    try:
        import config_compilado
        v = getattr(config_compilado, clave, None)
        if v:
            return v
    except ImportError:
        pass

    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
        else os.path.dirname(os.path.abspath(__file__))
    ruta = os.path.join(base, "config.json")
    if os.path.exists(ruta):
        try:
            import json
            with open(ruta, encoding="utf-8") as f:
                return json.load(f).get(clave.lower())
        except Exception:
            pass
    return None


def contexto_tls():
    """
    Contexto TLS que verifica contra un paquete de raices propio.

    Por que no basta con ssl.create_default_context(). En Windows, ese contexto
    verifica contra el almacen de certificados del sistema, y Windows descarga
    las raices que le faltan bajo demanda durante sus propios handshakes.
    OpenSSL, que es lo que usa Python, no dispara esa descarga: en un equipo que
    todavia no tiene cacheada la raiz de Microsoft, la conexion falla con
    "unable to get local issuer certificate" aunque el equipo este sano y el
    certificado del servidor sea correcto.

    Usar el paquete de certifi, que viaja dentro del ejecutable, hace que el
    comportamiento sea el mismo en Windows, macOS y Linux y no dependa del
    estado del almacen de cada equipo.

    Si la organizacion inspecciona el trafico TLS con un proxy, su raiz no esta
    en certifi ni puede estarlo: para ese caso se admite un paquete propio via
    MIGRADOR_CA_BUNDLE o config.json.
    """
    propio = _config("CA_BUNDLE", "MIGRADOR_CA_BUNDLE")
    if propio and os.path.exists(propio):
        return ssl.create_default_context(cafile=propio)
    try:
        import certifi
        ruta = certifi.where()
        if os.path.exists(ruta):
            return ssl.create_default_context(cafile=ruta)
    except Exception:
        pass
    # Sin certifi se cae al almacen del sistema, que es mejor que nada.
    return ssl.create_default_context()


def _explicar_ssl(host, e):
    """Traduce un fallo de verificacion TLS a algo sobre lo que se pueda actuar."""
    return (
        "No se pudo establecer una conexion segura con %s.\n\n"
        "Detalle tecnico: %s\n\n"
        "Las causas habituales son dos:\n"
        "  - El equipo esta detras de un proxy que inspecciona el trafico. En ese\n"
        "    caso hace falta el certificado raiz de la organizacion: pideselo a\n"
        "    TI y apunta a el con la variable MIGRADOR_CA_BUNDLE, o ponlo en un\n"
        "    config.json junto al ejecutable como \"ca_bundle\".\n"
        "  - La fecha y hora del equipo estan mal, lo que invalida cualquier\n"
        "    certificado. Comprueba que sean correctas." % (host, e))


APP_ID_POR_DEFECTO = _config("APP_ID", "MIGRADOR_APP_ID")
HOST_ORIGEN = _config("HOST_ORIGEN", "MIGRADOR_HOST_ORIGEN") or "imap.secureserver.net"
TENANT_ID = _config("TENANT_ID", "MIGRADOR_TENANT_ID") or "organizations"

# De donde se lee. "imap": un servidor IMAP con usuario y contrasena, que es el
# caso original. "m365": otro buzon de Microsoft 365, abierto con el mismo token
# de quien inicia sesion; esa persona necesita acceso total (FullAccess) al
# buzon de origen y al de destino. Asi el buzon de alguien que se fue, o uno
# compartido, se vuelca en una carpeta del buzon de otra persona sin
# contrasenas ni permisos nuevos en la aplicacion.
MODO = (_config("MODO", "MIGRADOR_MODO") or "imap").strip().lower()
if MODO not in ("imap", "m365", "gmail"):
    sys.exit("MODO desconocido: %r. Validos: imap, m365, gmail" % MODO)
# "gmail" es IMAP con contrasena de aplicacion, pero con etiquetas en vez de
# carpetas: ver ordenar_gmail y nuevos_gmail.
if MODO == "gmail" and not _config("HOST_ORIGEN", "MIGRADOR_HOST_ORIGEN"):
    HOST_ORIGEN = "imap.gmail.com"


def dir_datos():
    """
    Carpeta donde viven el registro de avance y el token, por usuario.

    No puede ser la del programa. Empaquetado con PyInstaller, el programa se
    descomprime en un directorio temporal que se borra al salir: el registro
    desapareceria en cada cierre y la reanudacion, que es la garantia principal
    de esta herramienta, dejaria de existir sin que nadie lo notara.
    """
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    # Cada modo con su carpeta: comparten nombre de token y de registro, y dos
    # servidores de modos distintos corriendo a la vez se pisarian la sesion.
    d = os.path.join(base, {"m365": "MigradorM365", "gmail": "MigradorGmail"}.get(
        MODO, "MigradorCorreo"))
    os.makedirs(d, exist_ok=True)
    try:
        os.chmod(d, 0o700)   # el token que vive aqui vale como una sesion abierta
    except OSError:
        pass
    return d


LEDGER = os.path.join(dir_datos(), "avance.sqlite3")
BITACORA = os.path.join(dir_datos(), "registro.log")


def anotar(texto):
    """
    Deja constancia en un archivo de lo que va pasando.

    Por que hace falta. Esta herramienta corre en el equipo de otra persona, y
    cuando algo falla el mensaje aparece en una ventana que se cierra. Pedirle
    despues "que decia exactamente" no funciona: nadie guarda eso. Con un archivo
    hay algo concreto que enviar.

    No se escribe aqui nada que no deba salir del equipo: ni contrasenas, ni
    tokens, ni el contenido de los mensajes. Solo que se hizo y que fallo.
    """
    try:
        # Si crece demasiado se conserva la ultima mitad. Un registro que llena
        # el disco de alguien es peor que no tener registro.
        if os.path.exists(BITACORA) and os.path.getsize(BITACORA) > 2_000_000:
            with open(BITACORA, "r", encoding="utf-8", errors="replace") as f:
                cola = f.read()[-1_000_000:]
            with open(BITACORA, "w", encoding="utf-8") as f:
                f.write(cola)
        with open(BITACORA, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), texto))
    except OSError:
        pass   # no poder registrar nunca debe tumbar la migracion

# Banderas que tienen sentido copiar. \Recent no se puede fijar y es del servidor.
BANDERAS_UTILES = {"\\Seen", "\\Answered", "\\Flagged", "\\Draft"}

imaplib._MAXLINE = 10_000_000  # carpetas con muchos mensajes devuelven lineas largas

_parar = False


def _sigint(signum, frame):
    global _parar
    _parar = True
    print("\n\n  Interrupcion recibida. Cerrando limpio; el avance queda guardado.", flush=True)


signal.signal(signal.SIGINT, _sigint)


# --------------------------------------------------------------------------
# UTF-7 modificado (RFC 3501 5.1.3)
#
# Sin esto, las carpetas con acentos se crean con el nombre literal "&AOk-" en
# destino. Es donde se rompen la mayoria de las migraciones IMAP en espanol.
# --------------------------------------------------------------------------

def mutf7_codificar(texto):
    out, buf = [], []

    def volcar():
        if buf:
            u = "".join(buf).encode("utf-16-be")
            b = base64.b64encode(u).decode("ascii").rstrip("=")
            out.append("&" + b.replace("/", ",") + "-")
            buf.clear()

    for ch in texto:
        if ch == "&":
            volcar()
            out.append("&-")
        elif 0x20 <= ord(ch) <= 0x7E:
            volcar()
            out.append(ch)
        else:
            buf.append(ch)
    volcar()
    return "".join(out)


def mutf7_decodificar(texto):
    if isinstance(texto, bytes):
        texto = texto.decode("ascii", "replace")
    out, i = [], 0
    while i < len(texto):
        if texto[i] != "&":
            out.append(texto[i])
            i += 1
            continue
        j = texto.find("-", i)
        if j == -1:
            out.append(texto[i:])
            break
        trozo = texto[i + 1:j]
        if trozo == "":
            out.append("&")
        else:
            b = trozo.replace(",", "/")
            b += "=" * (-len(b) % 4)
            try:
                out.append(base64.b64decode(b).decode("utf-16-be"))
            except Exception:
                out.append(texto[i:j + 1])
        i = j + 1
    return "".join(out)


# --------------------------------------------------------------------------
# registro de avance
# --------------------------------------------------------------------------

class Registro:
    def __init__(self, ruta=LEDGER):
        self.db = sqlite3.connect(ruta)
        # WAL y synchronous=NORMAL hacen que confirmar despues de cada mensaje
        # cueste poco frente a la ida y vuelta de IMAP, que es lo que permite
        # guardar tan seguido sin penalizar la migracion.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS migrado (
                buzon TEXT NOT NULL,
                carpeta TEXT NOT NULL,
                uidvalidity INTEGER NOT NULL,
                uid_origen INTEGER NOT NULL,
                uid_destino INTEGER,
                bytes INTEGER,
                ts REAL,
                PRIMARY KEY (buzon, carpeta, uidvalidity, uid_origen)
            )""")
        # Que carpeta de destino se uso la ultima vez para este buzon.
        #
        # El registro de avance se indexa por buzon de origen, no por destino.
        # Sin esto, teclear el nombre de la carpeta con una mayuscula distinta
        # crea un destino nuevo, se copia todo otra vez y lo anterior queda
        # huerfano en una carpeta que nadie vuelve a mirar. Paso: cuatro intentos
        # produjeron cuatro carpetas.
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS destino_usado (
                buzon TEXT PRIMARY KEY,
                destino TEXT,
                ts REAL
            )""")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS fallo (
                buzon TEXT, carpeta TEXT, uid_origen INTEGER,
                motivo TEXT, ts REAL
            )""")
        self.db.commit()

    def hechos(self, buzon, carpeta, uidvalidity):
        cur = self.db.execute(
            "SELECT uid_origen FROM migrado WHERE buzon=? AND carpeta=? AND uidvalidity=?",
            (buzon, carpeta, uidvalidity))
        return {r[0] for r in cur}

    def invalidar_carpeta(self, buzon, carpeta, uidvalidity_vieja):
        self.db.execute(
            "DELETE FROM migrado WHERE buzon=? AND carpeta=? AND uidvalidity=?",
            (buzon, carpeta, uidvalidity_vieja))
        self.db.commit()

    def uidvalidity_previa(self, buzon, carpeta):
        cur = self.db.execute(
            "SELECT DISTINCT uidvalidity FROM migrado WHERE buzon=? AND carpeta=?",
            (buzon, carpeta))
        v = [r[0] for r in cur]
        return v[0] if len(v) == 1 else None

    def anotar(self, buzon, carpeta, uidvalidity, uid_o, uid_d, nbytes):
        self.db.execute(
            "INSERT OR REPLACE INTO migrado VALUES (?,?,?,?,?,?,?)",
            (buzon, carpeta, uidvalidity, uid_o, uid_d, nbytes, time.time()))

    def anotar_fallo(self, buzon, carpeta, uid, motivo):
        self.db.execute("INSERT INTO fallo VALUES (?,?,?,?,?)",
                        (buzon, carpeta, uid, str(motivo)[:400], time.time()))
        self.db.commit()

    def destino_previo(self, buzon):
        f = self.db.execute("SELECT destino FROM destino_usado WHERE buzon=?",
                            (buzon,)).fetchone()
        return f[0] if f else None

    def fijar_destino(self, buzon, destino):
        self.db.execute("INSERT OR REPLACE INTO destino_usado VALUES (?,?,?)",
                        (buzon, destino, time.time()))
        self.db.commit()

    def commit(self):
        self.db.commit()

    def resumen(self):
        cur = self.db.execute(
            "SELECT buzon, carpeta, COUNT(*), COALESCE(SUM(bytes),0) "
            "FROM migrado GROUP BY buzon, carpeta ORDER BY buzon, carpeta")
        return cur.fetchall()

    def fallos(self):
        return self.db.execute(
            "SELECT buzon, carpeta, uid_origen, motivo FROM fallo ORDER BY ts DESC LIMIT 40"
        ).fetchall()


# --------------------------------------------------------------------------
# conexiones
# --------------------------------------------------------------------------

class CredencialOrigen(Exception):
    """No se pudo entrar en el buzon de origen. Lleva ya el texto para la persona."""


def conectar_titan(buzon, password, intentos=3):
    """
    Abre la sesion en el buzon de origen, reintentando con espera creciente.

    Muchos servidores limitan el ritmo de inicios de sesion y, al aplicarlo, responden
    AUTHENTICATIONFAILED: el mismo error que una contrasena equivocada. No hay
    forma de distinguirlos desde aqui, asi que primero se reintenta con pausa
    (si es limitacion, cede) y solo despues se avisa, nombrando las dos causas
    posibles en vez de acusar a la persona de haberse equivocado.
    """
    espera = 15
    ultimo = None
    for i in range(intentos):
        try:
            c = imaplib.IMAP4_SSL(HOST_ORIGEN, PUERTO, ssl_context=contexto_tls(),
                                  timeout=TIEMPO_ESPERA)
            c.login(buzon, password)
            return c
        except ssl.SSLError as e:
            raise CredencialOrigen(_explicar_ssl(HOST_ORIGEN, e))
        except imaplib.IMAP4.error as e:
            ultimo = e
            if i < intentos - 1:
                time.sleep(espera)
                espera *= 2
    raise CredencialOrigen(
        "No se pudo entrar en %s. Revisa que la contrasena sea la correcta; si lo es, "
        "el servidor del correo antiguo esta limitando los intentos y conviene esperar "
        "unos minutos antes de volver a probar. (%s)" % (buzon, ultimo))


def obtener_token(app_id, cache_ruta=None, al_mostrar_codigo=None):
    try:
        import msal
    except ImportError:
        sys.exit("Falta msal.  pip3 install msal")
    cache = msal.SerializableTokenCache()
    cache_ruta = cache_ruta or os.path.join(dir_datos(), "token.json")
    if os.path.exists(cache_ruta):
        cache.deserialize(open(cache_ruta).read())
    app = msal.PublicClientApplication(
        app_id, authority=f"https://login.microsoftonline.com/{TENANT_ID}", token_cache=cache)
    res = None
    cuentas = app.get_accounts()
    if cuentas:
        res = app.acquire_token_silent(SCOPE_IMAP, account=cuentas[0])
    if not res:
        flujo = app.initiate_device_flow(scopes=SCOPE_IMAP)
        if "user_code" not in flujo:
            raise RuntimeError("No se pudo iniciar device flow: "
                               + str(flujo.get("error_description")))
        if al_mostrar_codigo:
            al_mostrar_codigo(flujo)
        else:
            print("\n" + flujo["message"] + "\n", flush=True)
        res = app.acquire_token_by_device_flow(flujo)
    if "access_token" not in res:
        raise RuntimeError("Fallo la autenticacion: " + str(res.get("error_description")))
    if cache.has_state_changed:
        with open(cache_ruta, "w") as f:
            f.write(cache.serialize())
        os.chmod(cache_ruta, 0o600)
    return res["access_token"]


def conectar_exo(upn, token):
    try:
        c = imaplib.IMAP4_SSL(EXO_HOST, PUERTO, ssl_context=contexto_tls(),
                              timeout=TIEMPO_ESPERA)
    except ssl.SSLError as e:
        raise RuntimeError(_explicar_ssl(EXO_HOST, e))
    cadena = f"user={upn}\x01auth=Bearer {token}\x01\x01"
    c.authenticate("XOAUTH2", lambda _: cadena.encode())
    return c


def problema_origen_destino(modo, buzon, upn):
    """
    None si origen y destino se pueden combinar; si no, el motivo.

    En modo m365 copiar un buzon sobre si mismo no falla ni avisa: crea dentro de
    el una carpeta con todo su contenido, y parece una migracion buena. Ocurrio
    de verdad al teclear el mismo buzon en los dos campos. En los otros modos las
    direcciones iguales son sistemas distintos y no es el mismo buzon.
    """
    if modo == "m365" and buzon.strip().lower() == upn.strip().lower():
        return ("El buzon de origen y el de destino son el mismo (%s). Copiarlo sobre "
                "si mismo lo duplica dentro de si. Revisa el campo de destino." % buzon.strip())
    return None


def es_carpeta_previa(modo, carpeta, destino):
    """
    True si la carpeta de origen es la carpeta de destino de una migracion anterior
    (o esta dentro), que hay que omitir en modo m365.

    Un buzon que ya fue destino de otra migracion contiene la carpeta de aquella
    copia. Volver a copiarla con el buzon entero duplicaria todo en el nuevo
    destino. Se compara la carpeta entera o su ruta, no un simple prefijo de
    texto: "Correo pfagoni viejo" no es la misma carpeta que "Correo pfagoni".
    """
    if modo != "m365" or not destino:
        return False
    return carpeta == destino or carpeta.startswith(destino + "/")


def conectar_origen(buzon, password, app_id):
    """Abre el buzon de origen segun el modo."""
    if MODO == "gmail":
        try:
            return conectar_titan(buzon, password)
        except CredencialOrigen as e:
            raise CredencialOrigen(
                "Gmail no acepto el inicio de sesion de %s. Tiene que ser una "
                "contrasena de aplicacion (16 letras, se crea en "
                "myaccount.google.com/apppasswords con la verificacion en dos "
                "pasos activada), no la contrasena normal de la cuenta. "
                "Detalle: %s" % (buzon, e))
    if MODO != "m365":
        return conectar_titan(buzon, password)
    try:
        return conectar_exo(buzon, obtener_token(app_id))
    except imaplib.IMAP4.error as e:
        # Exchange responde igual a "no existe", "sin IMAP" y "sin permiso":
        # se nombran las tres en vez de adivinar.
        raise CredencialOrigen(
            "Microsoft 365 no dejo abrir %s con la cuenta que inicio sesion. "
            "Comprueba que esa cuenta tenga acceso total (FullAccess) al buzon, "
            "que el buzon tenga IMAP habilitado y que la direccion sea la "
            "correcta. Un permiso recien concedido puede tardar hasta una hora "
            "en aplicar. (%s)" % (buzon, e))


# --------------------------------------------------------------------------
# lectura de estructura
# --------------------------------------------------------------------------

_RE_LIST = re.compile(rb'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<nombre>.*)$')


def listar_carpetas(c):
    """Devuelve [(nombre_crudo, nombre_legible, delimitador)] solo seleccionables."""
    ok, datos = c.list()
    if ok != "OK":
        raise RuntimeError("LIST fallo")
    salida = []
    for linea in datos:
        if not isinstance(linea, bytes):
            continue
        m = _RE_LIST.match(linea)
        if not m:
            continue
        flags = m.group("flags").decode()
        if "\\Noselect" in flags:
            continue
        delim = m.group("delim").decode() or "/"
        crudo = m.group("nombre").decode("ascii", "replace").strip()
        if crudo.startswith('"') and crudo.endswith('"'):
            crudo = crudo[1:-1]
        salida.append((crudo, mutf7_decodificar(crudo), delim))
    return salida


def listar_carpetas_con_atributos(c):
    """Como listar_carpetas, pero con los atributos de LIST: [(crudo, legible, atributos)]."""
    ok, datos = c.list()
    if ok != "OK":
        raise RuntimeError("LIST fallo")
    salida = []
    for linea in datos:
        if not isinstance(linea, bytes):
            continue
        m = _RE_LIST.match(linea)
        if not m:
            continue
        flags = m.group("flags").decode()
        if "\\Noselect" in flags:
            continue
        crudo = m.group("nombre").decode("ascii", "replace").strip()
        if crudo.startswith('"') and crudo.endswith('"'):
            crudo = crudo[1:-1]
        salida.append((crudo, mutf7_decodificar(crudo), flags))
    return salida


# Gmail no tiene carpetas sino etiquetas. Por IMAP cada etiqueta es una carpeta,
# asi que un mensaje con tres etiquetas aparece tres veces, y "Todos" los
# contiene a todos otra vez. Copiar carpeta por carpeta, como con cualquier otro
# servidor, duplica el buzon entero como minimo.
#
# Se decide por el atributo de uso especial (RFC 6154) y no por el nombre, que
# cambia con el idioma de la cuenta: "[Gmail]/Enviados", "[Gmail]/Sent Mail"...
_GMAIL_NOMBRE = {"\\sent": "Enviados", "\\drafts": "Borradores", "\\all": "Archivados"}
# Vistas, no ubicaciones: Destacados e Importantes son marcas sobre mensajes que
# ya estan en otra carpeta. Spam y Papelera se dejan fuera por decision: Gmail
# los vacia solo a los 30 dias.
_GMAIL_FUERA = {"\\flagged", "\\important", "\\junk", "\\trash"}


def ordenar_gmail(carpetas):
    """
    [(crudo, legible, atributos)] -> [(crudo, nombre_en_destino)], en el orden
    en que se reparte cada mensaje: el que aparece en varias carpetas va a la
    primera. Recibidos, Enviados, Borradores, etiquetas por nombre, y al final
    Todos, que solo aporta lo archivado sin etiqueta.
    """
    especiales, etiquetas, todos = [], [], []
    for crudo, legible, atributos in carpetas:
        attrs = set(a.lower() for a in atributos.split())
        if attrs & _GMAIL_FUERA:
            continue
        if crudo.upper() == "INBOX":
            especiales.append((0, crudo, "INBOX"))
        elif "\\sent" in attrs:
            especiales.append((1, crudo, _GMAIL_NOMBRE["\\sent"]))
        elif "\\drafts" in attrs:
            especiales.append((2, crudo, _GMAIL_NOMBRE["\\drafts"]))
        elif "\\all" in attrs:
            todos.append((crudo, _GMAIL_NOMBRE["\\all"]))
        else:
            etiquetas.append((crudo, legible))
    especiales.sort()
    etiquetas.sort(key=lambda x: x[1].lower())
    return [(c, n) for _, c, n in especiales] + etiquetas + todos


def gmail_reconocible(carpetas):
    """
    True si LIST trajo los atributos de uso especial que hacen falta, en concreto
    el de "Todos". Gmail no anuncia SPECIAL-USE antes de iniciar sesion, asi que
    no se puede dar por hecho: sin el atributo, "Todos" y "Spam" parecerian
    etiquetas normales, se copiaria el buzon dos veces y llegaria el spam. Es
    preferible negarse a copiar.
    """
    return any("\\all" in set(a.lower() for a in atributos.split())
               for _, _, atributos in carpetas)


def nuevos_gmail(ids, vistos):
    """
    ids: {uid: msgid} de una carpeta. Devuelve los uid, en orden, cuyo mensaje no
    salio ya en una carpeta anterior, y anota todos los msgid como vistos.

    Un uid sin msgid se copia: ante la duda es mejor un duplicado que un
    mensaje perdido.
    """
    salida = []
    for uid in sorted(ids):
        msgid = ids[uid]
        if msgid is None or msgid not in vistos:
            salida.append(uid)
        if msgid is not None:
            vistos.add(msgid)
    return salida


def parsear_msgids(datos):
    """Respuesta de UID FETCH (X-GM-MSGID) -> {uid: msgid}."""
    salida = {}
    for linea in datos or []:
        if isinstance(linea, tuple):
            linea = linea[0]
        if not isinstance(linea, bytes):
            continue
        u = re.search(rb"UID (\d+)", linea)
        m = re.search(rb"X-GM-MSGID (\d+)", linea)
        if u:
            salida[int(u.group(1))] = m.group(1).decode() if m else None
    return salida


def uidvalidity_de(c, carpeta_cruda):
    ok, datos = c.select(f'"{carpeta_cruda}"', readonly=True)
    if ok != "OK":
        return None, 0
    uv = None
    for r in c.untagged_responses.get("UIDVALIDITY", []):
        uv = int(r)
    total = int(datos[0]) if datos and datos[0] else 0
    return uv, total


# --------------------------------------------------------------------------
# inventario
# --------------------------------------------------------------------------

def cmd_inventario(args):
    password = getpass.getpass(f"Contrasena de {args.buzon}: ")
    print(f"\nConectando a {HOST_ORIGEN} ...", flush=True)
    c = conectar_titan(args.buzon, password)
    carpetas = listar_carpetas(c)
    print(f"{len(carpetas)} carpetas seleccionables\n")
    print(f"{'CARPETA':<44}{'MENSAJES':>10}{'TAMANO':>13}")
    print("-" * 67)
    tm = tb = 0
    for crudo, legible, _ in carpetas:
        uv, total = uidvalidity_de(c, crudo)
        tam = 0
        if total:
            ok, res = c.uid("SEARCH", None, "ALL")
            ids = res[0].split() if ok == "OK" and res[0] else []
            for i in range(0, len(ids), 500):
                lote = b",".join(ids[i:i + 500])
                ok, sz = c.uid("FETCH", lote, "(RFC822.SIZE)")
                if ok == "OK":
                    for it in sz:
                        if isinstance(it, bytes):
                            m = re.search(rb"RFC822\.SIZE (\d+)", it)
                            if m:
                                tam += int(m.group(1))
        tm += total
        tb += tam
        print(f"{legible[:43]:<44}{total:>10}{tam/1048576:>11.1f} MB")
    print("-" * 67)
    print(f"{'TOTAL':<44}{tm:>10}{tb/1048576:>11.1f} MB")
    c.logout()


# --------------------------------------------------------------------------
# migracion
# --------------------------------------------------------------------------

def asegurar_carpeta(dst, ruta_legible, existentes=None, delim_dst="/"):
    """
    Crea la ruta en destino nivel por nivel y devuelve el nombre crudo.

    `existentes` es el conjunto de rutas que ya estan en destino. Sin el, esta
    funcion lanza un CREATE por cada nivel de cada carpeta en cada corrida:
    para diecisiete carpetas son unos cuarenta comandos, casi todos inutiles, y
    Exchange Online responde cortando la conexion. Con el, solo se crea lo que
    falta y una segunda corrida no manda ningun CREATE.
    """
    partes = ruta_legible.split("/")
    acumulado = []
    for p in partes:
        acumulado.append(p)
        ruta = delim_dst.join(acumulado)
        if existentes is not None and ruta in existentes:
            continue
        dst.create('"%s"' % mutf7_codificar(ruta))
        if existentes is not None:
            existentes.add(ruta)
    return mutf7_codificar(delim_dst.join(partes))


def contar_en_destino(dst, ruta_legible, delim_dst="/"):
    """Mensajes que hay realmente en una carpeta de destino. 0 si no existe."""
    try:
        ok, datos = dst.select('"%s"' % mutf7_codificar(ruta_legible), readonly=True)
        if ok != "OK":
            return 0
        return int(datos[0]) if datos and datos[0] else 0
    except Exception:
        return 0


def parsear_appenduid(resp):
    """Extrae el UID destino de la respuesta APPENDUID (capacidad UIDPLUS)."""
    try:
        txt = resp[0].decode() if isinstance(resp[0], bytes) else str(resp[0])
    except Exception:
        return None
    m = re.search(r"APPENDUID\s+\d+\s+(\d+)", txt)
    return int(m.group(1)) if m else None


def banderas_limpias(cadena):
    if not cadena:
        return ""
    hallados = re.findall(r"\\\w+", cadena)
    utiles = [f for f in hallados if f in BANDERAS_UTILES]
    return " ".join(utiles)


def migrar_buzon(buzon, password, upn, app_id, destino="Migracion Titan",
                 solo=None, limite_mb=0, progreso=None, debe_parar=None):
    """
    Copia un buzon de Titan a Microsoft 365. Devuelve el resumen final.

    progreso     invocable que recibe un dict por evento. Es lo que permite que
                 la misma funcion sirva a la linea de comandos y a la interfaz
                 grafica sin duplicar el bucle.
    debe_parar   invocable sin argumentos; si devuelve True, corta limpio. Se
                 usa para el boton de detener de la interfaz, igual que Ctrl-C
                 en la consola.
    """
    def emitir(tipo, **kw):
        if progreso:
            kw["tipo"] = tipo
            # Todo aviso lleva nivel. "info" se muestra a la persona; "tecnico"
            # solo se guarda para el reporte.
            if tipo == "aviso":
                kw.setdefault("nivel", "info")
                kw.setdefault("detalle", kw.get("texto", ""))
                kw.setdefault("texto", None)
                anotar("aviso: " + str(kw.get("detalle") or kw.get("texto")))
            elif tipo in ("estado", "plan", "fin"):
                anotar("%s: %s" % (tipo, {k: v for k, v in kw.items()
                                          if k not in ("tipo", "codigo", "url")}))
            progreso(kw)

    def parar():
        if _parar:
            return True
        return bool(debe_parar and debe_parar())

    problema = problema_origen_destino(MODO, buzon, upn)
    if problema:
        anotar("RECHAZADO: " + problema)
        raise RuntimeError(problema)

    reg = Registro()
    anotar("=" * 60)
    anotar("inicio de migracion  origen=%s  destino=%s  carpeta=%r"
           % (buzon, upn, destino))
    anotar("plataforma %s %s  modo %s  host origen %s" % (
        sys.platform, platform.machine(), MODO,
        EXO_HOST if MODO == "m365" else HOST_ORIGEN))
    # Si la carpeta de destino cambio respecto a la corrida anterior, hay que
    # decirlo antes de copiar nada: lo que ya estaba migrado no se vera, y se
    # volvera a copiar entero en el sitio nuevo.
    previo = reg.destino_previo(buzon)
    if previo is not None and previo != destino:
        emitir("aviso",
               texto=("Antes se copio a la carpeta %r y ahora se indico %r. "
                      "Lo ya copiado quedara en la carpeta anterior y se volvera "
                      "a copiar todo en la nueva." % (previo, destino)),
               detalle="cambio de carpeta de destino: %r -> %r" % (previo, destino))
        anotar("AVISO: la carpeta de destino cambio de %r a %r" % (previo, destino))
    reg.fijar_destino(buzon, destino)

    # El orden importa, y antes estaba al reves.
    #
    # Autenticar en Microsoft exige que una persona abra el navegador, teclee un
    # codigo e inicie sesion. Eso tarda minutos, no segundos. Si el buzon de
    # origen se conecta antes, su sesion se queda ociosa durante toda esa espera
    # y el servidor la cierra: al volver, el primer comando muere con un "broken
    # pipe" que no le dice nada a nadie. Le ocurre a cualquiera que no sea
    # instantaneo autenticandose, o sea, a todo el mundo.
    #
    # Primero el token, que es la parte lenta y la que depende de una persona.
    # Solo despues se abren las dos sesiones IMAP, que asi nacen y se usan
    # seguidas.
    emitir("estado", texto="Autenticando en Microsoft 365")
    token = obtener_token(app_id, al_mostrar_codigo=lambda f: emitir(
        "codigo", codigo=f.get("user_code"), url=f.get("verification_uri"),
        mensaje=f.get("message")))

    emitir("estado", texto="Conectando con Microsoft 365")
    dst = conectar_exo(upn, token)

    emitir("estado", texto="Conectando con el buzon de origen")
    src = conectar_origen(buzon, password, app_id)
    emitir("estado", texto="Ambos extremos conectados")
    anotar("conexiones abiertas despues de autenticar")

    # Se lee una sola vez que hay en destino. Es la diferencia entre una segunda
    # corrida silenciosa y una que vuelve a intentar crearlo todo.
    existentes = set()
    try:
        for _, legible_dst, _ in listar_carpetas(dst):
            existentes.add(legible_dst)
    except Exception as e:
        emitir("aviso", nivel="tecnico",
               detalle="no se pudo listar el destino: %s" % e)

    if MODO == "gmail":
        con_atributos = listar_carpetas_con_atributos(src)
        if not gmail_reconocible(con_atributos):
            raise RuntimeError(
                "Gmail no devolvio los atributos que identifican la carpeta "
                "\"Todos\" (\\All). Sin ellos no se puede evitar copiar todo dos "
                "veces ni traer el spam, asi que no se copio nada. Carpetas vistas: "
                + ", ".join(l for _, l, _ in con_atributos)[:300])
        carpetas = [(c, n, "/") for c, n in ordenar_gmail(con_atributos)]
    else:
        carpetas = listar_carpetas(src)
    vistos_gmail = set()
    repetidos_gmail = 0
    if MODO == "m365":
        previas = [l for _, l, _ in carpetas if es_carpeta_previa(MODO, l, destino)]
        if previas:
            carpetas = [c for c in carpetas if not es_carpeta_previa(MODO, c[1], destino)]
            emitir("aviso",
                   texto=("El buzon de origen ya tiene una carpeta %r de una migracion "
                          "anterior; se omite para no copiarla otra vez." % destino),
                   detalle="omitidas carpetas previas: %s" % ", ".join(previas))
    if solo:
        carpetas = [c for c in carpetas if c[1] == solo]
        if not carpetas:
            raise RuntimeError("No existe la carpeta %r en origen." % solo)

    # Recuento previo, para poder dar un porcentaje honesto desde el principio.
    plan = []
    total_pendientes = 0
    for crudo, legible, _ in carpetas:
        uv, total = uidvalidity_de(src, crudo)
        if uv is None:
            continue
        # Un buzon de Microsoft 365 expone Calendario, Contactos, Tareas y
        # otras carpetas sin mensajes. Crearlas vacias en el destino solo
        # ensucia la carpeta de la persona que recibe.
        if MODO == "m365" and not total:
            continue
        previa = reg.uidvalidity_previa(buzon, legible)
        if previa is not None and previa != uv:
            emitir("aviso", texto="UIDVALIDITY cambio en %s; se remigra" % legible)
            reg.invalidar_carpeta(buzon, legible, previa)
        ya = reg.hechos(buzon, legible, uv)

        # El registro dice lo que se ENVIO, no lo que ESTA. Si el buzon de
        # destino perdio mensajes -por un borrado, por una restauracion, por lo
        # que sea-, creerle al registro significa no copiar nada y anunciar
        # exito con el destino vacio. Es el peor fallo posible en una
        # herramienta de migracion: perdida silenciosa con luz verde.
        #
        # Por eso se cuenta lo que hay de verdad. Si falta, el tramo se invalida
        # y se vuelve a copiar.
        if ya:
            destino_legible = "%s/%s" % (destino, legible) if destino else legible
            hay = contar_en_destino(dst, destino_legible)
            if hay < len(ya):
                emitir("aviso",
                       texto="%s: faltan mensajes en el destino, se vuelven a copiar"
                       % legible,
                       detalle="el registro anotaba %d y en destino hay %d"
                       % (len(ya), hay))
                reg.invalidar_carpeta(buzon, legible, uv)
                ya = set()

        ok, res = src.uid("SEARCH", None, "ALL")
        uids = [int(x) for x in (res[0].split() if ok == "OK" and res[0] else [])]
        if MODO == "gmail" and uids:
            ok, res = src.uid("FETCH", "1:*", "(X-GM-MSGID)")
            ids = parsear_msgids(res if ok == "OK" else [])
            unicos = nuevos_gmail({u: ids.get(u) for u in uids}, vistos_gmail)
            repetidos_gmail += len(uids) - len(unicos)
            uids = unicos
        pendientes = [u for u in uids if u not in ya]
        plan.append((crudo, legible, uv, pendientes, total, len(ya)))
        total_pendientes += len(pendientes)
    if MODO == "gmail":
        emitir("aviso",
               texto=("Gmail: %d mensajes distintos; %d apariciones repetidas por "
                      "etiquetas se copian una sola vez. Spam y Papelera no se "
                      "copian." % (len(vistos_gmail), repetidos_gmail)),
               detalle="gmail: %d msgid unicos, %d repetidos omitidos"
               % (len(vistos_gmail), repetidos_gmail))
    emitir("plan", carpetas=len(plan), pendientes=total_pendientes)

    limite = limite_mb * 1048576 if limite_mb else None
    t_inicio = time.time()
    tot_msj = tot_bytes = tot_salt = tot_err = 0

    # Las conexiones viven en un dict porque se reemplazan al reconectar y hay
    # funciones anidadas que necesitan ver el reemplazo.
    conex = {"src": src, "dst": dst}
    # Una conexion recien abierta esta en estado AUTH, sin carpeta seleccionada.
    # Reconectar y seguir leyendo sin volver a seleccionar falla con "FETCH
    # illegal in state AUTH". Aqui se recuerda cual estaba abierta para poder
    # dejar la sesion nueva como estaba la vieja.
    seleccion = {"src": None}

    historial_reconexion = []

    def reconectar(motivo, extremo="ambos"):
        """
        Rehace las dos conexiones con espera creciente.

        Exchange Online corta la sesion por inactividad y por limitacion de
        velocidad, y lo hace sin aviso: la primera senal es un EOF en medio de
        un comando. Reintentar de inmediato suele volver a fallar, asi que la
        espera se duplica hasta un minuto.
        """
        # Dos niveles a proposito. Lo que ve la persona tiene que ser tranquilo y
        # en su idioma; "socket error: EOF" no le dice nada y le hace pensar que
        # algo se rompio. El detalle tecnico se guarda igual, para el reporte y
        # para diagnosticar, pero no se le pone delante.
        emitir("aviso", texto="Se interrumpio la conexion. Reconectando",
               detalle="conexion perdida: %s" % motivo)
        # Se rehace SOLO el extremo que fallo. Antes se rehacian los dos, de
        # modo que un corte del destino provocaba un inicio de sesion nuevo en
        # el origen; encadenados, esos inicios agotan el limite de intentos del
        # servidor de origen y la migracion muere por un problema del otro lado.
        # Paso: veinte reconexiones en un minuto dejaron a Titan rechazando la
        # contrasena.
        cuales = ("src", "dst") if extremo == "ambos" else (extremo,)
        for c in cuales:
            try:
                conex[c].logout()
            except Exception:
                pass

        # Freno cuando las reconexiones se amontonan: si se encadenan, el
        # problema no es un corte puntual sino algo sistemico, y reintentar cada
        # pocos segundos lo empeora en vez de resolverlo.
        ahora = time.time()
        historial_reconexion.append(ahora)
        del historial_reconexion[:-20]
        recientes = [t for t in historial_reconexion if ahora - t < 300]
        if len(recientes) > 4:
            calma = min(30 * len(recientes), 600)
            emitir("aviso",
                   texto="Demasiadas interrupciones seguidas. Esperando antes de continuar",
                   detalle="%d reconexiones en 5 min; pausa de %ds" % (len(recientes), calma))
            time.sleep(calma)
        # Doce intentos con techo de cinco minutos cubren mas de media hora. El
        # caso que obliga a ser asi de paciente no es la red: es que la persona
        # cierre la tapa del portatil a media migracion. Con seis intentos y
        # techo de un minuto, una suspension de diez minutos mataba una corrida
        # de horas.
        espera = 2
        for intento in range(12):
            time.sleep(espera)
            try:
                if "src" in cuales:
                    conex["src"] = conectar_origen(buzon, password, app_id)
                if "dst" in cuales:
                    conex["dst"] = conectar_exo(upn, obtener_token(app_id))
                # Restaurar la carpeta seleccionada es parte de reconectar, no un
                # extra: sin esto la sesion nueva no sirve para seguir leyendo y
                # el siguiente FETCH muere. Reconectar bien significa dejarlo
                # todo como estaba, no solo abrir el socket.
                if "src" in cuales and seleccion["src"]:
                    conex["src"].select('"%s"' % seleccion["src"], readonly=True)
                emitir("aviso", texto="Conexion restablecida. La copia continua",
                       detalle="reconectado en el intento %d, carpeta %s"
                       % (intento + 1, seleccion["src"]))
                return True
            except Exception as e:
                # Los reintentos fallidos no se le muestran: son ruido mientras
                # el proceso todavia se esta resolviendo solo.
                emitir("aviso", nivel="tecnico",
                       detalle="reintento %d fallido: %s" % (intento + 1, e))
                espera = min(espera * 2, 300)
        emitir("aviso", texto="No se pudo restablecer la conexion",
               detalle="agotados los 12 reintentos de reconexion")
        return False

    def con_reintento(descripcion, fn, intentos=4, extremo="ambos"):
        """Ejecuta fn reconectando si la conexion se cae. Devuelve (ok, valor)."""
        for i in range(intentos):
            try:
                return True, fn()
            except (imaplib.IMAP4.abort, OSError) as e:
                if i == intentos - 1 or not reconectar("%s: %s" % (descripcion, e), extremo):
                    return False, None
            except imaplib.IMAP4.error as e:
                # "illegal in state AUTH" significa que la sesion perdio la
                # carpeta seleccionada. Es un problema de estado de la conexion,
                # no del mensaje, y se arregla reconectando; tratarlo como error
                # del mensaje aborta la migracion entera por algo recuperable.
                if "illegal in state" not in str(e):
                    raise
                if i == intentos - 1 or not reconectar("%s: %s" % (descripcion, e), extremo):
                    return False, None
        return False, None

    for crudo, legible, uv, pendientes, total, ya_n in plan:
        if parar():
            break
        destino_legible = "%s/%s" % (destino, legible) if destino else legible
        emitir("carpeta", nombre=legible, destino=destino_legible,
               total=total, ya=ya_n, pendientes=len(pendientes))
        # La carpeta se crea ANTES de mirar si hay mensajes pendientes. Una
        # carpeta vacia en origen es parte de como la persona organizo su buzon
        # y debe existir en destino; omitirla es perder informacion, aunque no
        # sean correos. Crear una que ya existe no hace danio.
        #
        # Esto tampoco puede quedar fuera de la proteccion de reconexion: antes
        # cualquier corte al crear una carpeta abortaba la corrida entera.
        ok, crudo_dst = con_reintento("crear carpeta " + destino_legible,
                                      lambda: asegurar_carpeta(conex["dst"], destino_legible,
                                                               existentes),
                                      extremo="dst")
        if not ok:
            emitir("aviso", texto="No se pudo crear la carpeta %s; se omite" % legible,
                   detalle="fallo al crear %s en destino" % destino_legible)
            reg.anotar_fallo(buzon, legible, 0, "no se pudo crear la carpeta en destino")
            tot_err += 1
            continue
        if not pendientes:
            continue
        seleccion["src"] = crudo
        con_reintento("seleccionar " + legible,
                      lambda: conex["src"].select('"%s"' % crudo, readonly=True),
                      extremo="src")

        hechos_carpeta = 0
        # Exchange Online marca \Seen en TODO lo que recibe por APPEND, aunque el
        # origen no lo tuviera y aunque se le pase una lista de banderas explicita.
        # Sin corregirlo, la persona recibe su archivo historico entero marcado
        # como leido. Se anotan aqui los UID de destino que deben quedar sin leer
        # y se corrigen en bloque al cerrar la carpeta: un solo STORE en vez de
        # uno por mensaje.
        sin_leer_dst = []
        for uid in pendientes:
            if parar():
                break

            def traer():
                return conex["src"].uid("FETCH", str(uid), "(FLAGS INTERNALDATE BODY.PEEK[])")

            ok, r = con_reintento("leer uid %d" % uid, traer, extremo="src")
            if not ok:
                reg.anotar_fallo(buzon, legible, uid, "no se pudo leer tras reconectar")
                tot_err += 1
                continue
            estado, d = r
            if estado != "OK" or not d or not isinstance(d[0], tuple):
                reg.anotar_fallo(buzon, legible, uid, "FETCH vacio")
                tot_err += 1
                continue

            meta = d[0][0].decode("utf-8", "replace")
            cuerpo = d[0][1]
            if limite and len(cuerpo) > limite:
                emitir("aviso",
                       texto="Un mensaje de %.1f MB se omitio por superar el limite"
                       % (len(cuerpo) / 1048576),
                       detalle="uid %d omitido en %s: %d bytes" % (uid, legible, len(cuerpo)))
                tot_salt += 1
                continue

            mflags = re.search(r"FLAGS \(([^)]*)\)", meta)
            flags = banderas_limpias(mflags.group(1) if mflags else "")
            mfecha = re.search(r'INTERNALDATE "([^"]+)"', meta)
            fecha = '"%s"' % mfecha.group(1) if mfecha else None

            def escribir():
                return conex["dst"].append('"%s"' % crudo_dst,
                                           "(%s)" % flags if flags else "", fecha, cuerpo)

            ok, r = con_reintento("escribir uid %d" % uid, escribir, extremo="dst")
            if not ok:
                reg.anotar_fallo(buzon, legible, uid, "no se pudo escribir tras reconectar")
                tot_err += 1
                continue

            # Un APPEND rechazado no siempre es un mensaje malo: en buzones
            # grandes lo habitual es que Exchange este limitando el ritmo, y ahi
            # lo que toca es esperar y repetir, no dar el mensaje por perdido.
            # Sin esto, una tanda de limitacion se traduce en decenas de correos
            # que nunca llegaron y nadie echa en falta hasta mucho despues.
            okA, respA = r
            reintentos_append = 0
            while okA != "OK" and reintentos_append < 4:
                reintentos_append += 1
                pausa = 15 * reintentos_append
                emitir("aviso", nivel="tecnico",
                       detalle="APPEND rechazado (%s); reintento %d tras %ds"
                       % (respA, reintentos_append, pausa))
                if reintentos_append == 1:
                    emitir("aviso",
                           texto="El servidor esta limitando el ritmo. Esperando",
                           detalle="primer APPEND rechazado en %s" % legible)
                time.sleep(pausa)
                ok, r = con_reintento("reescribir uid %d" % uid, escribir, extremo="dst")
                if not ok:
                    break
                okA, respA = r

            if okA != "OK":
                reg.anotar_fallo(buzon, legible, uid,
                                 "APPEND rechazado tras %d reintentos: %s"
                                 % (reintentos_append, respA))
                tot_err += 1
                continue

            uid_dst = parsear_appenduid(respA)
            if uid_dst and "\\Seen" not in flags:
                sin_leer_dst.append(uid_dst)
            reg.anotar(buzon, legible, uv, uid, uid_dst, len(cuerpo))
            hechos_carpeta += 1
            tot_msj += 1
            tot_bytes += len(cuerpo)

            dt = time.time() - t_inicio
            emitir("avance", mensajes=tot_msj, bytes=tot_bytes,
                   pendientes_totales=total_pendientes,
                   carpeta=legible, carpeta_hechos=hechos_carpeta,
                   carpeta_total=len(pendientes),
                   velocidad=(tot_bytes / 1048576) / max(dt, 0.001),
                   segundos=dt,
                   # Reloj de pared y tamano del ultimo mensaje. Sin esto, desde
                   # fuera no hay forma de distinguir un proceso atascado de uno
                   # que lleva tres minutos con un adjunto de 30 MB.
                   marca=time.time(),
                   ultimo_mb=len(cuerpo) / 1048576)
            # Se confirma tras CADA mensaje. Antes era cada veinticinco, y un
            # corte en medio dejaba hasta veinticinco mensajes copiados en
            # destino pero sin anotar: al reanudar se volvian a copiar y
            # quedaban duplicados en el buzon de la persona.
            reg.commit()

        if sin_leer_dst:
            # STORE exige la carpeta seleccionada en modo escritura, que es la
            # unica escritura que hace el migrador sobre el destino ademas del
            # propio APPEND. El origen sigue intacto.
            def restaurar_no_leidos():
                conex["dst"].select('"%s"' % crudo_dst)
                for i in range(0, len(sin_leer_dst), 500):
                    lote = ",".join(str(u) for u in sin_leer_dst[i:i + 500])
                    conex["dst"].uid("STORE", lote, "-FLAGS", "(\\Seen)")
                return True

            ok, _ = con_reintento("marcar como no leidos en " + destino_legible,
                                  restaurar_no_leidos, extremo="dst")
            if ok:
                emitir("aviso",
                       texto="%s: %d mensajes conservan su estado de no leido"
                       % (legible, len(sin_leer_dst)),
                       detalle="STORE -FLAGS (\\Seen) sobre %d uid de destino"
                       % len(sin_leer_dst))
            else:
                emitir("aviso",
                       texto="%s: no se pudo restaurar el estado de no leido" % legible,
                       detalle="fallo el STORE sobre %d uid" % len(sin_leer_dst))
        reg.commit()

    src, dst = conex["src"], conex["dst"]
    reg.commit()

    # Comprobacion final. Declarar "termino" sin mirar el destino es una
    # afirmacion sin respaldo; esto la convierte en un hecho comprobado.
    emitir("estado", texto="Comprobando el resultado")
    descuadres = []
    for crudo, legible, uv, pendientes, total, ya_n in plan:
        esperado = len(reg.hechos(buzon, legible, uv))
        if esperado == 0:
            continue
        destino_legible = "%s/%s" % (destino, legible) if destino else legible
        hay = contar_en_destino(conex["dst"], destino_legible)
        if hay < esperado:
            descuadres.append((legible, esperado, hay))
    if descuadres:
        for legible, esperado, hay in descuadres:
            emitir("aviso",
                   texto="%s: se esperaban %d mensajes y hay %d" % (legible, esperado, hay),
                   detalle="descuadre en la comprobacion final")

    dt = time.time() - t_inicio
    resumen = {
        "descuadres": len(descuadres),
        "mensajes": tot_msj, "bytes": tot_bytes, "omitidos": tot_salt,
        "errores": tot_err, "segundos": dt,
        "velocidad": (tot_bytes / 1048576) / dt if (tot_bytes and dt) else 0.0,
        "interrumpido": parar(),
    }
    emitir("fin", **resumen)
    try:
        src.logout()
        dst.logout()
    except Exception:
        pass
    return resumen


def cmd_migrar(args):
    password = None if MODO == "m365" else getpass.getpass(
        "Contrasena de %s (origen): " % args.buzon)

    def al_evento(e):
        t = e["tipo"]
        if t == "estado":
            print(e["texto"] + " ...", flush=True)
        elif t == "codigo":
            print("\n" + e["mensaje"] + "\n", flush=True)
        elif t == "aviso":
            linea = e.get("texto") or e.get("detalle") or ""
            if e.get("detalle") and e.get("texto"):
                linea += "   [%s]" % e["detalle"]
            print("   " + linea, flush=True)
        elif t == "plan":
            print("\n%d carpetas, %d mensajes pendientes\n" % (e["carpetas"], e["pendientes"]))
        elif t == "carpeta":
            print("\n[%s]  %d en origen, %d ya migrados, %d pendientes"
                  % (e["nombre"], e["total"], e["ya"], e["pendientes"]))
        elif t == "avance" and e["carpeta_hechos"] % 25 == 0:
            print("   %d/%d  %.0f MB  %.2f MB/s"
                  % (e["carpeta_hechos"], e["carpeta_total"],
                     e["bytes"] / 1048576, e["velocidad"]), flush=True)

    r = migrar_buzon(args.buzon, password, args.upn, args.app_id,
                     destino=args.destino, solo=args.solo,
                     limite_mb=args.limite_mb, progreso=al_evento)

    print("\n" + "=" * 60)
    print("Mensajes copiados : %d" % r["mensajes"])
    print("Volumen           : %.1f MB" % (r["bytes"] / 1048576))
    print("Omitidos por tam. : %d" % r["omitidos"])
    print("Errores           : %d" % r["errores"])
    print("Tiempo            : %.1f min" % (r["segundos"] / 60))
    if r["velocidad"]:
        print("Velocidad media   : %.2f MB/s" % r["velocidad"])
        # Proyeccion a tamanios de referencia, para saber cuanto tardaria un
        # buzon grande sin tener que migrarlo entero para averiguarlo.
        for gb in (1, 5, 10, 20):
            print("  proyeccion para %5.0f GB : %6.1f h"
                  % (gb, gb * 1024 / r["velocidad"] / 3600))
    if r["interrumpido"]:
        print("\nInterrumpido. Vuelve a correr el mismo comando para continuar.")


# --------------------------------------------------------------------------
# reporte de lo migrado
# --------------------------------------------------------------------------

def generar_reporte(buzon, upn, destino, resumen, diagnostico=None, ruta=None):
    """
    Construye el comprobante de la migracion, en HTML autocontenido.

    Para que existe. Al terminar, la persona necesita algo que pueda guardar y
    ensenar: cuantos mensajes se copiaron, de que carpetas y a donde. Sin eso,
    "ya termino" es una afirmacion que nadie puede comprobar despues.

    Lleva tambien las incidencias tecnicas, que en pantalla se le ahorran pero
    en el comprobante deben estar: si algo hay que revisar mas adelante, este es
    el documento donde mirar.
    """
    import html as _html
    reg = Registro()
    filas = [f for f in reg.resumen() if f[0] == buzon]
    fallos = [f for f in reg.fallos() if f[0] == buzon]
    ahora = time.strftime("%Y-%m-%d %H:%M")

    def esc(x):
        return _html.escape(str(x))

    tot_msj = sum(f[2] for f in filas)
    tot_b = sum(f[3] for f in filas)

    cuerpo = []
    cuerpo.append("<h1>Comprobante de migracion de correo</h1>")
    cuerpo.append("<table class=cab>")
    for etq, val in (("Buzon de origen", buzon), ("Cuenta de destino", upn),
                     ("Carpeta de destino", destino or "(raiz del buzon)"),
                     ("Fecha del informe", ahora)):
        cuerpo.append("<tr><th>%s</th><td>%s</td></tr>" % (esc(etq), esc(val)))
    cuerpo.append("</table>")

    cuerpo.append("<div class=cifras>")
    for val, etq in ((tot_msj, "mensajes copiados"),
                     ("%.1f MB" % (tot_b / 1048576), "volumen"),
                     ("%.1f min" % (resumen.get("segundos", 0) / 60), "duracion"),
                     (resumen.get("errores", 0), "errores")):
        cuerpo.append("<div><b>%s</b><span>%s</span></div>" % (esc(val), esc(etq)))
    cuerpo.append("</div>")

    cuerpo.append("<h2>Detalle por carpeta</h2><table><tr><th>Carpeta</th>"
                  "<th class=n>Mensajes</th><th class=n>MB</th></tr>")
    for _, carpeta, n, b in filas:
        cuerpo.append("<tr><td>%s</td><td class=n>%d</td><td class=n>%.1f</td></tr>"
                      % (esc(carpeta), n, b / 1048576))
    cuerpo.append("<tr class=tot><td>Total</td><td class=n>%d</td>"
                  "<td class=n>%.1f</td></tr></table>" % (tot_msj, tot_b / 1048576))

    if fallos:
        cuerpo.append("<h2>Mensajes que no se pudieron copiar</h2>"
                      "<table><tr><th>Carpeta</th><th>uid</th><th>Motivo</th></tr>")
        for _, carpeta, uid, motivo in fallos:
            cuerpo.append("<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
                          % (esc(carpeta), esc(uid), esc(motivo)))
        cuerpo.append("</table>")
    else:
        cuerpo.append("<h2>Mensajes que no se pudieron copiar</h2>"
                      "<p class=ok>Ninguno. Todos los mensajes se copiaron.</p>")

    if diagnostico:
        cuerpo.append("<h2>Incidencias tecnicas</h2>"
                      "<p class=nota>Las interrupciones de conexion son normales: el servidor "
                      "de Microsoft corta sesiones largas y la herramienta reconecta sola y "
                      "continua donde iba. Se listan para poder revisarlas si hiciera falta, "
                      "no porque indiquen un problema.</p><ul class=diag>")
        for d in reversed(diagnostico):
            cuerpo.append("<li>%s</li>" % esc(d))
        cuerpo.append("</ul>")

    cuerpo.append("<h2>Alcance de esta copia</h2><ul>"
                  "<li>Se copiaron <b>carpetas y mensajes</b>, con su fecha original y su "
                  "estado de leido o no leido.</li>"
                  "<li><b>No</b> se copiaron calendarios, contactos, tareas ni reglas: el "
                  "protocolo usado solo transporta correo.</li>"
                  "<li>El buzon de origen <b>no se modifico</b>. Sigue intacto.</li></ul>")

    doc = """<!doctype html><html lang=es><head><meta charset=utf-8>
<title>Comprobante de migracion</title><style>
body{font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     color:#1a1c1f;background:#fff;max-width:760px;margin:0 auto;padding:40px 24px}
h1{font-size:22px;margin:0 0 22px}h2{font-size:16px;margin:32px 0 10px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #e2e5ea}
.n{text-align:right;font-variant-numeric:tabular-nums}
.tot td{font-weight:700;border-top:2px solid #1a1c1f;border-bottom:0}
table.cab{width:auto}table.cab th{color:#5b6270;font-weight:600;padding-right:20px}
.cifras{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:24px 0}
.cifras div{background:#f7f8fa;border-radius:8px;padding:14px}
.cifras b{display:block;font-size:20px}.cifras span{font-size:12px;color:#5b6270}
.ok{color:#0a7c42}.nota{font-size:13px;color:#5b6270;background:#f7f8fa;
     padding:12px 14px;border-radius:8px}
.diag{font:12.5px/1.7 ui-monospace,Menlo,monospace;color:#5b6270}
@media print{body{padding:0}}
</style></head><body>__CUERPO__</body></html>"""
    doc = doc.replace("__CUERPO__", "\n".join(cuerpo))

    if ruta:
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(doc)
    return doc


def cmd_estado(args):
    reg = Registro()
    filas = reg.resumen()
    if not filas:
        print("Sin avance registrado todavia.")
        return
    print(f"{'BUZON':<36}{'CARPETA':<30}{'MSJ':>8}{'MB':>10}")
    print("-" * 84)
    tm = tb = 0
    for buzon, carpeta, n, b in filas:
        tm += n; tb += b
        print(f"{buzon[:35]:<36}{carpeta[:29]:<30}{n:>8}{b/1048576:>10.1f}")
    print("-" * 84)
    print(f"{'TOTAL':<66}{tm:>8}{tb/1048576:>10.1f}")
    f = reg.fallos()
    if f:
        print(f"\nUltimos {len(f)} fallos:")
        for buzon, carpeta, uid, motivo in f:
            print(f"  {carpeta[:24]:<26} uid {uid:<8} {motivo[:60]}")


def cmd_reporte(args):
    reg = Registro()
    filas = [f for f in reg.resumen() if f[0] == args.buzon]
    if not filas:
        sys.exit("No hay nada registrado para %s" % args.buzon)
    resumen = {"segundos": 0, "errores": len([f for f in reg.fallos() if f[0] == args.buzon])}
    generar_reporte(args.buzon, args.upn, args.destino, resumen, ruta=args.salida)
    print("Comprobante escrito en %s" % args.salida)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("inventario", help="Arbol, conteo y volumen del buzon origen")
    a.add_argument("--buzon", required=True)
    a.set_defaults(func=cmd_inventario)

    b = sub.add_parser("migrar", help="Copia Titan -> M365, reanudable e idempotente")
    b.add_argument("--buzon", required=True, help="Buzon Titan de origen")
    b.add_argument("--upn", required=True, help="Cuenta M365 de destino")
    b.add_argument("--app-id", default=APP_ID_POR_DEFECTO)
    b.add_argument("--destino", default="Migracion Titan",
                   help="Carpeta contenedora en destino; vacio para migrar a la raiz")
    b.add_argument("--solo", help="Migrar una sola carpeta, por nombre legible")
    b.add_argument("--limite-mb", type=int, default=0,
                   help="Omitir mensajes mayores a N MB (0 = sin limite)")
    b.set_defaults(func=cmd_migrar)

    c = sub.add_parser("estado", help="Que se ha migrado hasta ahora")
    c.set_defaults(func=cmd_estado)

    d = sub.add_parser("reporte", help="Comprobante en HTML de lo migrado")
    d.add_argument("--buzon", required=True)
    d.add_argument("--upn", default="")
    d.add_argument("--destino", default="Migracion Titan")
    d.add_argument("--salida", default="comprobante-migracion.html")
    d.set_defaults(func=cmd_reporte)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
