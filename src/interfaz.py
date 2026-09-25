#!/usr/bin/env python3
"""
Interfaz local del migrador de buzones: servidor IMAP -> Microsoft 365.

Que es. El mismo motor de migrador.py, con una cara. Levanta un servidor en la
propia maquina y abre el navegador: el empleado ve un formulario, un codigo para
iniciar sesion en Microsoft y una barra de progreso.

Por que un navegador y no una aplicacion de escritorio. Una app empaquetada para
Mac exige firma de desarrollador y notarizacion; sin eso Gatekeeper la bloquea, y
ensenarle a la gente a saltarse esa advertencia es justo el habito que no se
quiere en una organizacion que persigue ISO 27001. El navegador ya esta instalado,
ya es de confianza y se ve igual en Mac y en Windows.

Decisiones de seguridad, que aqui no son decorativas porque esto pide una
contrasena:

  - Escucha solo en 127.0.0.1. No es accesible desde la red.
  - La URL lleva un testigo aleatorio. Sin el, cualquier pagina abierta en el
    navegador podria hablarle al servidor local; con el, no.
  - La contrasena de Titan vive en memoria mientras corre y no se escribe a
    disco, ni a registro, ni al historial. La respuesta de estado nunca la
    incluye.
  - Un solo uso: al terminar, el servidor se apaga.

Uso:
    python3 interfaz.py --app-id <GUID>
    python3 interfaz.py --app-id <GUID> --puerto 8765 --no-abrir
"""

import argparse
import http.server
import json
import os
import secrets
import socketserver
import sys
import threading
import time
import urllib.parse
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from migrador import (migrar_buzon, generar_reporte, APP_ID_POR_DEFECTO,  # noqa: E402
                      dir_datos, BITACORA, anotar, MODO)

TESTIGO = secrets.token_urlsafe(24)

# Estado compartido entre el hilo de migracion y las peticiones del navegador.
# Se protege con un cerrojo porque el servidor atiende en varios hilos.
ESTADO = {
    "fase": "formulario",   # formulario | autenticando | migrando | terminado | error
    "texto": "",
    "codigo": None,
    "url_codigo": None,
    "mensajes": 0,
    "bytes": 0,
    "pendientes_totales": 0,
    "carpeta": "",
    "carpeta_hechos": 0,
    "carpeta_total": 0,
    "velocidad": 0.0,
    "segundos": 0.0,
    "marca": 0.0,          # reloj de pared del ultimo avance
    "ultimo_mb": 0.0,      # tamanio del ultimo mensaje copiado
    "avisos": [],        # lo que se le muestra a la persona
    "resumen": None,
    "error": None,
    "bitacora": None,
}
# El detalle tecnico se guarda aparte y no viaja al navegador: no le sirve a
# quien esta migrando su correo y solo le hace pensar que algo se rompio. Va al
# comprobante, que es donde importa poder revisarlo.
DIAGNOSTICO = []
DATOS = {"buzon": "", "upn": "", "destino": ""}
CERROJO = threading.Lock()
DETENER = {"si": False}


def fijar(**kw):
    with CERROJO:
        ESTADO.update(kw)


def agregar_aviso(texto):
    with CERROJO:
        ESTADO["avisos"].insert(0, texto)
        del ESTADO["avisos"][40:]


def agregar_diagnostico(texto):
    with CERROJO:
        DIAGNOSTICO.append("%s  %s" % (time.strftime("%H:%M:%S"), texto))
        del DIAGNOSTICO[500:]


def al_evento(e):
    t = e["tipo"]
    if t == "estado":
        fijar(texto=e["texto"])
    elif t == "codigo":
        fijar(fase="autenticando", codigo=e.get("codigo"),
              url_codigo=e.get("url"), texto="Esperando el inicio de sesion")
    elif t == "plan":
        fijar(fase="migrando", pendientes_totales=e["pendientes"],
              texto="%d carpetas, %d mensajes por copiar" % (e["carpetas"], e["pendientes"]))
    elif t == "carpeta":
        fijar(fase="migrando", carpeta=e["nombre"],
              carpeta_hechos=0, carpeta_total=e["pendientes"])
    elif t == "avance":
        fijar(fase="migrando", mensajes=e["mensajes"], bytes=e["bytes"],
              carpeta=e["carpeta"], carpeta_hechos=e["carpeta_hechos"],
              carpeta_total=e["carpeta_total"], velocidad=e["velocidad"],
              segundos=e["segundos"], pendientes_totales=e["pendientes_totales"],
              marca=e.get("marca", 0.0), ultimo_mb=e.get("ultimo_mb", 0.0))
    elif t == "aviso":
        if e.get("detalle"):
            agregar_diagnostico(e["detalle"])
        # Solo los de nivel info y con texto propio llegan a la pantalla.
        if e.get("nivel", "info") == "info" and e.get("texto"):
            agregar_aviso(e["texto"])
    elif t == "fin":
        fijar(fase="terminado", resumen={k: v for k, v in e.items() if k != "tipo"})


