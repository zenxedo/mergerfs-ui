import os
import subprocess
import uvicorn
import shlex
import re
import time
import signal
from collections import deque
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse

# Configuration (env-overridable so the image is not tied to a single host)
TOOLS_DIR = os.environ.get("MERGERFS_TOOLS_DIR", "/app/tools/src")
# Mount path of the mergerfs pool inside the container (e.g. /mnt/pool). When
# unset, a pool is detected by a filesystem type containing "mergerfs".
POOL_MOUNT = os.environ.get("MERGERFS_POOL_MOUNT", "").strip()
# Optional caption shown under the pool total (defaults to a drive count).
POOL_LABEL = os.environ.get("MERGERFS_POOL_LABEL", "").strip()
PORT = int(os.environ.get("PORT", "8480"))
TOOL_NAME_RE = re.compile(r"mergerfs\.(balance|consolidate|ctl|dedup|dup|fsck|mktrash)")
app = FastAPI()


def same_origin(request: Request) -> bool:
    """Reject cross-site posts (CSRF). Header-less clients (e.g. curl) are allowed."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    return origin.split("://", 1)[-1] == request.headers.get("host", "")

# Global memory state
DISK_STATS_HISTORY = {}
CPU_STATS_HISTORY = {"last_idle": 0, "last_total": 0, "last_time": 0}

# Advanced task state memory for persistence tracking
ACTIVE_PROCESSES = {} 
PROCESS_LOGS = {}     # Holds a rolling cache of the console output in RAM

TOOL_EXAMPLES = {
    "mergerfs.ctl": ["mergerfs.ctl -m /storage info", "mergerfs.ctl -m /storage add path /mnt/drive2"],
    "mergerfs.fsck": ["mergerfs.fsck -v -f manual /storage", "mergerfs.fsck --size /storage"],
    "mergerfs.dup": ["mergerfs.dup --count 3 --execute /storage"],
    "mergerfs.dedup": ["mergerfs.dedup -vv -d newest --execute /storage"],
    "mergerfs.balance": ["mergerfs.balance -p 5 /storage"],
    "mergerfs.consolidate": ["mergerfs.consolidate --execute /storage"]
}

TOOLS = {
    "mergerfs.ctl": {
        "description": "Control mergerfs instance. Syntax: mergerfs.ctl -m <mount> <action> <args>",
        "actions": ["info", "add", "remove", "list", "get", "set"],
        "options": []
    },
    "mergerfs.fsck": {
        "description": "Audit and fix inconsistencies in permissions and ownership.",
        "options": [
            {"name": "verbose", "short": "v", "type": "level", "help": "Print details of audit item"},
            {"name": "size", "short": "s", "type": "bool", "help": "Only consider if the size is the same"},
            {"name": "fix", "short": "f", "type": "choice", "choices": ["manual", "newest", "nonroot"], "help": "Fix policy: manual (ask), newest (mtime), nonroot (fix non-root ownership)"}
        ]
    },
    "mergerfs.dup": {
        "description": "Duplicate files & directories across multiple drives in a pool.",
        "options": [
            {"name": "count", "short": "c", "type": "value", "help": "Number of copies to create (default: 2)"},
            {"name": "dup", "short": "d", "type": "choice", "choices": ["newest", "oldest", "smallest", "largest", "mergerfs"], "help": "Strategy to choose which file to duplicate"},
            {"name": "prune", "short": "p", "type": "bool", "help": "Remove files above `count`."},
            {"name": "execute", "short": "e", "type": "bool", "help": "Execute `rsync` and `rm` commands. Not just print them."},
            {"name": "include", "short": "I", "type": "value", "help": "fnmatch filter to include files."},
            {"name": "exclude", "short": "E", "type": "value", "help": "fnmatch filter to exclude files."}
        ]
    },
    "mergerfs.dedup": {
        "description": "Remove duplicate files across branches of a mergerfs pool.",
        "options": [
            {"name": "verbose", "short": "v", "type": "level", "help": "Incremental verbosity: 1 (cmds), 2 (status), 3 (info)"},
            {"name": "dedup", "short": "d", "type": "choice", "choices": ["manual", "oldest", "newest", "largest", "smallest", "mostfreespace"], "help": "Strategy for determining which file to keep"},
            {"name": "ignore", "short": "i", "type": "choice", "choices": ["none", "same-size", "different-size", "same-time", "different-time", "same-hash", "different-hash"], "help": "Ignore files based on attributes"},
            {"name": "strict", "short": "s", "type": "bool", "help": "Skip if all files have the same value."},
            {"name": "execute", "short": "e", "type": "bool", "help": "Perform file removal."},
            {"name": "include", "short": "I", "type": "value", "help": "fnmatch filter to include files."},
            {"name": "exclude", "short": "E", "type": "value", "help": "fnmatch filter to exclude files."}
        ]
    },
    "mergerfs.balance": {
        "description": "Balance files based on percentage drive filled.",
        "options": [
            {"name": "percentage", "short": "p", "type": "value", "help": "Percentage range of freespace (default 2.0)"},
            {"name": "include", "short": "i", "type": "value", "help": "fnmatch compatible file filter"},
            {"name": "exclude", "short": "e", "type": "value", "help": "fnmatch compatible file filter"},
            {"name": "include-path", "short": "I", "type": "value", "help": "fnmatch compatible path filter"},
            {"name": "exclude-path", "short": "E", "type": "value", "help": "fnmatch compatible path filter"}
        ]
    },
    "mergerfs.consolidate": {
        "description": "Consolidate files in a single directory onto a single drive.",
        "options": [
            {"name": "max-files", "short": "m", "type": "value", "help": "Skip dirs with > N files (default: 256)"},
            {"name": "max-size", "short": "M", "type": "value", "help": "Skip dirs with files > N (default: 16G)"},
            {"name": "execute", "short": "e", "type": "bool", "help": "Execute `rsync` commands."},
            {"name": "include-path", "short": "I", "type": "value", "help": "fnmatch path include filter."},
            {"name": "exclude-path", "short": "E", "type": "value", "help": "fnmatch path exclude filter."}
        ]
    }
}

# ---------- SYSTEM METRICS ENGINES ----------
def get_hardware_maps():
    mapping = {}
    base_sys_dir = "/sys/block"
    if not os.path.exists(base_sys_dir): return mapping
    for dev_node in os.listdir(base_sys_dir):
        if not dev_node.startswith("sd"): continue
        device_dir = os.path.join(base_sys_dir, dev_node)
        model_path = os.path.join(device_dir, "device/model")
        serial_path = os.path.join(device_dir, "device/serial")
        vpd_path = os.path.join(device_dir, "device/vpd_pg80")
        model, serial = "Unknown", "Unknown"
        try:
            if os.path.exists(model_path):
                with open(model_path, "r") as f: model = f.read().strip()
            if os.path.exists(serial_path):
                with open(serial_path, "r") as f: serial = f.read().strip()
            if (not serial or serial == "Unknown") and os.path.exists(vpd_path):
                with open(vpd_path, "rb") as f:
                    raw_vpd = f.read()
                    if len(raw_vpd) > 4:
                        parsed = raw_vpd[4:].decode('utf-8', errors='ignore').strip()
                        if parsed: serial = parsed
            mapping[dev_node] = {"model": model, "serial": serial}
        except Exception: continue
    return mapping

def get_io_speeds():
    global DISK_STATS_HISTORY
    current_time = time.time()
    speeds = {}
    if not os.path.exists("/proc/diskstats"): return speeds
    try:
        with open("/proc/diskstats", "r") as f: lines = f.readlines()
        for line in lines:
            parts = line.split()
            if len(parts) < 14: continue
            dev_node = parts[2]
            if not dev_node.startswith("sd"): continue
            sectors_read, sectors_written = int(parts[5]), int(parts[9])
            if dev_node in DISK_STATS_HISTORY and DISK_STATS_HISTORY[dev_node] is not None:
                prev_read, prev_write, prev_time = DISK_STATS_HISTORY[dev_node]
                time_delta = current_time - prev_time
                if time_delta > 0:
                    read_mb = ((sectors_read - prev_read) * 512) / (1024 * 1024) / time_delta
                    write_mb = ((sectors_written - prev_write) * 512) / (1024 * 1024) / time_delta
                    speeds[dev_node] = {"read": round(read_mb, 1), "write": round(write_mb, 1)}
                else: speeds[dev_node] = {"read": 0.0, "write": 0.0}
            else: speeds[dev_node] = {"read": 0.0, "write": 0.0}
            DISK_STATS_HISTORY[dev_node] = (sectors_read, sectors_written, current_time)
    except Exception: pass
    return speeds

def get_system_metrics():
    global CPU_STATS_HISTORY
    metrics = {"cpu": 0.0, "ram_used": "0.0", "ram_total": "0.0", "uptime": "Unknown", "updates": 0}
    try:
        with open("/proc/uptime", "r") as f:
            uptime_seconds = float(f.readline().split()[0])
            days = int(uptime_seconds // 86400)
            hours = int((uptime_seconds % 86400) // 3600)
            metrics["uptime"] = f"{days}d, {hours}h" if days > 0 else f"{hours}h"
    except Exception: pass
    try:
        with open("/proc/meminfo", "r") as f:
            mem = {l.split()[0].rstrip(':'): int(l.split()[1]) for l in f.readlines()[:4]}
        total_gb = round(mem["MemTotal"] / (1024 * 1024), 1)
        avail_gb = round(mem["MemAvailable"] / (1024 * 1024), 1) if "MemAvailable" in mem else round(mem["MemFree"] / (1024 * 1024), 1)
        metrics["ram_total"] = f"{total_gb} GB"
        metrics["ram_used"] = f"{round(total_gb - avail_gb, 1)} GB"
    except Exception: pass
    try:
        with open("/proc/stat", "r") as f:
            cpu_fields = [int(x) for x in f.readline().split()[1:5]]
        idle_ticks, total_ticks = cpu_fields[3], sum(cpu_fields)
        prev_idle, prev_total = CPU_STATS_HISTORY["last_idle"], CPU_STATS_HISTORY["last_total"]
        idle_delta, total_delta = idle_ticks - prev_idle, total_ticks - prev_total
        if total_delta > 0: metrics["cpu"] = round((1.0 - (idle_delta / total_delta)) * 100, 1)
        CPU_STATS_HISTORY["last_idle"], CPU_STATS_HISTORY["last_total"] = idle_ticks, total_ticks
    except Exception: pass
    try:
        if os.path.exists("/var/lib/update-notifier/updates-available"):
            with open("/var/lib/update-notifier/updates-available", "r") as f:
                match = re.search(r'(\d+)\s+packages\s+can\s+be\s+updated', f.read())
                if match: metrics["updates"] = int(match.group(1))
    except Exception: pass
    return metrics

def get_drive_status(mount_path):
    if os.path.exists(mount_path) and os.path.isdir(mount_path):
        try:
            os.listdir(mount_path)
            return {"status": "ONLINE", "color": "text-success"}
        except Exception: return {"status": "LOCKED", "color": "text-danger"}
    return {"status": "OFFLINE", "color": "text-muted"}

def get_storage_data():
    hw_map = get_hardware_maps()
    io_map = get_io_speeds()
    try:
        df_output = subprocess.run(["df", "-h"], capture_output=True, text=True, check=True).stdout
    except Exception: return [], {"size": "0", "used": "0", "avail": "0", "use_pct": "0%"}
    drives, pool_metrics, seen_mounts = [], {"size": "0", "used": "0", "avail": "0", "use_pct": "0%"}, set()
    for line in df_output.strip().split("\n")[1:]:
        parts = line.split()
        if len(parts) < 6: continue
        filesystem, size, used, avail, use_pct, mount = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
        if filesystem == "overlay" or mount.startswith("/var/lib/docker") or filesystem == "shm": continue
        if "mergerfs" in filesystem.lower() or (POOL_MOUNT and mount.startswith(POOL_MOUNT)):
            pool_metrics = {"size": size, "used": used, "avail": avail, "use_pct": use_pct}
            continue
        if "/dev/sd" in filesystem or "disk" in mount or mount == "/":
            if mount in seen_mounts: continue
            dev_node = re.sub(r'\d+$', '', os.path.basename(filesystem))
            hw_info = hw_map.get(dev_node, {"model": "Unknown", "serial": "Unknown"})
            status_data = get_drive_status(mount)
            io_data = io_map.get(dev_node, {"read": 0.0, "write": 0.0})
            seen_mounts.add(mount)
            drives.append({
                "mount": mount, "device": filesystem, "node": dev_node,
                "model": hw_info["model"], "serial": hw_info["serial"], "size": size,
                "used": used, "avail": avail, "use_pct": use_pct,
                "pct_val": int(use_pct.replace("%", "")) if use_pct != "-" else 0,
                "drive_status": status_data["status"], "status_color": status_data["color"],
                "read_speed": io_data["read"], "write_speed": io_data["write"]
            })
    return drives, pool_metrics

# ---------- PAGE TEMPLATE ----------
def page(title, body):
    return f"""
