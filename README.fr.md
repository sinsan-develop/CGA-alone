# CGA Studio

Langue : [한국어](README.md) | [English](README.en.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [Tiếng Việt](README.vi.md) | [Français](README.fr.md) | [Deutsch](README.de.md)

CGA Studio est une plateforme web permettant de concevoir, entraîner, tester et exploiter des bots d’IA conversationnelle sur plusieurs canaux. Elle centralise les bots et versions, intentions, entités, dictionnaires, flux de dialogue, réponses, canaux, tests et états d’exploitation.

## Fonctionnalités principales

- Coréen, anglais, chinois simplifié, japonais, vietnamien, français et allemand
- NLU par ML, vecteurs sémantiques, embeddings externes et LLM
- Réponses fixes, Semantic RAG, LLM RAG et réponses directes par LLM
- Entraînement et indexation versionnés via un NLU Training Worker dédié
- Bot Test avec analyse Runtime, Variables et Trace
- Connexion à Webchat, Kakao et Microsoft Teams
- Conversion Kakao : réponses rapides, cartes simples, listes et carrousels
- Administration des utilisateurs, groupes, licences, files, journaux et états
- Exécution CPU par défaut et accélération NVIDIA GPU en option

## Architecture

| Composant | Rôle |
|---|---|
| `studio` | Interface Web CGA Studio basée sur Next.js |
| `api` | API FastAPI d’authentification, conception, exécution et administration |
| `nlu-training-worker` | Entraînement et indexation ML/Semantic |
| `vector-worker` | Embeddings et recherche vectorielle |
| `redis` | Cache de lecture |
| PostgreSQL | Base externe pour utilisateurs, bots, versions et données d’exploitation |

## Prérequis

- Git
- Docker Engine et Docker Compose V2 (`docker compose`)
- PostgreSQL accessible avec une base et un utilisateur CGA déjà créés
- Une licence CGA émise et la clé publique PEM servant à la vérifier
- Pilote NVIDIA et NVIDIA Container Toolkit pour le mode GPU

> Ce dépôt ne fournit pas de conteneur PostgreSQL. L’hôte défini dans `CGA_DATABASE_URL` doit être joignable via le réseau Docker externe `common_default`.

## Installation

### 1. Cloner et créer le fichier d’environnement

```bash
git clone https://github.com/OWNER/CGA.git
cd CGA
cp .env.example .env
```
Sous Windows PowerShell, utilisez `Copy-Item .env.example .env`.

### 2. Configurer les variables obligatoires

```dotenv
CGA_DATABASE_URL=postgresql+psycopg://cga_user:replace-with-password@shared-db:5432/cga
CGA_JWT_SECRET=replace-with-a-random-secret-of-at-least-32-characters
CGA_INITIAL_ADMIN_PASSWORD=replace-with-a-strong-password
CGA_LICENSE_PUBLIC_KEY=base64-encoded-public-pem
CORS_ORIGINS=http://localhost:5172
```
`CGA_LICENSE_PUBLIC_KEY` doit contenir l’encodage Base64 du **fichier PEM public complet**, et non son empreinte SHA-256. Ne commitez jamais la clé privée ni le véritable fichier `.env`.

### 3. Préparer les réseaux et PostgreSQL

Créez ces réseaux une seule fois s’ils n’existent pas :

```bash
docker network create proxy-network
docker network create common_default
```
Connectez le conteneur PostgreSQL à `common_default`, puis vérifiez l’hôte, la base, l’utilisateur et le mot de passe de `CGA_DATABASE_URL`. `proxy-network` sert à la connexion au reverse proxy.

### 4. Démarrer en mode CPU

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml config -q
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```
### 5. Démarrer avec NVIDIA GPU (optionnel)

Définissez `CGA_TORCH_INDEX_URL` dans `.env`, puis ajoutez l’overlay GPU :

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.gpu.yml config -q
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.gpu.yml up -d --build
```
L’overlay affecte le GPU à `api`, `nlu-training-worker` et `vector-worker`. Ne l’utilisez pas pour un déploiement CPU seul.

### 6. Vérifier et se connecter

```bash
docker compose ps
curl http://localhost:5172/health/ready
```
Ouvrez `http://localhost:5172`. L’identifiant administrateur initial est `master` et son mot de passe est la valeur de `CGA_INITIAL_ADMIN_PASSWORD`. Après connexion, chargez la licence émise dans **Admin > Licence**.

## Mise à jour et arrêt

Exécutez `git pull --ff-only`, puis relancez la commande CPU ou GPU. Pour arrêter en conservant les volumes :

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml down
```
N’utilisez pas `down -v` sauf si vous souhaitez supprimer les données persistantes.

## Dépannage

- `CGA_DATABASE_URL is required` : renseignez l’URL de base dans `.env`.
- Échec DB : vérifiez PostgreSQL et le réseau `common_default`.
- Licence non appliquée : vérifiez le Base64 du PEM public et chargez la licence correspondante.
- GPU indisponible : vérifiez `nvidia-smi` et NVIDIA Container Toolkit.
- Studio non prêt : consultez `docker compose ps` et `docker compose logs api studio`.

## Documentation

- [CGA Getting Started](docs/manual/cga-getting-started/README.fr.md)
- [Manuel utilisateur CGA](docs/manual/cga-user-manual/README.fr.md)
- [Guide CGA NLU](docs/manual/cga-nlu-guide/README.fr.md)

## Licence et sécurité

La visibilité publique du code ne constitue pas une licence produit CGA. Une licence émise peut être requise. Ne publiez jamais `.env`, mots de passe DB, secrets JWT, clés privées de licence, clés API Provider ou identifiants de production.
