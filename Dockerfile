FROM python:3.8-slim

WORKDIR /app

ENV PYTHONFAULTHANDLER=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY requirements.txt .

RUN pip install -r requirements.txt

COPY migrations migrations
COPY brightsky brightsky
COPY setup.py .

RUN pip install .

ENTRYPOINT ["python", "-m", "brightsky"]
