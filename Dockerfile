FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY run_v3.py ./run_v3.py
RUN pip install --no-cache-dir .[api]
EXPOSE 8000
CMD ["uvicorn", "fieldshift.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
