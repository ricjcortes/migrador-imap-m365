#!/bin/bash
# Construye el ejecutable del Migrador de Correo en macOS o Linux.
#
# Uso, desde la raiz del paquete:
#   bash receta/construir.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
VENV=".venv-construccion"

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "No se encontro '$PY'. Instala Python 3.9 o superior."
  exit 1
fi

echo "Python: $("$PY" -V 2>&1)"

if [ ! -d "$VENV" ]; then
  echo "Creando entorno de construccion..."
  "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r requisitos.txt
fi

"$VENV/bin/python" receta/generar_config.py

echo "Construyendo..."
rm -rf build dist
"$VENV/bin/pyinstaller" --noconfirm --clean receta/MigradorCorreo.spec

# Firma ad-hoc en macOS. No sustituye a la notarizacion de Apple, pero evita que
# el binario quede sin firma alguna, que es lo que Gatekeeper trata peor.
if [ "$(uname -s)" = "Darwin" ] && command -v codesign >/dev/null 2>&1; then
  codesign --force --deep --sign - dist/MigradorCorreo 2>/dev/null || true
fi

echo
echo "Construido: $(pwd)/dist/MigradorCorreo"
echo "Ahora comprueba que sirve:  $PY verificar.py"
