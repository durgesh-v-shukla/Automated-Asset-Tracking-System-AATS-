import asyncio
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

try:
    from bleak import BleakScanner
except Exception:  # pragma: no cover
    BleakScanner = None


class BluetoothMonitor:
    def __init__(
        self,
        tracked_devices: List[Dict],
        poll_interval_sec: int = 5,
        on_change_callback: Optional[Callable[[Dict], None]] = None,
    ) -> None:
        self.tracked_devices = tracked_devices
        self.poll_interval_sec = poll_interval_sec
        self.on_change_callback = on_change_callback
        self.running = False
        self._state: Dict[str, str] = {}
        self._available = BleakScanner is not None

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    async def _scan_devices(self) -> Dict[str, Optional[int]]:
        if BleakScanner is None:
            return {}
        discovered = await BleakScanner.discover(timeout=4.0, return_adv=False)
        by_mac: Dict[str, Optional[int]] = {}
        for item in discovered:
            mac = (item.address or "").upper()
            if not mac:
                continue
            rssi = getattr(item, "rssi", None)
            if rssi is None:
                metadata = getattr(item, "metadata", None) or {}
                rssi = metadata.get("rssi")
            if rssi is None:
                by_mac[mac] = None
                continue
            by_mac[mac] = int(rssi)
        return by_mac

    def _emit(self, payload: Dict) -> None:
        if self.on_change_callback:
            self.on_change_callback(payload)

    def is_available(self) -> bool:
        return self._available

    def _resolve_status(self, present: bool, rssi: Optional[int], weak_threshold: int) -> str:
        if not present:
            return "MISSING"
        if rssi < weak_threshold:
            return "WEAK_SIGNAL"
        return "CONNECTED"

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        thread = threading.Thread(target=self._monitor_loop, daemon=True)
        thread.start()

    def stop(self) -> None:
        self.running = False

    def _monitor_loop(self) -> None:
        if not self._available:
            print("[!] Bleak is not installed. Bluetooth monitoring is disabled.")
            return

        while self.running:
            try:
                seen = asyncio.run(self._scan_devices())
            except Exception as exc:
                print(f"Bluetooth scan error: {exc}")
                seen = {}

            for device in self.tracked_devices:
                device_id = device["device_id"]
                mac = device["mac"].upper()
                alias = device.get("alias", device_id)
                weak_threshold = int(device.get("weak_rssi_threshold", -75))

                present = mac in seen
                rssi = seen.get(mac)
                new_status = self._resolve_status(present, rssi, weak_threshold)
                old_status = self._state.get(device_id)

                if old_status != new_status:
                    self._state[device_id] = new_status
                    self._emit(
                        {
                            "device_id": device_id,
                            "device_label": alias,
                            "device_type": "bluetooth",
                            "status": new_status,
                            "rssi": rssi,
                            "observed_at": self._now(),
                            "source": "bluetooth_monitor",
                        }
                    )

            time.sleep(self.poll_interval_sec)
