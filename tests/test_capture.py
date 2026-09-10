"""Masquage des données personnelles avant écriture d'une capture.

Une page de drive chargée en session connectée contient le nom, l'adresse de
retrait et le numéro de carte. Rien de tout ça ne doit se retrouver dans un
fichier qu'on s'échange pour corriger un sélecteur.
"""

from src.drive.capture import _looks_interesting, redact


class TestMasquage:
    def test_email(self):
        assert "houe.thomas@gmail.com" not in redact("compte houe.thomas@gmail.com")

    def test_telephone(self):
        assert "06 12 34 56 78" not in redact("tel 06 12 34 56 78")

    def test_numero_de_carte(self):
        masque = redact('"fidelite": "9876543210123"')
        assert "9876543210123" not in masque

    def test_le_reste_est_preserve(self):
        # Un prix ou un grammage ne doit surtout pas être masqué.
        texte = '<span class="prix">5,10 €</span> Lait demi-écrémé 6x1L'
        assert redact(texte) == texte


class TestFiltreXHR:
    def test_garde_les_reponses_produits(self):
        assert _looks_interesting("https://x.fr/api/recherche?q=lait", "application/json")

    def test_ignore_images_et_traceurs(self):
        assert not _looks_interesting("https://x.fr/logo.png", "image/png")
        assert not _looks_interesting("https://x.fr/api/produits", "text/html")
