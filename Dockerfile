# The image-encoder — Python + Pillow (WebP via bundled libwebp), nothing else.
FROM python:3.14-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
RUN useradd --system --no-create-home encoder
USER encoder
EXPOSE 8087
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD ["python", "-c", "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8087')}/health\", timeout=2).status == 200 else 1)"]
CMD ["python", "server.py"]
