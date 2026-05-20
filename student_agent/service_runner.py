import atexit
import json
import os
import threading
import time
from typing import Dict, Optional, Tuple

from bluetooth_monitor import BluetoothMonitor
from device_monitor import USBDeviceMonitor
from mqtt_client import MQTTClient


class AgentRuntime:
    def __init__(self, config_path: Optional[str] = None) -> None:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self._base_dir = base_dir
        self._config_path = config_path or os.path.join(base_dir, "config.json")
        self._mqtt_client: Optional[MQTTClient] = None
        self._usb_monitor: Optional[USBDeviceMonitor] = None
        self._bt_monitor: Optional[BluetoothMonitor] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._running = False
        self._lab_pc: Tuple[str, str] | None = None
        self._agent_version: str = "1.0.0"
        self._heartbeat_interval_sec: int = 30
        self._pidfile = os.path.join(self._base_dir, "agent.pid")
        self._bluetooth_available: bool = False

    def _load_config(self) -> Dict:
        with open(self._config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def start(self) -> None:
        if self._running:
            return

        config = self._load_config()
        lab_id = config["lab_id"]
        pc_id = config["pc_id"]
        self._lab_pc = (lab_id, pc_id)
        self._agent_version = config.get("agent_version", "1.0.0")
        scan_interval_sec = int(config.get("scan_interval_sec", 2))
        self._heartbeat_interval_sec = int(config.get("heartbeat_interval_sec", 30))

        self._mqtt_client = MQTTClient(
            broker=config["broker"],
            port=int(config["port"]),
            lab_id=lab_id,
            pc_id=pc_id,
            agent_version=self._agent_version,
        )
        self._mqtt_client.connect()

        def device_callback(event: Dict) -> None:
            if self._lab_pc is None or self._mqtt_client is None:
                return
            payload = {
                "event_id": event.get("event_id"),
                "lab_id": lab_id,
                "pc_id": pc_id,
                "device_id": event["device_id"],
                "device_label": event.get("device_label", event["device_id"]),
                "device_type": event["device_type"],
                "status": event["status"],
                "rssi": event.get("rssi"),
                "observed_at": event["observed_at"],
                "agent_time": event.get("agent_time"),
                "source": event.get("source", "agent"),
            }
            self._mqtt_client.publish_event(payload)

        self._usb_monitor = USBDeviceMonitor(
            tracked_devices=config.get("usb_devices", []),
            poll_interval_sec=scan_interval_sec,
            on_change_callback=device_callback,
        )
        self._bt_monitor = BluetoothMonitor(
            tracked_devices=config.get("bluetooth_devices", []),
            poll_interval_sec=max(scan_interval_sec, 5),
            on_change_callback=device_callback,
        )
        self._bluetooth_available = self._bt_monitor.is_available()

        self._running = True

        # Write pidfile so external tools (uninstall) can locate the running agent
        try:
            with open(self._pidfile, "w", encoding="utf-8") as pf:
                pf.write(str(os.getpid()))
        except Exception:
            pass

        def heartbeat_loop() -> None:
            assert self._lab_pc is not None
            lab, pc = self._lab_pc
            while self._running and self._mqtt_client is not None:
                self._mqtt_client.publish_status(
                    {
                        "lab_id": lab,
                        "pc_id": pc,
                        "pc_status": "online",
                        "last_seen": None,
                        "agent_version": self._agent_version,
                        "bluetooth_available": self._bluetooth_available,
                    }
                )
                time.sleep(self._heartbeat_interval_sec)

        self._usb_monitor.start()
        self._bt_monitor.start()
        self._heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def stop(self) -> None:
        self._running = False
        if self._usb_monitor is not None:
            self._usb_monitor.stop()
        if self._bt_monitor is not None:
            self._bt_monitor.stop()
        if self._mqtt_client is not None and self._lab_pc is not None:
            lab, pc = self._lab_pc
            try:
                self._mqtt_client.publish_status(
                    {
                        "lab_id": lab,
                        "pc_id": pc,
                        "pc_status": "offline",
                        "last_seen": None,
                        "agent_version": self._agent_version,
                        "bluetooth_available": self._bluetooth_available,
                    }
                )
            except Exception:
                pass
            self._mqtt_client.disconnect()

        # Remove pidfile on clean shutdown
        try:
            if os.path.exists(self._pidfile):
                os.remove(self._pidfile)
        except Exception:
            pass


_runtime: Optional[AgentRuntime] = None


def run_foreground(config_path: Optional[str] = None) -> None:
    global _runtime
    _runtime = AgentRuntime(config_path=config_path)

    @atexit.register
    def _on_exit() -> None:
        try:
            if _runtime is not None:
                _runtime.stop()
        except Exception:
            pass

    _runtime.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        if _runtime is not None:
            _runtime.stop()


if __name__ == "__main__":
    run_foreground()

