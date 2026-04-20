# Scenario Generator – Lineage and Design Rationale

This document maps the new `scenario_generator/` package back to the original
`syntheticscenariogenerator-master` codebase.  For each design element it
states (i) what was **inspired** from the original, (ii) **how** it was
re-implemented, and (iii) **why** the change was made.  It is written so
that paragraphs can be reused directly in the thesis motivation section.

---

## 1. Scope: from "simulation pipeline" to "demand generator"

**Original.**  `DatasetGenerator.py` orchestrates six phases in a single
procedural script:

| Phase | Function | Responsibility |
|-------|----------|----------------|
| 0 | `generate_roadworks` | Close edges / write rerouters (OSIRIS, random, or list) |
| 1 | `generate_traffic` → `generate_realistic_traffic` | OD matrix + vehicle generation |
| 2 | `generate_virtual_sensors` | Place induction loops on edges |
| 3 | `generate_sumo_additional_files` | Write edgedata additional files |
| 4 | `run_simulation` | Invoke `sumo` via `os.system` (with/without roadworks) |
| 5 | `process_simulation_output` | Parse detector XML, FCD, edgedata into CSVs |

**New.**  Only Phase 1 remains inside the generator.  Phases 0 and 2–5 are
out of scope: they concern *scenario execution and observation* and are
handled elsewhere in the thesis pipeline.

**Why the change.**
* **Separation of concerns.** Demand generation, sensor placement,
  road-closure generation, simulation execution, and output post-processing
  are logically independent.  Bundling them into one script made each step
  untestable in isolation and impossible to swap out.
* **Reusability.** The new generator produces a plain `routes.xml`
  plus OD pickles.  Any downstream stage (simulation, analysis, counter-
  factual experiments) consumes those files without knowing about sensors
  or roadworks.
* **Thesis focus.** The contribution evaluated in the thesis is the
  *synthetic demand model*, not the SUMO wrapper.  Keeping only the
  relevant module shortens the deliverable and aligns the code with the
  text.

---

## 2. Pipeline architecture: monolith → three stages

**Original.**  Two large files do the work:

* `DatasetGenerator.py` (~750 lines) – procedural orchestration.
* `utils/PathUtil.py` (~650 lines) – region-path discovery, OD allocation,
  vehicle writing, SUMO invocation, FCD/detector helpers.

The demand routine `allocate_paths` (PathUtil.py, lines 501–563) mixes
three concerns in one function: it builds the structural OD, draws
departure times, and selects routes.

**New.**  Eight focused modules, each with a single responsibility and a
module-level docstring:

```
generator.py             # CLI entry point
traffic_generator.py     # Pipeline orchestration (Stages 1–3)
od_model.py              # Gravity model + temporal profile + commute overlay
zones.py                 # TAZ parsing + edge sampling weights
route_library.py         # Edge-expanded graph + k-shortest paths + cache
vehicle_sampler.py       # Departure-time and route-choice sampling
exports.py               # routes.xml / out_od.xml / CSV writers
config_manager.py        # Typed TOML accessors
```

**Why the change.**
* Maps cleanly onto the **classical four-step model** vocabulary
  (generation → distribution → assignment → choice) which the thesis
  cites, so each module corresponds to a section of the write-up.
* Each stage produces a well-typed intermediate artefact
  (OD matrices → candidate routes → vehicles), enabling unit testing and
  visual inspection between stages.

---

## 3. Temporal demand profile: cubic spline of hand-picked factors → two-Gaussian kernel

**Original.**  `generate_synthetic_traffic` in `DatasetGenerator.py`
(lines 177–199) fits a **cubic spline** through a 9-point hand-authored
traffic profile and adds a hard-coded ±500-vehicle jitter:

```python
ins_rates_fact = [0.1, 0.3, 0.67, 1, 0.7, 0.6, 0.7, 0.9, 0.4]
ins_rate_perturbation = [random.uniform(-500, 500) for _ in ...]  # "hard-coded... ugly"
cs = CubicSpline(x, ins_rates)
```

