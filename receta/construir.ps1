# Construye el ejecutable del Migrador de Correo en Windows.
#
# Requisito: Python 3.9 o superior instalado desde python.org, marcando
# "Add python.exe to PATH" durante la instalacion.
#
# Uso, en PowerShell y desde la raiz del paquete:
#   .\receta\construir.ps1

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$py = "python"
try { & $py --version | Out-Null } catch {
    Write-Host "No se encontro Python en el PATH."
    Write-Host "Instalalo desde https://www.python.org/downloads/windows/"
    Write-Host "y marca 'Add python.exe to PATH' durante la instalacion."
    exit 1
}
Write-Host "Python: $(& $py --version)"

$venv = ".venv-construccion"
if (-not (Test-Path $venv)) {
    Write-Host "Creando entorno de construccion..."
    & $py -m venv $venv
    & "$venv\Scripts\pip.exe" install --quiet --upgrade pip
    & "$venv\Scripts\pip.exe" install --quiet -r requisitos.txt
}

& "$venv\Scripts\python.exe" receta\generar_config.py

Write-Host "Construyendo..."
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
& "$venv\Scripts\pyinstaller.exe" --noconfirm --clean receta\MigradorCorreo.spec

Write-Host ""
Write-Host "Construido: $(Get-Location)\dist\MigradorCorreo.exe"
Write-Host "Ahora comprueba que sirve:  python verificar.py"
