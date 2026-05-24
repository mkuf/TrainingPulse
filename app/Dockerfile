FROM python:3.12-slim

WORKDIR /app

COPY packages/trainingpulse_common /packages/trainingpulse_common
COPY plugins/withings /plugins/withings
COPY plugins/fddb /plugins/fddb
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -e /packages/trainingpulse_common \
    && pip install --no-cache-dir -e /plugins/withings -e /plugins/fddb

COPY app /app

EXPOSE 8000 8001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
