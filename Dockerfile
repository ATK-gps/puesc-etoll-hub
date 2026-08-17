FROM python:3.9-slim

WORKDIR /app

COPY web_app/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8000
EXPOSE 8000

CMD ["python", "web_app/server.py"]
