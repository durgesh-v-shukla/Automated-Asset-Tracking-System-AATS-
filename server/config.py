import os
from dataclasses import dataclass


def env_or_default(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default

    value = value.strip()
    return value if value else default


@dataclass
class Settings:
    host: str = os.getenv("AATS_HOST", "0.0.0.0")
    port: int = int(os.getenv("AATS_PORT", "8000"))
    mqtt_broker: str = os.getenv("AATS_MQTT_BROKER", "localhost")
    mqtt_port: int = int(os.getenv("AATS_MQTT_PORT", "1883"))
    db_path: str = os.getenv("AATS_DB_PATH", "server/database/aats.db")
    usb_missing_timeout_sec: int = int(os.getenv("AATS_USB_TIMEOUT_SEC", "15"))
    bluetooth_missing_timeout_sec: int = int(os.getenv("AATS_BT_TIMEOUT_SEC", "300"))
    admin_username: str = env_or_default("AATS_ADMIN_USERNAME", "admin")
    admin_password: str = env_or_default("AATS_ADMIN_PASSWORD", "admin")
    # How long (seconds) before a PC heartbeat is considered stale/offline
    heartbeat_staleness_timeout_sec: int = int(os.getenv("AATS_HEARTBEAT_STALENESS_SEC", "120"))


settings = Settings()
