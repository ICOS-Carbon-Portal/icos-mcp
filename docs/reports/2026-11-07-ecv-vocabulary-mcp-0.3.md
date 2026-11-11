# ICOS zarr and MCP — work report, 2026-11-07

The vocabulary mapping (version 2) went live across the builder, the proxy
and the MCP server in one day: every variable now says which GCOS
Essential Climate Variable its **observation** contributes to and how, a
CF standard name only where it is an exact match, and the unit conversion
to it.  The Python MCP server shipped it as 0.3.0, became public and was
listed in the MCP Registry.

**Live:** zpd `234d1f5`; every served store re-finalized with the v2 table;
<https://mcp.icos-cp.eu> icos-mcp 0.3.0 (`4da9c57`).  zarrm9 `caf9c73` ..
`9cf98a5` (5 commits).

---

## 1. The framing (docs/ecv-vocabulary-plan.md)

Alex's point, and the reason for version 2: **ECVs are products; what ICOS
provides are the observations they are built from.**  So `ecv` names the
ECV a variable contributes to, `ecv_role` says observation or derived and
how (gap-filled, partitioned, aggregated), and `ecv_relation: related`
marks a variable outside the ECV's own set -- NEE, GPP and RECO to the
greenhouse-gas ECV, 14CO2 to the anthropogenic fluxes ECV; radon none.

## 2. In the stores (`caf9c73` .. `4a46982`)

* `standard_name` is written **only for an exact CF match**, and a
  builder's non-exact one is removed.  That fixed four live defects no
  check could see: the daily NEE carried the half-hourly mole-flux name;
  AGAGE wrote the placeholder `mole_fraction_of_%`; icos-socat a name not
  in CF; the eco PPFD an energy-flux name on a photon flux.
* `vocabulary_mappings` (SSSOM-shaped, as JSON text) gives the exact or
  close P07 term with the conversion **from the array's own units**, and
  the EXV link; `units_also_as` the same quantity in other communities'
  units.  CF's net downward land carbon flux is NEP, so aggregated NEE maps
  to it with a negative factor and a stated `cf-error`.
* The ObsPack spelling `mole_fraction_of_co2_in_dry_air` is not a CF name
  (checked against the table, v95); it is kept as `obspack_standard_name`.
* Applied by `Ecv.m9` inside `Finalize.Run`, so every build and `Finb`
  write it alike.

**The gate took four runs, each red for a reason worth keeping**:
agagecheck held species names to the source files, which carry AGAGE's
placeholder; `/variables` reported an unfinalized store's builder spelling
as a CF name; and the icos-obspack gas store is digested before finalize,
so its digest cannot move.  Then `Finb` in place over all nine served
stores (metadata backed up first), the shuttle in 466 s; the store check:
1,675,917 array entries consistent.

## 3. In the proxy and the passport

`/variables` entries gain `ecv`, `ecv_uri`, `ecv_role`, `ecv_relation`,
`cf_standard_name` and `vocabulary_issues`, derived from the arrays at
answer time, never saved.  Passports name the P07 term beside the ECV, the
role as `measurementTechnique`, the issues as `description`.

## 4. The MCP server 0.3.0 (`4da9c57`)

Variables carry the ECV fields; `describe_variable` adds the mappings, the
conversion and the issues; `list_datasets` names each store's ECVs;
search matches ECV names and new land words (soil moisture, albedo, snow,
PAR).  Every tool argument now has a description.  The repository was made
**public** and `registry/server.json` registered it in the **MCP
Registry** with its icon and repository.

## Left open

mcpd was not ported to 0.3.0 the same day: its oracle pin predated the
licence-gate commits, so following the Python server meant porting those
too (done on 2026-11-09, with the gate refused rather than ported).