Alongside this, `generate_two_peak_profile` (`PathUtil.py`, lines 435–446)
already exists but is used only by the OD routine and is never integrated
with the vehicle insertion logic.

**New (`od_model.py`, `generate_two_peak_profile`, `gaussian_profile`).**
The two-Gaussian kernel is kept as-is mathematically – the original
expression for `morning + evening` appears almost verbatim in the new
`generate_two_peak_profile`.  The cubic-spline path and the ±500 jitter
are removed.  A new helper `allocate_hourly_totals` converts the
continuous profile into **exact integer hourly totals** using the
largest-remainder (Hamilton) method.

**Why the change.**
* The spline profile required nine magic numbers, was not interpretable,
  and the author's own comment ("hard-coded... ugly") flagged the problem.
* The Gaussian form has only four interpretable parameters
  (`morning_peak_hour`, `evening_peak_hour`, amplitudes, `sigma`) that can
  be read directly from traffic-census literature.
* Largest-remainder rounding guarantees that the sum of hourly totals
  equals the configured `daily_total` exactly, which the original spline
  implementation could not guarantee.  This is essential for downstream
  validation (`expected_total == len(vehicles_with_routes)` in the new
  `_write_outputs`).

---

## 4. Spatial OD distribution: "contribution matrix" → gravity model

**Original (`PathUtil.py`, `generate_base_od_from_contribution`, lines
449–475, and `generate_hourly_od_demand`, lines 478–498).**
The OD share matrix is derived from a **contribution matrix** `C` in which
each row is a discovered region-path and each column a region.  The
structural OD is then computed as

```python
W = C.T.dot(C)          # regions co-appearing in paths
```

i.e. two regions have high OD demand iff many discovered paths happen to
contain both of them.  Entries for OD pairs absent from `od_pairs` are
zeroed, the diagonal is removed, and the matrix is row-normalised.

**New (`od_model.build_gravity_od_matrix`).**  A textbook singly-
constrained gravity model:

```
w_ij = m_i · m_j · exp(−β · c_ij),  i ≠ j
S = W / Σ W                         # share matrix
```

where `m_i` is the zone mass (Section 5), `c_ij` is a TAZ-to-TAZ travel
cost (Section 6), and `β` is a distance-decay parameter.

**Why the change.**
* **Circular causality.** In the original, OD demand is an artefact of
  which paths were discovered, and paths are discovered *before* any
  demand exists.  This makes calibration meaningless: changing
  `min_reg_path_len` or `num_edge_paths_per_reg_path` silently changes
  the OD matrix.
* **No distance semantics.** Two regions connected by a single long
  motorway produce exactly the same structural weight as two adjacent
  small regions sharing many short paths, because only *co-occurrence* is
  counted.  The gravity model is the standard way to encode the empirical
  observation that flows decrease with travel cost.
* **Parameterisation.** The gravity `β` has a clear interpretation
  (the higher it is, the more short trips dominate) and can be calibrated
  against empirical trip-length distributions.  `C.T @ C` has no such
  knob.
* **Literature alignment.** The gravity model is the canonical spatial
  interaction model in transport planning (Ortúzar & Willumsen, 2011),
  which the thesis can cite directly.

---

## 5. Zone mass: region-path co-occurrence → capacity-weighted edge length

**Original.**  Zones ("regions"/TAZs) have no explicit mass.  Their
influence on demand is purely emergent from how many paths touch them
(Section 4).  `initialize_road_net_mappings` (PathUtil.py, lines 155–189)
simply maps each edge to a region polygon.

**New (`zones.TrafficZone`, `zones._edge_weight`).**  Each zone carries a
scalar mass

```
mass_i = Σ_{e ∈ zone_i} length(e) × lanes(e)
```

The same per-edge weight vector doubles as a probability distribution for
sampling concrete origin/destination edges.

**Why the change.**
* Provides the gravity model with a meaningful `m_i` that approximates
  zone capacity / attractivity in the absence of land-use data.
* Uses information already present in the SUMO network (no external
  data), unlike population-based masses which would require additional
  GIS inputs.
