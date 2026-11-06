# B2B car lists evaluation system for CarSuperMarket

## Local configuration

Option 1, with conda

```bash
conda create --name uk-lists-evaluator python=3.9 poetry -y
conda activate uk-lists-evaluator
poetry install --with dev
```

Option 2, with pip

```bash
python3 -m pip install virtualenv \
    && virtualenv .venv -p 3.9 \
    && source .venv/bin/activate \
    && python -m pip install --upgrade pip \
    && python -m pip -r requirements.txt
```

## Local development

Sign-in is Keycloak SSO; see `docs/keycloak_sso.md`. To work locally without Keycloak (VPN and
AWS SSO login needed, the app reads users from the dev database), from the repo root:

```bash
cd source_code/router
LOCAL_ENV=1 LOCAL_LOGIN_AS=<existing username> poetry run uvicorn main:app --app-dir backend --reload --port 8080
```

Then open <http://127.0.0.1:8080/>. `/login` signs you in as that user. It does not work in Docker
(docker-compose sets `AWS_LAMBDA_FUNCTION_NAME`, which switches the bypass off).

### Using FastAPI directly (not the way that reproduces the production environment)

```bash
poetry run uvicorn main:app --reload
```

### Using Docker (recommended to reproduce the production environment)

Running the FastAPI app in a Docker container

```bash
docker build -t uk_list_evaluator .
docker run -p 9000:8080 -e AWS_PROFILE=aramis -e AWS_REGION=us-east-2 -v ~/.aws:/root/.aws uk_list_evaluator
```
Invoking the FastAPI app in a Docker container

```bash
curl "http://localhost:9000/2015-03-31/functions/function/invocations" -d '@docs/request.json'
```
