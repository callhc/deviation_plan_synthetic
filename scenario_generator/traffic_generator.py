"""High-level orchestration for the three-stage scenario-generation pipeline.

This module deliberately contains almost no modelling details.  Its job is to
wire together the three stages in a readable sequence:

1. **Stage 1 – Demand generation**: build 24 hourly TAZ-level OD matrices,
   either from a gravity model or from an externally supplied pickle.
2. **Stage 2 – Route library**: compute *k* candidate routes per active OD
   pair using Yen's algorithm on the edge-expanded road network.
3. **Stage 3 – Vehicle sampling**: assign a departure time and a route to
   every trip, then write SUMO-ready output files.
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import sumolib

from scenario_generator.config_manager import ConfigManager
from scenario_generator.exports import (
    write_hourly_od_csvs,
    write_taz_relations_xml,
    write_vehicle_def_to_file,
)
from scenario_generator.od_model import (
    allocate_hourly_totals,
    build_commute_od_matrices,
    build_gravity_od_matrix,
    build_hourly_od_shares,
    generate_hourly_od_matrices,
    generate_two_peak_profile,
    parse_commute_pattern,
    validate_external_od_matrices,
)
from scenario_generator.route_library import (
    EdgeRouter,
    RouteCandidate,
    build_candidate_route_library,
    estimate_zone_impedance_matrix,
)
from scenario_generator.vehicle_sampler import sample_vehicles_from_od
from scenario_generator.visualize import save_all as save_all_plots
from scenario_generator.zones import TrafficZone, load_taz_zones

HourlyODMatrices = dict[int, pd.DataFrame]
RouteLibrary = dict[tuple[str, str], list[RouteCandidate]]

_NB_HOURS = 24
_SECONDS_PER_DAY = 86400


@dataclass(frozen=True)
class GenerationSettings:
    """All runtime parameters needed by the generator after config parsing."""

    interval_seconds: int
    random_seed: int
    candidate_route_count: int
    endpoint_samples_per_od: int


class TrafficGenerator:
    """Generate one complete SUMO scenario from a road network and a config file."""

    def __init__(self, net: sumolib.net.Net, config: ConfigManager):
        self.net = net
        self.config = config

    def generate(self) -> None:
        """Run the full three-stage scenario-generation pipeline."""
        settings = self._build_settings()
        self._validate_time_scope(settings.interval_seconds)

        zones = load_taz_zones(self.net, self.config.taz_path)
        router = EdgeRouter(self.net)
        rng = np.random.default_rng(settings.random_seed)

        # --- Stage 1: Demand generation ---
        hourly_od_matrices, used_external_od = self._build_hourly_od_matrices(
            zones=zones,
            router=router,
            settings=settings,
        )

        # --- Stage 2: Route library ---
        route_library = self._build_route_library(
            hourly_od_matrices=hourly_od_matrices,
            zones=zones,
            router=router,
            settings=settings,
            used_external_od=used_external_od,
        )

        # --- Stage 3: Vehicle sampling ---
        vehicles = sample_vehicles_from_od(
            hourly_od_matrices,
            route_library,
            interval_seconds=settings.interval_seconds,
            rng=rng,
        )

        self._write_outputs(hourly_od_matrices, vehicles, settings.interval_seconds)
        self._log_generation_summary(hourly_od_matrices, vehicles)
        save_all_plots(
            hourly_od_matrices,
            vehicles,
            output_dir=Path(self.config.output_path) / "plots",
            route_library=route_library,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_settings(self) -> GenerationSettings:
        return GenerationSettings(
            interval_seconds=self.config.aggregation_interval_seconds,
            random_seed=self.config.random_seed,
            candidate_route_count=self.config.candidate_route_count,
            endpoint_samples_per_od=self.config.endpoint_samples_per_od,
        )

    def _validate_time_scope(self, interval_seconds: int) -> None:
        """Enforce the intentionally simplified one-day hourly model."""
        if interval_seconds != 3600:
            raise ValueError(
                "scenario_generator supports hourly demand only; "
                "set data_aggr_frequency = 3600."
            )
        duration = self.config.sim_end_time - self.config.sim_begin_time
        if self.config.sim_begin_time != 0 or duration != _SECONDS_PER_DAY:
            raise ValueError(
                "scenario_generator supports a single 24-hour day only; "
                "set sim_begin_time = 0 and sim_end_time = 86400."
            )

    # --- Stage 1 -------------------------------------------------------

    def _build_hourly_od_matrices(
        self,
        *,
        zones: dict[str, TrafficZone],
        router: EdgeRouter,
        settings: GenerationSettings,
    ) -> tuple[HourlyODMatrices, bool]:
        """Return hourly OD matrices and a flag indicating external-OD use."""
        if self.config.od_matrices_path:
            with open(self.config.od_matrices_path, "rb") as fh:
                raw = pickle.load(fh)
            matrices = validate_external_od_matrices(
                raw,
                list(zones.keys()),
                nb_hours=_NB_HOURS,
                interval_seconds=settings.interval_seconds,
            )
            return matrices, True

        return self._build_synthetic_hourly_od_matrices(zones, router, settings), False

    def _build_synthetic_hourly_od_matrices(
        self,
        zones: dict[str, TrafficZone],
        router: EdgeRouter,
        settings: GenerationSettings,
    ) -> HourlyODMatrices:
        """Generate synthetic OD matrices from a gravity model with optional commute overlay."""
        profile_cfg = self.config.temporal_profile_config

        # Two-peak temporal profile → integer hourly totals
        hourly_profile = generate_two_peak_profile(
            hours=_NB_HOURS,
            morning_peak=int(profile_cfg.get("morning_peak_hour", 8)),
            evening_peak=int(profile_cfg.get("evening_peak_hour", 17)),
            morning_amp=float(profile_cfg.get("morning_amplitude", 1.0)),
            evening_amp=float(profile_cfg.get("evening_amplitude", 1.2)),
            sigma=float(profile_cfg.get("sigma_hours", 2.5)),
        )
        hourly_totals = allocate_hourly_totals(
            hourly_profile,
            total_daily_demand=self.config.daily_total_demand,
            min_per_hour=int(profile_cfg.get("min_hourly_trips", 0)),
        )

        # Gravity OD share matrix (spatial distribution)
        impedance_matrix = estimate_zone_impedance_matrix(
            zones,
            router,
            endpoint_samples_per_od=settings.endpoint_samples_per_od,
            random_seed=settings.random_seed,
        )
        base_od_share = build_gravity_od_matrix(zones, impedance_matrix, beta=self.config.gravity_beta)

        # Optional commute overlay (directional AM/PM bias)
        hourly_od_shares = self._build_hourly_od_shares(zones, impedance_matrix, base_od_share)

        return generate_hourly_od_matrices(
            base_od_share,
            hourly_totals,
            interval_seconds=settings.interval_seconds,
            hourly_od_shares=hourly_od_shares,
        )

    def _build_hourly_od_shares(
        self,
        zones: dict[str, TrafficZone],
        impedance_matrix: pd.DataFrame,
        base_od_share: pd.DataFrame,
    ) -> dict[int, pd.DataFrame] | None:
        """Apply the optional commute layer; return None if no commute pattern is configured."""
        commute_pattern = parse_commute_pattern(
            self.config.commute_pattern_config,
            list(zones.keys()),
        )
        if commute_pattern is None:
            return None

        morning_share, evening_share = build_commute_od_matrices(zones, impedance_matrix, commute_pattern)
        return build_hourly_od_shares(
            base_od_share,
            nb_hours=_NB_HOURS,
            commute_pattern=commute_pattern,
            commute_morning_share=morning_share,
            commute_evening_share=evening_share,
        )

    # --- Stage 2 -------------------------------------------------------

    def _build_route_library(
        self,
        *,
        hourly_od_matrices: HourlyODMatrices,
        zones: dict[str, TrafficZone],
        router: EdgeRouter,
        settings: GenerationSettings,
        used_external_od: bool,
    ) -> RouteLibrary:
        """Build candidate routes and handle unroutable OD cells."""
        cache_name = f"candidate_routes_{Path(self.config.taz_path).stem}.pkl"
        cache_path = Path(self.config.output_path) / "cache" / cache_name
        route_library, unroutable_pairs = build_candidate_route_library(
            hourly_od_matrices,
            zones,
            router,
            k_paths=settings.candidate_route_count,
            endpoint_samples_per_od=settings.endpoint_samples_per_od,
            random_seed=settings.random_seed,
            cache_path=cache_path,
        )

        if not unroutable_pairs:
            return route_library

        if used_external_od and self.config.get_bool("external_od", "drop_unroutable_pairs", default=False):
            dropped = set(unroutable_pairs)
            for origin, destination in dropped:
                for matrix in hourly_od_matrices.values():
                    matrix.loc[origin, destination] = 0
            labels = ", ".join(f"{o}->{d}" for o, d in unroutable_pairs)
            print(f"Dropped unroutable external OD pairs: {labels}")
            return {pair: cands for pair, cands in route_library.items() if pair not in dropped}

        mode = "external" if used_external_od else "synthetic"
        labels = ", ".join(f"{o}->{d}" for o, d in unroutable_pairs)
        raise ValueError(f"Unroutable {mode} OD pairs: {labels}")

    # --- Output --------------------------------------------------------

    def _write_outputs(
        self,
        hourly_od_matrices: HourlyODMatrices,
        vehicles: list[tuple[float, list[str]]],
        interval_seconds: int,
    ) -> None:
        """Write all scenario-generator output files."""
        self.config.ensure_output_dir()

        write_taz_relations_xml(hourly_od_matrices, interval_seconds, self.config.outpath("out_od.xml"))
        with open(self.config.outpath("od_matrices.pkl"), "wb") as fh:
            pickle.dump(hourly_od_matrices, fh)

        vehicles_with_routes = [v for v in vehicles if v[1]]
        expected_total = int(sum(int(m.values.sum()) for m in hourly_od_matrices.values()))
        if expected_total != len(vehicles_with_routes):
            raise ValueError(
                f"Vehicle count mismatch: OD matrices imply {expected_total} vehicles "
                f"but {len(vehicles_with_routes)} routes were generated."
            )

        write_hourly_od_csvs(hourly_od_matrices, self.config.output_path)
        write_vehicle_def_to_file(vehicles_with_routes, self.config.outpath("routes.xml"), vType=None)

    @staticmethod
    def _log_generation_summary(
        hourly_od_matrices: HourlyODMatrices,
        vehicles: list[tuple[float, list[str]]],
    ) -> None:
        total = 0
        for begin in sorted(hourly_od_matrices):
            hour_sum = int(hourly_od_matrices[begin].values.sum())
            total += hour_sum
            print(f"  Hour {begin // 3600:02d}:00 – {hour_sum:5d} trips")
        print(f"Total trips (OD matrices) : {total}")
        print(f"Total vehicles written    : {len(vehicles)}")
