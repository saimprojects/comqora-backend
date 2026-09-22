FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock && useradd --create-home sellflow
COPY --chown=sellflow:sellflow . .
RUN mkdir -p /app/staticfiles && chown sellflow:sellflow /app/staticfiles
USER sellflow
EXPOSE 8000
CMD ["python", "deploy.py", "web"]
