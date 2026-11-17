import os
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    db_path: Path
    # Operator key checked against the X-Api-Key header. Empty means /v1 is
    # closed (503) rather than open.
    api_key: str
    # Band definition; same env names and defaults as shared/contracts/config.py.
    n_runs: int = 5
    band_low: int = 1
    band_high: int = 3

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=Path(os.environ.get("SDE_DB_PATH", ROOT_DIR / "work" / "sde.db")),
            api_key=os.environ.get("SDE_API_KEY", ""),
            n_runs=int(os.environ.get("N_RUNS", "5")),
            band_low=int(os.environ.get("BAND_LOW", "1")),
            band_high=int(os.environ.get("BAND_HIGH", "3")),
        )
