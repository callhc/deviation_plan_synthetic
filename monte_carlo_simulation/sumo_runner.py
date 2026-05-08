"""SUMO simulation runner for Monte Carlo iterations.

Handles routing via duarouter (with randomised edge weights), writing
SUMO configuration files, executing the sumo binary, and parsing the
resulting tripinfo XML for aggregate performance metrics.
"""

import statistics
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict


def run_duarouter(
    trips_file: Path,
    net_xml: Path,
    output_routes: Path,
    seed: int,
    random_factor: float = 1.2,
) -> Path:
    """Route a trips.xml file with duarouter using randomised edge weights.

    Each call draws a fresh set of per-edge cost multipliers from
    Uniform[1.0, random_factor] via --weights.random-factor, so
    every iteration produces a different set of routes from the same OD
    demand.  This is the source of stochasticity in the Monte Carlo loop.

    Unroutable trips are skipped silently with --ignore-errors so that a
    single disconnected OD pair does not abort the entire run.

    The .alt.xml file that duarouter always writes alongside the main
    output is deleted after a successful run — it is not used downstream.

    Parameters
    ----------
    trips_file:
        Path to the baseline trips.xml produced by scenario_generator.
    net_xml:
        Path to the SUMO network file.
    output_routes:
        Destination path for the routed routes.xml file.
    seed:
        Random seed passed to duarouter; also used as the run identifier.
    random_factor:
        Upper bound for the per-edge cost multiplier drawn by duarouter.
        Must be ≥ 1.0.  The default of 1.2 scales each edge cost by a
        value in [1.0, 1.2], modelling day-to-day variability in perceived
        travel costs.

    Returns
    -------
    Path
        Path to the written routes.xml file (*output_routes*).

    Raises
    ------
    RuntimeError
        If duarouter exits with a non-zero return code.
    """
    output_routes.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        [
            "duarouter",
            "--net-file", str(net_xml.resolve()),
            "--route-files", str(trips_file.resolve()),
            "--output-file", str(output_routes),
            "--seed", str(seed),
            "--weights.random-factor", str(random_factor),
            "--no-warnings",
            "--ignore-errors",
            "--no-step-log",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"duarouter failed (seed={seed}): {err}")

    # duarouter always writes an .alt.xml alongside the main output; remove it.
    alt_file = output_routes.parent / (output_routes.stem + ".alt.xml")
    if alt_file.exists():
        alt_file.unlink()

    return output_routes


def run_sumo(
    net_xml: Path,
    routes_file: Path,
    output_dir: Path,
    seed: int,
    begin: float,
    end: float,
) -> Path:
    """Write a SUMO config file, execute sumo, and return the tripinfo path.

    Parameters
    ----------
    net_xml:
        Path to the SUMO network file.
    routes_file:
        Path to the routes (or trips) file for this run.
    output_dir:
        Directory where the config and tripinfo output are written.
    seed:
        SUMO random seed for this iteration.
    begin:
        Simulation start time in seconds.
    end:
        Simulation end time in seconds.

    Returns
    -------
    Path
        Path to the tripinfo.xml output file.

    Raises
    ------
    RuntimeError
        If sumo exits with a non-zero return code.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    tripinfo = output_dir / "tripinfo.xml"

    cfg = f"""<?xml version="1.0"?>
<configuration>
    <input>
        <net-file value="{net_xml.resolve()}"/>
        <route-files value="{routes_file.resolve()}"/>
    </input>
    <time>
        <begin value="{begin}"/>
        <end value="{end}"/>
    </time>
    <output>
        <tripinfo-output value="{tripinfo.resolve()}"/>
    </output>
    <random_number>
        <seed value="{seed}"/>
    </random_number>
    <report>
        <no-warnings value="true"/>
        <no-step-log value="true"/>
        <tripinfo-output.write-unfinished value="true"/>
    </report>
</configuration>"""

    cfg_file = output_dir / "sim.sumocfg"
    cfg_file.write_text(cfg)

    proc = subprocess.run(
        ["sumo", "-c", str(cfg_file)], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"sumo failed (seed={seed}): {err}")

    return tripinfo


def _is_completed_trip(trip_elem: ET.Element) -> bool:
    """Return True only for trips that arrived within the simulation window.

    SUMO writes partial records for unfinished vehicles when
    tripinfo-output.write-unfinished=true.  These records have either a
    negative arrival time, no arrival attribute at all, or a non-empty
    vaporized attribute.

    Parameters
    ----------
    trip_elem:
        A <tripinfo> XML element from a SUMO tripinfo.xml file.

    Returns
    -------
    bool
        True if the vehicle completed its trip within the simulation window.
    """
    if trip_elem.get("vaporized"):
        return False
    arrival = trip_elem.get("arrival")
    if arrival is None:
        return False
    try:
        return float(arrival) >= 0
    except (TypeError, ValueError):
        return False


def calc_metrics(tripinfo: Path) -> Dict[str, float]:
    """Parse a SUMO tripinfo.xml file and return aggregate performance metrics.

    Parameters
    ----------
    tripinfo:
        Path to the tripinfo.xml file produced by SUMO.

    Returns
    -------
    dict[str, float]
        Keys:

        delay
            Total vehicle-hours of SUMO timeLoss for completed trips only.
        travel_time
            Mean trip duration in minutes for completed trips only.
        completed
            Number of trips that arrived within the simulation window.
        unfinished
            Number of partial records written by SUMO for vehicles that did
            not arrive (vaporized, still travelling, or teleported out).
    """
    if not tripinfo.exists():
        return {"delay": 0.0, "travel_time": 0.0, "completed": 0, "unfinished": 0}

    all_trips = ET.parse(str(tripinfo)).getroot().findall("tripinfo")
    if not all_trips:
        return {"delay": 0.0, "travel_time": 0.0, "completed": 0, "unfinished": 0}

    finished = [t for t in all_trips if _is_completed_trip(t)]
    unfinished_count = len(all_trips) - len(finished)

    if not finished:
        return {"delay": 0.0, "travel_time": 0.0, "completed": 0, "unfinished": unfinished_count}

    durations = [float(t.get("duration", 0)) for t in finished]
    time_loss = [float(t.get("timeLoss", 0)) for t in finished]

    return {
        "delay": sum(time_loss) / 3600,
        "travel_time": statistics.mean(durations) / 60,
        "completed": len(finished),
        "unfinished": unfinished_count,
    }
