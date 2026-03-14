# simple-scan-web

A SANE-based document scanner with a REST API and web UI. Use it from
the browser, or drive it entirely via API from Home Assistant, shell scripts,
or any HTTP client.

## Features

- **REST API** for programmatic scanning — trigger scans, manage sessions,
  and retrieve PDFs from any HTTP client
- **Web UI** with dark/light theme toggle for scanning from a phone or browser
- Auto-discovers SANE scanners on the network
- Multi-page scanning with live thumbnail previews
- Exports merged PDFs to a configurable output directory
- Session state persisted across restarts
- OpenAPI docs at `/api-docs`

## API

All scanning operations are available as REST endpoints. The web UI is just
a client for this API — anything the UI can do, you can do with `curl`.

| Endpoint                 | Method | Description                     |
|--------------------------|--------|---------------------------------|
| `/scan/start`            | POST   | Start new scan session (page 1) |
| `/scan/append`           | POST   | Scan and append next page       |
| `/scan/finish`           | POST   | Merge pages into PDF and export |
| `/scan/cancel`           | POST   | Discard session                 |
| `/scan/status`           | GET    | Current session state           |
| `/scan/page/{n}`         | GET    | Page thumbnail                  |
| `/scan/page/{n}/full`    | GET    | Full-size page image            |
| `/scanners`              | GET    | List discovered scanners        |
| `/scanners/select`       | POST   | Select a scanner                |
| `/scanners/refresh`      | POST   | Re-discover scanners            |
| `/scan/history`          | GET    | List completed scans            |
| `/scan/history/{id}/pdf` | GET    | Download a completed PDF        |
| `/api-docs`              | GET    | OpenAPI docs (Swagger UI)       |
| `/`                      | GET    | Web UI                          |

### Example: scan a 2-page document with curl

```bash
curl -X POST http://localhost:8080/scan/start    # scan page 1
curl -X POST http://localhost:8080/scan/append   # scan page 2
curl -X POST http://localhost:8080/scan/finish   # export PDF
```

## Quick start (Docker)

```bash
docker build -t simple-scan-web .

docker run -d \
  -p 8080:8080 \
  -e CONSUME_DIR=/consume \
  -v /path/to/output:/consume \
  --name simple-scan-web \
  simple-scan-web
```

Open `http://<host>:8080` on your phone or browser.

Scanners are discovered automatically via `scanimage -L`.

## Deploy on k3s

1. Build and push your image:
   ```bash
   docker build -t your-registry/simple-scan-web:latest .
   docker push your-registry/simple-scan-web:latest
   ```

2. Edit `deploy/k8s.yaml`:
   - Set `image:` to your image path
   - Configure the `consume` volume to point to your output directory
     (NFS, hostPath, or PVC)
   - Uncomment the Ingress block if you have Traefik/Nginx ingress

3. Apply:
   ```bash
   kubectl apply -f deploy/k8s.yaml
   ```

## Configuration (env vars)

| Variable      | Default                | Description                           |
|---------------|------------------------|---------------------------------------|
| `CONSUME_DIR` | `/consume`             | Output directory for exported PDFs    |
| `RESOLUTION`  | `300`                  | Scan resolution in DPI                |
| `SCAN_MODE`   | `Color`                | `Color`, `Gray`, or `Lineart`         |
| `TEMP_DIR`    | `/tmp/simple-scan-web` | Temp storage for in-progress sessions |

## Notes

- **Single replica only** — session state is in-memory. The k8s manifest uses
  `Recreate` strategy and `replicas: 1`.
- Scanners are auto-discovered on startup and polled every 5 minutes.
  Manual refresh is available in the UI.
- Session state and scan history are persisted to JSON files in `TEMP_DIR`,
  surviving application restarts.
- History entries are automatically cleaned up when their PDF is removed
  from the output directory.
