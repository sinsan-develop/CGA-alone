# CGA Studio

Sprache: [한국어](README.md) | [English](README.en.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [Tiếng Việt](README.vi.md) | [Français](README.fr.md) | [Deutsch](README.de.md)

CGA Studio ist eine webbasierte Plattform zum Entwerfen, Trainieren, Testen und Betreiben dialogorientierter KI-Bots über mehrere Kanäle. Bots und Versionen, Intents, Entitäten, Wörterbücher, Dialogabläufe, Antworten, Kanäle, Tests und Betriebszustände werden in einem Arbeitsbereich verwaltet.

## Hauptfunktionen

- Koreanisch, Englisch, vereinfachtes Chinesisch, Japanisch, Vietnamesisch, Französisch und Deutsch
- NLU mit ML, semantischen Vektoren, externen Embeddings und LLM
- Feste Antworten, Semantic RAG, LLM RAG und direkte LLM-Antworten
- Versionsbezogenes Training und Indexieren über einen eigenen NLU Training Worker
- Bot Test mit Runtime-, Variables- und Trace-Analyse
- Integration mit Webchat, Kakao und Microsoft Teams
- Kakao Quick Replies, Basic Cards, List Cards und Carousel-Konvertierung
- Verwaltung von Benutzern, Gruppen, Lizenzen, Queues, Logs und Systemzustand
- CPU als Standard, optionale NVIDIA-GPU-Beschleunigung

## Architektur

| Komponente | Aufgabe |
|---|---|
| `studio` | Next.js-basierte CGA-Studio-Weboberfläche |
| `api` | FastAPI-API für Authentifizierung, Design, Runtime und Administration |
| `nlu-training-worker` | ML-/Semantic-Training und Indexierung |
| `vector-worker` | Embeddings und Vektorsuche |
| `redis` | Lese-Cache |
| PostgreSQL | Externe DB für Benutzer, Bots, Versionen und Betriebsdaten |

## Voraussetzungen

- Git
- Docker Engine und Docker Compose V2 (`docker compose`)
- Erreichbarer PostgreSQL-Server mit angelegter CGA-Datenbank und Benutzer
- Ausgestellte CGA-Lizenz und der öffentliche PEM-Schlüssel zur Prüfung
- NVIDIA-Treiber und NVIDIA Container Toolkit für den GPU-Modus

> Dieses Repository enthält keinen PostgreSQL-Container. Der Host in `CGA_DATABASE_URL` muss über das externe Docker-Netzwerk `common_default` erreichbar sein.

## Installation

### 1. Repository und Umgebungsdatei

```bash
git clone https://github.com/OWNER/CGA.git
cd CGA
cp .env.example .env
```
Unter Windows PowerShell verwenden Sie `Copy-Item .env.example .env`.

### 2. Pflichtvariablen konfigurieren

```dotenv
CGA_DATABASE_URL=postgresql+psycopg://cga_user:replace-with-password@shared-db:5432/cga
CGA_JWT_SECRET=replace-with-a-random-secret-of-at-least-32-characters
CGA_INITIAL_ADMIN_PASSWORD=replace-with-a-strong-password
CGA_LICENSE_PUBLIC_KEY=base64-encoded-public-pem
CORS_ORIGINS=http://localhost:5173
```
`CGA_LICENSE_PUBLIC_KEY` muss die Base64-Codierung der **vollständigen öffentlichen PEM-Datei** enthalten, nicht deren SHA-256-Fingerabdruck. Privater Schlüssel und echte `.env` dürfen nie committed werden.

### 3. Netzwerke und PostgreSQL vorbereiten

Diese Netzwerke nur einmal anlegen, sofern sie fehlen:

```bash
docker network create proxy-network
docker network create common_default
```
Verbinden Sie den PostgreSQL-Container mit `common_default` und prüfen Sie Host, Datenbank, Benutzer und Passwort in `CGA_DATABASE_URL`. `proxy-network` dient der Reverse-Proxy-Anbindung.

### 4. CPU-Modus starten

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml config -q
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```
### 5. NVIDIA-GPU-Modus starten (optional)

Setzen Sie `CGA_TORCH_INDEX_URL` in `.env` und laden Sie das GPU-Overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.gpu.yml config -q
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.gpu.yml up -d --build
```
Das Overlay weist `api`, `nlu-training-worker` und `vector-worker` GPU-Geräte zu. Für reine CPU-Systeme wird es nicht eingebunden.

### 6. Prüfen und anmelden

```bash
docker compose ps
curl http://localhost:5173/health/ready
```
Öffnen Sie `http://localhost:5173`. Die initiale Administrator-ID lautet `master`; das Passwort ist der Wert von `CGA_INITIAL_ADMIN_PASSWORD`. Laden Sie danach unter **Admin > Lizenz** die ausgestellte Lizenzdatei hoch.

## Aktualisieren und stoppen

Führen Sie `git pull --ff-only` aus und wiederholen Sie den CPU- oder GPU-Build. Zum Stoppen bei Erhalt der Volumes:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml down
```
Verwenden Sie `down -v` nur, wenn persistente Daten absichtlich gelöscht werden sollen.

## Fehlerbehebung

- `CGA_DATABASE_URL is required`: DB-Verbindungszeichenfolge in `.env` setzen.
- DB-Verbindung fehlgeschlagen: PostgreSQL und Netzwerk `common_default` prüfen.
- Lizenz nicht aktiv: Base64 des öffentlichen PEM prüfen und passende Lizenz hochladen.
- GPU nicht verfügbar: zuerst `nvidia-smi` und NVIDIA Container Toolkit prüfen.
- Studio nicht ready: `docker compose ps` und `docker compose logs api studio` prüfen.

## Dokumentation

- [CGA Getting Started](docs/manual/cga-getting-started/README.de.md)
- [CGA-Benutzerhandbuch](docs/manual/cga-user-manual/README.de.md)
- [CGA-NLU-Leitfaden](docs/manual/cga-nlu-guide/README.de.md)

## Lizenz und Sicherheit

Öffentlich sichtbarer Quellcode ist keine CGA-Produktlizenz. Für die Nutzung kann eine ausgestellte Lizenz erforderlich sein. Veröffentlichen Sie niemals `.env`, DB-Passwörter, JWT-Secrets, private Lizenzschlüssel, Provider-API-Keys oder Produktionszugänge.
