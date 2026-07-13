FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY requirements ./requirements
COPY app ./app
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
RUN pip install --no-cache-dir --require-hashes -r requirements/runtime.lock.txt
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"]
