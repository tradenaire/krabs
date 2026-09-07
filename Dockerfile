FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 KRABS_RUN_MODE=standby KRABS_DATA_DIR=/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
COPY tests ./tests
COPY start.py .
RUN NUMBA_DISABLE_JIT=1 python -m unittest discover -s tests -q && NUMBA_DISABLE_JIT=1 python -c "import bot.main"
CMD ["python", "start.py"]
