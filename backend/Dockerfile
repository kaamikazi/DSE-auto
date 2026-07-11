FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml requirements.txt ./
COPY app ./app
RUN pip install --no-cache-dir ".[postgres]"
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

