"""
AATS Admin Setup Script
=======================
Place this file in the ROOT of your AATS project (same level as server/ and admin_dashboard/).

To build into EXE:
    pip install pyinstaller
    pyinstaller --onefile --uac-admin admin_setup.py

The --uac-admin flag ensures Windows asks for Administrator permissions on launch.

# ============================================================
# FUTURE WORKS:
# ============================================================
# 1. SCOPED FIREWALL RULES
#    Currently firewall rules allow the entire local subnet.
#    Update open_firewall_ports() to scope to specific lab IPs:
#    e.g. add  remoteip=192.168.1.0/24  to the netsh command
#
# 2. MQTT AUTHENTICATION
#    Add password protection to Mosquitto so only authorized
#    agents can connect:
#    - Set allow_anonymous false in mosquitto.conf
#    - Create credentials: mosquitto_passwd -c C:\\mosquitto\\passwd labagent
#    - Add mqtt_username and mqtt_password to agent config.json
#    - Update mqtt_client.py in student_agent to send credentials
#
# 3. FASTAPI RATE LIMITING
#    Add a rate limiter to /auth/login to prevent brute force
#    attacks on the admin token. Use slowapi library.
# ============================================================
"""

import atexit
import ctypes
import hashlib
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
import winreg

# ── Configuration ──────────────────────────────────────────
BROADCAST_PORT     = 37020        # UDP port used to announce admin IP to agents
BROADCAST_MSG      = "AATS_ADMIN" # prefix agents listen for
BROADCAST_INTERVAL = 5            # seconds between each broadcast
DASHBOARD_PORT     = 5500
API_PORT           = 8000
MQTT_PORT          = 1883
MOSQUITTO_PATH     = r"C:\Program Files\mosquitto\mosquitto.exe"
MOSQUITTO_ALT_PATH = r"C:\Program Files (x86)\mosquitto\mosquitto.exe"
MOSQUITTO_BUNDLED_REL_PATH = os.path.join("mqtt_broker", "mosquitto.exe")
MOSQUITTO_BUNDLED_SHA_FILE = os.path.join("mqtt_broker", "mosquitto.sha256")
STARTUP_REG_KEY    = r"Software\Microsoft\Windows\CurrentVersion\Run"
LEGACY_ADMIN_STARTUP_NAMES = ("AATSAdmin", "AATSAdminSetup", "admin_setup")
# ───────────────────────────────────────────────────────────


def is_admin() -> bool:
    """Check if the script is running with Administrator privileges."""
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def get_python() -> str:
    """
    Get the real Python executable.
    When running as a PyInstaller EXE, sys.executable points to the EXE itself
    so we need to find the actual python.exe on the system instead.
    """
    if getattr(sys, "frozen", False):
        # Running as PyInstaller EXE — find python.exe on the system PATH
        result = subprocess.run(
            ["where", "python"], capture_output=True, text=True
        )
        lines = result.stdout.strip().splitlines()
        if lines:
            return lines[0]
        print("[!] Python not found on PATH. Please install Python and add it to PATH.")
        input("Press Enter to exit...")
        sys.exit(1)
    return sys.executable


def get_base_dir() -> str:
    """
    Get the project root directory.
    When running as a PyInstaller EXE from dist/, we go one level up
    to reach the actual project root where server/ and admin_dashboard/ live.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


def get_local_ip() -> str:
    """Get the local IPv4 address of this machine."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def open_firewall_ports() -> None:
    """Open required ports in Windows Firewall."""
    print("[*] Configuring firewall rules...")
    rules = [
        ("AATS MQTT 1883",      MQTT_PORT),
        ("AATS API 8000",       API_PORT),
        ("AATS Dashboard 5500", DASHBOARD_PORT),
    ]
    for name, port in rules:
        # Delete existing rule first to avoid duplicates
        os.system(f'netsh advfirewall firewall delete rule name="{name}" >nul 2>&1')
        os.system(
            f'netsh advfirewall firewall add rule name="{name}" '
            f'dir=in action=allow protocol=TCP localport={port} enable=yes >nul 2>&1'
        )
    print("[+] Firewall rules configured.")


