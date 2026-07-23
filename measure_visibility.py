#!/usr/bin/env python3
"""
W3 — Sichtbarkeits-Messung an Panorama-Livecams.

Liest die im Weitsicht-Admin (admin/calibrate.php) gesetzten Referenzpunkte
(Gipfel/Orte in bekannter Distanz, mit Bild-Pixel x/y) über api/refpoints.php,
holt pro Cam das aktuelle Panorama und prüft an jedem Referenz-Pixel den lokalen
Kontrast. Ein Punkt gilt als sichtbar, wenn der Kontrast über der Schwelle liegt
(ferner Gipfel/Ort gegen Dunst verschwindet = niedriger Kontrast). Die Distanz
des FERNSTEN noch sichtbaren Punkts ist die gemessene Mindest-Fernsicht.

Ergebnis je Cam → POST an api/webcam-ingest.php (Tabelle fernsicht_observed).

Manueller Ansatz statt Skyline-Matching (W1/W2 verworfen): der Mensch markiert
im Bild nur real anvisierbare, sichtbare Punkte — keine Horizont-/Offset-/
Verdeckungs-Probleme mehr. Diese Datei ersetzt die W1-Landmark-Pipeline als
laufenden Schritt; generate_landmarks.py bleibt nur als Geometrie-Referenz.

WICHTIG — Kalibrierung: die Kontrast-Schwelle (CONTRAST_THRESHOLD) ist der
zentrale Tuning-Parameter und MUSS an echten annotierten Bildern kalibriert
werden. Dazu `--dry-run --cam <id>` nutzen: druckt je Punkt den rohen Kontrast,
ohne zu posten. Der Rohwert wird zusätzlich in `detail` mitgeschrieben.

Env:
  REFPOINTS_URL        z.B. https://tool.wetteralarm.ch/weitsicht/stage/api/refpoints.php
  INGEST_URL           z.B. https://tool.wetteralarm.ch/weitsicht/stage/api/webcam-ingest.php
  WEBCAM_INGEST_TOKEN  Token (X-Ingest-Token) — matcht WEBCAM_INGEST_TOKEN der Weitsicht-.env
  CONTRAST_THRESHOLD   Default 0.035 (Coefficient of Variation der Luminanz im Patch)
  PATCH_RADIUS_PX      Default 8 (halbe Fensterkante in Pixel)
  NIGHT_LUMA           Default 35 (mittlere Bild-Luminanz darunter → quality=night, keine Messung)
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

CONTRAST_THRESHOLD = float(_env("CONTRAST_THRESHOLD", "0.035"))
PATCH_RADIUS_PX    = int(_env("PATCH_RADIUS_PX", "8"))
NIGHT_LUMA         = float(_env("NIGHT_LUMA", "35"))
HTTP_TIMEOUT       = int(_env("HTTP_TIMEOUT", "45"))

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


def download_gray(url: str):
    """Panorama laden → (Graustufen-Array float32 [H,W], observed_at-ISO|None)."""
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
    img = Image.open(io.BytesIO(r.content)).convert("L")
    return np.asarray(img, dtype=np.float32), observed_at


def local_contrast(gray: np.ndarray, xf: float, yf: float, radius: int) -> float:
    """Coefficient of Variation (std/mean) der Luminanz im Patch um (xf,yf).
    Dunst senkt den lokalen Kontrast — ferner Gipfel/Ort gegen Himmel = hoher CoV,
    im Dunst verschluckt = niedriger CoV. Robust gegen globale Helligkeit."""
    h, w = gray.shape
    px = min(max(int(round(xf * (w - 1))), 0), w - 1)
    py = min(max(int(round(yf * (h - 1))), 0), h - 1)
    x0, x1 = max(px - radius, 0), min(px + radius + 1, w)
    y0, y1 = max(py - radius, 0), min(py + radius + 1, h)
    patch = gray[y0:y1, x0:x1]
    if patch.size == 0:
        return 0.0
    mean = float(patch.mean())
    if mean < 1e-3:
        return 0.0
    return float(patch.std() / mean)


def measure_cam(cam: dict) -> dict | None:
    cam_id = cam.get("id")
    points = cam.get("points") or []
    image  = cam.get("image")
    if not cam_id or not image or not points:
        return None

    try:
        gray, observed_at = download_gray(image)
    except Exception as e:  # noqa: BLE001
        log.warning("Cam %s: Bild-Download/Decode fehlgeschlagen: %s", cam_id, e)
        return {
            "cam_id": cam_id,
            "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "measured_km": None, "max_ref_km": None,
            "visible_count": 0, "total_count": len(points),
            "band": band_for_alt(cam.get("alt")), "quality": "error", "detail": [],
        }

    if observed_at is None:
        observed_at = datetime.now(timezone.utc)

    mean_luma = float(gray.mean())
    night = mean_luma < NIGHT_LUMA

    detail, visible_dists, all_dists = [], [], []
    for p in points:
        dist_km = int(p.get("dist_km") or 0)
        all_dists.append(dist_km)
        c = local_contrast(gray, float(p.get("x", 0)), float(p.get("y", 0)), PATCH_RADIUS_PX)
        vis = (not night) and (c >= CONTRAST_THRESHOLD)
        if vis:
            visible_dists.append(dist_km)
        detail.append({
            "name": p.get("name"), "dist_km": dist_km,
            "contrast": round(c, 4), "visible": bool(vis),
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
    log.info("Refpoints-Stand %s — %d Cams, %d Punkte (Schwelle=%.3f, Patch=%dpx)",
             data.get("updated_at"), len(cams), data.get("point_count", 0),
             CONTRAST_THRESHOLD, PATCH_RADIUS_PX)

    observations = []
    for cam in cams:
        obs = measure_cam(cam)
        if obs is None:
            continue
        observations.append(obs)
        vd = obs["measured_km"]
        log.info("  %-28s %s  sichtbar %d/%d  → gemessen %s km  [%s]",
                 obs["cam_id"], obs["observed_at"], obs["visible_count"], obs["total_count"],
                 (vd if vd is not None else "—"), obs["quality"])
        if args.dry_run:
            for d in obs["detail"]:
                flag = "✓" if d["visible"] else "·"
                log.info("      %s %-24s %4d km  contrast=%.4f", flag, str(d["name"])[:24], d["dist_km"], d["contrast"])

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
