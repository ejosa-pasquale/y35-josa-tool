"""Parcheggi della sede da OpenStreetMap.

Dato l'indirizzo della sede (o le coordinate), cerca i parcheggi mappati su
OpenStreetMap nel raggio indicato, li classifica (aziendale, probabile,
da confermare, pubblico, incerto) e stima i posti auto.

Fonti, in ordine:
  1. Overpass API: parcheggi + perimetri (aree industriali/commerciali, edifici
     o aree con il nome dell'azienda). Dati piu' completi.
  2. Nominatim (fallback): parcheggi per tipo nel riquadro intorno alla sede,
     piu' gli oggetti con il nome dell'azienda come perimetro.

Le immagini satellitari non vengono analizzate: si usano solo i dati vettoriali
di OpenStreetMap (licenza ODbL, attribuzione obbligatoria).

La classificazione e il calcolo dei posti sono funzioni pure (testabili senza
rete); le chiamate di rete stanno in cerca_parcheggi().
"""
from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

ATTRIBUZIONE = "© contributori OpenStreetMap (ODbL)"
USER_AGENT = os.environ.get("OSM_USER_AGENT", "Y35-tool/1.0 (dimensionamento ricarica flotte)")
NOMINATIM_URL = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org")
OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")

M2_PER_POSTO = 25.0          # posto + corsia, parcheggio a raso
M2_PER_POSTO_STRADA = 15.0   # posti in linea lungo strada, senza corsia
DIST_PROBABILE_M = 60.0      # privato entro questa distanza dalla sede: probabile aziendale
DIST_INCERTO_VICINO_M = 30.0 # senza tag entro questa distanza: da confermare

TIPI_INCLUSI = ("aziendale", "probabile")
ACCESSO_PUBBLICO = {"yes", "public", "permissive", "customers"}
ACCESSO_RISERVATO = {"private", "employees", "no", "delivery", "destination", "residents", "permit"}


# ---------------------------------------------------------------- geometria

def _proiettore(lat0: float, lon0: float):
    k = 111_320.0
    kx = k * math.cos(math.radians(lat0))
    return lambda lat, lon: ((lon - lon0) * kx, (lat - lat0) * k)


def area_m2(poly: list[tuple[float, float]]) -> float:
    if len(poly) < 3:
        return 0.0
    s = 0.0
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def centroide(poly: list[tuple[float, float]]) -> tuple[float, float]:
    pts = poly[:-1] if len(poly) > 1 and poly[0] == poly[-1] else poly
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def punto_in_poligono(p: tuple[float, float], poly: list[tuple[float, float]]) -> bool:
    x, y = p
    dentro = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xi:
                dentro = not dentro
    return dentro


def _dist_punto_segmento(p, a, b) -> float:
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def dist_punto_poligono(p: tuple[float, float], poly: list[tuple[float, float]]) -> float:
    if len(poly) == 1:
        return math.hypot(p[0] - poly[0][0], p[1] - poly[0][1])
    if len(poly) >= 3 and punto_in_poligono(p, poly):
        return 0.0
    return min(_dist_punto_segmento(p, poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly)))


