FROM python:3.12-slim

WORKDIR /app

COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

COPY server.py .

ENV PORT=8080
EXPOSE 8080

CMD ["python", "-u", "server.py"]
