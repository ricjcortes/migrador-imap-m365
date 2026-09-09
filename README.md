# Migrador de correo IMAP a Microsoft 365

Herramienta para que cada persona migre su propio buzón de un servidor IMAP
—Titan, GoDaddy, cPanel, el que sea— a Microsoft 365, sin que un administrador
tenga que conocer su contraseña.

Se ejecuta en el equipo de la persona, abre una página en su navegador, y copia
carpetas y mensajes conservando **fechas originales, estado de leído y banderas**.
Es reanudable e idempotente: se puede detener y continuar, y correrla dos veces
no duplica nada.

**El buzón de origen no se modifica.** Se abre en solo lectura.

## Lo que hace y lo que no

| Sí | No |
|---|---|
| Correos y carpetas, con jerarquía completa | Calendarios |
| Fechas originales (`INTERNALDATE`) | Contactos |
| Leído, marcado, respondido, borrador | Tareas |
| Nombres con acentos y eñes (UTF-7 modificado) | Reglas de bandeja |
| Carpetas vacías | Respuestas automáticas |

## Configuración

El código **no lleva ningún identificador de organización**. Para funcionar
necesita el id de una aplicación de Entra registrada como *cliente público* con
el permiso delegado `IMAP.AccessAsUser.All` de *Office 365 Exchange Online*, y
consentimiento de administrador.

Se le indica de una de estas tres formas, en este orden de preferencia:

1. Variable de entorno `MIGRADOR_APP_ID` (y opcionalmente `MIGRADOR_TENANT_ID`).
2. Inyectado al construir: `receta/generar_config.py` lee esas mismas variables y
   genera un módulo que viaja dentro del ejecutable. Es como se construyen los
   binarios que se entregan.
3. Un `config.json` junto al ejecutable: `{"app_id": "...", "tenant_id": "..."}`.

---

# Construir el ejecutable

Hay tres formas. Elige una.

---

## Forma A: con un agente (Claude Code, Cowork o similar) — la recomendada

Abre el agente **en la carpeta de este paquete** y pégale el texto que está entre
las líneas de abajo, tal cual.

Está pensado para cuando no sabes —o no quieres saber— nada de Python. El agente
se encarga de instalar lo que falte y de leer los errores; la construcción en sí
la hacen los scripts, que son los mismos siempre.

```
Estás en la carpeta de un paquete que contiene el código de una herramienta de
migración de correo. Tu tarea es construir el ejecutable y verificar que
funciona.

QUÉ TIENES QUE CONSEGUIR
Un ejecutable en la carpeta `dist/` que arranque en un equipo sin Python ni
ninguna dependencia instalada, y que pase todas las comprobaciones de
`verificar.py`.

PASOS
1. Mira qué sistema operativo y qué arquitectura tiene este equipo, y dímelo.
2. Comprueba si hay Python 3.9 o superior disponible. Si no lo hay, instálalo:
   en Windows desde python.org asegurando que quede en el PATH; en macOS con
   Homebrew o desde python.org; en Linux con el gestor de paquetes de la
   distribución. Dime qué versión quedó.
3. Ejecuta el script de construcción que corresponda:
   - Windows:      .\receta\construir.ps1
   - macOS/Linux:  bash receta/construir.sh
4. Ejecuta `python verificar.py` (o `python3 verificar.py`).
5. Si alguna comprobación falla, diagnostica y corrige la causa, y vuelve a
   construir y verificar. Repite hasta que pasen todas o hasta que llegues a algo
   que no puedas resolver.
6. Cuando terminen de pasar, dime dónde quedó el ejecutable, cuánto pesa, para
   qué sistema y arquitectura sirve, y qué tuviste que instalar o cambiar.

QUÉ NO DEBES HACER
- No modifiques nada dentro de `src/`. Ahí está la lógica de migración, que ya
  está probada. Si crees que hay un error, dímelo en vez de corregirlo.
- No cambies el identificador de aplicación ni el del tenant que aparecen en
  `src/migrador.py`.
- No subas este paquete ni el ejecutable a ningún sitio, ni lo envíes a ninguna
  parte.
- No desactives ni relajes ninguna comprobación de `verificar.py` para que
  pasen. Si una falla, es información, no un obstáculo.
- No firmes el ejecutable con ningún certificado ni intentes notarizarlo.

SI ALGO SE ATASCA
Los problemas típicos, por si ayudan: en Windows, que Python no esté en el PATH;
en macOS con Homebrew, que pip se niegue a instalar por PEP 668 (se resuelve con
el entorno virtual que el script ya crea, no con --break-system-packages); y en
cualquier sistema, que falte el módulo venv y haya que instalarlo aparte.
```

