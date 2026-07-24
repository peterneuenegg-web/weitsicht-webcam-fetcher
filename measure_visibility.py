#!/usr/bin/env python3
"""
W3 — Sichtbarkeits-Messung an Panorama-Livecams.

Liest die im Weitsicht-Admin (admin/calibrate.php) gesetzten Referenzpunkte
(Gipfel/Orte in bekannter Distanz, mit Bild-Pixel x/y) über api/refpoints.php,
holt pro Cam das aktuelle Panorama und prüft an jedem Referenz-Pixel den
Kontrast. Die Distanz des FERNSTEN noch sichtbaren Punkts ist die gemessene
Mindest-Fernsicht.

ZWEI METRIKEN je Punkt-Typ (kind), weil ein Ort im Tal keinen Himmel-Hintergrund
hat wie ein Gipfel:
  - GIPFEL (kind=peak): Weber-Kontrast Objekt-gegen-Himmel — Gipfel gegen den
    Himmel direkt darüber. Entspricht der meteorologischen Sichtweite
    (Koschmieder), Sichtgrenze ~0.02. Material-robust (Fels wie Schnee).
  - ORTE (kind=c/t/v) + manuell: lokaler Detailkontrast (std/mittel im Patch) —
    „erkenne ich noch Struktur?"; Dunst glättet den Ort zum grauen Fleck.
Entsprechend zwei Schwellen: PEAK_CONTRAST_THRESHOLD / PLACE_CONTRAST_THRESHOLD.

ZWEITES SIGNAL (sky_clarity 0–100): aus dem Horizont-Himmel (oberes Bildband,
360°-Mittel) — sattes Blau = klar, grau/weiss = Dunst. Braucht keine Punkte, gilt
für alle Cams; misst den Dunst-Zustand statt km (siehe sky_clarity()).

Ergebnis je Cam → POST an api/webcam-ingest.php (Tabelle fernsicht_observed).

Manueller Ansatz statt Skyline-Matching (W1/W2 verworfen): der Mensch markiert
im Bild nur real anvisierbare, sichtbare Punkte — keine Horizont-/Offset-/
Verdeckungs-Probleme mehr. generate_landmarks.py bleibt nur Geometrie-Referenz.

WICHTIG — Kalibrierung: die Schwellen an echten annotierten Bildern justieren.
Dazu `--dry-run --cam <id>`: druckt je Punkt Metrik + Rohkontrast + Schwelle,
ohne zu posten. Rohwert, method und threshold stehen auch in `detail`.

Env:
  REFPOINTS_URL            https://tool.wetteralarm.ch/weitsicht/stage/api/refpoints.php
  INGEST_URL               https://tool.wetteralarm.ch/weitsicht/stage/api/webcam-ingest.php
  WEBCAM_INGEST_TOKEN      X-Ingest-Token — matcht WEBCAM_INGEST_TOKEN der Weitsicht-.env
  PEAK_CONTRAST_THRESHOLD  Default 0.02 (Gipfel, Weber-Kontrast; Fallback: CONTRAST_THRESHOLD)
  PLACE_CONTRAST_THRESHOLD Default 0.03 (Orte, lokaler Detailkontrast)
  PATCH_RADIUS_PX          Default 8 (halbe Fensterkante in Pixel)
  NIGHT_LUMA               Default 35 (mittlere Bild-Luminanz darunter → quality=night)
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

import numpy as np
import requests
from PIL import Image

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("measure-visibility")

def _env(name: str, default: str) -> str:
    """os.environ.get, aber leere Strings (z.B. nicht gesetzte GitHub-vars, die
    als '' ankommen) fallen auf den Default zurück."""
    v = os.environ.get(name, "").strip()
    return v if v else default


REFPOINTS_URL = _env("REFPOINTS_URL", "")
INGEST_URL    = _env("INGEST_URL", "")
HEARTBEAT_URL = _env("HEARTBEAT_URL", "")   # optional: api/external-heartbeat.php
TOKEN         = _env("WEBCAM_INGEST_TOKEN", "")

# Zwei Schwellen, weil zwei Metriken (siehe measure_cam):
#  - Gipfel: Weber-Kontrast Objekt-gegen-Himmel → Koschmieder-Sichtgrenze ~0.02
#  - Orte:   lokaler Detailkontrast (std/mittel) → eigener, empirischer Wert
# PEAK_CONTRAST_THRESHOLD fällt auf das alte CONTRAST_THRESHOLD zurück (Kompat).
PEAK_THRESHOLD  = float(_env("PEAK_CONTRAST_THRESHOLD",  _env("CONTRAST_THRESHOLD", "0.02")))
PLACE_THRESHOLD = float(_env("PLACE_CONTRAST_THRESHOLD", "0.03"))
PATCH_RADIUS_PX = int(_env("PATCH_RADIUS_PX", "8"))
NIGHT_LUMA      = float(_env("NIGHT_LUMA", "35"))
HTTP_TIMEOUT    = int(_env("HTTP_TIMEOUT", "45"))

# Roundshot-Bild-URLs leiten (302) auf storage.roundshot.com/.../<datum>/<zeit>/…
_TS_RE = re.compile(r"(20\d\d)[-_/](\d\d)[-_/](\d\d)[-_/T ](\d\d)[-_h](\d\d)")


def mask(tok: str) -> str:
    return (tok[:4] + "…") if tok else "(leer)"


def fetch_refpoints() -> dict:
    if not REFPOINTS_URL or not TOKEN:
        log.error("REFPOINTS_URL und WEBCAM_INGEST_TOKEN müssen gesetzt sein.")
        sys.exit(2)
    r = requests.get(REFPOINTS_URL, headers={"X-Ingest-Token": TOKEN}, timeout=HTTP_TIMEOUT)
    if r.status_code == 403:
        log.error("403 vom Refpoints-Endpoint — Token stimmt nicht (lokal %s).", mask(TOKEN))
        sys.exit(3)
    r.raise_for_status()
    return r.json()


def download_image(url: str):
    """Panorama laden → (RGB-Array float32 [H,W,3], observed_at|None)."""
    r = requests.get(url, timeout=HTTP_TIMEOUT, stream=True)
    r.raise_for_status()
    observed_at = None
    m = _TS_RE.search(r.url)  # finale URL nach Redirect
    if m:
        y, mo, d, h, mi = (int(g) for g in m.groups())
        try:
            observed_at = datetime(y, mo, d, h, mi, tzinfo=timezone.utc)
        except ValueError:
            observed_at = None
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    return np.asarray(img, dtype=np.float32), observed_at


# Luminanz-Gewichte (Rec. 601) für die Graustufen-Ableitung aus RGB.
_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)


def sky_clarity(rgb: np.ndarray):
    """Himmel-Klarheits-Index 0..100 aus dem Horizont-Himmel (oberes Bildband,
    über die volle 360°-Breite gemittelt → gleicht Sonnen-/Gegenrichtung aus).
    Sattes Blau (Rayleigh, klare Luft) → hoch; grau/weiss (Mie-Luftlicht durch
    Dunst/Nebel) → tief. Median gegen einzelne Gipfel/Wolken im Band robust.

    ZWEITES, unabhängiges Signal neben der Landmarken-Sicht: misst den
    atmosphärischen Dunst-Zustand, nicht km. Verwechselt hohe Bewölkung mit Dunst
    (grauer Himmel) — das löst erst die Kombination mit den Landmarken auf.
    Rückgabe: (index|None, metrics). None bei zu dunkel (Nacht/Dämmerung)."""
    h, w, _ = rgb.shape
    band = rgb[0:max(1, int(h * 0.30)), :, :]          # oberes 30% = Himmel
    r = float(np.median(band[:, :, 0]))
    g = float(np.median(band[:, :, 1]))
    b = float(np.median(band[:, :, 2]))
    mx, mn = max(r, g, b), min(r, g, b)
    bright = mx / 255.0
    if bright < 0.12:                                  # zu dunkel → keine Aussage
        return None, {"sat": 0.0, "blue": 0.0, "bright": round(bright, 3)}
    sat = (mx - mn) / mx if mx > 0 else 0.0            # HSV-Sättigung 0..1
    blue = max(0.0, (b - r)) / 255.0                   # Blau-Überschuss 0..1
    idx = int(round(100.0 * min(1.0, 0.6 * sat + 1.4 * blue)))  # provisorisch, zu kalibrieren
    return idx, {"sat": round(sat, 3), "blue": round(blue, 3), "bright": round(bright, 3)}


def _pix(gray: np.ndarray, xf: float, yf: float):
    h, w = gray.shape
    px = min(max(int(round(xf * (w - 1))), 0), w - 1)
    py = min(max(int(round(yf * (h - 1))), 0), h - 1)
    return px, py, w, h


def local_contrast(gray: np.ndarray, xf: float, yf: float, radius: int) -> float:
    """ORTE: Coefficient of Variation (std/mean) der Luminanz im Patch um (xf,yf).
    Ein ferner Ort im Tal hat KEINEN Himmel als Hintergrund — Sichtbarkeit heisst
    hier: erkenne ich noch Struktur/Detail? Dunst glättet den Ort zum grauen Fleck
    → niedriger CoV. Robust gegen globale Helligkeit (Normierung auf den Mittelwert)."""
    px, py, w, h = _pix(gray, xf, yf)
    x0, x1 = max(px - radius, 0), min(px + radius + 1, w)
    y0, y1 = max(py - radius, 0), min(py + radius + 1, h)
    patch = gray[y0:y1, x0:x1]
    if patch.size == 0:
        return 0.0
    mean = float(patch.mean())
    if mean < 1e-3:
        return 0.0
    return float(patch.std() / mean)


def sky_contrast(gray: np.ndarray, xf: float, yf: float, radius: int) -> float:
    """GIPFEL: Weber-Kontrast Objekt-gegen-Himmel |L_obj - L_sky| / L_sky.
    L_sky = Patch knapp OBERHALB des Punkts (Himmel), L_obj = knapp UNTERHALB
    (Gipfel). Entspricht der meteorologischen Sichtweite (Koschmieder): der
    Kontrast geht gegen 0, wenn der Gipfel im Dunst mit dem Himmel verschmilzt;
    Sichtgrenze bei ~0.02. Voraussetzung: Punkt sitzt auf der Silhouetten-Kante
    (Gipfel gegen Himmel). Material-robust — funktioniert für Fels wie Schnee."""
    px, py, w, h = _pix(gray, xf, yf)
    x0, x1 = max(px - radius, 0), min(px + radius + 1, w)
    sky = gray[max(py - 2 * radius, 0):py, x0:x1]          # oberhalb = Himmel
    obj = gray[py:min(py + 2 * radius + 1, h), x0:x1]      # unterhalb = Gipfel
    if sky.size == 0 or obj.size == 0:
        return 0.0
    l_sky = float(sky.mean())
    l_obj = float(obj.mean())
    if l_sky < 1e-3:
        return 0.0
    return abs(l_obj - l_sky) / l_sky


def measure_cam(cam: dict) -> dict | None:
    cam_id = cam.get("id")
    points = cam.get("points") or []
    image  = cam.get("image")
    if not cam_id or not image or not points:
        return None

    try:
        rgb, observed_at = download_image(image)
    except Exception as e:  # noqa: BLE001
        log.warning("Cam %s: Bild-Download/Decode fehlgeschlagen: %s", cam_id, e)
        return {
            "cam_id": cam_id,
            "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "measured_km": None, "max_ref_km": None,
            "visible_count": 0, "total_count": len(points),
            "band": band_for_alt(cam.get("alt")), "quality": "error", "detail": [],
            "sky_clarity": None, "sky_metrics": None,
        }

    gray = rgb @ _LUMA                       # Luminanz für die Kontrast-Metriken
    if observed_at is None:
        observed_at = datetime.now(timezone.utc)

    mean_luma = float(gray.mean())
    night = mean_luma < NIGHT_LUMA
    sky_idx, sky_m = sky_clarity(rgb)        # zweites Signal: Himmel-Klarheit

    # 1. Pass: Kontrast + Eigen-Sichtbarkeit je Punkt (absolute Schwelle).
    pts = []
    for p in points:
        dist_km = int(p.get("dist_km") or 0)
        xf, yf = float(p.get("x", 0)), float(p.get("y", 0))
        # Metrik nach Punkt-Typ: Gipfel = Objekt-gegen-Himmel, sonst lokaler Detailkontrast.
        # 'manual' ohne Himmelsannahme → lokal (sicherer Default).
        if p.get("kind") == "peak":
            c, method, thr = sky_contrast(gray, xf, yf, PATCH_RADIUS_PX), "sky", PEAK_THRESHOLD
        else:
            c, method, thr = local_contrast(gray, xf, yf, PATCH_RADIUS_PX), "local", PLACE_THRESHOLD
        own_vis = (not night) and (c >= thr)
        pts.append({"name": p.get("name"), "dist_km": dist_km, "method": method,
                    "contrast": round(c, 4), "threshold": thr, "own_vis": own_vis})

    # Fernste Distanz mit eigener Sichtbarkeit.
    max_vis = max((q["dist_km"] for q in pts if q["own_vis"]), default=0)

    # 2. Pass: Monotonie-Korrektur. Sichtweite ist monoton — wer X km weit sieht,
    # sieht garantiert alles Nähere. Ein näherer Punkt mit künstlich niedrigem
    # Kontrast (Berg statt Himmel dahinter, Schnee) gilt daher als sichtbar, wenn
    # ein WEITERER Punkt sichtbar ist. Hebt measured_km NICHT an (nur ≤ max_vis).
    detail, visible_dists, all_dists = [], [], []
    for q in pts:
        all_dists.append(q["dist_km"])
        eff = q["own_vis"] or (max_vis > 0 and q["dist_km"] <= max_vis)
        if eff:
            visible_dists.append(q["dist_km"])
        detail.append({
            "name": q["name"], "dist_km": q["dist_km"], "method": q["method"],
            "contrast": q["contrast"], "threshold": q["threshold"],
            "visible": bool(eff), "mono": bool(eff and not q["own_vis"]),
        })

    return {
        "cam_id": cam_id,
        "observed_at": observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "measured_km": (max(visible_dists) if visible_dists else None),
        "max_ref_km": (max(all_dists) if all_dists else None),
        "visible_count": len(visible_dists),
        "total_count": len(points),
        "band": band_for_alt(cam.get("alt")),
        "quality": ("night" if night else "ok"),
        "detail": detail,
        "sky_clarity": sky_idx,
        "sky_metrics": sky_m,
    }


def band_for_alt(alt) -> str | None:
    try:
        a = float(alt)
    except (TypeError, ValueError):
        return None
    if a >= 1800:
        return "high"
    if a >= 1000:
        return "mid"
    return "low"


def post_observations(observations: list[dict]) -> None:
    if not INGEST_URL:
        log.error("INGEST_URL nicht gesetzt — kann nicht posten.")
        sys.exit(2)
    payload = {
        "source": "webcam",
        "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "observations": observations,
    }
    r = requests.post(INGEST_URL, headers={"X-Ingest-Token": TOKEN, "Content-Type": "application/json"},
                      data=json.dumps(payload), timeout=HTTP_TIMEOUT)
    if r.status_code not in (200, 201):
        log.error("Ingest fehlgeschlagen: HTTP %s — %s", r.status_code, r.text[:300])
        sys.exit(4)
    log.info("Ingest ok: %s", r.text[:200])


def ping_heartbeat(status: str = "ok", message: str = "") -> None:
    """Best-effort Liveness-Ping an api/external-heartbeat.php (job=ingest_webcam)."""
    if not HEARTBEAT_URL:
        return
    try:
        requests.get(
            HEARTBEAT_URL,
            params={"job": "ingest_webcam", "status": status, "message": message[:200]},
            headers={"X-Ingest-Token": TOKEN}, timeout=15,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Heartbeat-Ping fehlgeschlagen: %s", e)


def main() -> int:
    ap = argparse.ArgumentParser(description="W3 — Fernsicht-Messung an Livecams")
    ap.add_argument("--dry-run", action="store_true", help="messen + drucken, NICHT posten")
    ap.add_argument("--cam", help="nur diese Cam-ID messen (Kalibrierung)")
    ap.add_argument("--limit", type=int, default=0, help="max. N Cams (0 = alle)")
    args = ap.parse_args()

    data = fetch_refpoints()
    cams = data.get("cams") or []
    if args.cam:
        cams = [c for c in cams if c.get("id") == args.cam]
    if args.limit > 0:
        cams = cams[:args.limit]
    log.info("Refpoints-Stand %s — %d Cams, %d Punkte (Schwelle Gipfel=%.3f / Orte=%.3f, Patch=%dpx)",
             data.get("updated_at"), len(cams), data.get("point_count", 0),
             PEAK_THRESHOLD, PLACE_THRESHOLD, PATCH_RADIUS_PX)

    observations = []
    for cam in cams:
        obs = measure_cam(cam)
        if obs is None:
            continue
        observations.append(obs)
        vd = obs["measured_km"]
        sky = obs.get("sky_clarity")
        log.info("  %-28s %s  sichtbar %d/%d  → gemessen %s km  Himmel=%s  [%s]",
                 obs["cam_id"], obs["observed_at"], obs["visible_count"], obs["total_count"],
                 (vd if vd is not None else "—"),
                 (str(sky) if sky is not None else "—"), obs["quality"])
        if args.dry_run and obs.get("sky_metrics"):
            m = obs["sky_metrics"]
            log.info("      Himmel: Klarheit=%s  sat=%.3f blue=%.3f bright=%.3f",
                     (sky if sky is not None else "—"), m.get("sat", 0), m.get("blue", 0), m.get("bright", 0))
        if args.dry_run:
            for d in obs["detail"]:
                flag = ("✓M" if d.get("mono") else "✓ ") if d["visible"] else "· "
                log.info("      %s %-24s %4d km  %-5s contrast=%.4f (>=%.3f)",
                         flag, str(d["name"])[:24], d["dist_km"],
                         d.get("method", ""), d["contrast"], d.get("threshold", 0))

    if not observations:
        log.warning("Keine messbaren Cams (keine Referenzpunkte gesetzt?).")
        return 0

    if args.dry_run:
        log.info("Dry-Run — nichts gepostet (%d Beobachtungen).", len(observations))
        return 0

    post_observations(observations)
    ping_heartbeat("ok", f"{len(observations)} cams")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
