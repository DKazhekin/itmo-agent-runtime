FROM python:3.12.12-slim-bookworm
WORKDIR /app
RUN mkdir /model-state && chown 1000:1000 /model-state
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY runtime /app/runtime
COPY client /app/client
COPY contracts /app/contracts
COPY tests /app/tests
COPY baf /app/baf
COPY scripts /app/scripts
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["python", "-m", "uvicorn", "runtime.gateway:app", "--host", "0.0.0.0", "--port", "8000"]