* Makes arterial-heavy zones generate and attract more demand than
  residential side-streets, matching the empirical trip-generation role
  of road hierarchy.

---

## 6. Route generation: region-adjacency graph → edge-expanded road graph

**Original.**  Routing is two-level and sensor-aware:

1. `evaluate_neighbor_regions` (PathUtil.py, lines 64–111) builds a
   region adjacency graph via `build_region_adjacency_graph`
   (lines 41–61), an `O(R²)` scan over every region pair.
2. `initialize_reg_paths` (lines 192–225) enumerates all shortest
   *region sequences* for OD pairs using `networkx.all_shortest_paths`,
   then
3. `get_route` (lines 114–152) turns each region sequence into an edge
   path by picking one "via" edge per region, biased toward sensored
   lanes (`prob_choice_sensored_link`, `sensors_to_edge_mapping`) and
   stitching together shortest paths via `net.getShortestPath(..., vClass="private")`.

Parameters tied to this approach: `min_reg_path_len`,
`num_edge_paths_per_reg_path`, `prob_choice_sensored_link`,
`allows_path_with_no_sensors`, `wip_path_dir`.

**New (`route_library.EdgeRouter`, `build_candidate_route_library`).**  A
single **edge-expanded directed graph**: every drivable non-internal
edge is a node, and arcs connect edge `u` → edge `v` whenever `v` is
reachable via a SUMO connection, weighted by the length of `v`.  For
each active OD pair (those with positive demand) the generator:

1. samples `endpoint_samples_per_od` (origin_edge, destination_edge)
   pairs proportional to edge capacity,
2. computes up to `k_paths` **shortest simple paths** with Yen's
   algorithm (`networkx.shortest_simple_paths`),
3. deduplicates and keeps the `k_paths` lowest-cost routes per OD pair.

**Why the change.**
* **Removes sensor coupling.** In the original, "which paths exist" is
  partly determined by where sensors are.  That coupling makes the
  generator unusable when the goal is to *study different sensor
  placements*, because the demand depends on the placement under study.
* **Canonical algorithm.** Yen's *k*-shortest simple paths is the
  textbook method for generating route alternatives in traffic
  assignment.  It gives a clear model for the "choice set" presented to
  drivers and has a well-known complexity profile.
* **Cheaper and simpler.**  The region-adjacency graph is never needed:
  routing directly at the edge level skips an entire precomputation and
  removes `shapely`/polygon logic.
* **Deterministic per pair.** Each OD pair gets its own RNG stream
  derived from the hash of `(seed, origin, destination)`, guaranteeing
  reproducibility regardless of iteration order – the original relied on
  the global `random` module, so results depended on dictionary
  iteration order.

**What was kept from the original.** The idea of precomputing a *route
library* rather than routing each vehicle on the fly, and the idea of
sampling edge endpoints inside zones, are both carried over directly.
They are both efficient and conceptually sound; only the routing method
itself was replaced.

---

## 7. Impedance for the gravity model

**New stage only.**  Because the original did not use a gravity model,
there is no TAZ-to-TAZ cost matrix to compare with.  The new
`estimate_zone_impedance_matrix` samples a small number of
(origin, destination) edge pairs per TAZ pair and uses the minimum
observed shortest-path length as the representative TAZ-level cost.

**Why.**  An all-pairs-shortest-path computation at the edge level would
be prohibitively expensive on real city networks; the sampling
approximation is coarse but monotonic in true travel cost, which is all
the gravity decay term requires.

---

## 8. Vehicle sampling and route assignment

**Original (`PathUtil.allocate_paths`, lines 501–563).**

* Departure times: `np.random.uniform(begin, begin + interval, count)`.
* Route choice: `random.choice(edge_paths)` over a flattened list
  mixing every region-path that happens to share the same (origin,
  destination) endpoints.

**New (`vehicle_sampler.sample_vehicles_from_od`).**

* Departure times: same uniform-over-interval formulation.  This is
  kept verbatim because the hourly OD framework offers no sub-hour
  information, so a uniform intra-hour assumption is the
  maximum-entropy choice.
