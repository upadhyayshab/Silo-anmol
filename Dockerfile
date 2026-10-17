FROM python:3.12-slim AS build

RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir gunicorn

# Exact versions for Python 3.12 / linux, compiled from requirements/dev.txt +
# SharedBackend/requirements.txt (reproducible builds). After changing either:
#   uv pip compile requirements/dev.txt SharedBackend/requirements.txt \
#     --python-version 3.12 --python-platform linux --no-header --no-annotate -o requirements/prod.txt
COPY requirements/prod.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && rm requirements.txt

FROM python:3.12-slim AS runtime

# Runtime system libraries required by native extensions:
#   libexpat1  → rasterio._base (libexpat.so.1)
#   libpq5     → psycopg2 (libpq.so.5)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libexpat1 \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin /usr/local/bin

COPY --from=build /src /src
COPY app app
COPY SharedBackend SharedBackend
COPY alembic.ini alembic.ini
COPY migrations migrations
ENV PYTHONPATH=/src/app/:/src/SharedBackend/src/


EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
