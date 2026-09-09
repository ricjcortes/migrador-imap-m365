# -*- mode: python ; coding: utf-8 -*-
"""
Receta de empaquetado del Migrador de Correo.

La misma para Windows, macOS y Linux: PyInstaller genera un binario para el
sistema donde se ejecuta, asi que lo que cambia es donde se corre, no la receta.

Modo onefile: un unico ejecutable que se puede copiar a un USB o enviar por
correo. Arranca un par de segundos mas lento porque se descomprime en un
temporal cada vez, y a cambio no hay carpeta que se rompa moviendo un archivo.

Consola visible a proposito. Sin ella, si algo falla al arrancar la persona no ve
nada, no sabe que paso y no tiene que copiar para pedir ayuda.
"""

import os

# SPECPATH lo define PyInstaller: es la carpeta de este archivo. Se usa para no
# depender del directorio desde el que se lance la construccion.
RAIZ = os.path.dirname(SPECPATH)
SRC = os.path.join(RAIZ, "src")

a = Analysis(
    [os.path.join(SRC, "lanzador.py")],
    pathex=[SRC],
    binaries=[],
    datas=[],
    hiddenimports=[
        # msal carga estos de forma dinamica y el analisis no los detecta solo.
        "msal", "msal.application", "msal.oauth2cli", "msal.authority",
        "msal.token_cache", "msal.throttled_http_client",
        # De la biblioteca estandar, declarados para que no queden fuera.
        "imaplib", "sqlite3", "ssl", "email", "webbrowser",
        # Generado por receta/generar_config.py justo antes de construir. Puede
        # no existir: entonces el binario sale generico y pide configuracion.
        "config_compilado",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Nada de esto se usa y abulta el binario de forma considerable.
        "tkinter", "unittest", "pydoc", "doctest", "test",
        "numpy", "PIL", "matplotlib", "pandas",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MigradorCorreo",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX dispara falsos positivos en antivirus de Windows
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
