# 3-Tier Vertical Cloud Gaming Latency Simulator

An undergraduate research thesis project modeling **dynamic task migration** across a
3-tier vertical cloud architecture — **End Node (Z) → Middle Edge Gateway (Y) → Cloud Server (X)** —
under simulated network latency degradation.

The simulator demonstrates how game-state execution "migrates" up the tiers (toward the cloud)
when latency is healthy, and falls back down (toward the edge / end node) when the network
degrades — all observable in real time through an interactive dashboard.

---

## Architecture

```
End Node (Z)  ──►  Middle Edge Gateway (Y)  ──►  Cloud Server (X)
   input sampler      decision proxy /           heavy game-state
   + latency monitor   local fallback             execution engine
```

| Tier | Container | IP | Port | Role |
|------|-----------|----|------|------|
| **Cloud X** | `cloud_x` | `172.20.0.10` | `8000` | Heavy game-state execution engine (FastAPI) |
| **Middle Y** | `middle_y` | `172.20.0.20` | `8001` | Decision proxy + local fallback (FastAPI) |
| **End Z** | `end_z` | `172.20.0.30` | `8002` | Input sampler + latency monitor (FastAPI) |

### Middle Y — two-layer design

Middle Y separates **control** from **data path** so the game never pays probe overhead:

- **Layer 2 (Background Probe):** an `asyncio.Task` probes Cloud X's `/health` every 1s and
  updates cached state (`_cloud_rtt_ms`, `_forward_enabled`).
- **Layer 1 (Request Handler):** reads the cached `_forward_enabled` flag on each request and
  either forwards to Cloud X or executes locally — no extra HTTP call.

---

## Directory Structure

```
final_year_project/
├── AGENTS.md                    # AI/agent instructions
├── docker-compose.yml           # 3-service topology on 172.20.0.0/16
├── requirements.txt             # shared Python dependencies
├── README.md                    # this file
├── shared/                      # common code across all tiers
│   ├── __init__.py
│   ├── config.py                # IPs, ports, latency thresholds
│   └── schemas.py               # Pydantic v2 request/response models
├── cloud_x/                     # Cloud Server X
│   ├── main.py                  # FastAPI app (POST /execute)
│   └── Dockerfile
├── middle_y/                    # Middle Edge Gateway Y
│   ├── main.py                  # FastAPI app (proxy + probe + /state)
│   └── Dockerfile
├── end_z/                       # End Node Z
│   ├── main.py                  # FastAPI app (sampler + /metrics)
│   └── Dockerfile
├── gui/                         # Streamlit dashboard
│   └── main.py                  # interactive monitoring & control
└── scripts/
    ├── inject_latency.sh        # tc netem chaos injection/reset
    └── run_benchmark.py         # benchmark harness (writes CSV)
```

---

## Prerequisites

- **Docker** and **Docker Compose** (for the simulator services)
- **Python 3.11+** (for the GUI and benchmark scripts)
- Linux with `tc` / `iproute2` support (installed automatically in the containers)

---

## Quick Start — Simulator

From the repository root:

```bash
# Build all three service images
docker compose build

# Start the full simulator stack
docker compose up -d

# Verify health
curl http://localhost:8000/health   # Cloud X
curl http://localhost:8001/health   # Middle Y
curl http://localhost:8002/health   # End Z

# Stop and tear down
docker compose down -v
```

---

## Running the GUI

The Streamlit dashboard runs on the **host** (not in a container) so it can reach
the Docker CLI and the service ports.

### 1. Set up a virtual environment (first time only)

```bash
cd final_year_project
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> **Note:** if your host Python lacks `pip`/`ensurepip` (common on minimal installs),
> bootstrap pip inside the venv first:
> ```bash
> python3 -m venv --without-pip .venv
> source .venv/bin/activate
> curl -sS https://bootstrap.pypa.io/get-pip.py | python
> pip install -r requirements.txt
> ```

### 2. Ensure the simulator is running

```bash
docker compose up -d
```

### 3. Launch the dashboard

```bash
source .venv/bin/activate
streamlit run gui/main.py --server.port 8501
```

Open **http://localhost:8501** in your browser.

### What the dashboard shows

- **Tier health cards** — live status, RTT, and (for End Z) its target node.
- **Latency over time** — rolling chart coloured by latency band.
- **Middle Y state** — `forward_enabled`, `cloud_rtt_ms`, `threshold_ms`.
- **Latency control** — set link delays (Z↔Y and Y↔X) with sliders.
- **Threshold override** — change the offload RTT threshold at runtime.
- **Start / Stop** — bring the simulator up or down.


---

## Latency Injection (Chaos Testing)

### Via the GUI

Use the **Latency Control** section — set the **Z ↔ Y delay** and **Y ↔ X delay**
sliders and click *Apply Link Delays*. Each link delay is split evenly between the
two endpoints of that link.

### Via the command line

```bash
bash scripts/inject_latency.sh inject 100   # add 100ms to all three containers
bash scripts/inject_latency.sh reset        # clear all tc netem rules
```

> Containers must have `cap_add: [NET_ADMIN]` (already set in `docker-compose.yml`).

---

## Benchmarking

```bash
source .venv/bin/activate
python3 scripts/run_benchmark.py
```

This runs a sequence of latency phases (baseline → degraded → recovery), measures the
round-trip time and `executed_by` for each sample, and writes `benchmark_results.csv`.

---

## API Reference

| Tier | Method | Endpoint | Description |
|------|--------|----------|-------------|
| Cloud X | `GET` | `/health` | Liveness probe |
| Cloud X | `POST` | `/execute` | Execute a `GameStateRequest`, returns `GameStateResponse` |
| Middle Y | `GET` | `/health` | Liveness probe |
| Middle Y | `POST` | `/execute` | Proxy entry-point — forwards or executes locally |
| Middle Y | `GET` | `/state` | Live probe state (`forward_enabled`, `cloud_rtt_ms`, `threshold_ms`) |
| Middle Y | `GET` | `/threshold` | Current offload threshold |
| Middle Y | `PUT` | `/threshold` | Update offload threshold at runtime |
| End Z | `GET` | `/health` | Liveness probe + sampler status |
| End Z | `GET` | `/metrics` | Last N latency readings |

---

## Latency Thresholds

Defined in `shared/config.py`:

| Constant | Default | Meaning |
|----------|---------|---------|
| `LATENCY_IDEAL_MS` | 20 ms | Optimal cloud-gaming RTT |
| `LATENCY_GOOD_MS` | 50 ms | Acceptable, no action |
| `LATENCY_DEGRADE_MS` | 100 ms | Degradation onset |
| `LATENCY_MIGRATE_MS` | 200 ms | Hard threshold to migrate |
| `LATENCY_LOCAL_FALLBACK_MS` | 300 ms | Cloud unusable → execute locally |
| `MIDDLE_Y_OFFLOAD_RTT_MS` | 40 ms | Middle Y forwards to Cloud X if RTT ≤ this |

---

## License

Academic research project — provided as-is for educational use.