* Route choice: one route drawn from the well-defined `k`-candidate
  library for each OD pair, via a seeded `numpy.random.Generator`.  The
  `route_sampling.cost_scale` parameter is reserved for a future MNL
  extension.
* Output is sorted and validated: the vehicle count must exactly match
  the OD matrix total, raising `ValueError` otherwise.

**Why the change.**
* **Reproducibility.** `random.choice` relies on global state, so two
  runs with the same config could produce different `routes.xml` files.
  The new code uses `np.random.default_rng(random_seed)` throughout.
* **Integrity check.** The explicit mismatch check at write time caught
  bugs that were invisible in the original, where the printed "expected
  vs. written" counts were merely informational.
* **Separation of concerns.** Sampling no longer discovers routes on
  the fly; Stages 2 and 3 are cleanly decoupled.

---

## 9. External OD matrices: trusted input → validated input

**Original.**  `DatasetGenerator.generate_realistic_traffic` (lines
123–126) simply unpickles the user-supplied `od_matrices` and passes it
to `allocate_paths`.  Any inconsistency (unknown zone ids, fractional
values, missing hours, non-zero diagonal) propagates silently.

**New (`od_model.validate_external_od_matrices`).**  An explicit
validator enforces:

* every TAZ id exists in the network file,
* values are non-negative,
* values are integer (trip counts, not flows),
* diagonal is zero (no intra-zonal trips),
* coverage is exactly 24 hourly slots (missing hours filled with
  zeros),
* interval keys can be hour indices or seconds and are normalised.

An optional `external_od.drop_unroutable_pairs` flag tolerates missing
OD pairs by zeroing them rather than aborting.

**Why the change.**
* External OD matrices are typically produced by a different toolchain
  (e.g. a calibration pipeline) and are the most common source of
  silent pipeline errors.  Fail-fast validation replaces hours of
  debugging with a one-line error message.
* The diagonal/integer checks formalise what the new demand model
  assumes, which was implicit in the original.

---

## 10. Commute overlay: new contribution

**Original.**  None.  OD shares are hour-independent (the structural `W`
is the same every hour, only multiplied by the temporal profile).

**New (`od_model.build_commute_od_matrices` +
`build_hourly_od_shares`).**  An optional `[commute_pattern]` TOML
section assigns every zone a (`residential`, `work`) role weight.  Two
directional gravity matrices are built:

```
w^{AM}_ij ∝ m_i m_j · e^{-β c_ij} · r_i · e_j       # home → work
w^{PM}_ij ∝ m_i m_j · e^{-β c_ij} · e_i · r_j       # work → home
```

For each hour, the base gravity matrix is blended with a Gaussian-
weighted sum of AM/PM matrices and re-normalised, producing **24
distinct OD share matrices** instead of one.

**Why.**
* A single, hour-independent OD matrix cannot reproduce the directional
  asymmetry between morning and evening peaks, which is the
  phenomenon the thesis experiments measure.
* Keeping the commute overlay optional preserves backward
  compatibility: omitting the `[commute_pattern]` section reverts to the
  original (symmetric) gravity behaviour.

---

## 11. Configuration and reproducibility

**Original.**

* TOML is read as a raw `dict`; boolean values are stored as strings
  (e.g. `"True"`) and parsed with `eval(config['fcd.enable'])` – a known
  code-smell and a security risk.
* No path resolution: paths are hard-coded relative to wherever the
  script is launched.
* No global `random_seed`.  `random` and `numpy.random` are called
  directly, and the path-discovery cache (`wip_path/paths.pkl`) has no
  invalidation logic.

**New (`config_manager.ConfigManager`, `_resolve_known_paths`).**

* Typed accessors (`get_int`, `get_float`, `get_bool`, `get_mapping`)
  instead of `eval`.
* Paths are resolved relative to the config file itself, then to the
  current working directory – configs are portable between machines.
* A single `random_seed` flows through the entire pipeline
  (`TrafficGenerator._build_settings`, then per-OD-pair seeded RNG in
  `route_library.make_pair_rng`).
