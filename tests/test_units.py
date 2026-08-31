"""Lecture des conditionnements et conversion d'unités (constat C3)."""

import pytest

from src.units import Pack, UnknownUnit, base_unit, canonical_unit, parse_pack, to_base


class TestParsePack:
    @pytest.mark.parametrize(
        "label, size, unit, count",
        [
            ("Lait demi-écrémé 6x1L", 1.0, "l", 6),
            ("Litière silice 5 L", 5.0, "l", 1),
            ("Prince 1,2 kg", 1.2, "kg", 1),
            ("Bigard 640 g", 640.0, "g", 1),
            ("Papier toilette 24 rouleaux", 24.0, "rouleau", 1),
            ("Lessive 45 lavages", 45.0, "lavage", 1),
            ("Ultima 9 kg", 9.0, "kg", 1),
            ("Vinaigre blanc bidon 5 L", 5.0, "l", 1),
        ],
    )
    def test_formats_reels(self, label, size, unit, count):
        pack = parse_pack(label)
        assert pack == Pack(size, unit, count)

    def test_format_absent_renvoie_none(self):
        # C'est le déclencheur de la règle P4 : sans format, pas de €/kg.
        assert parse_pack("Steak haché promo") is None
        assert parse_pack("") is None

    def test_multiplicateur_donne_la_quantite_totale(self):
        assert parse_pack("Sardines 3x93g").total_base == pytest.approx(0.279)


class TestConversion:
    def test_unite_de_base_par_famille(self):
        assert base_unit("g") == "kg"
        assert base_unit("cl") == "L"
        assert base_unit("rouleau") == "unite"

    def test_conversion(self):
        assert to_base(640, "g") == pytest.approx(0.64)
        assert to_base(75, "cl") == pytest.approx(0.75)

    def test_alias_et_accents(self):
        assert canonical_unit("Litres") == "l"
        assert canonical_unit("RLX") == "rouleau"

    def test_unite_inconnue_leve(self):
        # On refuse de deviner : mieux vaut une erreur qu'un prix au kilo faux.
        with pytest.raises(UnknownUnit):
            canonical_unit("bidule")
