"""
Pruebas del reparto de Gmail: que carpetas se copian, en que orden, y que cada
mensaje vaya una sola vez.

Sin red: son funciones puras. Correr con
    python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import migrador  # noqa: E402

# Lo que devuelve LIST en una cuenta de Gmail en espanol, ya pasado por
# listar_carpetas_con_atributos: (crudo, legible, atributos).
GMAIL_ES = [
    ("INBOX", "INBOX", "\\HasNoChildren"),
    ("Clientes", "Clientes", "\\HasNoChildren"),
    ("Bancos/BBVA", "Bancos/BBVA", "\\HasNoChildren"),
    ("[Gmail]/Todos", "[Gmail]/Todos", "\\All \\HasNoChildren"),
    ("[Gmail]/Borradores", "[Gmail]/Borradores", "\\Drafts \\HasNoChildren"),
    ("[Gmail]/Destacados", "[Gmail]/Destacados", "\\Flagged \\HasNoChildren"),
    ("[Gmail]/Enviados", "[Gmail]/Enviados", "\\HasNoChildren \\Sent"),
    ("[Gmail]/Importantes", "[Gmail]/Importantes", "\\HasNoChildren \\Important"),
    ("[Gmail]/Papelera", "[Gmail]/Papelera", "\\HasNoChildren \\Trash"),
    ("[Gmail]/Spam", "[Gmail]/Spam", "\\HasNoChildren \\Junk"),
]


class OrdenarGmail(unittest.TestCase):
    def test_orden_y_nombres_de_destino(self):
        self.assertEqual(migrador.ordenar_gmail(GMAIL_ES), [
            ("INBOX", "INBOX"),
            ("[Gmail]/Enviados", "Enviados"),
            ("[Gmail]/Borradores", "Borradores"),
            ("Bancos/BBVA", "Bancos/BBVA"),
            ("Clientes", "Clientes"),
            ("[Gmail]/Todos", "Archivados"),
        ])

    def test_excluye_vistas_spam_y_papelera(self):
        crudos = [c for c, _ in migrador.ordenar_gmail(GMAIL_ES)]
        for fuera in ("[Gmail]/Destacados", "[Gmail]/Importantes",
                      "[Gmail]/Papelera", "[Gmail]/Spam"):
            self.assertNotIn(fuera, crudos)

    def test_atributos_sin_distinguir_mayusculas(self):
        r = migrador.ordenar_gmail([("[Gmail]/Sent Mail", "[Gmail]/Sent Mail", "\\hasnochildren \\sent")])
        self.assertEqual(r, [("[Gmail]/Sent Mail", "Enviados")])

    def test_idioma_indistinto_porque_decide_el_atributo(self):
        ingles = [("[Gmail]/All Mail", "[Gmail]/All Mail", "\\All"),
                  ("[Gmail]/Trash", "[Gmail]/Trash", "\\Trash")]
        self.assertEqual(migrador.ordenar_gmail(ingles), [("[Gmail]/All Mail", "Archivados")])


class NuevosGmail(unittest.TestCase):
    def repartir(self, carpetas):
        vistos = set()
        return [migrador.nuevos_gmail(ids, vistos) for ids in carpetas]

    def test_mensaje_con_varias_etiquetas_va_solo_a_la_primera(self):
        inbox = {1: "100", 2: "200"}
        clientes = {7: "100", 8: "300"}      # 100 ya salio en INBOX
        todos = {50: "100", 51: "200", 52: "300", 53: "400"}
        r = self.repartir([inbox, clientes, todos])
        self.assertEqual(r, [[1, 2], [8], [53]])   # 400: solo archivado

    def test_orden_de_uids_se_conserva(self):
        self.assertEqual(self.repartir([{9: "a", 3: "b", 5: "c"}]), [[3, 5, 9]])

    def test_sin_msgid_se_copia_ante_la_duda(self):
        self.assertEqual(self.repartir([{1: None, 2: "x"}, {3: None}]), [[1, 2], [3]])


class ParsearMsgids(unittest.TestCase):
    def test_respuesta_real_de_fetch(self):
        datos = [b"1 (X-GM-MSGID 1278455344230334865 UID 3)",
                 b"2 (UID 4 X-GM-MSGID 1278455344230334866)",
                 None]
        self.assertEqual(migrador.parsear_msgids(datos),
                         {3: "1278455344230334865", 4: "1278455344230334866"})


if __name__ == "__main__":
    unittest.main()