def find_mosquitto_path() -> str | None:
    """Return first detected Mosquitto executable path, if installed."""
    for candidate in (MOSQUITTO_PATH, MOSQUITTO_ALT_PATH):
        if os.path.exists(candidate):
            return candidate
    return None


def sha256_file(path: str) -> str:
    """Compute SHA-256 for a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def read_hash_from_file(path: str) -> str | None:
    """Read first hash token from a .sha256 text file."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            line = f.readline().strip()
        if not line:
            return None
        token = line.split()[0].strip().lower()
        if len(token) == 64:
            return token
    except Exception:
        pass
    return None


def verify_sha256(path: str, expected_hash: str) -> bool:
    """Return True if file hash exactly matches expected SHA-256."""
    actual = sha256_file(path)
    return actual == expected_hash.strip().lower()


def find_bundled_mosquitto_path(base_dir: str) -> str | None:
    """Return bundled Mosquitto path if present and hash-valid when provided."""
    bundled_path = os.path.join(base_dir, MOSQUITTO_BUNDLED_REL_PATH)
    if not os.path.exists(bundled_path):
        return None

    # Optional integrity sources: env var overrides sha file.
    expected_hash = os.environ.get("AATS_MOSQUITTO_BUNDLED_SHA256", "").strip().lower()
    if not expected_hash:
        expected_hash = read_hash_from_file(os.path.join(base_dir, MOSQUITTO_BUNDLED_SHA_FILE)) or ""

    if expected_hash:
        if not verify_sha256(bundled_path, expected_hash):
            print("[!] Bundled Mosquitto hash verification failed. Skipping bundled binary.")
            return None
        print("[+] Bundled Mosquitto hash verified.")
    else:
        print("[!] Bundled Mosquitto found without SHA-256 metadata. Using it anyway.")

    return bundled_path


def install_mosquitto_with_winget() -> bool:
    """Attempt to install Mosquitto using winget on supported systems."""
    print("[*] Attempting automatic Mosquitto install with winget...")
    check = subprocess.run(["where", "winget"], capture_output=True, text=True)
    if check.returncode != 0:
        print("[!] winget is not available on this machine.")
        return False

    cmd = [
        "winget",
        "install",
        "-e",
        "--id",
        "EclipseMosquitto.Mosquitto",
        "--accept-package-agreements",
        "--accept-source-agreements",
    ]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print("[!] winget install failed.")
        return False

    print("[+] Mosquitto installed via winget.")
    return True


def install_mosquitto_from_verified_download() -> bool:
    """
    Secure download/install path.
    Requires BOTH env vars:
      AATS_MOSQUITTO_INSTALLER_URL
      AATS_MOSQUITTO_INSTALLER_SHA256
    """
    url = os.environ.get("AATS_MOSQUITTO_INSTALLER_URL", "").strip()
    expected_hash = os.environ.get("AATS_MOSQUITTO_INSTALLER_SHA256", "").strip().lower()

    if not url or not expected_hash:
        return False

    if len(expected_hash) != 64:
        print("[!] AATS_MOSQUITTO_INSTALLER_SHA256 is invalid (must be 64 hex chars).")
        return False

    print("[*] Attempting verified Mosquitto installer download...")
    tmp_dir = tempfile.gettempdir()
    installer_path = os.path.join(tmp_dir, "aats-mosquitto-installer.exe")

    try:
        urllib.request.urlretrieve(url, installer_path)
    except Exception as e:
        print(f"[!] Download failed: {e}")
        return False

    if not verify_sha256(installer_path, expected_hash):
        print("[!] Downloaded installer SHA-256 mismatch. Refusing to execute.")
        try:
            os.remove(installer_path)
        except Exception:
            pass
        return False

    print("[+] Installer SHA-256 verified.")
    result = subprocess.run([installer_path, "/S"])
    try:
        os.remove(installer_path)
    except Exception:
        pass

    if result.returncode != 0:
        print("[!] Silent installer failed.")
        return False

    print("[+] Mosquitto installed via verified download.")
    return True


