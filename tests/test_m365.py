"""
Pruebas de las reglas del modo m365: no copiar un buzon sobre si mismo y no
arrastrar la carpeta de una migracion anterior.

Sin red. Correr con
    python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import migrador  # noqa: E402


class OrigenDestino(unittest.TestCase):
    def test_mismo_buzon_es_invalido(self):
        self.assertIsNotNone(migrador.problema_origen_destino("m365", "a@x.com", "a@x.com"))

    def test_mayusculas_y_espacios_no_engañan(self):
        self.assertIsNotNone(migrador.problema_origen_destino("m365", "Pfagoni@X.com", " pfagoni@x.com "))

    def test_buzones_distintos_es_valido(self):
        self.assertIsNone(migrador.problema_origen_destino("m365", "a@x.com", "b@x.com"))

    def test_en_titan_no_aplica(self):
        # Origen y destino viven en sistemas distintos: mismas direcciones no es el mismo buzon.
        self.assertIsNone(migrador.problema_origen_destino("imap", "a@x.com", "a@x.com"))
        self.assertIsNone(migrador.problema_origen_destino("gmail", "a@x.com", "a@x.com"))


class CarpetaDeMigracionPrevia(unittest.TestCase):
    """Un buzon de origen que ya fue destino de una migracion tiene la carpeta de
    aquella copia. Copiarla de nuevo duplica todo."""

    def test_la_carpeta_de_destino_y_sus_hijas_se_omiten(self):
        self.assertTrue(migrador.es_carpeta_previa("m365", "Correo pfagoni", "Correo pfagoni"))
        self.assertTrue(migrador.es_carpeta_previa("m365", "Correo pfagoni/INBOX", "Correo pfagoni"))
        self.assertTrue(migrador.es_carpeta_previa("m365", "Correo pfagoni/Calendario/Cobranza", "Correo pfagoni"))

    def test_carpetas_normales_no_se_omiten(self):
        for c in ("INBOX", "Elementos enviados", "Calendario/Cobranza", "Clientes"):
            self.assertFalse(migrador.es_carpeta_previa("m365", c, "Correo pfagoni"))

    def test_un_nombre_que_solo_empieza_igual_no_cuenta(self):
        self.assertFalse(migrador.es_carpeta_previa("m365", "Correo pfagoni viejo", "Correo pfagoni"))
        self.assertFalse(migrador.es_carpeta_previa("m365", "Correo pfagoniX/INBOX", "Correo pfagoni"))

    def test_sin_carpeta_de_destino_no_se_omite_nada(self):
        self.assertFalse(migrador.es_carpeta_previa("m365", "INBOX", ""))

    def test_solo_en_modo_m365(self):
        self.assertFalse(migrador.es_carpeta_previa("imap", "Correo pfagoni", "Correo pfagoni"))
        self.assertFalse(migrador.es_carpeta_previa("gmail", "Correo pfagoni", "Correo pfagoni"))


if __name__ == "__main__":
    unittest.main()
