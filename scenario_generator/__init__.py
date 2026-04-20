"""Scenario generation for thesis-scale SUMO demand experiments.

The package is intentionally organised around the three stages of the current
workflow:

1. build hourly TAZ-level OD matrices,
2. build candidate routes on the real road network,
3. sample one route per vehicle and export SUMO-ready files.

The public entry point is :mod:`scenario_generator.generator`, which reads a
TOML config and delegates the actual generation to
:class:`scenario_generator.traffic_generator.TrafficGenerator`.
"""

from scenario_generator.config_manager import ConfigManager
from scenario_generator.traffic_generator import TrafficGenerator

__all__ = ["ConfigManager", "TrafficGenerator"]
