# microservice-image

The "encoder outside the JDK" the gallery needed. A tiny framework-free Python service (Pillow),
the same shape as the race sim: raw image bytes in, a re-encoded image out. WebP is the reason it
exists — the JVM's ImageIO cannot write it — but the endpoint is format-generic.

```
POST /encode?format=webp[&quality=82]   body: raw image bytes  -> 200 encoded bytes
GET  /health                                                   -> 200 {"status": "UP"}
```

Stateless, deterministic per input. `microservice-memes` calls it to serve smaller WebP to clients
that accept it; if this service is down, memes serves the PNG it already has — an outage degrades
quality, never availability.

```bash
pip install -r requirements.txt && python3 server.py    # :8087
python3 -m unittest test_server
```
