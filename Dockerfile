FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev --extra web

COPY . .

EXPOSE 8501

CMD ["uv", "run", "streamlit", "run", "db_agent/web.py", \
     "--server.port=8501", "--server.address=0.0.0.0", \
     "--server.headless=true"]