def dist_poligoni(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> float:
    """Distanza minima tra i bordi (0 se si toccano o uno contiene un vertice dell'altro)."""
    return min(min(dist_punto_poligono(p, b) for p in a), min(dist_punto_poligono(p, a) for p in b))


# ---------------------------------------------------------------- modello

@dataclass
class OggettoOSM:
    osm_id: str                      # es. "way/1194069454"
    tags: dict
    poly: list[tuple[float, float]]  # metri, x est / y nord, rispetto alla sede (1 punto = nodo)


@dataclass
class Parcheggio:
    osm_id: str
    nome: str
    tipo: str
    motivo: str
    distanza_m: float
    area_m2: float
    posti: Optional[int]
    posti_da_capacity: bool
    incluso_default: bool
    poligono: list[tuple[float, float]] = field(default_factory=list)
    tags: dict = field(default_factory=dict)


def _nome_corrisponde(tags: dict, azienda: Optional[str]) -> bool:
    if not azienda:
        return False
    a = azienda.strip().lower()
    if len(a) < 3:
        return False
    return any(a in str(tags.get(k, "")).lower() for k in ("name", "operator", "brand", "owner"))


def stima_posti(tags: dict, area: float) -> tuple[Optional[int], bool]:
    cap = str(tags.get("capacity", "")).strip()
    if cap.isdigit() and int(cap) > 0:
        return int(cap), True
    if area <= 0:
        return None, False
    lungo_strada = tags.get("parking") in ("street_side", "lane", "on_street")
    m2 = M2_PER_POSTO_STRADA if lungo_strada else M2_PER_POSTO
    return max(1, round(area / m2)), False


def classifica(ogg: OggettoOSM, perimetri: list[OggettoOSM], azienda: Optional[str]) -> tuple[str, str, float]:
    """Ritorna (tipo, motivo, distanza_m). La distanza e' dalla sede o dal perimetro piu' vicino."""
    tags = ogg.tags
    access = str(tags.get("access", "")).lower()
    d_sede = dist_punto_poligono((0.0, 0.0), ogg.poly) if len(ogg.poly) >= 3 else math.hypot(*ogg.poly[0])
    d_perim = min((dist_poligoni(ogg.poly, p.poly) for p in perimetri if len(p.poly) >= 3), default=math.inf)
    dist = min(d_sede, d_perim)
    c = centroide(ogg.poly) if len(ogg.poly) >= 3 else ogg.poly[0]
    dentro = any(len(p.poly) >= 3 and punto_in_poligono(c, p.poly) for p in perimetri)
    tag_txt = f"access={access}" if access else "nessun tag access"
    if tags.get("fee") == "yes":
        tag_txt += ", a pagamento"

    if _nome_corrisponde(tags, azienda):
        return "aziendale", f"porta il nome dell'azienda · {tag_txt}", dist
    if dentro:
        return "aziendale", f"dentro il perimetro dell'azienda · {tag_txt}", 0.0
    if access in ACCESSO_PUBBLICO or tags.get("fee") == "yes":
        return "pubblico", f"{tag_txt} · a {dist:.0f} m", dist
    if access in ACCESSO_RISERVATO:
        if access not in ("permit", "residents", "destination") and dist <= DIST_PROBABILE_M:
            return "probabile", f"{tag_txt} · a {dist:.0f} m dalla sede", dist
        return "da_confermare", f"{tag_txt} · a {dist:.0f} m", dist
    if dist <= DIST_INCERTO_VICINO_M:
        return "da_confermare", f"{tag_txt} · a {dist:.0f} m", dist
    return "incerto", f"{tag_txt} · a {dist:.0f} m", dist


def analizza(parcheggi: list[OggettoOSM], perimetri: list[OggettoOSM], azienda: Optional[str] = None) -> dict:
    """Classifica i parcheggi e calcola il totale dei posti inclusi di default."""
    out: list[Parcheggio] = []
    for o in parcheggi:
        tipo, motivo, dist = classifica(o, perimetri, azienda)
        area = area_m2(o.poly) if len(o.poly) >= 3 else 0.0
        posti, da_cap = stima_posti(o.tags, area)
        nome = o.tags.get("name") or o.tags.get("addr:street") or o.tags.get("_via") or "Parcheggio senza nome"
        out.append(Parcheggio(
            osm_id=o.osm_id, nome=nome, tipo=tipo, motivo=motivo, distanza_m=round(dist, 1),
            area_m2=round(area), posti=posti, posti_da_capacity=da_cap,
            incluso_default=(tipo in TIPI_INCLUSI and posti is not None),
            poligono=[(round(x, 1), round(y, 1)) for x, y in o.poly], tags=o.tags,
        ))
    ordine = {"aziendale": 0, "probabile": 1, "da_confermare": 2, "incerto": 3, "pubblico": 4}
    out.sort(key=lambda p: (ordine[p.tipo], p.distanza_m))
    inclusi = [p for p in out if p.incluso_default]
    return {
        "parcheggi": out,
        "posti_stimati": sum(p.posti or 0 for p in inclusi),
        "area_inclusa_m2": sum(p.area_m2 for p in inclusi),
        "perimetri": [
            {"osm_id": p.osm_id, "nome": p.tags.get("name", ""), "poligono": [(round(x, 1), round(y, 1)) for x, y in p.poly]}
            for p in perimetri if len(p.poly) >= 3
        ],
    }


# ---------------------------------------------------------------- rete

_cache: dict = {}
_CACHE_TTL_S = 24 * 3600
_nominatim_lock = threading.Lock()
_nominatim_ultimo = [0.0]


def _get(url: str, params: dict, timeout: float = 20.0):
    import httpx
    return httpx.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=timeout)


