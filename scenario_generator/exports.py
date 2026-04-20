"""Export helpers for scenario-generator artifacts.

The generator emits three downstream-facing outputs:

- routes.xml – SUMO vehicle definitions with inline routes.
- out_od.xml – TAZ-level demand as SUMO tazRelation elements.
- odmat_hour*.csv – one CSV per hour for manual inspection / debugging.

All XML is written via xml.etree.ElementTree so special characters in
zone or edge ids are escaped automatically.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Sequence

import pandas as pd
import sumolib


def write_vehicle_def_to_file(
    vehicles_sorted: Sequence[tuple[float, Sequence[str]]],
    out_fname: str,
    vType: dict | None = None,
) -> None:
    """Write vehicle definitions to a SUMO routes.xml file.

    Each vehicle receives a sequential id veh_0, veh_1, ... and an
    inline <route> element listing the edge sequence. Vehicles with
    empty routes are silently skipped.

    Parameters
    ----------
    vehicles_sorted:
        (departure_time, edge_list) tuples in ascending departure order.
    out_fname:
        Destination file path.
    vType:
        Optional vehicle type dict with keys id, vClass, guiShape.  
        When provided a <vType> element is prepended and each vehicle references it.
    """
    root = ET.Element("routes")

    if vType is not None:
        ET.SubElement(
            root,
            "vType",
            attrib={
                "id": str(vType["id"]),
                "vClass": str(vType["vClass"]),
                "guiShape": str(vType["guiShape"]),
            },
        )

    veh_idx = 0
    for tick, route in vehicles_sorted:
        if not route:
            continue
        vehicle_el = ET.SubElement(
            root,
            "vehicle",
            attrib={"id": f"veh_{veh_idx}", "depart": str(tick)},
        )
        if vType is not None:
            vehicle_el.set("type", str(vType["id"]))
        ET.SubElement(vehicle_el, "route", attrib={"edges": " ".join(route)})
        veh_idx += 1

    with open(out_fname, "w", encoding="utf-8") as fd:
        sumolib.xml.writeHeader(fd, "$Id$", "routes")
        # Write all children individually; the header already opened <routes>.
        for child in root:
            fd.write("    " + ET.tostring(child, encoding="unicode") + "\n")
        fd.write("</routes>")


def write_taz_relations_xml(
    od_dict: dict[int, pd.DataFrame],
    aggr_freq: int,
    output_path: str,
) -> None:
    """Write hourly OD matrices as a SUMO tazRelation XML file.

    Only cells with strictly positive trip counts are emitted.
    """
    root = ET.Element("data")

    for begin in sorted(od_dict):
        df = od_dict[begin]
        end = float(begin) + aggr_freq
        interval_el = ET.SubElement(
            root,
            "interval",
            attrib={
                "id": "DEFAULT_VEHTYPE",
                "begin": f"{float(begin):.2f}",
                "end": f"{end:.2f}",
            },
        )

        for from_id in df.index:
            for to_id in df.columns:
                count = int(df.loc[from_id, to_id])
                if count <= 0:
                    continue
                ET.SubElement(
                    interval_el,
                    "tazRelation",
                    attrib={"from": str(from_id), "to": str(to_id), "count": str(count)},
                )

    ET.ElementTree(root).write(output_path, encoding="utf-8", xml_declaration=True)


def write_hourly_od_csvs(od_matrices: dict[int, pd.DataFrame], out_dir: str) -> None:
    """Persist one semicolon-delimited CSV per hourly OD matrix."""
    target_dir = Path(out_dir) / "trans_mat_definition"
    target_dir.mkdir(parents=True, exist_ok=True)

    for begin in sorted(od_matrices):
        od_matrices[begin].to_csv(
            target_dir / f"odmat_hour{begin}.csv",
            sep=";",
        )