<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <title>{title}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body {{ background:#141419; padding-bottom: 60px; font-family: 'Segoe UI', sans-serif; color: #e2e8f0; }}
        .card {{ background: #22222b; border: none; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.3); color: #e2e8f0; }}
        .section-title {{ font-weight: 700; border-left: 5px solid #38bdf8; padding-left: 12px; margin: 25px 0 15px 0; color: #f1f5f9; font-size: 1.25rem; }}
        
        /* Unified Theme Colors (Overriding Light Blue Components) */
        .text-primary, .navbar-brand, .form-label fw-bold, label b {{ color: #38bdf8 !important; }}
        .btn-outline-primary {{ color: #38bdf8; border-color: #38bdf8; }}
        .btn-outline-primary:hover {{ background-color: #38bdf8; color: #141419; font-weight: bold; }}
        
        .form-control, .form-select {{ background-color: #1a1a24 !important; color: #ffffff !important; border: 1px solid #475569 !important; }}
        .form-control:focus, .form-select:focus {{ border-color: #38bdf8 !important; box-shadow: 0 0 0 0.25rem rgba(56, 189, 248, 0.25) !important; }}
        
        .preview-box {{ background: #111115; color: #00ff00; border: 2px solid #475569; padding: 15px; border-radius: 10px; font-family: monospace; font-size: 1.1rem; width: 100%; }}
        .btn-run {{ background: #10b981; color: white; font-weight: bold; padding: 12px; border-radius: 10px; border: none; font-size: 1.1rem; }}
        .btn-run:hover {{ background: #059669; }}
        .btn-stop {{ background: #ef4444; color: white; font-weight: bold; padding: 12px; border-radius: 10px; border: none; font-size: 1.1rem; display: none; }}
        .btn-stop:hover {{ background: #dc2626; }}
        
        .ref-box {{ background: #111115; color: #f8f9fa; padding: 15px; border-radius: 8px; font-size: 0.85rem; overflow-x: auto; border: 1px solid #3b4252; }}
        code {{ color: #f43f5e; font-weight: bold; background: #111115; padding: 2px 4px; border-radius: 4px; }}
        .text-muted {{ color: #94a3b8 !important; }}
        .text-info {{ color: #38bdf8 !important; }}
        .font-monospace {{ color: #cbd5e1 !important; }}
        .tool-desc {{ color: #cbd5e1 !important; font-size: 0.85rem; }}
        .sub-metrics {{ color: #cbd5e1 !important; font-size: 0.85rem; font-weight: 500; }}
        .progress {{ background-color: #475569; height: 18px; border-radius: 9px; }}
        .progress-bar {{ font-size: 0.75rem; font-weight: bold; line-height: 18px; }}
        .table {{ color: #e2e8f0; }}
        .table-dark-custom th {{ background: #17171e; color: #f1f5f9; border-bottom: 2px solid #3f3f46; font-weight: 700; }}
        .table-dark-custom td {{ background: #22222b; border-bottom: 1px solid #3f3f46; vertical-align: middle; color: #e2e8f0; }}
        .metric-label {{ font-size: 0.85rem; color: #94a3b8; font-weight: bold; text-transform: uppercase; letter-spacing: 0.05em; }}
        .metric-value {{ font-size: 2.25rem; font-weight: 800; color: #38bdf8; }}
        .badge-io {{ font-family: monospace; font-size: 0.8rem; padding: 3px 6px; border-radius: 4px; display: inline-block; width: 85px; text-align: center; }}
        .badge-read {{ background: #1e293b; color: #38bdf8; border: 1px solid #0369a1; }}
        .badge-write {{ background: #1e293b; color: #f43f5e; border: 1px solid #9f1239; }}
        .sys-row {{ display: flex; justify-content: space-between; margin-bottom: 3px; font-size: 0.9rem; border-bottom: 1px dashed #2d2d3d; padding-bottom: 3px; }}
        .sys-row:last-child {{ border: none; }}
        .sys-val {{ font-weight: bold; color: #f1f5f9; font-family: monospace; }}
        .live-terminal {{ background: #0a0a0f; color: #10b981; border: 2px solid #3b4252; border-radius: 10px; font-family: monospace; height: 350px; overflow-y: auto; padding: 15px; display: none; margin-top: 20px; font-size: 0.95rem; white-space: pre-wrap; }}
        .active-task-badge {{ font-size: 0.75rem; padding: 2px 6px; border-radius: 4px; background: #9f1239; color: #fecdd3; font-weight: bold; margin-left: 10px; animation: flash 2s infinite; }}
        @keyframes flash {{ 0% {{ opacity: 0.5; }} 50% {{ opacity: 1; }} 100% {{ opacity: 0.5; }} }}
    </style>
    <script>
        let isManual = false;
        function updatePreview() {{
            if (isManual) return;
            const form = document.getElementById("toolform");
            if (!form) return;
            const data = new FormData(form);
            const tool = form.dataset.tool;
            let cmd = [tool];
            const target = data.get("target_path") || "";
            const action = data.get("__action") || "";
            if (tool === "mergerfs.ctl") {{
                if (target) cmd.push("-m", target);
                if (action) cmd.push(action);
                const posArg = data.get("__pos_arg");
                if (posArg) cmd.push(posArg);
            }} else {{
                for (let [key, value] of data.entries()) {{
                    if (key === "target_path" || key === "__action" || key === "__pos_arg" || key === "__manual_cmd" || !value) continue;
                    if (key === "verbose") {{
                        const vCount = parseInt(value);
                        if (vCount > 0) cmd.push("-" + "v".repeat(vCount));
                    }} else if (value === "on") {{
                        cmd.push("--" + key);
                    }} else {{
                        cmd.push("--" + key, value);
                    }}
                }}
                if (target) cmd.push(target);
            }}
            document.getElementById("preview-text").value = cmd.join(" ");
        }}
        
        async function runMetricsLoop() {{
            if (window.location.pathname !== "/") return;
            try {{
                const res = await fetch("/api/metrics");
                if (res.ok) {{
                    const data = await res.json();
                    document.getElementById("pool-size").innerText = data.pool.size;
                    document.getElementById("pool-sub").innerText = data.pool.used + " Used / " + data.pool.avail + " Available";
                    document.getElementById("pool-avail-box").innerText = data.pool.avail;
                    document.getElementById("sys-cpu").innerText = data.system.cpu + " %";
                    document.getElementById("sys-ram").innerText = data.system.ram_used + " / " + data.system.ram_total;
                    document.getElementById("sys-uptime").innerText = data.system.uptime;
                    document.getElementById("sys-updates").innerText = data.system.updates + " Pending";
                    
                    Object.keys(data.active_tasks).forEach(tool => {{
                        const container = document.getElementById("status-badge-" + tool);
                        if (container && !container.querySelector(".active-task-badge")) {{
                            container.innerHTML += '<span class="active-task-badge">RUNNING IN BACKGROUND</span>';
                        }}
                    }});
                    
                    data.drives.forEach(d => {{
                        const rEl = document.getElementById("r-" + d.node);
                        const wEl = document.getElementById("w-" + d.node);
                        if (rEl && wEl) {{ rEl.innerText = "R: " + d.read_speed + " MB/s"; wEl.innerText = "W: " + d.write_speed + " MB/s"; }}
                    }});
                }}
            }} catch(e) {{ console.error(e); }}
            setTimeout(runMetricsLoop, 2000);
        }}

        async function handleFormRun(e) {{
            e.preventDefault();
            const form = e.target;
            const runBtn = document.getElementById("btn-submit-run");
            const stopBtn = document.getElementById("btn-submit-stop");
            const term = document.getElementById("terminal-console");
            const tool = form.dataset.tool;
            
            runBtn.style.display = "none";
            stopBtn.style.display = "block";
            term.style.display = "block";
            term.innerText = "Connecting to streaming log queue...\\n";

            const formData = new FormData(form);
            
            // Fire the stream pipeline asynchronously
            fetch(form.action, {{ method: 'POST', body: formData }}).then(async (response) => {{
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                while (true) {{
                    const {{ value, done }} = await reader.read();
                    if (done) break;
                    term.innerText += decoder.decode(value);
                    term.scrollTop = term.scrollHeight;
                }}
            }}).catch(err => {{}});

            // CRITICAL FIX: Instantly ignite state tracking validation check heartbeat loop
            setTimeout(() => checkActiveState(tool), 200);
        }}

        async function checkActiveState(tool) {{
            try {{
                const res = await fetch("/api/status/" + tool);
                const data = await res.json();
                const runBtn = document.getElementById("btn-submit-run");
                const stopBtn = document.getElementById("btn-submit-stop");
                const term = document.getElementById("terminal-console");
                
                if (data.running) {{
                    runBtn.style.display = "none";
                    stopBtn.style.display = "block";
                    term.style.display = "block";
                    term.innerText = data.logs;
                    term.scrollTop = term.scrollHeight;
                    setTimeout(() => checkActiveState(tool), 1000);
                }} else {{
                    runBtn.style.display = "block";
                    stopBtn.style.display = "none";
                    if (data.logs) {{
                        term.style.display = "block";
                        term.innerText = data.logs;
                    }}
                }}
            }} catch(e) {{}}
        }}

        async function stopActiveProcess() {{
            const form = document.getElementById("toolform");
            const tool = form.dataset.tool;
            await fetch("/api/stop/" + tool, {{ method: 'POST' }});
        }}

        document.addEventListener("DOMContentLoaded", () => {{
            const form = document.getElementById("toolform");
            if (form) {{
                form.querySelectorAll("input, select").forEach(el => {{
                    el.addEventListener("input", updatePreview);
                    el.addEventListener("change", updatePreview);
                }});
                updatePreview();
                form.addEventListener("submit", handleFormRun);
                checkActiveState(form.dataset.tool);
            }}
            runMetricsLoop();
        }});
    </script>
</head>
<body>
    <nav class="navbar navbar-dark bg-dark mb-4 shadow-sm"><div class="container"><a class="navbar-brand fw-bold" href="/">MERGERFS STORAGE MANAGER</a></div></nav>
    <div class="container">{body}</div>
</body>
</html>
"""

# ---------- BACKEND API CONTROL LOGIC ENDPOINTS ----------
@app.get("/api/metrics")
def api_metrics():
    drives, pool = get_storage_data()
    system = get_system_metrics()
    active_tasks = {k: True for k, p in ACTIVE_PROCESSES.items() if p.poll() is None}
    return JSONResponse(content={"drives": drives, "pool": pool, "system": system, "active_tasks": active_tasks})

@app.get("/api/status/{tool}")
def api_tool_status(tool: str):
    global ACTIVE_PROCESSES, PROCESS_LOGS
    process = ACTIVE_PROCESSES.get(tool)
    is_running = process is not None and process.poll() is None
    logs_list = list(PROCESS_LOGS.get(tool, []))
    return {"running": is_running, "logs": "".join(logs_list)}

@app.post("/api/stop/{tool}")
def api_stop_tool(tool: str, request: Request):
    global ACTIVE_PROCESSES
    if not same_origin(request):
        return JSONResponse(status_code=403, content={"detail": "cross-origin request rejected"})
    process = ACTIVE_PROCESSES.get(tool)
    if process and process.poll() is None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            return {"status": "terminated"}
        except Exception:
            process.terminate()
    return {"status": "not running"}

@app.get("/", response_class=HTMLResponse)
def home():
    drives, pool = get_storage_data()
    sys_init = get_system_metrics()
    pool_caption = POOL_LABEL or f"{len(drives)} drives detected"

    metrics_html = f"""
    <div class="row g-3 mb-4">
        <div class="col-md-4">
            <div class="card p-3 text-center d-flex flex-column justify-content-center" style="height: 140px;">
                <span class="metric-label">Total Pool Capacity</span>
                <span class="metric-value" id="pool-size">{pool['size']}</span>
                <span class="sub-metrics" id="pool-sub">{pool['used']} Used / {pool['avail']} Available</span>
                <small class="text-info mt-1 fw-bold" style="font-size:0.75rem;">{pool_caption}</small>
            </div>
        </div>
        <div class="col-md-4">
            <div class="card p-3 text-center d-flex flex-column justify-content-center" style="height: 140px;">
                <span class="metric-label">Pool Free Space</span>
                <span class="metric-value text-info" id="pool-avail-box">{pool['avail']}</span>
                <div class="progress mt-2 mx-3">
                    <div class="progress-bar bg-warning" style="width: {pool['use_pct']}">Pool Usage: {pool['use_pct']}</div>
                </div>
            </div>
        </div>
        <div class="col-md-4">
            <div class="card px-4 py-3 d-flex flex-column justify-content-center" style="height: 140px;">
                <div class="sys-row"><span class="metric-label mb-0">Host Uptime:</span><span class="sys-val" id="sys-uptime">{sys_init['uptime']}</span></div>
                <div class="sys-row"><span class="metric-label mb-0">CPU Load:</span><span class="sys-val" id="sys-cpu">{sys_init['cpu']} %</span></div>
                <div class="sys-row"><span class="metric-label mb-0">Memory:</span><span class="sys-val" id="sys-ram">{sys_init['ram_used']} / {sys_init['ram_total']}</span></div>
                <div class="sys-row"><span class="metric-label mb-0">OS Updates:</span><span class="sys-val text-warning" id="sys-updates">{sys_init['updates']} Pending</span></div>
            </div>
        </div>
    </div>
    """

    table_rows = ""
    for d in drives:
        progress_color = "bg-danger" if d['pct_val'] >= 90 else "bg-warning" if d['pct_val'] >= 75 else "bg-success"
        table_rows += f"""
        <tr>
            <td class="fw-bold text-info">{d['mount']}</td>
            <td><code>{d['device']}</code></td>
            <td>{d['model']}</td>
            <td><span class="font-monospace">{d['serial']}</span></td>
            <td class="fw-bold text-white">{d['size']}</td>
            <td class="fw-bold text-info">{d['avail']}</td>
            <td class="{d['status_color']} fw-bold"><small>{d['drive_status']}</small></td>
            <td>
                <div class="badge-io badge-read me-1" id="r-{d['node']}">R: {d['read_speed']} MB/s</div>
                <div class="badge-io badge-write" id="w-{d['node']}">W: {d['write_speed']} MB/s</div>
            </td>
            <td style="width: 15%;">
                <div class="progress">
                    <div class="progress-bar {progress_color}" style="width: {d['use_pct']}">{d['use_pct']}</div>
                </div>
            </td>
        </tr>
        """
    
    inventory_html = f"""
    <div class="card p-4 mb-4">
        <h5 class="fw-bold mb-3 text-white">Storage Array Inventory</h5>
        <div class="table-responsive">
            <table class="table table-dark-custom mb-0">
                <thead>
                    <tr>
                        <th>Mount Point</th>
                        <th>Device</th>
                        <th>Drive Model</th>
                        <th>Serial Number</th>
                        <th>Total Size</th>
                        <th>Available</th>
                        <th>Array Status</th>
                        <th>Disk Activity</th>
                        <th>Utilization</th>
                    </tr>
                </thead>
                <tbody>
                    {table_rows}
                </tbody>
            </table>
        </div>
    </div>
    """

    cards = "".join([f"""
        <div class="card mb-2">
            <div class="card-body d-flex justify-content-between align-items-center py-2 px-3">
                <div id="status-badge-{name}"><h6 class="fw-bold mb-0 text-info d-inline">{name}</h6></div>
                <p class="tool-desc mb-0">{cfg['description']}</p>
                <a href="/tool/{name}" class="btn btn-sm btn-outline-primary px-3 fw-bold">Open Tool</a>
            </div>
        </div>""" for name, cfg in TOOLS.items()])
    
    tools_html = f"""
    <div class="section-title">MergerFS Management Toolkit</div>
    {cards}
    """
    
    return page("Dashboard", f"{metrics_html}{inventory_html}{tools_html}")

@app.get("/tool/{tool}", response_class=HTMLResponse)
def tool_form(tool: str):
    if tool not in TOOLS: return page("Error", "Tool not defined.")
    config = TOOLS[tool]
    help_text = subprocess.run(["python3", os.path.join(TOOLS_DIR, tool), "--help"], capture_output=True, text=True).stdout
    examples = TOOL_EXAMPLES.get(tool, [])
    
    fields = f"""
    <div class="section-title">1. Target Selection</div>
    <div class="mb-4">
        <input class="form-control form-control-lg" name="target_path" placeholder="/storage" required>
        <small class="text-muted">Enter path as seen by container.</small>
    </div>
    """

    if "actions" in config:
        fields += f"""<div class="section-title">2. Subcommand</div>
        <select class="form-select mb-2" name="__action" required>
            <option value="">-- Action --</option>
            {"".join(f"<option value='{c}'>{c}</option>" for c in config['actions'])}
        </select>
        <input class="form-control mb-4" name="__pos_arg" placeholder="Extra args (e.g. path /mnt/disk2)">"""

    fields += "<div class='section-title'>3. Configuration</div><div class='row'>"
    for opt in config.get("options", []):
        label = f"--{opt['name']}" + (f" (-{opt['short']})" if opt.get('short') else "")
        if opt["type"] == "level":
            fields += f"""<div class='col-md-6 mb-3'><label class='form-label fw-bold'>{label}</label>
                <select class='form-select' name='{opt['name']}'>
                    <option value=''>None</option><option value='1'>-v</option><option value='2'>-vv</option><option value='3'>-vvv</option>
                </select></div>"""
        elif opt["type"] == "choice":
            fields += f"""<div class='col-md-6 mb-3'><label class='form-label fw-bold'>{label}</label>
                <select class='form-select' name='{opt['name']}'>
                    <option value=''>Default</option>
                    {"".join(f"<option value='{c}'>{c}</option>" for c in opt['choices'])}
                </select></div>"""
        elif opt["type"] == "bool":
            fields += f"""<div class="col-md-6 mb-3"><div class="form-check pt-3">
                <input class="form-check-input" type="checkbox" name="{opt['name']}" id="{opt['name']}">
                <label class="form-check-label" for="{opt['name']}"><b>{label}</b><br><small class='text-muted'>{opt['help']}</small></label>
            </div></div>"""
        else:
            fields += f"""<div class='col-md-6 mb-3'><label class='form-label fw-bold'>{label}</label>
                <input class='form-control' name='{opt['name']}' placeholder='Value'><small class='text-muted'>{opt['help']}</small></div>"""
    
    body = f"""
    <div class="card shadow"><div class="card-body p-4">
        <h2 class="text-info mb-4 fw-bold">{tool}</h2>
        <form id="toolform" data-tool="{tool}" method="post" action="/run/{tool}">
            {fields}
            <div class="section-title">4. Live Preview</div>
            <div class="form-check form-switch mb-2">
                <input class="form-check-input" type="checkbox" id="manual_switch" onchange="toggleManual()">
                <label class="form-check-label" for="manual_switch">Manual Override</label>
            </div>
            <textarea id="preview-text" name="__manual_cmd" class="preview-box" readonly></textarea>
            <div class="manual-note">⚠️ Manual Override Active.</div>
            
            <button type="submit" id="btn-submit-run" class="btn btn-run w-100 mt-4 shadow-sm">RUN TOOL</button>
            <button type="button" id="btn-submit-stop" class="btn btn-stop w-100 mt-4 shadow-sm" onclick="stopActiveProcess()">STOP PROCESS</button>
        </form>
        
        <div id="terminal-console" class="live-terminal"></div>
        
        <div class="section-title">5. Reference Guidance</div>
        <p class="fw-bold mb-1 mt-3 small text-white">Usage Examples:</p>
        <pre class="ref-box">{"\\n".join(examples)}</pre>
        <p class="fw-bold mb-1 mt-3 small text-white">Raw Help Text:</p>
        <pre class="ref-box">{help_text}</pre>
    </div></div>"""
    return page(tool, body)

@app.post("/run/{tool}")
async def run_tool(tool: str, request: Request):
    global ACTIVE_PROCESSES, PROCESS_LOGS
    if not same_origin(request):
        return JSONResponse(status_code=403, content={"detail": "cross-origin request rejected"})
    if tool in ACTIVE_PROCESSES and ACTIVE_PROCESSES[tool].poll() is None:
        def stream_existing():
            yield "--- RE-ATTACHED TO RUNNING PROCESS ---\\n\\n"
            logs = list(PROCESS_LOGS.get(tool, []))
            yield "".join(logs)
        return StreamingResponse(stream_existing(), media_type="text/plain")

    form = await request.form()
    manual_cmd = form.get("__manual_cmd")
    if manual_cmd and manual_cmd.strip():
        cmd = shlex.split(manual_cmd)
        if cmd and TOOL_NAME_RE.fullmatch(cmd[0]):
            cmd = ["python3", os.path.join(TOOLS_DIR, cmd[0])] + cmd[1:]
        else:
            cmd = ["python3", os.path.join(TOOLS_DIR, tool)] + cmd
    else:
        target = form.get("target_path", "").strip()
        cmd = ["python3", os.path.join(TOOLS_DIR, tool)]
        if tool == "mergerfs.ctl":
            if target: cmd += ["-m", target]
            if form.get("__action"): cmd.append(form.get("__action"))
            if form.get("__pos_arg"): cmd.append(form.get("__pos_arg"))
        else:
            for k, v in form.items():
                if k in ["target_path", "__action", "__pos_arg", "__manual_cmd"] or not v: continue
                if k == "verbose": cmd.append("-" + "v" * int(v))
                elif v == "on": cmd.append(f"--{k}")
                else: cmd += [f"--{k}", str(v).strip()]
            if target: cmd.append(target)

    PROCESS_LOGS[tool] = deque(maxlen=2000)

    def stream_new():
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, preexec_fn=os.setsid)
        ACTIVE_PROCESSES[tool] = process
        init_msg = f"EXEC: {' '.join(cmd)}\\n----------------------------------------\\n"
        PROCESS_LOGS[tool].append(init_msg)
        yield init_msg
        for line in iter(process.stdout.readline, ''): 
            PROCESS_LOGS[tool].append(line)
            yield line
        process.wait()

    return StreamingResponse(stream_new(), media_type="text/plain")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)