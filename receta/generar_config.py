#!/usr/bin/env python3
"""
Escribe src/config_compilado.py con los identificadores del tenant.

Se ejecuta antes de construir. Lee MIGRADOR_APP_ID, MIGRADOR_TENANT_ID y MIGRADOR_MODO
del entorno; si no estan, no escribe nada y el binario sale generico, pidiendo la
configuracion al arrancar.

El archivo que genera NO se versiona: es justamente lo que no debe estar en un
repositorio publico.
"""

import os
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESTINO = os.path.join(RAIZ, "src", "config_compilado.py")

app_id = os.environ.get("MIGRADOR_APP_ID", "").strip()
tenant = os.environ.get("MIGRADOR_TENANT_ID", "").strip()

if not app_id:
    if os.path.exists(DESTINO):
        os.remove(DESTINO)
    print("Sin MIGRADOR_APP_ID: el binario saldra generico.")
    print("Quien lo use tendra que poner un config.json al lado del ejecutable.")
    sys.exit(0)

with open(DESTINO, "w", encoding="utf-8") as f:
    f.write('# Generado durante la construccion. No se versiona.\n')
    f.write('APP_ID = "%s"\n' % app_id)
    if tenant:
        f.write('TENANT_ID = "%s"\n' % tenant)
    modo = os.environ.get("MIGRADOR_MODO", "").strip().lower()
    if modo:
        f.write('MODO = "%s"\n' % modo)

print("Configuracion compilada: app_id %s...%s" % (app_id[:8], app_id[-4:]))
