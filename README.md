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

### Using FastAPI directly (not the way that reproduces the production environment)

```bash
poetry run uvicorn main:app --reload
```

### Using Docker (recommended to reproduce the production environment)

Running the FastAPI app in a Docker container

```bash
docker build -t uk-list-evaluator .
docker run --platform linux/amd64 -p 9000:8080 -e LOCAL_ENV=1 docker-image:test
```
Invoking the FastAPI app in a Docker container

```bash
curl "http://localhost:9000/2015-03-31/functions/function/invocations" -d '@request.json'
```