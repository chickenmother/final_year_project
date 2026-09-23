"""GUI Dashboard — 3-Tier Vertical Cloud Gaming Latency Simulator.

Streamlit-based interactive dashboard for monitoring and controlling the
simulator in real time.  Connects to the three FastAPI services to:

- Display live health status of Cloud X, Middle Y, and End Z
- Plot the rolling RTT timeline with latency-band overlays
- Inject / reset artificial network latency via tc netem
- Override the offload threshold on Middle Y at runtime
- Start / stop the full simulator stack

Usage
-----
    source .venv/bin/activate
    streamlit run gui/main.py --server.port 8501
"""

from __future__ import annotations

import subprocess
import re
import time
from collections import deque
from typing import Any

import httpx
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# -- detect host timezone ---------------------------------------------------
import datetime as _dt
_HOST_UTC_OFFSET = _dt.datetime.now().astimezone().utcoffset() or _dt.timedelta()
_HOST_TZ_NAME = getattr(time, "tzname", ("UTC",))[0] or "UTC"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CLOUD_X_HEALTH = "http://localhost:8000/health"
MIDDLE_Y_HEALTH = "http://localhost:8001/health"
MIDDLE_Y_THRESHOLD = "http://localhost:8001/threshold"
MIDDLE_Y_STATE = "http://localhost:8001/state"
END_Z_HEALTH = "http://localhost:8002/health"
END_Z_METRICS = "http://localhost:8002/metrics"

MAX_HISTORY_POINTS = 120
POLL_INTERVAL_S = 1.0

# Band colours + thresholds (match shared/config.py).
BAND_COLOURS: dict[str, str] = {
    "ideal": "#00CC96",
    "good": "#636EFA",
    "degraded": "#FFA15A",
    "migrate": "#EF553B",
    "local_fallback": "#B82E2E",
}
BAND_LIMITS: dict[str, int] = {
    "ideal": 20,
    "good": 50,
    "degraded": 100,
    "migrate": 200,
    "local_fallback": 9999,
}

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="3-Tier Latency Simulator",
    page_icon="🛜",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Session state (persists across reruns)
# ---------------------------------------------------------------------------
if "rtt_history" not in st.session_state:
    st.session_state["rtt_history"] = deque(maxlen=MAX_HISTORY_POINTS)
if "threshold" not in st.session_state:
    st.session_state["threshold"] = 40.0
if "_last_observed_ms" not in st.session_state:
    st.session_state["_last_observed_ms"] = 0


# ---------------------------------------------------------------------------
# Helpers — data fetching
# ---------------------------------------------------------------------------
def _fetch_json(url: str) -> dict[str, Any] | None:
    """GET *url*, return parsed JSON or ``None`` on failure."""
    try:
        resp = httpx.get(url, timeout=2.0)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _fetch_health(url: str) -> dict[str, Any]:
    """GET *url* health endpoint, return {ok, data, error, rtt_ms}."""
    t0 = time.perf_counter()
    try:
        resp = httpx.get(url, timeout=2.0)
        t1 = time.perf_counter()
        resp.raise_for_status()
        return {
            "ok": True,
            "data": resp.json(),
            "error": "",
            "rtt_ms": round((t1 - t0) * 1000, 1),
        }
    except httpx.ConnectError:
        return {"ok": False, "data": None, "error": "Connection refused", "rtt_ms": -1.0}
    except httpx.TimeoutException:
        return {"ok": False, "data": None, "error": "Timeout", "rtt_ms": -1.0}
    except Exception as exc:
        return {"ok": False, "data": None, "error": type(exc).__name__, "rtt_ms": -1.0}


@st.cache_data(ttl=1)
def _sample_compose_state() -> dict[str, bool]:
    """Check which simulator containers are running."""
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        names = set(out.stdout.strip().splitlines())
        return {
            "cloud_x": "cloud_x" in names,
            "middle_y": "middle_y" in names,
            "end_z": "end_z" in names,
        }
    except Exception:
        return {"cloud_x": False, "middle_y": False, "end_z": False}


def _fetch_threshold() -> float:
    """Return the current offload threshold from Middle Y."""
    data = _fetch_json(MIDDLE_Y_THRESHOLD)
    if data:
        return float(data.get("threshold_ms", 40.0))
    return 40.0