---

## Forma B: a mano

Si prefieres hacerlo tú, son tres comandos.

**Requisito**: Python 3.9 o superior. En Windows, instálalo desde
[python.org](https://www.python.org/downloads/windows/) marcando
*Add python.exe to PATH*.

**Windows**, en PowerShell:

```powershell
.\receta\construir.ps1
python verificar.py
```

**macOS o Linux**:

```bash
bash receta/construir.sh
python3 verificar.py
```

El ejecutable queda en `dist/`.

---

## Qué comprueba `verificar.py`

No es un adorno: es lo que separa «construyó sin errores» de «funciona».

1. Que el binario existe, con qué arquitectura, y que se puede ejecutar.
2. Que **arranca en un entorno limpio**, sin PATH ni paquetes del sistema. Es lo
   que demuestra que es autocontenido y no depende de lo que hubiera instalado
   en el equipo donde se construyó.
3. Que sirve su página con el testigo correcto y responde 404 sin él.
4. Que **negocia TLS con los dos servidores de correo**. Esta es la que falla en
   silencio si se construye con un Python cuya biblioteca TLS ya no sirve para
   lo que exigen los servidores: el binario arranca perfectamente y luego no
   puede conectarse a nada.

Si algo falla, sale con error y dice qué. No entregues el binario hasta que pase
todo.

---

## Al entregar el ejecutable

Junto a él va `LEEME-PRIMERO.txt`, que son las instrucciones para quien lo va a
usar. Y hay dos advertencias que hay que dar, porque sin ellas la persona se topa
con un bloqueo del sistema y concluye que le mandaste un virus:

**En macOS.** El binario no está notarizado por Apple. Un doble clic normal lo
bloquea Gatekeeper sin explicar por qué. La primera vez hay que abrirlo con
**clic derecho → Abrir** y confirmar.

**En Windows.** El ejecutable no está firmado. SmartScreen mostrará un aviso azul
la primera vez: **Más información → Ejecutar de todas formas**. Además, algunos
antivirus marcan los binarios de PyInstaller como sospechosos; es un falso
positivo conocido del empaquetador.

Quitarse ambas de encima requiere certificados de firma de código: un Developer
ID de Apple con notarización, y un certificado para Windows.

---

---

## Forma C: con GitHub Actions, sin equipo de cada tipo

`.github/workflows/construir.yml` construye los cuatro binarios —Windows, Mac
Intel, Mac Apple Silicon y Linux— en los servidores de GitHub, y **verifica cada
uno antes de publicarlo**: si no arranca o no negocia TLS, el trabajo falla y no
deja artefacto.

Es la vía más repetible y la única que no exige tener un equipo de cada tipo.

1. En *Settings > Secrets and variables > Actions*, define `MIGRADOR_APP_ID` y
   `MIGRADOR_TENANT_ID`. Sin ellos los binarios salen genéricos y hay que
   ponerles un `config.json` al lado.
2. En la pestaña *Actions*, ejecuta *Construir ejecutables*.
3. Descarga los binarios de la sección *Artifacts*.

Nota sobre por qué los identificadores van como secretos y no en el código: el id
de un cliente público que ya tiene consentido el acceso a buzones para toda una
organización le ahorra la mitad del trabajo a quien quiera montar un phishing por
código de dispositivo contra ese tenant. No es un secreto en sentido estricto
—Microsoft los trata como públicos— pero publicarlo no aporta nada y sí facilita
el ataque, sobre todo en tenants sin Acceso Condicional.

Por la misma razón, **los binarios construidos no se publican como Release**: los
artefactos de Actions requieren sesión iniciada en GitHub para descargarse.

## Un límite que conviene tener claro

**Un equipo solo puede construir para su propio sistema.** PyInstaller genera un
binario para la plataforma donde se ejecuta, y eso no cambia por usar un agente:
para tener el `.exe` de Windows hace falta un Windows, y para el de macOS un Mac.
Es justo lo que resuelve la forma C, poniendo GitHub los equipos.

La excepción: un Mac con Apple Silicon puede construir para Mac con Intel usando
Rosetta 2 y el Python que macOS trae en `/usr/bin/python3`. Al revés no.

## Qué hay en el paquete

| Ruta | Qué es |
|---|---|
| `src/` | el código de la herramienta. No se toca |
| `receta/` | el archivo de PyInstaller y los scripts de construcción |
| `requisitos.txt` | las dos dependencias necesarias |
| `verificar.py` | las comprobaciones del binario resultante |
| `LEEME-PRIMERO.txt` | instrucciones para quien va a usar la herramienta |
| `.github/workflows/` | el flujo que construye las cuatro plataformas en GitHub |
