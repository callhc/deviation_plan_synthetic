"""Visualisation utilities for scenario-generator outputs.

Each public function accepts the data structures produced by the three
pipeline stages and returns a matplotlib.figure.Figure.  All
functions follow the same convention:

- They create and return their own Figure by default.
- Pass ax= (or axes=) to embed the plot into an existing layout.
- Call save_all to write every plot to disk in one go.

Available plots
---------------

   * - plot_hourly_demand
     - Bar chart of total trips per hour (the temporal profile).
   * - plot_od_heatmap
     - Heatmap of the aggregated daily OD matrix.
   * - plot_departure_histogram
     - Fine-grained histogram of vehicle departure times.
   * - plot_cumulative_departures
     - Empirical CDF of departure times over the simulation day.
   * - plot_zone_activity
     - Origins vs. destinations per zone (grouped bar chart).
   * - plot_route_lengths
     - Distribution of selected-route lengths (in number of edges).
   * - plot_candidate_costs
     - Box plot of candidate-route costs per OD pair (requires route library).
   * - plot_overview
     - Single figure combining the six core plots.
   * - save_all
     - Save every plot as an individual file.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Mapping

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

# Type aliases matching the rest of the package
HourlyODMatrices = Mapping[int, pd.DataFrame]
Vehicles = list[tuple[float, list[str]]]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_BLUE = "#2E6FBA"
_ORANGE = "#E07B39"
_GREY = "#AAAAAA"


def _apply_thesis_style(ax: plt.Axes) -> None:
    """Apply a clean, publication-friendly style to an Axes object."""
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(labelsize=9)
    ax.title.set_fontsize(11)
    ax.xaxis.label.set_fontsize(10)
    ax.yaxis.label.set_fontsize(10)


def _daily_od_matrix(hourly_od_matrices: HourlyODMatrices) -> pd.DataFrame:
    """Sum all hourly matrices into one daily matrix."""
    matrices = list(hourly_od_matrices.values())
    total = matrices[0].copy(deep=True).astype(float)
    for m in matrices[1:]:
        total += m.astype(float)
    return total


def _hour_labels(begins: list[int], interval: int = 3600) -> list[str]:
    """Convert second-based begin times to 'HH:00' strings."""
    return [f"{b // interval:02d}:00" for b in begins]


# ---------------------------------------------------------------------------
# Plot 1 — Hourly demand bar chart
# ---------------------------------------------------------------------------

def plot_hourly_demand(
    hourly_od_matrices: HourlyODMatrices,
    *,
    ax: plt.Axes | None = None,
    title: str = "Hourly Trip Distribution",
) -> plt.Figure:
    """Bar chart showing the total number of trips generated each hour.

    This plot directly illustrates the two-peak temporal demand profile
    built in Stage 1 (morning peak around 08:00, evening peak around 17:00).

    Parameters
    ----------
    hourly_od_matrices:
        The 24-hour OD matrices produced by Stage 1.
    ax:
        Optional existing Axes to draw into.  A new figure is created if
        omitted.
    title:
        Axes title string.
    """
    begins = sorted(hourly_od_matrices)
    counts = [int(hourly_od_matrices[b].values.sum()) for b in begins]
    labels = _hour_labels(begins)

    own_fig = ax is None
    if own_fig:
        fig, ax = plt.subplots(figsize=(10, 4))
    else:
        fig = ax.get_figure()

    bars = ax.bar(range(len(begins)), counts, color=_BLUE, width=0.7, zorder=3)
    ax.set_xticks(range(len(begins)))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_xlabel("Hour")
    ax.set_ylabel("Number of trips")
    ax.set_title(title)
    ax.yaxis.grid(True, linestyle="--", linewidth=0.5, alpha=0.7, zorder=0)
    ax.set_axisbelow(True)

    # Annotate peak hours
    peak_idx = int(np.argmax(counts))
    ax.bar(peak_idx, counts[peak_idx], color=_ORANGE, width=0.7, zorder=4)

    _apply_thesis_style(ax)
    if own_fig:
        fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Plot 2 — Daily OD heatmap
# ---------------------------------------------------------------------------

def plot_od_heatmap(
    hourly_od_matrices: HourlyODMatrices,
    *,
    ax: plt.Axes | None = None,
    title: str = "Daily OD Demand (total trips)",
    annotate: bool = True,
) -> plt.Figure:
    """Heatmap of the total daily OD matrix summed across all 24 hours.

    Rows are origin zones and columns are destination zones.  Cell colour
    encodes trip count; the diagonal is always zero (no intra-zonal demand).

    Parameters
    ----------
    annotate:
        Write the integer trip count inside each cell.  Disable for large
        zone sets where cell labels become illegible.
    """
    daily = _daily_od_matrix(hourly_od_matrices)
    zone_ids = list(daily.index)
    values = daily.to_numpy(dtype=float)

    own_fig = ax is None
    if own_fig:
        n = len(zone_ids)
        size = max(4, min(10, n * 0.8))
        fig, ax = plt.subplots(figsize=(size + 1, size))
    else:
        fig = ax.get_figure()

    im = ax.imshow(values, aspect="auto", cmap="Blues", interpolation="nearest")
    plt.colorbar(im, ax=ax, label="Trips / day", shrink=0.8)

    ax.set_xticks(range(len(zone_ids)))
    ax.set_yticks(range(len(zone_ids)))
    ax.set_xticklabels(zone_ids, rotation=45, ha="right", fontsize=8)
    ax.set_yticklabels(zone_ids, fontsize=8)
    ax.set_xlabel("Destination zone")
    ax.set_ylabel("Origin zone")
    ax.set_title(title)

    if annotate and len(zone_ids) <= 20:
        thresh = values.max() / 2.0
        for i in range(len(zone_ids)):
            for j in range(len(zone_ids)):
                v = int(values[i, j])
                if v == 0:
                    continue
                color = "white" if values[i, j] > thresh else "black"
                ax.text(j, i, str(v), ha="center", va="center", fontsize=7, color=color)

    _apply_thesis_style(ax)
    if own_fig:
        fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Plot 3 — Departure-time histogram
# ---------------------------------------------------------------------------

def plot_departure_histogram(
    vehicles: Vehicles,
    *,
    bin_minutes: int = 15,
    ax: plt.Axes | None = None,
    title: str = "Departure Time Distribution",
) -> plt.Figure:
    """Histogram of vehicle departure times at sub-hourly resolution.

    Parameters
    ----------
    bin_minutes:
        Width of each time bin in minutes.  Default 15 min gives 96 bins
        over 24 hours.
    """
    if not vehicles:
        raise ValueError("No vehicles to plot.")

    departures = np.array([v[0] for v in vehicles])
    bin_width = bin_minutes * 60
    bins = np.arange(0, 86400 + bin_width, bin_width)

    own_fig = ax is None
    if own_fig:
        fig, ax = plt.subplots(figsize=(10, 4))
    else:
        fig = ax.get_figure()

    ax.hist(departures, bins=bins, color=_BLUE, edgecolor="none", alpha=0.85, zorder=3)
    ax.set_xlabel("Departure time (hh:mm)")
    ax.set_ylabel(f"Vehicles / {bin_minutes} min")
    ax.set_title(title)

    tick_positions = np.arange(0, 86401, 3600)
    tick_labels = [f"{int(t // 3600):02d}:00" for t in tick_positions]
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels, rotation=45, ha="right", fontsize=8)
    ax.set_xlim(0, 86400)
    ax.yaxis.grid(True, linestyle="--", linewidth=0.5, alpha=0.7, zorder=0)
    ax.set_axisbelow(True)

    _apply_thesis_style(ax)
    if own_fig:
        fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Plot 4 — Cumulative departure CDF
# ---------------------------------------------------------------------------

def plot_cumulative_departures(
    vehicles: Vehicles,
    *,
    ax: plt.Axes | None = None,
    title: str = "Cumulative Departures",
) -> plt.Figure:
    """Empirical CDF of vehicle departure times over the simulation day.

    The S-curve shape shows at a glance where demand accelerates (steep
    segments) and plateaus (flat segments).
    """
    if not vehicles:
        raise ValueError("No vehicles to plot.")

    departures = np.sort([v[0] for v in vehicles])
    cdf = np.arange(1, len(departures) + 1) / len(departures) * 100

    own_fig = ax is None
    if own_fig:
        fig, ax = plt.subplots(figsize=(8, 4))
    else:
        fig = ax.get_figure()

    ax.plot(departures, cdf, color=_BLUE, linewidth=1.5)
    ax.fill_between(departures, cdf, alpha=0.12, color=_BLUE)

    tick_positions = np.arange(0, 86401, 3600)
    tick_labels = [f"{int(t // 3600):02d}:00" for t in tick_positions]
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels, rotation=45, ha="right", fontsize=8)
    ax.set_xlim(0, 86400)
    ax.set_ylim(0, 100)
    ax.set_xlabel("Time of day")
    ax.set_ylabel("Cumulative departures (%)")
    ax.set_title(title)
    ax.yaxis.grid(True, linestyle="--", linewidth=0.5, alpha=0.7)
    ax.set_axisbelow(True)

    _apply_thesis_style(ax)
    if own_fig:
        fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Plot 5 — Zone activity (origins vs destinations)
# ---------------------------------------------------------------------------

def plot_zone_activity(
    hourly_od_matrices: HourlyODMatrices,
    *,
    ax: plt.Axes | None = None,
    title: str = "Zone Activity: Origins vs. Destinations",
) -> plt.Figure:
    """Grouped bar chart of total daily trip origins and destinations per zone.

    Origins are the row sums and destinations are the column sums of the
    aggregated daily OD matrix.  A zone that generates far more trips than
    it attracts is predominantly residential; the reverse suggests an
    employment centre.
    """
    daily = _daily_od_matrix(hourly_od_matrices)
    zone_ids = list(daily.index)
    origins = daily.sum(axis=1).values          # row sums  (outbound)
    destinations = daily.sum(axis=0).values     # col sums  (inbound)

    x = np.arange(len(zone_ids))
    width = 0.38

    own_fig = ax is None
    if own_fig:
        fig, ax = plt.subplots(figsize=(max(6, len(zone_ids) * 0.9), 4))
    else:
        fig = ax.get_figure()

    ax.bar(x - width / 2, origins,      width, label="Origins",      color=_BLUE,   zorder=3)
    ax.bar(x + width / 2, destinations, width, label="Destinations",  color=_ORANGE, zorder=3)

    ax.set_xticks(x)
    ax.set_xticklabels(zone_ids, rotation=45, ha="right", fontsize=8)
    ax.set_xlabel("Zone")
    ax.set_ylabel("Total daily trips")
    ax.set_title(title)
    ax.legend(fontsize=9)
    ax.yaxis.grid(True, linestyle="--", linewidth=0.5, alpha=0.7, zorder=0)
    ax.set_axisbelow(True)

    _apply_thesis_style(ax)
    if own_fig:
        fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Plot 6 — Route length distribution (edge count)
# ---------------------------------------------------------------------------

def plot_route_lengths(
    vehicles: Vehicles,
    *,
    ax: plt.Axes | None = None,
    title: str = "Distribution of Route Lengths",
) -> plt.Figure:
    """Histogram of the number of edges in each selected vehicle route.

    Edge count is a simple proxy for route complexity and spatial extent.
    Short routes (few edges) correspond to intra-district trips; long routes
    span multiple zones.
    """
    if not vehicles:
        raise ValueError("No vehicles to plot.")

    lengths = np.array([len(v[1]) for v in vehicles if v[1]])
    if lengths.size == 0:
        raise ValueError("All vehicles have empty routes.")

    own_fig = ax is None
    if own_fig:
        fig, ax = plt.subplots(figsize=(7, 4))
    else:
        fig = ax.get_figure()

    bins = np.arange(lengths.min(), lengths.max() + 2) - 0.5
    ax.hist(lengths, bins=bins, color=_BLUE, edgecolor="white", linewidth=0.4, zorder=3)
    ax.axvline(float(np.mean(lengths)), color=_ORANGE, linewidth=1.5,
               linestyle="--", label=f"Mean = {np.mean(lengths):.1f} edges")
    ax.axvline(float(np.median(lengths)), color=_GREY, linewidth=1.5,
               linestyle=":", label=f"Median = {np.median(lengths):.0f} edges")

    ax.set_xlabel("Route length (number of edges)")
    ax.set_ylabel("Number of vehicles")
    ax.set_title(title)
    ax.legend(fontsize=9)
    ax.yaxis.grid(True, linestyle="--", linewidth=0.5, alpha=0.7, zorder=0)
    ax.set_axisbelow(True)

    _apply_thesis_style(ax)
    if own_fig:
        fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Plot 7 — Candidate route costs (requires route library)
# ---------------------------------------------------------------------------

def plot_candidate_costs(
    route_library: Mapping[tuple[str, str], list],
    *,
    ax: plt.Axes | None = None,
    title: str = "Candidate Route Costs per OD Pair",
    max_pairs: int = 30,
) -> plt.Figure:
    """Box plot of candidate route costs (metres) for each active OD pair.

    Each box summarises the spread of the *k* pre-computed candidate lengths
    for one OD pair.  Pairs are sorted by their median cost.  When more than
    ``max_pairs`` pairs are present only the top ``max_pairs`` by total demand
    are shown.

    Parameters
    ----------
    route_library:
        The candidate route library produced by Stage 2.
    max_pairs:
        Maximum number of OD pairs to display (avoids an unreadable plot for
        large networks).
    """
    if not route_library:
        raise ValueError("route_library is empty.")

    # Build a list of (label, costs) sorted by median cost
    data = []
    for (origin, destination), candidates in route_library.items():
        costs = [c.cost for c in candidates]
        label = f"{origin}→{destination}"
        data.append((label, costs, float(np.median(costs))))

    data.sort(key=lambda x: x[2])
    if len(data) > max_pairs:
        data = data[:max_pairs]

    labels = [d[0] for d in data]
    cost_lists = [d[1] for d in data]

    own_fig = ax is None
    if own_fig:
        fig, ax = plt.subplots(figsize=(max(6, len(labels) * 0.55), 5))
    else:
        fig = ax.get_figure()

    bp = ax.boxplot(
        cost_lists,
        patch_artist=True,
        medianprops=dict(color=_ORANGE, linewidth=1.8),
        boxprops=dict(facecolor=_BLUE + "55", edgecolor=_BLUE),
        whiskerprops=dict(color=_BLUE),
        capprops=dict(color=_BLUE),
        flierprops=dict(marker="o", markersize=3, color=_GREY, alpha=0.6),
    )

    ax.set_xticks(range(1, len(labels) + 1))
    ax.set_xticklabels(labels, rotation=60, ha="right", fontsize=7)
    ax.set_ylabel("Route cost (m)")
    ax.set_title(title)
    ax.yaxis.grid(True, linestyle="--", linewidth=0.5, alpha=0.7, zorder=0)
    ax.set_axisbelow(True)

    _apply_thesis_style(ax)
    if own_fig:
        fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Combined overview figure
# ---------------------------------------------------------------------------

def plot_overview(
    hourly_od_matrices: HourlyODMatrices,
    vehicles: Vehicles,
    *,
    route_library: Mapping[tuple[str, str], list] | None = None,
    title: str = "Scenario Generator — Overview",
) -> plt.Figure:
    """Single figure with all six core plots arranged in a 3 × 2 grid.

    Provides a one-page snapshot of the generated scenario: temporal
    distribution (top row), spatial distribution (middle row), and
    route/vehicle characteristics (bottom row).

    Parameters
    ----------
    route_library:
        Optional; enables the candidate-cost box plot instead of
        the route-length histogram in the bottom-right panel.
    """
    fig = plt.figure(figsize=(15, 11))
    fig.suptitle(title, fontsize=13, fontweight="bold", y=0.98)

    axes = fig.subplot_mosaic(
        [["hourly",     "departure"],
         ["heatmap",    "cdf"],
         ["zones",      "routes"]],
        gridspec_kw={"hspace": 0.55, "wspace": 0.35},
    )

    plot_hourly_demand(hourly_od_matrices,    ax=axes["hourly"])
    plot_departure_histogram(vehicles,         ax=axes["departure"])
    plot_od_heatmap(hourly_od_matrices,        ax=axes["heatmap"], annotate=False)
    plot_cumulative_departures(vehicles,       ax=axes["cdf"])
    plot_zone_activity(hourly_od_matrices,     ax=axes["zones"])

    if route_library is not None:
        plot_candidate_costs(route_library,    ax=axes["routes"])
    else:
        plot_route_lengths(vehicles,           ax=axes["routes"])

    return fig


# ---------------------------------------------------------------------------
# Save all plots to disk
# ---------------------------------------------------------------------------

def save_all(
    hourly_od_matrices: HourlyODMatrices,
    vehicles: Vehicles,
    output_dir: str | Path,
    *,
    route_library: Mapping[tuple[str, str], list] | None = None,
    fmt: str = "pdf",
    dpi: int = 150,
) -> None:
    """Generate and save every plot as an individual file.

    Files are written to *output_dir* with the naming scheme
    ``plot_<name>.<fmt>``.  A combined overview file
    ``plot_overview.<fmt>`` is also written.

    Parameters
    ----------
    output_dir:
        Directory where the plots will be saved (created if absent).
    route_library:
        Optional route library; enables the candidate-cost box plot.
    fmt:
        Matplotlib-supported format string: ``"pdf"``, ``"png"``, ``"svg"``.
    dpi:
        Resolution for raster formats (ignored for PDF/SVG).
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    individual_plots = [
        ("hourly_demand",        lambda: plot_hourly_demand(hourly_od_matrices)),
        ("od_heatmap",           lambda: plot_od_heatmap(hourly_od_matrices)),
        ("departure_histogram",  lambda: plot_departure_histogram(vehicles)),
        ("cumulative_departures",lambda: plot_cumulative_departures(vehicles)),
        ("zone_activity",        lambda: plot_zone_activity(hourly_od_matrices)),
        ("route_lengths",        lambda: plot_route_lengths(vehicles)),
    ]
    if route_library is not None:
        individual_plots.append(
            ("candidate_costs", lambda: plot_candidate_costs(route_library))
        )

    for name, build in individual_plots:
        fig = build()
        fig.savefig(out / f"plot_{name}.{fmt}", dpi=dpi, bbox_inches="tight")
        plt.close(fig)

    fig_ov = plot_overview(hourly_od_matrices, vehicles, route_library=route_library)
    fig_ov.savefig(out / f"plot_overview.{fmt}", dpi=dpi, bbox_inches="tight")
    plt.close(fig_ov)

    print(f"Saved {len(individual_plots) + 1} plot(s) to '{out}'.")