def _get_forward_state() -> dict:
    """Return Middle Y's forwarding decision state, with safe defaults."""
    state = _fetch_json(MIDDLE_Y_STATE)
    if not state:
        return {"forward_enabled": False, "cloud_rtt_ms": 0.0, "threshold_ms": 40.0}
    return {
        "forward_enabled": bool(state.get("forward_enabled", False)),
        "cloud_rtt_ms": float(state.get("cloud_rtt_ms", 0.0) or 0.0),
        "threshold_ms": float(state.get("threshold_ms", 40.0) or 40.0),
    }


# ---------------------------------------------------------------------------
# Helpers — control actions
# ---------------------------------------------------------------------------
def _fmt_ms(value: float) -> str:
    """Format a ms delay for tc (integer-looking values have no decimals)."""
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.1f}"


def _apply_netem(container: str, delay_ms: float) -> tuple[bool, str]:
    """Set tc netem delay on *container* (reset if *delay_ms* <= 0).

    Supports fractional milliseconds (e.g. ``2.5ms``) so odd link delays can
    be split evenly across endpoints.  Returns ``(success, error_message)``.
    """
    # Always clear existing rules first (ignore errors if none exist).
    subprocess.run(
        f"docker exec {container} tc qdisc del dev eth0 root".split(),
        capture_output=True,
    )
    if delay_ms <= 0:
        return True, ""

    cmd = (
        f"docker exec {container} tc qdisc add dev eth0 "
        f"root netem delay {_fmt_ms(delay_ms)}ms"
    )
    result = subprocess.run(
        cmd.split(), capture_output=True, text=True, timeout=10
    )
    return result.returncode == 0, result.stderr.strip()


def compose_up() -> str:
    """Run ``docker compose up -d`` and return stdout/stderr."""
    result = subprocess.run(
        ["docker", "compose", "up", "-d"],
        capture_output=True, text=True, timeout=60,
    )
    return result.stdout.strip() or result.stderr.strip() or "(no output)"


def compose_down() -> str:
    """Run ``docker compose down --timeout 10`` and return stdout/stderr."""
    result = subprocess.run(
        ["docker", "compose", "down", "--timeout", "10"],
        capture_output=True, text=True, timeout=60,
    )
    return result.stdout.strip() or result.stderr.strip() or "(no output)"


def _update_threshold(new_ms: float) -> bool:
    """PUT the new offload threshold to Middle Y."""
    try:
        resp = httpx.put(
            MIDDLE_Y_THRESHOLD,
            json={"threshold_ms": new_ms},
            timeout=3.0,
        )
        return resp.status_code == 200
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title("🛜  3-Tier Vertical Cloud Gaming Latency Simulator")
st.caption(
    "Dynamic task migration: "
    "**End Node (Z) → Middle Edge Gateway (Y) → Cloud Server (X)** "
    "under simulated latency degradation."
)

# ---------------------------------------------------------------------------
# Toolbar — start / stop / netem reset
# ---------------------------------------------------------------------------
compose_state = _sample_compose_state()
all_up = all(compose_state.values())
up_count = sum(compose_state.values())

st.markdown("### Control")
c1, c2, c3, c4, c5 = st.columns([1, 1, 1, 2, 0.5])

with c1:
    if st.button("▶ Start", use_container_width=True, disabled=all_up):
        with st.spinner("Starting simulator..."):
            st.toast(compose_up())
        st.rerun()

with c2:
    stopped = not compose_state["cloud_x"]
    if st.button("■ Stop", use_container_width=True, disabled=stopped):
        with st.spinner("Stopping simulator..."):
            st.toast(compose_down())
        st.rerun()

with c3:
    if st.button("↻ Reset Netem", use_container_width=True):
        ok = 0
        for c in ("middle_y", "cloud_x", "end_z"):
            success, err = _apply_netem(c, 0)
            if success:
                ok += 1
            elif err:
                st.warning(f"{c}: {err}")
        st.toast(f"Netem rules cleared on {ok}/3 containers")
        st.rerun()

