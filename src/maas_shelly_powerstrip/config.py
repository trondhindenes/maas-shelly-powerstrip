from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables (or a .env file)."""

    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    # Hostname or IP of the Shelly power strip. Inside a container mDNS names
    # usually do not resolve, so prefer an IP with a DHCP reservation.
    shelly_host: str = "192.168.202.252"
    # Optional Shelly digest auth password (user is always "admin" on Gen2+).
    shelly_password: str | None = None
    shelly_timeout: float = 5.0

    # Watts. At or below this the attached PC is considered powered off,
    # even when the outlet relay is closed.
    idle_watt_threshold: float = 5.0
    # How long (seconds) usage must stay at/below the threshold to count as off.
    idle_window_seconds: float = 5.0
    # How often (seconds) the background sampler polls the strip.
    sample_interval: float = 1.0
    # How long (seconds) to keep the relay open when a power-cycle is required.
    cycle_off_seconds: float = 5.0

    # Optional bearer token. If set, every /outlets request must carry
    # "Authorization: Bearer <token>" (MAAS webhook "power_token").
    api_token: str | None = None
