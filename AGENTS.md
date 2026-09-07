# AGENT.md - 3-Tier Vertical Cloud Gaming Latency Simulator

This repository contains an undergraduate research thesis project modeling dynamic task migration across a 3-tier vertical cloud architecture: **End Node (Z) -> Middle Edge Gateway (Y) -> Cloud Server (X)** under simulated network latency degradation.

---

## 1. Quick Commands
Run these commands from the repository root:

- **Build Services:** `docker compose build`
- **Start Simulator:** `docker compose up -d`
- **Stop Simulator:** `docker compose down -v`
- **Run Unit Tests:** `pytest shared/ end_z/ middle_y/ cloud_x/`
- **Run Chaos Test:** `bash scripts/inject_latency.sh`

---

## 2. Directory Layout & Scope

```text
/
├── AGENT.md                       # Root AI instructions (this file)
├── docker-compose.yml             # Topology definition (172.20.0.0/16 subnet)
├── shared/                        # Common models & utilities across nodes
│   ├── config.py                  # Port/IP definitions & threshold constants
│   └── schemas.py                 # Pydantic models for REST payload schemas
├── cloud_x/                       # Heavy game-state execution engine (FastAPI)
├── middle_y/                      # Decision proxy and local fallback (FastAPI)
├── end_z/                         # Input sampler and latency monitor (FastAPI/Client)
├── skill/                         # Stored research insights & agent capabilities
└── scripts/                       # Network chaos & measurement utilities