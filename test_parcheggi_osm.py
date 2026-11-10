"""
Test dei parcheggi della sede da OpenStreetMap: classificazione, stima posti,
lettura della risposta Overpass ed endpoint (senza rete: le chiamate OSM sono
sostituite da dati finti).

Uso: python3 test_parcheggi_osm.py
"""
import os

os.environ["ACCESS_PASSWORD"] = "password-di-prova"
os.environ.pop("RESEND_API_KEY", None)

from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app.main import app  # noqa: E402
from josa_core import parcheggi_osm as po  # noqa: E402


def quadrato(x, y, lato):
    return [(x, y), (x + lato, y), (x + lato, y + lato), (x, y + lato), (x, y)]


def test_stima_posti():
    assert po.stima_posti({}, 1000) == (40, False)
    assert po.stima_posti({"capacity": "55"}, 1000) == (55, True)
    assert po.stima_posti({"parking": "street_side"}, 150) == (10, False)
    assert po.stima_posti({}, 0) == (None, False)
    print("[posti] 25 m² a posto, 15 lungo strada, capacity OSM se c'è")


def test_classificazione():
    perim = [po.OggettoOSM("way/1", {"name": "Acme Spa"}, quadrato(-100, -100, 200))]
    casi = [
        (po.OggettoOSM("way/2", {"access": "yes"}, quadrato(-50, -50, 20)), "aziendale"),        # dentro il perimetro
        (po.OggettoOSM("way/3", {"operator": "ACME"}, quadrato(400, 400, 20)), "aziendale"),     # nome azienda
        (po.OggettoOSM("way/4", {"access": "private"}, quadrato(130, 0, 30)), "probabile"),      # privato a 30 m
        (po.OggettoOSM("way/5", {"access": "private"}, quadrato(300, 0, 30)), "da_confermare"),  # privato lontano
        (po.OggettoOSM("way/6", {"access": "permit"}, quadrato(110, 0, 30)), "da_confermare"),   # permesso: va confermato
        (po.OggettoOSM("way/7", {"access": "yes", "fee": "yes"}, quadrato(110, 0, 30)), "pubblico"),
        (po.OggettoOSM("way/8", {"access": "customers"}, quadrato(110, 0, 30)), "pubblico"),
        (po.OggettoOSM("way/9", {}, quadrato(120, 0, 30)), "da_confermare"),                     # senza tag, a 20 m
        (po.OggettoOSM("way/10", {}, quadrato(300, 0, 30)), "incerto"),
    ]
    for o, atteso in casi:
        tipo, motivo, _ = po.classifica(o, perim, "acme")
        assert tipo == atteso, (o.osm_id, tipo, atteso, motivo)
    ris = po.analizza([c[0] for c in casi], perim, "acme")
    inclusi = [p for p in ris["parcheggi"] if p.incluso_default]
    assert {p.tipo for p in inclusi} == {"aziendale", "probabile"}
    assert ris["posti_stimati"] == sum(p.posti for p in inclusi)
    assert ris["parcheggi"][0].tipo == "aziendale" and ris["parcheggi"][-1].tipo == "pubblico"
    print(f"[classifica] 9 casi giusti; inclusi di default: {len(inclusi)}, posti {ris['posti_stimati']}")


def test_overpass():
    proj = po._proiettore(45.0, 9.0)
    d = 0.0009  # ~100 m
    elements = [
        {"type": "way", "id": 1, "tags": {"landuse": "industrial"},
         "geometry": [{"lat": 45.0 - d, "lon": 9.0 - d}, {"lat": 45.0 - d, "lon": 9.0 + d},
                      {"lat": 45.0 + d, "lon": 9.0 + d}, {"lat": 45.0 + d, "lon": 9.0 - d}, {"lat": 45.0 - d, "lon": 9.0 - d}]},
        {"type": "way", "id": 2, "tags": {"amenity": "parking", "access": "private"},
         "geometry": [{"lat": 45.0002, "lon": 9.0002}, {"lat": 45.0002, "lon": 9.0005},
                      {"lat": 45.0005, "lon": 9.0005}, {"lat": 45.0005, "lon": 9.0002}, {"lat": 45.0002, "lon": 9.0002}]},
        {"type": "node", "id": 3, "lat": 45.003, "lon": 9.003, "tags": {"amenity": "parking", "capacity": "12"}},
        {"type": "way", "id": 4, "tags": {"landuse": "residential"},
         "geometry": [{"lat": 45.01, "lon": 9.01}, {"lat": 45.01, "lon": 9.02}, {"lat": 45.02, "lon": 9.02}, {"lat": 45.01, "lon": 9.01}]},
    ]
    parcheggi, perimetri = po.elementi_overpass(elements, proj, None)
    assert [p.osm_id for p in parcheggi] == ["way/2", "node/3"]
    assert [p.osm_id for p in perimetri] == ["way/1"], "solo l'area che contiene la sede fa da perimetro"
    ris = po.analizza(parcheggi, perimetri)
    tipi = {p.osm_id: p.tipo for p in ris["parcheggi"]}
    assert tipi["way/2"] == "aziendale"
    assert ris["parcheggi"][1].posti == 12 and ris["parcheggi"][1].posti_da_capacity
    print(f"[overpass] perimetro riconosciuto, parcheggio interno aziendale, {ris['posti_stimati']} posti")


def test_endpoint():
    client = TestClient(app)
    h = {"x-access-token": auth.genera_token("email")}
    originale = po.cerca_parcheggi

    def finto(indirizzo=None, lat=None, lon=None, raggio_m=250.0, azienda=None):
        ris = po.analizza([po.OggettoOSM("way/9", {"access": "private", "name": "P dipendenti"}, quadrato(5, 5, 40))], [], azienda)
        ris.update({"sede": {"lat": 44.5, "lon": 11.3, "indirizzo_trovato": indirizzo}, "raggio_m": raggio_m,
                    "fonte": "test", "attribuzione": po.ATTRIBUZIONE})
        return ris

    po.cerca_parcheggi = finto
    try:
        r = client.post("/api/v1/site-scoring/parcheggi", json={"indirizzo": "Via Prova 1, Bologna"}, headers=h)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["posti_stimati"] == 64 and j["parcheggi"][0]["tipo"] == "probabile"
        assert "tags" not in j["parcheggi"][0] and "OpenStreetMap" in j["attribuzione"]
        r = client.post("/api/v1/site-scoring/parcheggi", json={"indirizzo": "  "}, headers=h)
        assert r.status_code == 422
        r = client.post("/api/v1/site-scoring/parcheggi", json={"indirizzo": "Via Prova 1"})
        assert r.status_code in (401, 403)
    finally:
        po.cerca_parcheggi = originale
    print("[endpoint] 200 con risultato, 422 senza indirizzo, accesso protetto")


if __name__ == "__main__":
    test_stima_posti()
    test_classificazione()
    test_overpass()
    test_endpoint()
    print("OK")
