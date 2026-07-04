# The image-encoder — Python + Pillow (WebP via bundled libwebp), nothing else.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
EXPOSE 8087
CMD ["python", "server.py"]