def hilo_migracion(datos, app_id):
    DATOS.update({k: datos.get(k, "") for k in ("buzon", "upn", "destino")})
    try:
        migrar_buzon(
            buzon=datos["buzon"], password=datos["password"],
            upn=datos["upn"], app_id=app_id, destino=datos["destino"],
            limite_mb=int(datos.get("limite_mb") or 0),
            progreso=al_evento, debe_parar=lambda: DETENER["si"])
    except Exception as e:
        import traceback
        anotar("FALLO EN LA MIGRACION:\n" + traceback.format_exc())
        fijar(fase="error", error=str(e), bitacora=BITACORA)
    finally:
        # La contrasena sale de memoria pase lo que pase.
        datos["password"] = None


PAGINA = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Migracion de correo</title>
<style>
:root{--tinta:#1a1c1f;--suave:#5b6270;--linea:#e2e5ea;--fondo:#f7f8fa;
      --panel:#fff;--acento:#0f62fe;--ok:#0a7c42;--mal:#b3261e}
@media(prefers-color-scheme:dark){:root{--tinta:#e9ecf1;--suave:#9aa3b2;
      --linea:#2b3038;--fondo:#14161a;--panel:#1c1f24}}
*{box-sizing:border-box}
body{margin:0;background:var(--fondo);color:var(--tinta);
     font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.caja{max-width:660px;margin:0 auto;padding:40px 24px 64px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--suave);margin:0 0 28px}
.panel{background:var(--panel);border:1px solid var(--linea);border-radius:12px;padding:24px;margin-bottom:16px}
label{display:block;font-weight:600;margin:16px 0 6px;font-size:13px}
label:first-child{margin-top:0}
input{width:100%;padding:10px 12px;border:1px solid var(--linea);border-radius:8px;
      background:var(--fondo);color:var(--tinta);font-size:15px;font-family:inherit}
input:focus{outline:2px solid var(--acento);outline-offset:-1px}
.pista{font-size:12.5px;color:var(--suave);margin-top:5px}
button{background:var(--acento);color:#fff;border:0;border-radius:8px;padding:12px 22px;
       font-size:15px;font-weight:600;cursor:pointer;font-family:inherit;margin-top:24px}
button:disabled{opacity:.5;cursor:default}
button.gris{background:transparent;color:var(--suave);border:1px solid var(--linea)}
.codigo{font:700 34px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace;
        letter-spacing:5px;text-align:center;padding:20px;background:var(--fondo);
        border-radius:10px;margin:18px 0 10px;user-select:all;cursor:pointer}
.fila{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.fila button{margin-top:0;padding:9px 16px;font-size:14px}
#copiado{font-size:13px;color:var(--ok);opacity:0;transition:opacity .2s}
#copiado.ver{opacity:1}
.barra{height:10px;background:var(--fondo);border-radius:99px;overflow:hidden;margin:14px 0 6px}
.barra i{display:block;height:100%;background:var(--acento);width:0;transition:width .4s}
.cifras{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:20px 0 4px}
.cifra{background:var(--fondo);border-radius:8px;padding:12px}
.cifra b{display:block;font-size:20px}
.cifra span{font-size:12px;color:var(--suave)}
.reg{font:12.5px/1.7 ui-monospace,Menlo,monospace;color:var(--suave);
     max-height:190px;overflow:auto;margin-top:16px;border-top:1px solid var(--linea);padding-top:12px}
.ok{color:var(--ok)}.mal{color:var(--mal)}
a{color:var(--acento)}
.aviso{font-size:13px;color:var(--suave);border-left:3px solid var(--linea);padding-left:12px;margin-top:20px}
</style></head><body><div class="caja">
<h1>Migracion de tu correo</h1>
<p class="sub">Copia el contenido de tu buzon antiguo a Microsoft 365. Tu correo antiguo no se modifica ni se borra.</p>

<div class="panel" id="p-form">
  <label>Tu direccion de correo antigua</label>
  <input id="buzon" placeholder="nombre@empresa-anterior.com" autocomplete="off">
  <label>Contrasena de ese correo</label>
  <input id="password" type="password" autocomplete="off">
  <div class="pista">Se usa solo durante la copia. No se guarda en ningun sitio.</div>
  <label>Tu cuenta de Microsoft 365</label>
  <input id="upn" placeholder="nombre@empresa.com" autocomplete="off">
  <label>Carpeta donde se copiara</label>
  <input id="destino" value="Correo anterior">
  <div class="pista">Todo se copia dentro de esta carpeta, asi tu bandeja actual no se mezcla.</div>
  <button id="ir">Comenzar</button>
</div>

<div class="panel" id="p-auth" hidden>
  <b id="t-auth">Conectando con Microsoft</b>
  <p class="sub" style="margin:8px 0 0" id="s-auth">Un momento.</p>
  <div id="bloque-codigo" hidden>
  <div class="codigo" id="codigo" title="Pulsa para copiar">------</div>
  <div class="fila">
    <button id="copiar">Copiar codigo</button>
    <a id="enlace" href="#" target="_blank" rel="noopener">Abrir la pagina de Microsoft</a>
    <span id="copiado">Copiado</span>
  </div>
  <p class="sub" style="margin-top:16px">Esta ventana continuara sola en cuanto termines.</p>
  </div>
</div>

<div class="panel" id="p-prog" hidden>
  <b id="t-fase">Copiando</b>
  <div class="barra"><i id="barra"></i></div>
  <div class="pista" id="t-carpeta"></div>
  <div class="cifras">
    <div class="cifra"><b id="c-msj">0</b><span>mensajes</span></div>
    <div class="cifra"><b id="c-mb">0</b><span>MB copiados</span></div>
    <div class="cifra"><b id="c-eta">--</b><span>tiempo restante</span></div>
  </div>
  <button class="gris" id="detener">Detener</button>
  <div class="aviso">Puedes detenerla cuando quieras. Al volver a empezar continua donde iba, sin duplicar nada.</div>
  <div class="reg" id="reg"></div>
</div>

<div class="panel" id="p-fin" hidden>
  <b id="t-fin" class="ok">Copia terminada</b>
  <div class="cifras">
    <div class="cifra"><b id="f-msj">0</b><span>mensajes</span></div>
    <div class="cifra"><b id="f-mb">0</b><span>MB</span></div>
    <div class="cifra"><b id="f-min">0</b><span>minutos</span></div>
  </div>
  <div class="pista" id="f-detalle"></div>
  <div class="fila" style="margin-top:20px">
    <a id="reporte" href="#"><button>Descargar comprobante</button></a>
  </div>
  <div class="pista">Guardalo o imprimelo: deja constancia de que se copio y de donde a donde.</div>
</div>

<div class="panel" id="p-err" hidden>
  <b class="mal">No se pudo completar</b>
  <div class="pista" id="t-err" style="white-space:pre-wrap"></div>
  <div class="pista" id="t-log" style="margin-top:14px"></div>
</div>
</div>
<script>
const T = new URLSearchParams(location.search).get("t");
const $ = id => document.getElementById(id);
const ver = (id, si) => $(id).hidden = !si;

$("ir").onclick = async () => {
  const d = {buzon:$("buzon").value.trim(), password:$("password").value,
             upn:$("upn").value.trim(), destino:$("destino").value.trim()};
  if(!d.buzon || !d.password || !d.upn){ alert("Faltan datos por llenar."); return; }
  $("ir").disabled = true;
  $("password").value = "";
  await fetch("/iniciar?t="+T, {method:"POST", headers:{"Content-Type":"application/json"},
                                body: JSON.stringify(d)});
  ver("p-form", false);
  sondear();
};

$("detener").onclick = () => { fetch("/detener?t="+T, {method:"POST"}); $("detener").disabled = true; };

async function copiarCodigo(){
  const txt = $("codigo").textContent.trim();
  if(!txt || txt === "------") return;
  try {
    await navigator.clipboard.writeText(txt);
  } catch(e) {
    const r = document.createRange();
    r.selectNodeContents($("codigo"));
    const sel = window.getSelection();
    sel.removeAllRanges(); sel.addRange(r);
    try { document.execCommand("copy"); } catch(e2) { return; }
  }
  $("copiado").classList.add("ver");
  setTimeout(()=>$("copiado").classList.remove("ver"), 2000);
}
$("copiar").onclick = copiarCodigo;
$("codigo").onclick = copiarCodigo;

function tiempo(s){
  if(!isFinite(s) || s<=0) return "--";
  const h = Math.floor(s/3600), m = Math.round((s%3600)/60);
  return h ? h+" h "+m+" min" : m+" min";
}

async function sondear(){
  let e;
  try { e = await (await fetch("/estado?t="+T)).json(); } catch(x){ setTimeout(sondear, 1500); return; }

  ver("p-auth", e.fase === "autenticando");
  ver("p-prog", e.fase === "migrando");
  ver("p-fin",  e.fase === "terminado");
  ver("p-err",  e.fase === "error");

  if(e.fase === "autenticando"){
    // Si ya hay sesion guardada, Microsoft no pide codigo. Sin decirlo, la
    // pantalla parece saltarse un paso y da la impresion de que algo fallo.
    ver("bloque-codigo", !!e.codigo);
    if(e.codigo){
      $("t-auth").textContent = "Inicia sesion en Microsoft";
      $("s-auth").textContent = "Abre la pagina, escribe este codigo y entra con tu cuenta de Microsoft 365.";
      $("codigo").textContent = e.codigo;
      if(e.url_codigo){ $("enlace").href = e.url_codigo; }
    } else {
      $("t-auth").textContent = "Conectando con Microsoft";
      $("s-auth").textContent = "Tu sesion sigue activa, asi que no hace falta codigo. Un momento.";
    }
  }

  if(e.fase === "migrando"){
    const pct = e.pendientes_totales ? Math.min(100, 100*e.mensajes/e.pendientes_totales) : 0;
    $("barra").style.width = pct.toFixed(1)+"%";
    $("t-carpeta").textContent = e.carpeta
      ? "Carpeta: "+e.carpeta+"  ("+e.carpeta_hechos+" de "+e.carpeta_total+")" : e.texto;
    $("c-msj").textContent = e.mensajes;
    $("c-mb").textContent = (e.bytes/1048576).toFixed(0);
    const faltan = e.pendientes_totales - e.mensajes;
    const porSeg = e.segundos ? e.mensajes/e.segundos : 0;
    $("c-eta").textContent = porSeg>0 ? tiempo(faltan/porSeg) : "--";
    $("reg").innerHTML = (e.avisos||[]).map(a=>"<div>"+a.replace(/[<>&]/g,"")+"</div>").join("");
  }

  if(e.fase === "terminado" && e.resumen){
    const r = e.resumen;
    $("f-msj").textContent = r.mensajes;
    $("f-mb").textContent = (r.bytes/1048576).toFixed(1);
    $("f-min").textContent = (r.segundos/60).toFixed(1);
    const nada = r.mensajes === 0 && !r.interrumpido && !r.errores && !r.descuadres;
    // Un descuadre en la comprobacion final pesa mas que todo lo demas: significa
    // que el destino no tiene lo que deberia, y eso no se anuncia como exito.
    $("t-fin").textContent = r.descuadres ? "Revisa el resultado"
                           : r.interrumpido ? "Copia detenida"
                           : nada ? "No habia nada nuevo que copiar"
                           : "Copia terminada";
    $("t-fin").className = r.descuadres ? "mal" : r.interrumpido ? "" : "ok";
    let d;
    if(nada){
      // Cero mensajes con cero errores no es un fallo: es que ya estaba todo.
      // Sin decirlo, la pantalla de ceros se lee como que algo salio mal.
      d = "Tu correo ya estaba copiado en Microsoft 365. No se duplico nada.";
    } else if(r.descuadres){
      d = "En "+r.descuadres+" carpeta(s) el destino no tiene todos los mensajes esperados. "
        + "Vuelve a ejecutar la herramienta: copiara lo que falte sin duplicar nada. "
        + "El detalle esta en el comprobante.";
    } else {
      d = "Velocidad media "+r.velocidad.toFixed(2)+" MB/s.";
      if(r.errores) d += "  "+r.errores+" mensajes con error, van en el comprobante.";
      if(r.omitidos) d += "  "+r.omitidos+" omitidos por tamano.";
      if(r.interrumpido) d += "  Vuelve a abrir esta herramienta para continuar donde iba.";
    }
    $("f-detalle").textContent = d;
    $("reporte").href = "/reporte?t="+T;
    return;
  }
  if(e.fase === "error"){
    $("t-err").textContent = e.error || "";
    $("t-log").textContent = e.bitacora
      ? "Envia este archivo a quien te dio la herramienta: " + e.bitacora : "";
    return;
  }
  setTimeout(sondear, 1000);
}
</script></body></html>
"""


def pagina():
    """
    La misma pagina para los dos modos. En m365 no hay contrasena que pedir:
    el origen se abre con la sesion de Microsoft de quien inicia, y la carpeta
    no trae valor por defecto porque aqui siempre es el correo de otra persona
    y un nombre generico lo mezclaria con lo que ya haya.
    """
    if MODO == "gmail":
        return _aplicar(PAGINA, CAMBIOS_GMAIL)
    if MODO != "m365":
        return PAGINA
    cambios = [
        ("<title>Migracion de correo</title>",
         "<title>Traspaso de buzon de Microsoft 365</title>"),
        ("<h1>Migracion de tu correo</h1>",
         "<h1>Traspaso de buzon de Microsoft 365</h1>"),
        ("Copia el contenido de tu buzon antiguo a Microsoft 365. "
         "Tu correo antiguo no se modifica ni se borra.",
         "Copia el correo de un buzon de Microsoft 365 a una carpeta de otro. "
         "El buzon de origen no se modifica ni se borra."),
        ("<label>Tu direccion de correo antigua</label>",
         "<label>Buzon de origen</label>"),
        ('placeholder="nombre@empresa-anterior.com"',
         'placeholder="buzon@empresa.onmicrosoft.com"'),
        ('<label>Contrasena de ese correo</label>\n'
         '  <input id="password" type="password" autocomplete="off">\n'
         '  <div class="pista">Se usa solo durante la copia. '
         'No se guarda en ningun sitio.</div>',
         '<input id="password" type="hidden" value="">'),
        ("<label>Tu cuenta de Microsoft 365</label>",
         "<label>Buzon de destino</label>"),
        ('<input id="destino" value="Correo anterior">',
         '<input id="destino" value="" placeholder="Correo de la persona de origen">'),
        ("Todo se copia dentro de esta carpeta, asi tu bandeja actual no se mezcla.",
         "Todo se copia dentro de esta carpeta. Inicia sesion una persona con "
         "acceso total a los dos buzones."),
        ("if(!d.buzon || !d.password || !d.upn){",
         "if(!d.buzon || !d.upn || !d.destino){"),
    ]
    return _aplicar(PAGINA, cambios)


def _aplicar(p, cambios):
    for viejo, nuevo in cambios:
        if viejo not in p:
            raise RuntimeError("La pagina cambio y el modo %s no encuentra: %s" % (MODO, viejo[:60]))
        p = p.replace(viejo, nuevo)
    return p


# Gmail pide una contrasena de aplicacion, no la normal: si la pagina no lo dice,
# la persona teclea la suya, Google la rechaza y el error parece del programa.
CAMBIOS_GMAIL = [
    ("<title>Migracion de correo</title>", "<title>Migracion desde Gmail</title>"),
    ("<h1>Migracion de tu correo</h1>", "<h1>Migracion desde Gmail</h1>"),
    ("Copia el contenido de tu buzon antiguo a Microsoft 365. "
     "Tu correo antiguo no se modifica ni se borra.",
     "Copia una cuenta de Gmail a Microsoft 365. La cuenta de Gmail no se modifica "
     "ni se borra. Cada correo se copia una sola vez aunque tenga varias etiquetas; "
     "Spam y Papelera no se copian."),
    ("<label>Tu direccion de correo antigua</label>", "<label>Cuenta de Gmail</label>"),
    ('placeholder="nombre@empresa-anterior.com"', 'placeholder="cuenta@gmail.com"'),
    ("<label>Contrasena de ese correo</label>",
     "<label>Contrasena de aplicacion de Google</label>"),
    ("Se usa solo durante la copia. No se guarda en ningun sitio.",
     "No es la contrasena normal: son 16 letras que se crean en "
     "myaccount.google.com/apppasswords, con la verificacion en dos pasos activada. "
     "Se usa solo durante la copia y no se guarda."),
    ('<input id="destino" value="Correo anterior">', '<input id="destino" value="Correo Gmail">'),
]



class Manejador(http.server.BaseHTTPRequestHandler):
    app_id = None

    def _autorizado(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return secrets.compare_digest(q.get("t", [""])[0], TESTIGO)

    def _responder(self, codigo, cuerpo, tipo="application/json; charset=utf-8"):
        datos = cuerpo if isinstance(cuerpo, bytes) else cuerpo.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(datos)

    def do_GET(self):
        ruta = urllib.parse.urlparse(self.path).path
        if ruta == "/" and self._autorizado():
            return self._responder(200, pagina(), "text/html; charset=utf-8")
        if ruta == "/reporte" and self._autorizado():
            with CERROJO:
                resumen = dict(ESTADO.get("resumen") or {})
                diag = list(DIAGNOSTICO)
                datos = dict(DATOS)
            doc = generar_reporte(datos["buzon"], datos["upn"], datos["destino"],
                                  resumen, diagnostico=diag)
            datos_b = doc.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="comprobante-migracion.html"')
            self.send_header("Content-Length", str(len(datos_b)))
            self.end_headers()
            self.wfile.write(datos_b)
            return
        if ruta == "/estado" and self._autorizado():
            with CERROJO:
                # La contrasena no esta en ESTADO y nunca debe llegar aqui.
                return self._responder(200, json.dumps(ESTADO))
        self._responder(404, "{}")

    def do_POST(self):
        ruta = urllib.parse.urlparse(self.path).path
        if not self._autorizado():
            return self._responder(404, "{}")
        if ruta == "/detener":
            DETENER["si"] = True
            return self._responder(200, '{"ok":true}')
        if ruta == "/iniciar":
            n = int(self.headers.get("Content-Length") or 0)
            datos = json.loads(self.rfile.read(n) or b"{}")
            fijar(fase="autenticando", texto="Conectando")
            threading.Thread(target=hilo_migracion, args=(datos, self.app_id),
                             daemon=True).start()
            return self._responder(200, '{"ok":true}')
        self._responder(404, "{}")

    def log_message(self, *a):
        # Silencio deliberado: el registro por defecto imprime la URL completa,
        # que lleva el testigo. No aporta nada y lo filtraria a la consola.
        pass


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--app-id", default=APP_ID_POR_DEFECTO,
                   help="Id de la aplicacion de Entra; ya viene uno compilado")
    p.add_argument("--puerto", type=int, default=8765)
    p.add_argument("--no-abrir", action="store_true", help="No abrir el navegador solo")
    args = p.parse_args()

    if not args.app_id:
        raise SystemExit(
            "Falta el identificador de la aplicacion de Entra.\n\n"
            "Este ejecutable se construyo sin el. Ponlo de una de estas formas:\n"
            "  - variable de entorno MIGRADOR_APP_ID\n"
            "  - un config.json junto al ejecutable, con {\"app_id\": \"...\"}\n"
            "  - el argumento --app-id\n")

    Manejador.app_id = args.app_id
    socketserver.ThreadingTCPServer.allow_reuse_address = True

    # Si el puerto esta ocupado se busca otro. En el equipo de un empleado no se
    # puede dar por hecho que el 8765 este libre, y morir con "Address already in
    # use" no es una respuesta aceptable para quien solo quiere migrar su correo.
    srv = None
    for puerto in range(args.puerto, args.puerto + 20):
        try:
            srv = socketserver.ThreadingTCPServer(("127.0.0.1", puerto), Manejador)
            break
        except OSError:
            continue
    if srv is None:
        raise SystemExit("No se encontro un puerto libre entre %d y %d."
                         % (args.puerto, args.puerto + 19))
    url = "http://127.0.0.1:%d/?t=%s" % (puerto, TESTIGO)

    print("Migrador de correo en marcha.")
    print("  " + url)
    print("\nSolo escucha en esta maquina. Cierra con Ctrl-C.\n")
    if not args.no_abrir:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nCerrado.")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
