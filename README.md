# weitsicht-webcam-fetcher

Webcam-**Sicht-Verifikation** für Weitsicht (SPEC §10, Phase 2). Prüft an
Panorama-Livecams, ob bekannte **ferne Referenzpunkte** (Gipfel/Orte) sichtbar
sind, und leitet daraus eine **gemessene Mindest-Fernsicht** ab („sichtbar bis
≥ X km") — echte Ground-Truth neben dem Modell-Score, fürs UI und später zur
Kalibrierung.

Separates Repo (wie hail-/kenda-/forecast-Fetcher), weil Bild-CV eigene
Abhängigkeiten hat und in GitHub Actions läuft, nicht auf Infomaniak.

## Ansatz: manuelle Referenzpunkte (W3)

Der ursprüngliche Plan (automatisches Skyline-Matching, W1/W2) wurde **verworfen**
— Horizont-Erkennung ohne DEM war zu fragil und zeigte Gipfel an, wohin die
Kamera gar nicht blickt. Stattdessen jetzt:

1. **Mensch markiert** im Weitsicht-Admin (`admin/calibrate.php`) pro Cam beliebig
   viele Referenzpunkte: klickt im Panorama auf einen Gipfel/Ort und wählt ihn aus
   dem Katalog (Distanz automatisch via haversine) — nur real sichtbare Punkte.
2. **Worker misst** laufend: holt pro Cam das aktuelle Bild und prüft an jedem
   Referenz-Pixel den lokalen Kontrast (ferner Punkt gegen Dunst = niedriger
   Kontrast). Sichtbar/nicht → **gemessene Fernsicht = Distanz des fernsten noch
   sichtbaren Punkts**.

Keine Horizont-/Offset-/Verdeckungs-Probleme mehr — der Mensch löst die schwere
Wahrnehmungsaufgabe einmalig, der Worker macht nur noch Kontrastmessung.

## Meilensteine

| | Stand | Inhalt |
|---|---|---|
| ~~W1 Geometrie~~ | verworfen | `generate_landmarks.py` bleibt nur als Geometrie-Referenz |
| ~~W2 Kalibrierung~~ | verworfen | Skyline-Kreuzkorrelation (ohne DEM zu fragil) |
| **W3** Messung | ✅ gebaut | `measure_visibility.py`: Refpoints → Kontrast → gemessene km → Ingest |
| **W4** Backend/UI | ✅ gebaut | Weitsicht: `fernsicht_observed`, `webcam-ingest.php`, Admin-Anzeige |

## W3 — `measure_visibility.py`

Liest die Referenzpunkte token-gesichert über `api/refpoints.php` (self-contained:
pro Cam Bild-URL + Geometrie + Pixel), misst je Punkt den Kontrast, postet das
Ergebnis an `api/webcam-ingest.php` und pingt `api/external-heartbeat.php`.

**Zwei Kontrast-Metriken je Punkt-Typ** (weil ein Ort im Tal keinen Himmel als
Hintergrund hat wie ein Gipfel):

| Typ (`kind`) | Metrik | Schwelle | Idee |
|---|---|---|---|
| Gipfel (`peak`) | Weber-Kontrast Objekt-gegen-Himmel | `PEAK_CONTRAST_THRESHOLD` ≈ 0.02 | Koschmieder-Sichtweite; Gipfel verschmilzt im Dunst mit dem Himmel. Material-robust. |
| Ort (`c/t/v`), `manual` | lokaler Detailkontrast (std/mittel) | `PLACE_CONTRAST_THRESHOLD` ≈ 0.03 | „erkenne ich noch Struktur?"; Dunst glättet den Ort zum grauen Fleck. |

Nacht (mittlere Bild-Luminanz `< NIGHT_LUMA`) → keine Messung (`quality=night`).

**⚠ Kalibrierung nötig:** beide Schwellen an echten annotierten Bildern justieren.
`detail[]` enthält je Punkt `method`, `contrast` und `threshold`. Dazu:

```bash
# lokal, mit gesetzten Env-Vars — misst + druckt je Punkt den Rohkontrast, ohne zu posten
python measure_visibility.py --dry-run --cam <cam_id>
```

Der Rohkontrast steht zusätzlich in jeder Beobachtung unter `detail[]`, also auch
im Admin nach echten Läufen auswertbar.

### Env / Secrets (GitHub Actions Repository-Secrets)

| Secret | Beispiel |
|---|---|
| `REFPOINTS_URL` | `https://tool.wetteralarm.ch/weitsicht/stage/api/refpoints.php` |
| `INGEST_URL` | `https://tool.wetteralarm.ch/weitsicht/stage/api/webcam-ingest.php` |
| `HEARTBEAT_URL` | `https://tool.wetteralarm.ch/weitsicht/stage/api/external-heartbeat.php` |
| `WEBCAM_INGEST_TOKEN` | langer Zufallswert, **identisch** zu `WEBCAM_INGEST_TOKEN` in der Weitsicht-.env |

Optionale Tuning-**Variables** (nicht Secrets): `PEAK_CONTRAST_THRESHOLD`,
`PLACE_CONTRAST_THRESHOLD`, `PATCH_RADIUS_PX`, `NIGHT_LUMA`.

### Auslösung

`workflow_dispatch` (zuverlässiger Trigger kommt aus dem Weitsicht-Cron
`trigger_webcam_fetcher.php` via GitHub-API) + schedule-Fallback tagsüber stündlich.

## Beziehung zu Weitsicht

Ergebnis (gemessene Fernsicht je Cam) geht über `api/webcam-ingest.php` in die
Tabelle `fernsicht_observed` und wird im Admin neben dem Modell-Score angezeigt.
Details: `Weitsicht/Doku/` (SPEC §10, ENTSCHEIDUNGEN).

## Lizenz / Daten

Peaks/Orte: OpenStreetMap (ODbL). Livecam-Bilder: Wetter-Alarm/Roundshot. Code: intern.