def _nominatim(path: str, params: dict):
    # Policy Nominatim: al massimo 1 richiesta al secondo
    with _nominatim_lock:
        attesa = 1.05 - (time.monotonic() - _nominatim_ultimo[0])
        if attesa > 0:
            time.sleep(attesa)
        try:
            r = _get(f"{NOMINATIM_URL}/{path}", {**params, "format": "jsonv2" if path == "search" else "json"})
        finally:
            _nominatim_ultimo[0] = time.monotonic()
    r.raise_for_status()
    return r.json()


def geocodifica(indirizzo: str) -> dict:
    res = _nominatim("search", {"q": indirizzo, "limit": 1, "countrycodes": "it"})
    if not res:
        res = _nominatim("search", {"q": indirizzo, "limit": 1})
    if not res:
        raise ValueError(f"Indirizzo non trovato su OpenStreetMap: {indirizzo}")
    e = res[0]
    return {"lat": float(e["lat"]), "lon": float(e["lon"]), "indirizzo_trovato": e.get("display_name", indirizzo)}


def _geojson_a_poly(g: dict, proj) -> list[tuple[float, float]]:
    if g["type"] == "Point":
        lon, lat = g["coordinates"]
        return [proj(lat, lon)]
    if g["type"] == "Polygon":
        return [proj(lat, lon) for lon, lat in g["coordinates"][0]]
    if g["type"] == "MultiPolygon":
        anelli = [r[0] for r in g["coordinates"]]
        anello = max(anelli, key=len)
        return [proj(lat, lon) for lon, lat in anello]
    if g["type"] == "LineString":
        return [proj(lat, lon) for lon, lat in g["coordinates"]]
    return []


def _da_nominatim(lat: float, lon: float, raggio: float, azienda: Optional[str]) -> tuple[list, list]:
    proj = _proiettore(lat, lon)
    dlat = raggio / 111_320.0
    dlon = raggio / (111_320.0 * math.cos(math.radians(lat)))
    viewbox = f"{lon - dlon},{lat + dlat},{lon + dlon},{lat - dlat}"
    trovati = _nominatim("search", {"amenity": "parking", "viewbox": viewbox, "bounded": 1, "limit": 50})
    perim_raw = []
    if azienda:
        perim_raw = _nominatim("search", {"q": azienda, "viewbox": viewbox, "bounded": 1, "limit": 10})

    def dettagli(elenco):
        ids = [f"{e['osm_type'][0].upper()}{e['osm_id']}" for e in elenco]
        if not ids:
            return []
        return _nominatim("lookup", {"osm_ids": ",".join(ids[:50]), "polygon_geojson": 1, "extratags": 1, "addressdetails": 1})

    parcheggi = []
    for e in dettagli(trovati):
        tags = dict(e.get("extratags") or {})
        if e.get("name"):
            tags.setdefault("name", e["name"])
        via = (e.get("address") or {}).get("road")
        if via:
            tags["_via"] = via
        poly = _geojson_a_poly(e.get("geojson") or {"type": "Point", "coordinates": [float(e["lon"]), float(e["lat"])]}, proj)
        if poly:
            parcheggi.append(OggettoOSM(f"{e['osm_type']}/{e['osm_id']}", tags, poly))
    perimetri = []
    for e in dettagli(perim_raw):
        tags = dict(e.get("extratags") or {})
        tags.setdefault("name", e.get("name") or azienda)
        poly = _geojson_a_poly(e.get("geojson") or {}, proj)
        if len(poly) >= 3 and _nome_corrisponde(tags, azienda):
            perimetri.append(OggettoOSM(f"{e['osm_type']}/{e['osm_id']}", tags, poly))
    return parcheggi, perimetri


def _overpass_query(lat: float, lon: float, raggio: float, azienda: Optional[str]) -> str:
    a = f"(around:{raggio:.0f},{lat:.6f},{lon:.6f})"
    nome = ""
    if azienda and len(azienda.strip()) >= 3:
        safe = "".join(ch for ch in azienda.strip() if ch.isalnum() or ch in " -&.")
        nome = f'nwr{a}["name"~"{safe}",i];nwr{a}["operator"~"{safe}",i];'
    return (
        "[out:json][timeout:25];("
        f"nwr{a}[amenity=parking];"
        f'way{a}[landuse~"^(industrial|commercial|retail)$"];'
        f"{nome}"
        ");out geom tags;"
    )