with c4:
    colour = (
        "#00CC96" if up_count == 3
        else "#FFA15A" if up_count > 0
        else "#EF553B"
    )
    st.markdown(
        f"● **Simulator:** "
        f"<span style='color:{colour};font-weight:bold'>"
        f"{up_count}/3 containers up</span>",
        unsafe_allow_html=True,
    )

with c5:
    if st.button("🔄", help="Refresh now", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# ---------------------------------------------------------------------------
# Tier health cards — with a highlighted "current execution target"
# ---------------------------------------------------------------------------
st.markdown("---")
st.markdown("### Tier Health")

fwd = _get_forward_state()
exec_target = "Cloud X  ☁" if fwd["forward_enabled"] else "Middle Y  ⚡"

# Flow banner — asterisk marks the node that currently executes tasks.
if fwd["forward_enabled"]:
    banner = (
        "End Z ──➜ Middle Y ──➜ **★ Cloud X**  "
        f"<span style='color:#2E7D32;font-weight:bold;font-size:0.9em'>EXECUTING</span>"
        f"&nbsp; <span style='color:#666'>· probe {fwd['cloud_rtt_ms']:.1f}ms ≤ "
        f"threshold {fwd['threshold_ms']:.0f}ms</span>"
    )
else:
    banner = (
        "End Z ──➜ **★ Middle Y**  "
        f"<span style='color:#B8860B;font-weight:bold;font-size:0.9em'>LOCAL FALLBACK</span>"
        f"&nbsp; <span style='color:#666'>· probe {fwd['cloud_rtt_ms']:.1f}ms > "
        f"threshold {fwd['threshold_ms']:.0f}ms</span>"
    )
st.markdown(banner, unsafe_allow_html=True)

health = {
    "Cloud X  ☁": _fetch_health(CLOUD_X_HEALTH),
    "Middle Y  ⚡": _fetch_health(MIDDLE_Y_HEALTH),
    "End Z  🖥": _fetch_health(END_Z_HEALTH),
}

cols = st.columns(3)
for i, (label, h) in enumerate(health.items()):
    with cols[i]:
        if h["ok"]:
            data = h["data"]
            rtt = h["rtt_ms"]
            rtt_str = f"  ⏱ {rtt:.1f} ms" if rtt >= 0 else ""
            is_executor = label == exec_target
            # Active execution node gets a glowing ring + badge.
            if is_executor:
                st.markdown(
                    "<div style='border:3px solid #2E7D32;border-radius:8px;"
                    "box-shadow:0 0 12px rgba(46,125,50,0.45);padding:10px;'>"
                    f"<b>{label}</b>&nbsp; <span style='color:#2E7D32;font-weight:bold;'>"
                    "★ EXECUTING</span></div>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    "<div style='border:1px solid #CCCCCC;border-radius:8px;padding:10px;'>"
                    f"<b>{label}</b>&nbsp; <span style='color:#666;'>Healthy</span></div>",
                    unsafe_allow_html=True,
                )
            st.success("Up", icon="✅")
            caption = f"Tier: {data.get('tier', '?')}{rtt_str}"
            if "End Z" in label:
                smp = data.get("samples_collected", 0)
                rng = data.get("running", False)
                dot = "●" if rng else "◌"
                # Dynamic target: depends on Middle Y's forwarding decision.
                if fwd["forward_enabled"]:
                    target_txt = (
                        f"Cloud X  (via Middle Y · probe {fwd['cloud_rtt_ms']:.1f}ms "
                        f"≤ {fwd['threshold_ms']:.0f}ms)"
                    )
                else:
                    target_txt = (
                        f"Middle Y  (local fallback · probe {fwd['cloud_rtt_ms']:.1f}ms "
                        f"> {fwd['threshold_ms']:.0f}ms)"
                    )
                caption += f"  {dot} {smp} samples\n\nTarget: {target_txt}"
            st.caption(caption)
        else:
            st.error(f"**{label}** — Down", icon="❌")
            st.caption(f"*{h['error']}*")
# ---------------------------------------------------------------------------
# RTT timeline chart (plotly)
# ---------------------------------------------------------------------------
st.markdown("---")
st.markdown("### 📈  Latency Over Time — RTT (Z → result → Z)")
st.caption("One line = End Z's measured round-trip: time from Z sending a request until the result returns to Z (a.k.a. the RTT).")

# Feed the history from End Z metrics (deduped — only new samples).
metrics = _fetch_json(END_Z_METRICS)
if metrics:
    last_seen = st.session_state["_last_observed_ms"]
    samples = metrics.get("samples", [])
    new_samples = 0
    for s in samples:
        rtt = s.get("rtt_ms", 0)
        obs_ms = s.get("observed_at_ms", 0)
        if rtt >= 0 and obs_ms > last_seen:
            ts = (
                pd.to_datetime(obs_ms, unit="ms", utc=True)
                + _HOST_UTC_OFFSET
            )
            st.session_state["rtt_history"].append(
                {"timestamp": ts, "rtt_ms": rtt, "band": s.get("band", "good")}
            )
            new_samples += 1
    if new_samples > 0 and samples:
        st.session_state["_last_observed_ms"] = samples[-1].get(
            "observed_at_ms", last_seen
        )

history = list(st.session_state["rtt_history"])
if history:
    df = pd.DataFrame(history)
    fig = go.Figure()

    # Single contiguous RTT series — one line, one legend entry.
    # y = End Z's measured round-trip time: request sent → result returned to Z.
    fig.add_trace(
        go.Scatter(
            x=df["timestamp"],
            y=df["rtt_ms"],
            mode="lines+markers",
            name="RTT (Z → result → Z)",
            line=dict(color="#1f77b4", width=2),
            marker=dict(size=4, color="#1f77b4"),
            hovertemplate="RTT %{y:.1f} ms<extra></extra>",
        )
    )

    # Live offload threshold (the decision boundary for Y → X forwarding),
    # shown as a single dashed red reference line.
    offload_threshold = _fetch_threshold()
    if offload_threshold and offload_threshold > 0:
        fig.add_hline(
            y=offload_threshold,
            line_dash="dash",
            line_color="#D62728",
            line_width=2,
            annotation_text=f"offload threshold {offload_threshold:.0f} ms",
            annotation_position="top right",
        )

    fig.update_layout(
        height=350,
        margin=dict(l=10, r=10, t=10, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left"),
        xaxis_title="time",
        yaxis_title="RTT (ms)",
        hovermode="x unified",
        template="plotly_white",
    )
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("Waiting for metrics...")
def _get_current_delay(container: str) -> float:
    """Return the current netem delay in ms on *container*, or 0 if not set."""
    try:
        r = subprocess.run(
            ["docker", "exec", container, "tc", "qdisc", "show", "dev", "eth0"],
            capture_output=True, text=True, timeout=5,
        )
        m = re.search(r"delay\s+(\d+\.?\d*)\s*ms", r.stdout)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return 0.0


# ---------------------------------------------------------------------------
# Latency control — link-based (Z↔Y, Y↔X)
# ---------------------------------------------------------------------------
st.markdown("---")
st.markdown("### ⚙ Latency Control (Link-Based)")

_end_delay = _get_current_delay("end_z")
_mid_delay = _get_current_delay("middle_y")
_cloud_delay = _get_current_delay("cloud_x")

# Reverse-engineer link delays from per-container netem.
# Each endpoint carries exactly HALF of one link:
#   end_z   = zy/2   -> zy = end_z * 2
#   cloud_x = yx/2   -> yx = cloud_x * 2
# (middle_y carries half of BOTH links, so we must NOT read it back directly
#  or it would leak one link's delay into the other's slider.)
_zy_current = int(round(_end_delay * 2))
_yx_current = int(round(_cloud_delay * 2))

z_y_delay = st.slider(
    "Z ↔ Y delay (ms)", 0, 500, int(_zy_current), step=1,
    help="Delay between End Node Z and Middle Edge Gateway Y",
)
y_x_delay = st.slider(
    "Y ↔ X delay (ms)", 0, 500, int(_yx_current), step=1,
    help="Delay between Middle Edge Gateway Y and Cloud Server X",
)

st.caption(
    "Each link delay is split evenly between both endpoints. "
    f"Result: end_z={z_y_delay / 2:g}ms, middle_y={(z_y_delay + y_x_delay) / 2:g}ms, "
    f"cloud_x={y_x_delay / 2:g}ms"
)

if st.button("Apply Link Delays", use_container_width=True):
    _end_val = z_y_delay / 2
    _mid_val = (z_y_delay + y_x_delay) / 2
    _cloud_val = y_x_delay / 2

    ok = 0
    targets = 0
    for name, c, val in [
        ("end_z", "end_z", _end_val),
        ("middle_y", "middle_y", _mid_val),
        ("cloud_x", "cloud_x", _cloud_val),
    ]:
        targets += 1
        success, err = _apply_netem(c, val)
        if success:
            ok += 1
        elif err:
            st.warning(f"**{name}** ({val}ms): {err}")

    if z_y_delay == 0 and y_x_delay == 0:
        st.success(f"Netem cleared on {ok}/{targets} containers")
    else:
        st.success(f"Link delays applied on {ok}/{targets} containers")
    st.rerun()

with st.expander("Current qdisc state"):
    for c in ("end_z", "middle_y", "cloud_x"):
        try:
            r = subprocess.run(
                ["docker", "exec", c, "tc", "qdisc", "show", "dev", "eth0"],
                capture_output=True, text=True, timeout=5,
            )
            st.code(f"{c}: {r.stdout.strip() or '(no rules)'}")
        except Exception:
            st.caption(f"{c}: not reachable")

# ---------------------------------------------------------------------------
# Middle Y state (live probe data)
# ---------------------------------------------------------------------------
st.markdown("---")
st.markdown("### 📡 Middle Y State")

state = _fetch_json(MIDDLE_Y_STATE)
if state:
    s1, s2, s3, s4 = st.columns(4)
    with s1:
        fwd = state.get("forward_enabled", False)
        st.metric("Forward Enabled", "✅ Yes" if fwd else "❌ No")
    with s2:
        st.metric("Decision RTT (measured)",
                  f"{state.get('measured_rtt_ms', 0):.1f} ms")
    with s3:
        st.metric("Y→X probe (health)",
                  f"{state.get('cloud_rtt_ms', 0):.1f} ms")
    with s4:
        st.metric("Offload Threshold", f"{state.get('threshold_ms', 0):.0f} ms")
    st.caption(
        "Decision RTT = End Z's measured round-trip (same RTT as the graph); "
        "it drives Y→X forwarding. Y→X probe is the health-check latency."
    )
else:
    st.caption("Middle Y not reachable")
# Threshold override
# ---------------------------------------------------------------------------
st.markdown("---")
st.markdown("### 🎛 Threshold Override")

ts1, ts2, ts3 = st.columns([2, 1, 3])

with ts1:
    current_threshold = _fetch_threshold()
    new_threshold = st.number_input(
        "Offload RTT threshold (ms)",
        min_value=1.0,
        max_value=2000.0,
        value=float(current_threshold),
        step=1.0,
    )

with ts2:
    st.markdown("&nbsp;")  # spacer
    if st.button("Update Threshold", use_container_width=True):
        if _update_threshold(new_threshold):
            st.session_state["threshold"] = new_threshold
            st.success(f"Threshold → {new_threshold:.0f} ms")
        else:
            st.error("Failed to reach Middle Y")

with ts3:
    st.markdown(
        f"**Current:** {current_threshold:.0f} ms  |  "
        f"**Default:** 40 ms\n\n"
        f"*RTT ≤ threshold → forward to Cloud X*\n\n"
        f"*RTT > threshold → local fallback at Middle Y*"
    )

st.caption(
    "Band breaks — "
    f"ideal ≤{BAND_LIMITS['ideal']} | "
    f"good ≤{BAND_LIMITS['good']} | "
    f"degraded ≤{BAND_LIMITS['degraded']} | "
    f"migrate ≤{BAND_LIMITS['migrate']} | "
    f"fallback >{BAND_LIMITS['migrate']}"
)

# ---------------------------------------------------------------------------
# Footer — auto-refresh
# ---------------------------------------------------------------------------
st.markdown("---")
st.caption(f"Last refresh: {_dt.datetime.now().strftime('%H:%M:%S')} {_HOST_TZ_NAME}  |  Auto-refresh: {int(POLL_INTERVAL_S)}s")
time.sleep(POLL_INTERVAL_S)
st.rerun()