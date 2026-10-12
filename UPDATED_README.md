# NE HOST — Updated build

This build keeps the control panel and backend in Python/Flask. It includes per-account one-server enforcement, per-server ownership checks, path traversal protection, safe ZIP extraction, file upload, browser editing, search, custom domain storage, VEX console branding, and optional Node.js project runtime support.

## Run

Install the Python dependencies used by the project, then run:

```bash
PORT=13506 ADMIN_USERNAME=neverexits ADMIN_PASSWORD='ضع_كلمة_مرور_قوية_هنا' python3 main.py
```

The application reads `PORT` from the hosting platform. If it is not provided, the default is `13506`. Set `ADMIN_EMAIL` optionally. Do not put passwords or tokens directly in source files.

## Runtimes

The panel itself remains Python. A newly created Python server uses `main.py` and `python3`; a Node.js project server uses `index.js` and `node` when the Node.js option is selected.

## Custom domains

Enter a domain you own during server creation. Point its DNS record to the server's public address. The application stores and displays the domain; DNS and reverse-proxy configuration must be completed on the hosting machine.

## Docker isolation

Docker is not installed or available in the current sandbox, so this package does not pretend to provide Docker isolation. The application includes account ownership isolation and path safety at the application layer. For production-grade container isolation, deploy on a host with Docker enabled and add per-server containers, non-root users, CPU/memory/PID limits, a read-only base image, an isolated network policy, and an explicit allowlist for outbound traffic before exposing untrusted projects.