def ensure_admin_manual_start_only() -> None:
    """Remove legacy admin auto-start entries if they exist."""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY, 0, winreg.KEY_SET_VALUE
        )
    except Exception:
        return

    try:
        removed_any = False
        for name in LEGACY_ADMIN_STARTUP_NAMES:
            try:
                winreg.DeleteValue(key, name)
                removed_any = True
            except FileNotFoundError:
                pass
        if removed_any:
            print("[+] Cleared legacy Admin auto-start entries (Admin remains manual-start).")
    finally:
        winreg.CloseKey(key)


def start_mosquitto(base_dir: str) -> tuple[subprocess.Popen | None, str]:
    """Start the Mosquitto MQTT broker."""
    print("[*] Starting Mosquitto broker...")

    # Check if already running — net start fails if service is already up
    check = subprocess.run(
        ["sc", "query", "mosquitto"], capture_output=True, text=True
    )
    if "RUNNING" in check.stdout:
        print("[+] Mosquitto already running.")
        return None, "service-running"

    # Try starting as a Windows service
    result = os.system("net start mosquitto >nul 2>&1")
    if result == 0:
        print("[+] Mosquitto started as a service.")
        return None, "service-started"

    # Fall back to running a detected installed exe directly.
    mosquitto_path = find_mosquitto_path()
    if mosquitto_path:
        proc = subprocess.Popen(
            [mosquitto_path, "-v"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("[+] Mosquitto started directly.")
        return proc, "installed-exe"

    # Offline fallback: use bundled broker if present.
    bundled = find_bundled_mosquitto_path(base_dir)
    if bundled:
        proc = subprocess.Popen(
            [bundled, "-v"],
            cwd=os.path.dirname(bundled),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("[+] Bundled Mosquitto started directly.")
        return proc, "bundled-exe"

    # Try to auto-install with winget, then retry service/path startup.
    if install_mosquitto_with_winget():
        time.sleep(2)
        if os.system("net start mosquitto >nul 2>&1") == 0:
            print("[+] Mosquitto started as a service after installation.")
            return None, "winget-install-service"

        mosquitto_path = find_mosquitto_path()
        if mosquitto_path:
            proc = subprocess.Popen(
                [mosquitto_path, "-v"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print("[+] Mosquitto started directly after installation.")
            return proc, "winget-install-exe"

    # Final auto path: verified installer download if env vars are provided.
    if install_mosquitto_from_verified_download():
        time.sleep(2)
        if os.system("net start mosquitto >nul 2>&1") == 0:
            print("[+] Mosquitto started as a service after verified installation.")
            return None, "verified-download-service"

        mosquitto_path = find_mosquitto_path()
        if mosquitto_path:
            proc = subprocess.Popen(
                [mosquitto_path, "-v"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print("[+] Mosquitto started directly after verified installation.")
            return proc, "verified-download-exe"

    print("[!] Mosquitto not found and auto-install failed.")
    print("    Optional secure download env vars:")
    print("      AATS_MOSQUITTO_INSTALLER_URL")
    print("      AATS_MOSQUITTO_INSTALLER_SHA256")
    print("    Optional bundled broker path:")
    print("      mqtt_broker/mosquitto.exe (+ optional mqtt_broker/mosquitto.sha256)")
    print("    Please install it manually from https://mosquitto.org/download/")
    input("Press Enter to exit...")
    sys.exit(1)


def print_mosquitto_provisioning_status(source: str) -> None:
    """Print a concise self-check line indicating broker provisioning source."""
    labels = {
        "service-running": "Windows service already running",
        "service-started": "Windows service start",
        "installed-exe": "Installed executable path",
        "bundled-exe": "Bundled offline executable",
        "winget-install-service": "winget install + Windows service",
        "winget-install-exe": "winget install + executable path",
        "verified-download-service": "Verified download + Windows service",
        "verified-download-exe": "Verified download + executable path",
    }
    label = labels.get(source, source)
    print(f"[+] Mosquitto provisioning path: {label}")


def start_fastapi(base_dir: str) -> subprocess.Popen:
    """Start the FastAPI server using uvicorn."""
    print("[*] Starting FastAPI server...")
    server_dir = os.path.join(base_dir, "server")
    proc = subprocess.Popen(
        [get_python(), "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", str(API_PORT)],
        cwd=server_dir,
    )
    time.sleep(2)
    if proc.poll() is not None:
        print("[!] FastAPI server exited during startup. Check the console output above for the traceback.")
    print("[+] FastAPI server started.")
    return proc


def ensure_server_requirements(base_dir: str) -> None:
    """Install the server-side Python requirements if uvicorn is missing."""
    python_exe = get_python()
    check = subprocess.run(
        [python_exe, "-c", "import uvicorn"],
        capture_output=True,
        text=True,
    )
    if check.returncode == 0:
        return

    server_dir = os.path.join(base_dir, "server")
    requirements_path = os.path.join(server_dir, "requirements.txt")
    print("[*] Installing FastAPI server dependencies...")
    result = subprocess.run(
        [python_exe, "-m", "pip", "install", "-r", requirements_path],
        cwd=server_dir,
    )
    if result.returncode != 0:
        print("[!] Failed to install server dependencies.")
        print(f"    Required file: {requirements_path}")
        input("Press Enter to exit...")
        sys.exit(1)
    print("[+] FastAPI server dependencies installed.")


def wait_for_api_ready(local_ip: str | None = None, timeout_sec: int = 60) -> bool:
    """Wait until the FastAPI health endpoint responds successfully.

    Tests multiple candidate addresses (127.0.0.1, localhost, and the
    optionally provided local_ip) to be resilient to binding/hostname
    differences and transient startup delays.
    """
    deadline = time.time() + timeout_sec
    candidates = [f"http://127.0.0.1:{API_PORT}/health", f"http://localhost:{API_PORT}/health"]
    if local_ip:
        candidates.append(f"http://{local_ip}:{API_PORT}/health")

    last_exc: Exception | None = None

    while time.time() < deadline:
        for url in candidates:
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.status == 200:
                        return True
            except Exception as e:
                last_exc = e
                # try next candidate
                continue
        time.sleep(1)

    # helpful diagnostics for the user
    if last_exc is not None:
        print(f"[!] API readiness check failed: last error: {last_exc}")
    return False


def start_dashboard(base_dir: str) -> subprocess.Popen:
    """Serve the admin dashboard via Python's HTTP server."""
    print("[*] Starting dashboard server...")
    dashboard_dir = os.path.join(base_dir, "admin_dashboard")
    proc = subprocess.Popen(
        [get_python(), "-m", "http.server", str(DASHBOARD_PORT)],
        cwd=dashboard_dir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print("[+] Dashboard server started.")
    return proc


def broadcast_ip(ip: str, stop_event: threading.Event) -> None:
    """
    Continuously broadcast the admin IP over UDP so agent PCs
    can auto-discover it without manual config.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    message = f"{BROADCAST_MSG}:{ip}".encode()
    print(f"[*] Broadcasting IP {ip} every {BROADCAST_INTERVAL}s on UDP port {BROADCAST_PORT}...")
    while not stop_event.is_set():
        try:
            sock.sendto(message, ("<broadcast>", BROADCAST_PORT))
        except Exception:
            pass
        time.sleep(BROADCAST_INTERVAL)
    sock.close()


def shutdown(procs: list, stop_event: threading.Event) -> None:
    """Cleanly stop all started processes and close firewall ports."""
    print("\n[*] Shutting down AATS Admin...")
    stop_event.set()

    # Stop any subprocesses (fastapi, dashboard, direct mosquitto)
    for proc in procs:
        if proc is not None:
            proc.terminate()

    # Always stop Mosquitto — covers both service and direct launch
    os.system("net stop mosquitto >nul 2>&1")
    print("[+] Mosquitto stopped.")

    # Remove firewall rules on exit
    for name in ("AATS MQTT 1883", "AATS API 8000", "AATS Dashboard 5500"):
        os.system(f'netsh advfirewall firewall delete rule name="{name}" >nul 2>&1')

    print("[+] Firewall rules removed.")
    print("[+] AATS Admin stopped. Goodbye!")


def main() -> None:
    print("=" * 52)
    print("         AATS — Admin Setup & Launcher")
    print("=" * 52)

    # Require admin privileges
    if not is_admin():
        print("[!] This program must be run as Administrator.")
        print("    Right-click the EXE and select 'Run as administrator'.")
        input("\nPress Enter to exit...")
        sys.exit(1)

    # Locate project root correctly whether running as .py or .exe
    base_dir = get_base_dir()
    print(f"[+] Project root: {base_dir}")

    # Get this machine's IP
    ip = get_local_ip()
    print(f"[+] Admin PC IP detected: {ip}")

    # Run DB migrations (if present) so existing installations are upgraded automatically
    migrate_script = os.path.join(base_dir, "server", "migrate_add_pending.py")
    if os.path.exists(migrate_script):
        print("[*] Applying database migrations (if needed)...")
        try:
            res = subprocess.run([get_python(), migrate_script], cwd=os.path.join(base_dir, "server"))
            if res.returncode == 0:
                print("[+] Database migration completed.")
            else:
                print("[!] Database migration script exited with a non-zero status. Continuing startup.")
        except Exception as e:
            print(f"[!] Failed to run migration script: {e}. Continuing startup.")

    # Admin dashboard should only run when admin explicitly starts it.
    ensure_admin_manual_start_only()

    # Setup
    open_firewall_ports()
    mosquitto_proc, mosquitto_source = start_mosquitto(base_dir)
    print_mosquitto_provisioning_status(mosquitto_source)
    time.sleep(2)  # give Mosquitto a moment to initialise

    ensure_server_requirements(base_dir)

    fastapi_proc   = start_fastapi(base_dir)
    if not wait_for_api_ready(ip):
        print(f"[!] FastAPI did not become ready on port {API_PORT}.")
        print("    Check the console output above for import errors or startup failures.")
        input("Press Enter to exit...")
        shutdown([fastapi_proc], threading.Event())
        sys.exit(1)

    dashboard_proc = start_dashboard(base_dir)
    time.sleep(3)  # give servers a moment to start

    # Start IP broadcaster so agent PCs can auto-discover
    stop_event = threading.Event()
    broadcast_thread = threading.Thread(
        target=broadcast_ip,
        args=(ip, stop_event),
        daemon=True,
    )
    broadcast_thread.start()

    # Open the dashboard in the default browser
    dashboard_url = f"http://localhost:{DASHBOARD_PORT}/login.html"
    print(f"[*] Opening dashboard at {dashboard_url}")
    webbrowser.open(dashboard_url)

    # Summary
    print("\n" + "=" * 52)
    print("  AATS Admin is running!")
    print(f"  Dashboard : http://localhost:{DASHBOARD_PORT}/login.html")
    print(f"  API       : http://localhost:{API_PORT}")
    print(f"  MQTT      : {ip}:{MQTT_PORT}")
    print(f"  MQTT Path : {mosquitto_source}")
    print(f"  Broadcast : sending IP every {BROADCAST_INTERVAL}s")
    print("=" * 52)
    print("\n  Press Ctrl+C or close this window to stop all services.\n")

    procs = [p for p in [mosquitto_proc, fastapi_proc, dashboard_proc] if p]

    # Runs on ANY exit — X button, Ctrl+C, or crash
    atexit.register(shutdown, procs, stop_event)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass  # atexit handles cleanup


if __name__ == "__main__":
    main()