* The route-library cache stores a **metadata fingerprint**
  (`active_pairs`, `k_paths`, `endpoint_samples_per_od`, `random_seed`,
  `version`) and rebuilds itself when any of those change.

**Why.** Reproducibility is a core requirement for a thesis
deliverable; `eval()` on user input is unsafe; and a cache that is
silently stale is worse than no cache at all.

---

## 12. Dependency surface

**Removed dependencies.** `dijkstar`, `joblib` (Parallel), `tqdm`,
`scipy.interpolate` (`CubicSpline`), `shapely`, `xmltodict`,
`matplotlib` (except for the optional `visualize` helper).

**Why.**  Each removal corresponds to a feature that was replaced with
a simpler or unnecessary piece:

| Removed | Replaced by |
|---------|-------------|
| `dijkstar.Graph` in `evaluate_neighbor_regions` | not needed (no region adjacency graph) |
| `joblib.Parallel` in `initialize_*_reg_paths` | not needed (no region-path enumeration) |
| `scipy.interpolate.CubicSpline` | two-Gaussian analytic profile |
| `shapely.geometry.Polygon` | edge-level routing (no polygon containment) |
| `xmltodict` | `xml.etree.ElementTree` already handles inputs |

The new generator depends only on `numpy`, `pandas`, `networkx`, and
`sumolib` (plus `tomllib` from the standard library).

---

## 13. Summary table for the thesis

| Concern | Original | New | Nature of change |
|---------|----------|-----|------------------|
| Scope | 6-phase SUMO pipeline | Demand generation only | **Reduction** |
| Temporal profile | 9-point spline + jitter | Two-Gaussian kernel + largest-remainder rounding | **Replacement** (idea already existed in PathUtil) |
| Spatial OD | `W = Cᵀ C` (path co-occurrence) | Gravity model with zone mass and distance decay | **Replacement** |
| Zone mass | Implicit | Σ length × lanes | **Addition** |
| Routing | Region-graph paths + sensor bias | Edge-expanded `k`-shortest with Yen | **Replacement** |
| Route library | Pooled region-path lists | Per-OD-pair `k` candidates, deduplicated | **Restructuring** |
| Departure time | Uniform over hour | Uniform over hour | **Kept** |
| Route choice | `random.choice` | Seeded uniform over candidates (MNL-ready) | **Hardened** |
| External OD | Trusted | Validated and optionally repaired | **Hardened** |
| Commute pattern | — | AM/PM directional overlay | **New contribution** |
| Config | `eval()` on strings | Typed accessor with path resolution | **Modernisation** |
| Reproducibility | Global `random` | Per-OD-pair seeded `numpy.Generator` | **Hardening** |
| Cache | No invalidation | Metadata fingerprint | **Hardening** |
| Dependencies | 10+ | 4 | **Simplification** |

---

## 14. A paragraph you can paste into the motivation section

> The scenario generator presented in this thesis is a complete
> rewrite of an earlier Brussels case-study toolchain.  Several
> ingredients are inherited from that codebase – the two-Gaussian
> daily-demand profile, the idea of precomputing a route library
> rather than routing each vehicle on the fly, and the SUMO output
> format – because they are conceptually sound and well adapted to
> the target simulator.  Most of the machinery around them was
> however replaced.  The spatial distribution, originally derived
> from the co-occurrence of regions in discovered paths, is now a
> singly-constrained gravity model with capacity-weighted zone
> masses and an interpretable distance-decay parameter; this
> removes the circular dependency between path discovery and
> demand generation that precluded meaningful calibration.
> Routing was moved from a two-level region-adjacency graph
> biased toward sensored links to an edge-expanded graph with
> Yen's *k*-shortest-paths algorithm, decoupling the demand model
> from any particular sensor layout.  A directional commute
> overlay, absent from the original, introduces the AM/PM
> asymmetry required by the experiments.  Finally, configuration
> parsing, random-seed handling, cache invalidation, and external
> OD-matrix validation were all hardened to make the generator a
> reproducible research instrument rather than a bespoke script.