# ---------------------------------------------------------------------------
# Plot 8 — TAZ Map visualization
# ---------------------------------------------------------------------------

def plot_taz_map(
    taz_file: str | Path,
    *,
    ax: plt.Axes | None = None,
    title: str = "Traffic Analysis Zones (TAZ)",
) -> plt.Figure:
    """Plot the spatial layout of TAZs from their XML definition.

    Parses the TAZ shape and color from the XML file to construct
    a 2D spatial polygon map.
    """
    tree = ET.parse(taz_file)
    root = tree.getroot()

    own_fig = ax is None
    if own_fig:
        fig, ax = plt.subplots(figsize=(8, 8))
    else:
        fig = ax.get_figure()

    for taz in root.findall("taz"):
        taz_id = taz.get("id")
        shape_str = taz.get("shape")
        color_str = taz.get("color", "100,100,100")

        if not shape_str or not taz_id:
            continue

        # Parse shape: "x,y x,y x,y"
        points = []
        for pt in shape_str.split():
            x, y = map(float, pt.split(","))
            points.append((x, y))

        # Parse color: "r,g,b"
        try:
            r, g, b = map(int, color_str.split(","))
            color = (r / 255.0, g / 255.0, b / 255.0)
        except ValueError:
            color = (0.5, 0.5, 0.5)

        poly = mpatches.Polygon(points, facecolor=color, edgecolor="black", alpha=0.5, linewidth=1.0)
        ax.add_patch(poly)

        # Centroid for text
        cx = sum(p[0] for p in points) / len(points)
        cy = sum(p[1] for p in points) / len(points)
        ax.text(cx, cy, taz_id, ha="center", va="center", fontsize=9, fontweight="bold", color="black")

    ax.autoscale()
    ax.set_aspect("equal", "box")
    ax.set_xlabel("X (m)")
    ax.set_ylabel("Y (m)")
    ax.set_title(title)
    ax.yaxis.grid(True, linestyle="--", linewidth=0.5, alpha=0.7, zorder=0)
    ax.xaxis.grid(True, linestyle="--", linewidth=0.5, alpha=0.7, zorder=0)

    _apply_thesis_style(ax)
    if own_fig:
        fig.tight_layout()
    return fig
