# Cairn YAML Replay Demo

This branch is only for YAML replay demos.

It does not require the dispatcher, Docker, workers, provider API keys, or `dispatch.dev.yaml`.
The SQLite database is only initialized so the Cairn server and UI can boot.

Start:

```bash
uv run --project cairn cairn db migrate --db-path /tmp/cairn-replay-demo.db && \
uv run --project cairn cairn serve --db-path /tmp/cairn-replay-demo.db --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>, then click **Replay YAML** and upload a `.yaml` or `.yml` file.
