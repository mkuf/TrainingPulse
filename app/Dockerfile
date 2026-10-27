FROM python:3.12-slim AS base

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Test runner: dev dependencies only. The tests themselves are not in the build
# context; the compose `test` service mounts the repository at run time.
FROM base AS test

COPY app/requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

# Runtime image (the default target, as the last stage).
FROM base AS runtime

COPY app /app

EXPOSE 8000 8001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
