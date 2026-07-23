#!/usr/bin/env python3
"""
W1 — Geometrie-Engine: erzeugt landmarks.json.

Pro brauchbarer Panorama-Livecam (angle=360, hoch, working) berechnet aus der
Cam-Position + dem OSM-Peak-Katalog (peaks.json):
  - die fernen sichtbaren Gipfel (Distanz, Peilung, Höhenwinkel) — die
    Landmarks, deren Sichtbarkeit W3 im Bild prüft;
  - eine Soll-Skyline (Höhenwinkel je Azimut-Bin) — die Silhouette, gegen die
    W2 die Bild-Skyline kreuzkorreliert, um den Azimut-Offset zu finden.

Reine Geometrie (Erdkrümmung + Standard-Refraktion), KEINE Verdeckung durch
näheres Gelände (kein DEM) — die Landmark-Liste ist daher „über der Krümmungs-
Horizontlinie", leicht optimistisch. DEM-Occlusion ist eine spätere Verfeinerung.

Output: landmarks.json (Artifact). Selten nötig (Cams/Gelände ändern sich kaum).
Env: WA_LIVECAMS_URL (default my.wetteralarm.ch v9)
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import numpy as np
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("generate-landmarks")

HERE = Path(__file__).resolve().parent
WA_LIVECAMS_URL = os.environ.get("WA_LIVECAMS_URL", "https://my.wetteralarm.ch/v9/maps/livecams.json")

CH_BBOX = (5.8, 45.7, 10.6, 47.9)       # lng_min, lat_min, lng_max, lat_max
MIN_CAM_ALT = 1200                       # m — nur hohe Cams sehen weit
R_KM = 6371.0
R_EFF = 7435.0                           # km, mit Standard-Refraktion (7/6 R)
DIST_MIN, DIST_MAX = 25.0, 250.0         # km — nutzbarer Landmark-Bereich
ELEV_MIN_DEG = 0.15                      # über dem Krümmungs-Horizont
N_LANDMARKS = 6                          # fernste je Cam
SKYLINE_BINS = 720                       # 0.5°-Azimut-Auflösung


def haversine_km(lat1, lon1, lat2, lon2):
    la1, la2 = np.radians(lat1), np.radians(lat2)
    dla = np.radians(lat2 - lat1)
    dlo = np.radians(lon2 - lon1)
    a = np.sin(dla / 2) ** 2 + np.cos(la1) * np.cos(la2) * np.sin(dlo / 2) ** 2
    return 2 * R_KM * np.arcsin(np.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    la1, la2 = np.radians(lat1), np.radians(lat2)
    dlo = np.radians(lon2 - lon1)
    y = np.sin(dlo) * np.cos(la2)
    x = np.cos(la1) * np.sin(la2) - np.sin(la1) * np.cos(la2) * np.cos(dlo)
    return (np.degrees(np.arctan2(y, x)) + 360.0) % 360.0


def elev_angle_deg(cam_ele, peak_ele, dist_km):
    dh = (peak_ele - cam_ele) / 1000.0                # km
    drop = dist_km * dist_km / (2 * R_EFF)            # Erdkrümmung+Refraktion
    return np.degrees(np.arctan2(dh - drop, dist_km))


def load_peaks():
    data = json.loads((HERE / "peaks.json").read_text(encoding="utf-8"))
    p = np.array([[e[1], e[2], e[3]] for e in data["peaks"]], dtype=np.float64)  # lat,lon,ele
    names = [e[0] for e in data["peaks"]]
    log.info("Peaks geladen: %d", len(names))
    return names, p[:, 0], p[:, 1], p[:, 2]


def usable_cams():
    r = requests.get(WA_LIVECAMS_URL, timeout=30)
    r.raise_for_status()
    cams = r.json().get("livecams", [])
    out = []
    for c in cams:
        if (c.get("angle") == 360 and c.get("status") == "working"
                and isinstance(c.get("altitude"), (int, float)) and c["altitude"] >= MIN_CAM_ALT
                and c.get("lat") and c.get("long")
                and CH_BBOX[0] <= c["long"] <= CH_BBOX[2] and CH_BBOX[1] <= c["lat"] <= CH_BBOX[3]):
            out.append(c)
    log.info("Panorama-Cams (360°, >=%dm, working, CH): %d", MIN_CAM_ALT, len(out))
    return out


def main() -> int:
    names, plat, plon, pele = load_peaks()
    cams = usable_cams()
    if not cams:
        log.error("keine brauchbaren Cams"); return 1

    result = []
    for c in cams:
        clat, clon, calt = float(c["lat"]), float(c["long"]), float(c["altitude"])
        d = haversine_km(clat, clon, plat, plon)                      # (npeaks,)
        m = (d >= DIST_MIN) & (d <= DIST_MAX)
        if not m.any():
            continue
        di = d[m]
        brg = bearing_deg(clat, clon, plat[m], plon[m])
        elv = elev_angle_deg(calt, pele[m], di)
        vis = elv > ELEV_MIN_DEG
        if not vis.any():
            continue

        # Soll-Skyline: max Höhenwinkel je Azimut-Bin (nur sichtbare Peaks)
        skyline = np.full(SKYLINE_BINS, -5.0, dtype=np.float64)
        vb = brg[vis]; ve = elv[vis]
        bins = np.clip((vb / 360.0 * SKYLINE_BINS).astype(int), 0, SKYLINE_BINS - 1)
        for b, e in zip(bins, ve):
            if e > skyline[b]:
                skyline[b] = e

        # Landmarks: die fernsten sichtbaren Gipfel
        idx = np.where(vis)[0]
        order = idx[np.argsort(-di[idx])][:N_LANDMARKS]
        lm = [{
            "name": names[np.where(m)[0][j]],
            "dist_km": round(float(di[j]), 1),
            "bearing": round(float(brg[j]), 1),
            "elev": round(float(elv[j]), 2),
        } for j in order]

        result.append({
            "cam_id": c.get("id"),
            "label": c.get("label"),
            "lat": round(clat, 5), "lon": round(clon, 5), "alt": int(calt),
            "image": (c.get("images", {}).get("full", {}) or {}).get("url") or c.get("image"),
            "landmarks": lm,
            "skyline": [round(float(x), 2) for x in skyline],
        })

    out = {
        "_comment": "GENERIERT von generate_landmarks.py (W1). Soll-Skyline: Höhenwinkel je 0.5°-Azimut-Bin.",
        "peaks_source": "OSM natural=peak ele>=2000m",
        "skyline_bins": SKYLINE_BINS,
        "count": len(result),
        "cams": result,
    }
    (HERE / "landmarks.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    total_lm = sum(len(c["landmarks"]) for c in result)
    log.info("landmarks.json: %d Cams, %d Landmarks, fernste %d km",
             len(result), total_lm,
             int(max((c["landmarks"][0]["dist_km"] for c in result if c["landmarks"]), default=0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