def _da_overpass(lat: float, lon: float, raggio: float, azienda: Optional[str]) -> tuple[list, list]:
    import httpx
    proj = _proiettore(lat, lon)
    r = httpx.post(OVERPASS_URL, data={"data": _overpass_query(lat, lon, raggio, azienda)},
                   headers={"User-Agent": USER_AGENT}, timeout=30.0)
    r.raise_for_status()
    return elementi_overpass(r.json().get("elements", []), proj, azienda)


def elementi_overpass(elements: list, proj, azienda: Optional[str]) -> tuple[list, list]:
    """Separa i parcheggi dai perimetri in una risposta Overpass ('out geom tags')."""
    parcheggi, perimetri = [], []
    for e in elements:
        tags = e.get("tags") or {}
        if e["type"] == "node":
            poly = [proj(e["lat"], e["lon"])]
        elif e["type"] == "way" and e.get("geometry"):
            poly = [proj(g["lat"], g["lon"]) for g in e["geometry"]]
        elif e["type"] == "relation":
            outer = [m for m in e.get("members", []) if m.get("role") == "outer" and m.get("geometry")]
            if not outer:
                continue
            poly = [proj(g["lat"], g["lon"]) for g in max(outer, key=lambda m: len(m["geometry"]))["geometry"]]
        else:
            continue
        oid = f"{e['type']}/{e['id']}"
        if tags.get("amenity") == "parking":
            parcheggi.append(OggettoOSM(oid, tags, poly))
        elif len(poly) >= 3:
            nome_ok = _nome_corrisponde(tags, azienda)
            contiene_sede = punto_in_poligono((0.0, 0.0), poly)
            if nome_ok or (tags.get("landuse") and contiene_sede):
                perimetri.append(OggettoOSM(oid, tags, poly))
    return parcheggi, perimetri


def _centra_su_azienda(sede: dict, azienda: str, raggio_m: float = 1500.0) -> dict:
    """Se OSM ha un oggetto col nome dell'azienda vicino all'indirizzo (edificio,
    stabilimento), la sede viene centrata li': un indirizzo di via lunga puo'
    cadere a centinaia di metri dallo stabilimento."""
    lat, lon = sede["lat"], sede["lon"]
    dlat = raggio_m / 111_320.0
    dlon = raggio_m / (111_320.0 * math.cos(math.radians(lat)))
    try:
        res = _nominatim("search", {"q": azienda, "viewbox": f"{lon - dlon},{lat + dlat},{lon + dlon},{lat - dlat}",
                                    "bounded": 1, "limit": 5})
    except Exception:
        return sede
    for e in res:
        if _nome_corrisponde({"name": e.get("name") or e.get("display_name", "")}, azienda):
            return {**sede, "lat": float(e["lat"]), "lon": float(e["lon"]),
                    "centrata_su": f"{e.get('name') or azienda} ({e.get('category', '')}/{e.get('type', '')} su OSM)"}
    return sede


def cerca_parcheggi(indirizzo: Optional[str] = None, lat: Optional[float] = None, lon: Optional[float] = None,
                    raggio_m: float = 250.0, azienda: Optional[str] = None) -> dict:
    chiave = (str(indirizzo or "").strip().lower(), lat, lon, round(raggio_m), str(azienda or "").strip().lower())
    hit = _cache.get(chiave)
    if hit and time.time() - hit[0] < _CACHE_TTL_S:
        return hit[1]

    if lat is not None and lon is not None:
        sede = {"lat": lat, "lon": lon, "indirizzo_trovato": indirizzo or f"{lat:.5f}, {lon:.5f}"}
    elif indirizzo:
        sede = geocodifica(indirizzo)
    else:
        raise ValueError("Serve l'indirizzo della sede oppure le coordinate.")
    if azienda:
        sede = _centra_su_azienda(sede, azienda)

    fonte = "Overpass API"
    try:
        parcheggi, perimetri = _da_overpass(sede["lat"], sede["lon"], raggio_m, azienda)
    except Exception:
        fonte = "Nominatim"
        parcheggi, perimetri = _da_nominatim(sede["lat"], sede["lon"], raggio_m, azienda)

    ris = analizza(parcheggi, perimetri, azienda)
    ris.update({"sede": sede, "raggio_m": raggio_m, "fonte": fonte, "attribuzione": ATTRIBUZIONE})
    _cache[chiave] = (time.time(), ris)
    return ris
