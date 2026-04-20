"""Config access helpers for the scenario generator.

The generator reads plain TOML, but the rest of the codebase should not
need to know the exact nesting structure of that TOML.  This wrapper provides
typed accessors, path resolution relative to the config file, and a few
backward-compatibility defaults for legacy keys.
"""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any, Mapping


TRUE_STRINGS = {"1", "true", "yes", "on"}
FALSE_STRINGS = {"0", "false", "no", "off"}


class ConfigManager:
    """Wrap raw TOML data and expose typed scenario-generator settings."""

    def __init__(self, raw_config: Mapping[str, Any], source_path: str | Path | None = None):
        self._raw = copy.deepcopy(dict(raw_config))
        self._source_path = Path(source_path).resolve() if source_path is not None else None
        self._resolve_known_paths()

    @classmethod
    def from_toml(cls, conf_file: str | Path) -> "ConfigManager":
        """Load and resolve a TOML config file."""
        conf_path = Path(conf_file).resolve()
        with open(conf_path, "rb") as handle:
            raw_config = tomllib.load(handle)
        return cls(raw_config, source_path=conf_path)

    def get(self, *keys: str, default: Any = None) -> Any:
        """Safely retrieve nested configuration values."""
        current: Any = self._raw
        for key in keys:
            if isinstance(current, Mapping) and key in current:
                current = current[key]
            else:
                return default
        return current

    def get_int(self, *keys: str, default: int) -> int:
        """Return a configuration value coerced to ``int``."""
        return int(self.get(*keys, default=default))

    def get_float(self, *keys: str, default: float) -> float:
        """Return a configuration value coerced to ``float``."""
        return float(self.get(*keys, default=default))

    def get_bool(self, *keys: str, default: bool = False) -> bool:
        """Return a configuration value coerced to ``bool`` without ``eval``."""
        value = self.get(*keys, default=default)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in TRUE_STRINGS:
                return True
            if lowered in FALSE_STRINGS:
                return False
        return bool(value)

    def get_mapping(self, *keys: str) -> Mapping[str, Any] | None:
        """Return a nested mapping section if it exists."""
        value = self.get(*keys, default=None)
        return value if isinstance(value, Mapping) else None

    def ensure_output_dir(self) -> None:
        """Create the configured output directory if it does not already exist."""
        Path(self.output_path).mkdir(parents=True, exist_ok=True)

    def outpath(self, filename: str) -> str:
        """Return an absolute path inside the configured output directory."""
        return str(Path(self.output_path) / filename)

    @property
    def output_path(self) -> str:
        """Output directory for the generated scenario artifacts."""
        return str(self.get("output_path", default="./out"))

    @property
    def net_path(self) -> str:
        """Resolved path to the SUMO network file."""
        return str(self.get("net_path", default=""))

    @property
    def taz_path(self) -> str:
        """Resolved path to the TAZ file."""
        return str(self.get("taz", "path", default=""))

    @property
    def od_matrices_path(self) -> str | None:
        """Resolved path to an optional external OD-matrix pickle."""
        value = self.get("od_matrices", default=None)
        return str(value) if value else None

    @property
    def sim_begin_time(self) -> int:
        """Simulation start time in seconds."""
        return self.get_int("sim_begin_time", default=0)

    @property
    def sim_end_time(self) -> int:
        """Simulation end time in seconds."""
        return self.get_int("sim_end_time", default=86400)

    @property
    def aggregation_interval_seconds(self) -> int:
        """OD aggregation interval in seconds."""
        return self.get_int("data_aggr_frequency", default=3600)

    @property
    def random_seed(self) -> int:
        """Random seed shared by all stochastic generation steps."""
        return self.get_int("random_seed", default=42)

    @property
    def daily_total_demand(self) -> int:
        """Daily synthetic demand, preserving the legacy key as fallback."""
        return self.get_int(
            "synthetic_od",
            "daily_total",
            default=self.get_int("max_num_vehicles", default=0),
        )

    @property
    def gravity_beta(self) -> float:
        """Distance-decay parameter for the gravity OD model."""
        return self.get_float("synthetic_od", "distance_decay_beta", default=0.001)

    @property
    def candidate_route_count(self) -> int:
        """Number of routes retained per OD pair."""
        return self.get_int(
            "candidate_routes",
            "k_paths",
            default=self.get_int("num_link_path_per_reg_path", default=3),
        )

    @property
    def endpoint_samples_per_od(self) -> int:
        """How many origin/destination edge samples are tried per OD pair."""
        default_samples = max(self.candidate_route_count * 2, 6)
        return self.get_int(
            "candidate_routes",
            "endpoint_samples_per_od",
            default=default_samples,
        )

    @property
    def route_cost_scale(self) -> float:
        """Softmax scale that controls how strongly the shortest route is favoured."""
        return self.get_float("route_sampling", "cost_scale", default=500.0)

    @property
    def temporal_profile_config(self) -> Mapping[str, Any]:
        """Raw temporal-profile section used by the OD model."""
        return self.get_mapping("temporal_profile") or {}

    @property
    def commute_pattern_config(self) -> Mapping[str, Any] | None:
        """Raw commute-pattern section if present."""
        return self.get_mapping("commute_pattern")

    def _resolve_known_paths(self) -> None:
        """Resolve known filesystem paths relative to the config location."""
        if self._source_path is None:
            return

        for key_path in (("net_path",), ("od_matrices",), ("taz", "path")):
            raw_value = self.get(*key_path, default=None)
            if not raw_value:
                continue
            resolved = self._resolve_input_path(str(raw_value))
            self._set(key_path, resolved)

    def _resolve_input_path(self, raw_path: str) -> str:
        """Resolve an input path against common repo-relative locations."""
        expanded = self._expand_env_path(raw_path)
        candidate = Path(expanded)
        if candidate.is_absolute() or candidate.exists():
            return str(candidate)

        assert self._source_path is not None
        search_roots = [
            Path.cwd(),
            self._source_path.parent,
            self._source_path.parent.parent,
            self._source_path.parent.parent.parent,
        ]
        for root in search_roots:
            resolved = (root / candidate).resolve()
            if resolved.exists():
                return str(resolved)
        return str(candidate)

    @staticmethod
    def _expand_env_path(raw_path: str) -> str:
        """Expand ``$VAR``-prefixed config paths."""
        if raw_path.startswith("$"):
            env_name = raw_path.strip("$")
            return os.environ.get(env_name, raw_path)
        return raw_path

    def _set(self, keys: tuple[str, ...], value: Any) -> None:
        """Mutate a nested config key after path resolution."""
        target = self._raw
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value
