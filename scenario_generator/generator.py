#!/usr/bin/env python3
"""Command-line entry point for the scenario generator.

Usage:
    python -m scenario_generator.generator path/to/config.toml
"""

from __future__ import annotations

import sys

import sumolib

from scenario_generator.config_manager import ConfigManager
from scenario_generator.traffic_generator import TrafficGenerator


def main(conf_file: str | None = None) -> None:
    """Load the config, read the network, and run the generator."""
    if conf_file is None:
        if len(sys.argv) < 2:
            raise SystemExit(
                "Usage: python -m scenario_generator.generator <path/to/config.toml>"
            )
        conf_file = sys.argv[1]

    config = ConfigManager.from_toml(conf_file)
    config.ensure_output_dir()

    net = sumolib.net.readNet(config.net_path)

    print(f"Generating scenario at {config.output_path}...")
    TrafficGenerator(net, config).generate()
    print("Finished generating scenario.\n")


if __name__ == "__main__":
    main()
