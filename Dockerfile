FROM python:3.11-slim

# Системные зависимости для pdfplumber/openpyxl и сборки некоторых колёс
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Данные (SQLite, DuckDB, загруженные файлы пользователей) должны жить
# в примонтированном томе, а не внутри образа — см. docker-compose.yml
RUN mkdir -p /app/data/uploaded_files /app/data/db

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')" || exit 1

ENTRYPOINT ["streamlit", "run", "app.py", \
    "--server.address=0.0.0.0", \
    "--server.port=8501", \
    "--server.headless=true", \
    "--browser.gatherUsageStats=false"]
