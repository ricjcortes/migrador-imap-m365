#!/usr/bin/env python3
"""
Punto de entrada del ejecutable empaquetado del Migrador de Correo.

Existe aparte de interfaz.py porque un binario que abre alguien haciendo doble
clic tiene que aguantar cosas que un script lanzado desde la terminal no:

  - Argumentos que no puso nadie. macOS pasa un -psn_0_xxxx a las aplicaciones
    abiertas desde el Finder, y argparse aborta con eso.
  - Errores en el arranque. Si el programa muere, la ventana se cierra y la
    persona se queda sin saber que paso. Aqui se muestra el error y se espera a
    que pulse Intro.
  - Estar corriendo sin que se vea nada. Se imprime la direccion antes de abrir
    el navegador, para que haya de donde copiarla si no se abre solo.
"""

import os
import sys
import traceback

if getattr(sys, "frozen", False):
    # PyInstaller descomprime en _MEIPASS. Los modulos propios viajan ahi.
    sys.path.insert(0, getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
else:
    # Sin empaquetar, los modulos estan junto a este archivo.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    # Sin esto la salida se almacena en bufer cuando la consola no es un
    # terminal, y la direccion del navegador no aparece hasta que el programa
    # termina. Justo cuando el navegador no se abre solo y esa direccion es lo
    # unico que la persona necesita.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    # macOS anade su propio argumento al abrir desde el Finder. Se descarta antes
    # de que argparse lo vea.
    sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if not a.startswith("-psn_")]

    print("=" * 58)
    print("  Migrador de correo  ->  Microsoft 365")
    print("=" * 58)
    print()
    print("Se abrira tu navegador. Si no se abre solo, copia la direccion")
    print("que aparece abajo y pegala en el navegador.")
    print()
    print("Para cerrar el programa: cierra esta ventana.")
    print()

    from interfaz import main as arrancar
    arrancar()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nCerrado.")
    except Exception:
        print("\n" + "=" * 58)
        print("  El programa no pudo continuar.")
        print("=" * 58)
        traceback.print_exc()
        print()
        print("Copia este texto y enviaselo a quien te dio la herramienta.")
        try:
            input("\nPulsa Intro para cerrar...")
        except EOFError:
            pass
        sys.exit(1)
