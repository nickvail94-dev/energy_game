# energy_game.py
# Streamlit MVP: "Energy System Builder" (UK-style, simplified)
# Run: streamlit run energy_game.py

from __future__ import annotations
import json
import math
import random
from dataclasses import dataclass, asdict
from typing import Dict, List, Tuple, Any

import streamlit as st

# ----------------------------
# Constants & Model Defaults
# ----------------------------

START_YEAR = 2026
END_YEAR = 2035
HOUSEHOLDS_UK = 28_000_000  # rough; used only for scaling bills in MVP

REGIONS = ["Scotland", "North", "Midlands", "South West & Wales", "South East"]

# Very simple "resource multipliers" per region (affects VRE yield)
RESOURCE = {
    "Scotland": {"offshore": 1.15, "onshore": 1.10, "solar": 0.85},
    "North": {"offshore": 1.05, "onshore": 1.00, "solar": 0.95},
    "Midlands": {"offshore": 0.80, "onshore": 0.90, "solar": 1.00},
    "South West & Wales": {"offshore": 0.95, "onshore": 0.95, "solar": 1.05},
    "South East": {"offshore": 0.85, "onshore": 0.80, "solar": 1.10},
}

# Baseline demand and peak (arbitrary but coherent)
# Annual demand in TWh, peak in GW. (MVP-level realism.)
BASE_DEMAND_TWH = {
    "Scotland": 35,
    "North": 55,
    "Midlands": 65,
    "South West & Wales": 55,
    "South East": 90,
}
BASE_PEAK_GW = {
    "Scotland": 6,
    "North": 9,
    "Midlands": 11,
    "South West & Wales": 9,
    "South East": 15,
}

# Base inter-regional "export headroom" proxy (GW) per region.
# Think: ability to export/import without heavy curtailment.
BASE_EXPORT_LIMIT_GW = {
    "Scotland": 6.0,
    "North": 7.5,
    "Midlands": 8.5,
    "South West & Wales": 7.0,
    "South East": 9.0,
}

# Asset definitions (simplified)
ASSETS = ["Offshore wind", "Onshore wind", "Solar", "Gas CCGT", "Battery", "Transmission upgrade"]

# Capacity factors (average annual)
CF = {
    "Offshore wind": 0.50,
    "Onshore wind": 0.32,
    "Solar": 0.11,
}

# Costs (very simplified, in £m per GW or £/MWh)
# Capex in £m/GW, fixed opex in £m/GW-yr, variable cost £/MWh
COST = {
    "Offshore wind": {"capex_m_per_gw": 2500, "fix_opex_m_per_gw_yr": 60, "var_cost_per_mwh": 0},
    "Onshore wind": {"capex_m_per_gw": 1400, "fix_opex_m_per_gw_yr": 35, "var_cost_per_mwh": 0},
    "Solar": {"capex_m_per_gw": 700, "fix_opex_m_per_gw_yr": 15, "var_cost_per_mwh": 0},
    "Gas CCGT": {"capex_m_per_gw": 900, "fix_opex_m_per_gw_yr": 25, "var_cost_per_mwh": 60},  # fuel proxy added separately
    "Battery": {"capex_m_per_gw": 600, "fix_opex_m_per_gw_yr": 15, "var_cost_per_mwh": 3},
    "Transmission upgrade": {"capex_m_per_gw": 450, "fix_opex_m_per_gw_yr": 5, "var_cost_per_mwh": 0},
}

# Build times (years) before becoming operational, by asset
BUILD_TIME = {
    "Offshore wind": 4,
    "Onshore wind": 2,
    "Solar": 1,
    "Gas CCGT": 3,
    "Battery": 1,
    "Transmission upgrade": 2,
}

# How much export limit (GW) each GW of "Transmission upgrade" adds in that region
EXPORT_BOOST_PER_GW_TRANSMISSION = 2.0

# Emissions factors (gCO2/kWh)
EMISSIONS = {
    "Offshore wind": 0,
    "Onshore wind": 0,
    "Solar": 0,
    "Gas CCGT": 370,  # simplified direct emissions
    "Battery": 0,     # treated as storage, no direct emissions
}

# Base gas price proxy and volatility (used to convert gas generation to cost)
BASE_GAS_FUEL_COST_PER_MWH = 55  # £/MWh fuel component (added on top of var_cost for Gas CCGT)
GAS_SPIKE_MULTIPLIER = 1.7

# Battery simplification:
# Each GW battery provides up to X "energy shift" per year as firming and reduces gas use,
# constrained by 2-hour duration and number of cycles.
BATTERY_DURATION_HOURS = 2.0
BATTERY_CYCLES_PER_YEAR = 250  # rough; acts like how much energy can be moved from VRE to demand
BATTERY_ROUNDTRIP_EFF = 0.88
BATTERY_FIRMNESS = 0.5  # fraction of power counted toward firm capacity

# Policy levers
CFD_LEVELS = ["Low", "Medium", "High"]
PLANNING_LEVELS = ["Slow", "Normal", "Fast"]
NETWORK_LEVELS = ["Reactive", "Normal", "Anticipatory"]

POLICY_EFFECTS = {
    "CfD": {
        "Low":    {"levy_per_hh": 0,  "vre_build_time_delta": +1},
        "Medium": {"levy_per_hh": 20, "vre_build_time_delta": 0},
        "High":   {"levy_per_hh": 50, "vre_build_time_delta": -1},
    },
    "Planning": {
        "Slow":   {"build_time_delta_all": +1, "jr_risk": 0.05},
        "Normal": {"build_time_delta_all": 0,  "jr_risk": 0.10},
        "Fast":   {"build_time_delta_all": -1, "jr_risk": 0.22},
    },
    "Network": {
        "Reactive":      {"extra_bill_per_hh": 0,  "trans_build_time_delta": +1},
        "Normal":        {"extra_bill_per_hh": 10, "trans_build_time_delta": 0},
        "Anticipatory":  {"extra_bill_per_hh": 25, "trans_build_time_delta": -1},
    }
}

# Base bill used for presentation; we show "change" via system cost too
BASE_BILL_PER_HH = 1600

# Reliability mapping from margin to LOLP
def margin_to_lolp(margin: float) -> float:
    # margin is e.g. 0.15 = 15% surplus firm over peak
    if margin >= 0.15:
        return 0.001
    if margin >= 0.10:
        return 0.005
    if margin >= 0.05:
        return 0.02
    if margin >= 0.00:
        return 0.08
    return 0.20

# ----------------------------
# Events (deck style)
# ----------------------------

EVENTS = [
    {
        "name": "Gas price spike",
        "desc": "Gas fuel costs jump for 2 years.",
        "effect": {"gas_spike_years": 2},
        "severity": "High"
    },
    {
        "name": "Low wind year",
        "desc": "Wind output reduced this year.",
        "effect": {"wind_cf_multiplier": 0.85, "duration_years": 1},
        "severity": "Medium"
    },
    {
        "name": "Data centre boom (South East)",
        "desc": "Demand rises in South East for the rest of the game.",
        "effect": {"demand_uplift_region": "South East", "demand_uplift_twh": 10},
        "severity": "Medium"
    },
    {
        "name": "Transmission outage",
        "desc": "Export limits reduced this year (system stress).",
        "effect": {"export_limit_multiplier": 0.80, "duration_years": 1},
        "severity": "Medium"
    },
    {
        "name": "Judicial review delay",
        "desc": "One random in-flight project suffers +1 year delay.",
        "effect": {"jr_delay": 1},
        "severity": "Medium"
    },
    {
        "name": "Warm winter",
        "desc": "Demand down this year.",
        "effect": {"demand_multiplier": 0.95, "duration_years": 1},
        "severity": "Low"
    },
]

# ----------------------------
# State Structures
# ----------------------------

@dataclass
class BuildItem:
    asset: str
    region: str
    capacity_gw: float
    start_year: int
    ready_year: int

@dataclass
class ActiveModifiers:
    gas_spike_remaining: int = 0
    wind_cf_multiplier_remaining: int = 0
    wind_cf_multiplier_value: float = 1.0
    export_limit_multiplier_remaining: int = 0
    export_limit_multiplier_value: float = 1.0
    demand_multiplier_remaining: int = 0
    demand_multiplier_value: float = 1.0
    demand_uplift: Dict[str, float] = None

    def __post_init__(self):
        if self.demand_uplift is None:
            self.demand_uplift = {r: 0.0 for r in REGIONS}

def default_state() -> Dict[str, Any]:
    installed = {r: {a: 0.0 for a in ASSETS} for r in REGIONS}

    # Some starting capacity (to avoid empty system)
    installed["Scotland"]["Offshore wind"] = 3.0
    installed["North"]["Onshore wind"] = 1.5
    installed["Midlands"]["Solar"] = 1.0
    installed["South East"]["Gas CCGT"] = 5.0
    installed["Midlands"]["Gas CCGT"] = 3.0
    installed["South East"]["Battery"] = 0.5

    # Starting export limits
    export_limits = {r: BASE_EXPORT_LIMIT_GW[r] for r in REGIONS}

    # Policy defaults
    policy = {"CfD": "Medium", "Planning": "Normal", "Network": "Normal"}

    # Budget & “political capital”
    return {
        "year": START_YEAR,
        "installed": installed,
        "export_limits": export_limits,
        "policy": policy,
        "budget_billion": 20.0,     # per-year capex budget (very simplified)
        "political_capital": 100.0, # decreases on unpopular choices/shocks
        "build_queue": [],          # list[BuildItem] as dicts
        "history": [],              # list of yearly results dicts
        "modifiers": asdict(ActiveModifiers()),
        "event_deck": [],           # filled on init
        "last_event": None,
        "rng_seed": 42,
    }

# ----------------------------
# Helpers
# ----------------------------

def annuity_factor(rate: float = 0.06, years: int = 25) -> float:
    # Simple annuity factor for annualising capex
    if rate <= 0:
        return 1.0 / years
    return (rate * (1 + rate) ** years) / ((1 + rate) ** years - 1)

ANNUITY = annuity_factor()

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def ensure_deck(state: Dict[str, Any]) -> None:
    if state["event_deck"]:
        return
    deck = EVENTS.copy()
    rnd = random.Random(state["rng_seed"])
    rnd.shuffle(deck)
    state["event_deck"] = deck

def draw_event(state: Dict[str, Any]) -> Dict[str, Any] | None:
    ensure_deck(state)
    if not state["event_deck"]:
        return None
    return state["event_deck"].pop(0)

def apply_event(state: Dict[str, Any], event: Dict[str, Any]) -> None:
    mods = ActiveModifiers(**state["modifiers"])
    eff = event.get("effect", {})

    if "gas_spike_years" in eff:
        mods.gas_spike_remaining = max(mods.gas_spike_remaining, int(eff["gas_spike_years"]))

    if "wind_cf_multiplier" in eff:
        mods.wind_cf_multiplier_remaining = max(mods.wind_cf_multiplier_remaining, int(eff.get("duration_years", 1)))
        mods.wind_cf_multiplier_value = float(eff["wind_cf_multiplier"])

    if "export_limit_multiplier" in eff:
        mods.export_limit_multiplier_remaining = max(mods.export_limit_multiplier_remaining, int(eff.get("duration_years", 1)))
        mods.export_limit_multiplier_value = float(eff["export_limit_multiplier"])

    if "demand_multiplier" in eff:
        mods.demand_multiplier_remaining = max(mods.demand_multiplier_remaining, int(eff.get("duration_years", 1)))
        mods.demand_multiplier_value = float(eff["demand_multiplier"])

    if "demand_uplift_region" in eff:
        r = eff["demand_uplift_region"]
        mods.demand_uplift[r] = mods.demand_uplift.get(r, 0.0) + float(eff.get("demand_uplift_twh", 0.0))

    # JR delay handled during simulation step (affects queue)
    state["modifiers"] = asdict(mods)

def tick_modifiers(state: Dict[str, Any]) -> None:
    mods = ActiveModifiers(**state["modifiers"])

    if mods.gas_spike_remaining > 0:
        mods.gas_spike_remaining -= 1

    if mods.wind_cf_multiplier_remaining > 0:
        mods.wind_cf_multiplier_remaining -= 1
        if mods.wind_cf_multiplier_remaining == 0:
            mods.wind_cf_multiplier_value = 1.0

    if mods.export_limit_multiplier_remaining > 0:
        mods.export_limit_multiplier_remaining -= 1
        if mods.export_limit_multiplier_remaining == 0:
            mods.export_limit_multiplier_value = 1.0

    if mods.demand_multiplier_remaining > 0:
        mods.demand_multiplier_remaining -= 1
        if mods.demand_multiplier_remaining == 0:
            mods.demand_multiplier_value = 1.0

    state["modifiers"] = asdict(mods)

def enqueue_build(state: Dict[str, Any], asset: str, region: str, capacity_gw: float) -> Tuple[bool, str]:
    if capacity_gw <= 0:
        return False, "Capacity must be > 0."
    year = state["year"]

    # Determine build time with policy effects
    cfd = state["policy"]["CfD"]
    planning = state["policy"]["Planning"]
    network = state["policy"]["Network"]

    base = BUILD_TIME[asset]
    delta = 0

    # CfD affects VRE build times
    if asset in ["Offshore wind", "Onshore wind", "Solar"]:
        delta += POLICY_EFFECTS["CfD"][cfd]["vre_build_time_delta"]

    # Planning affects all build times
    delta += POLICY_EFFECTS["Planning"][planning]["build_time_delta_all"]

    # Network lever affects transmission build time additionally
    if asset == "Transmission upgrade":
        delta += POLICY_EFFECTS["Network"][network]["trans_build_time_delta"]

    bt = int(clamp(base + delta, 1, 6))
    ready = year + bt

    # Budget check (capex occurs immediately in MVP)
    capex_m = COST[asset]["capex_m_per_gw"] * capacity_gw
    capex_b = capex_m / 1000.0
    if capex_b > state["budget_billion"]:
        return False, f"Not enough budget this year. Need £{capex_b:.2f}bn, have £{state['budget_billion']:.2f}bn."

    state["budget_billion"] -= capex_b

    item = BuildItem(asset=asset, region=region, capacity_gw=capacity_gw, start_year=year, ready_year=ready)
    state["build_queue"].append(asdict(item))
    return True, f"Queued {capacity_gw:.2f} GW {asset} in {region} (ready {ready}). Cost £{capex_b:.2f}bn."

def commission_ready_projects(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    year = state["year"]
    new_queue = []
    commissioned = []
    for item in state["build_queue"]:
        if item["ready_year"] <= year:
            commissioned.append(item)
            a = item["asset"]
            r = item["region"]
            cap = float(item["capacity_gw"])
            if a == "Transmission upgrade":
                state["export_limits"][r] += cap * EXPORT_BOOST_PER_GW_TRANSMISSION
                state["installed"][r][a] += cap
            else:
                state["installed"][r][a] += cap
        else:
            new_queue.append(item)
    state["build_queue"] = new_queue
    return commissioned

def maybe_apply_jr_delay(state: Dict[str, Any]) -> bool:
    # JR risk depends on planning speed; event deck can also trigger a JR delay.
    planning = state["policy"]["Planning"]
    jr_risk = POLICY_EFFECTS["Planning"][planning]["jr_risk"]

    # Chance based (if no queue, no effect)
    if not state["build_queue"]:
        return False

    rnd = random.Random(state["rng_seed"] + state["year"] * 17)
    if rnd.random() < jr_risk:
        # Pick one random in-flight project and delay it by 1 year
        idx = rnd.randrange(len(state["build_queue"]))
        state["build_queue"][idx]["ready_year"] += 1
        return True
    return False

def apply_jr_event_delay(state: Dict[str, Any]) -> bool:
    # Event-driven JR delay: always triggers if queue exists
    if not state["build_queue"]:
        return False
    rnd = random.Random(state["rng_seed"] + state["year"] * 31)
    idx = rnd.randrange(len(state["build_queue"]))
    state["build_queue"][idx]["ready_year"] += 1
    return True

def simulate_year(state: Dict[str, Any]) -> Dict[str, Any]:
    year = state["year"]
    mods = ActiveModifiers(**state["modifiers"])

    # Demand by region (TWh) with multipliers/uplifts
    demand_twh = {}
    for r in REGIONS:
        base = BASE_DEMAND_TWH[r] + mods.demand_uplift.get(r, 0.0)
        demand_twh[r] = base * mods.demand_multiplier_value

    total_demand_twh = sum(demand_twh.values())

    # Effective export limits (GW)
    export_limit = {}
    for r in REGIONS:
        export_limit[r] = state["export_limits"][r] * mods.export_limit_multiplier_value

    # VRE energy production by region (TWh)
    # Apply low-wind modifier to wind only (offshore/onshore)
    vre_twh = {r: {"Offshore wind": 0.0, "Onshore wind": 0.0, "Solar": 0.0} for r in REGIONS}
    for r in REGIONS:
        for a in ["Offshore wind", "Onshore wind", "Solar"]:
            cap = state["installed"][r][a]
            cf = CF[a]
            # region resource factor
            if a == "Offshore wind":
                mult = RESOURCE[r]["offshore"]
                cf_eff = cf * mult * (mods.wind_cf_multiplier_value if mods.wind_cf_multiplier_remaining > 0 else 1.0)
            elif a == "Onshore wind":
                mult = RESOURCE[r]["onshore"]
                cf_eff = cf * mult * (mods.wind_cf_multiplier_value if mods.wind_cf_multiplier_remaining > 0 else 1.0)
            else:
                mult = RESOURCE[r]["solar"]
                cf_eff = cf * mult
            e_twh = cap * 8760 * cf_eff / 1e6  # GW * h -> GWh; /1e3 -> TWh (combined is /1e6)
            vre_twh[r][a] = e_twh

    # Curtailment approximation:
    # If VRE energy exceeds (local demand + export ability), curtail.
    # Convert export ability GW to annual "export energy headroom" (TWh) using a utilization factor.
    EXPORT_UTIL = 0.55
    export_energy_headroom_twh = {r: export_limit[r] * 8760 * EXPORT_UTIL / 1e6 for r in REGIONS}

    curtailed_twh = {r: 0.0 for r in REGIONS}
    used_vre_twh = {r: 0.0 for r in REGIONS}
    total_vre_twh = 0.0

    for r in REGIONS:
        total_vre = sum(vre_twh[r].values())
        total_vre_twh += total_vre
        cap_to_use = demand_twh[r] + export_energy_headroom_twh[r]
        used = min(total_vre, cap_to_use)
        used_vre_twh[r] = used
        curtailed_twh[r] = max(0.0, total_vre - used)

    total_curtailed_twh = sum(curtailed_twh.values())

    # Battery: absorbs part of curtailment and displaces gas (energy shift)
    # Available charge energy is curtailed VRE (TWh) * eff; discharge capped by cycles/duration.
    battery_discharge_twh = {r: 0.0 for r in REGIONS}
    battery_charge_twh = {r: 0.0 for r in REGIONS}

    for r in REGIONS:
        batt_gw = state["installed"][r]["Battery"]
        if batt_gw <= 0:
            continue
        # Annual energy throughput cap
        batt_energy_gwh = batt_gw * BATTERY_DURATION_HOURS * 1000  # GW * h -> GWh
        annual_throughput_gwh = batt_energy_gwh * BATTERY_CYCLES_PER_YEAR
        annual_throughput_twh = annual_throughput_gwh / 1e3

        # charge limited by curtailment
        charge_twh = min(curtailed_twh[r], annual_throughput_twh)
        discharge_twh = charge_twh * BATTERY_ROUNDTRIP_EFF

        battery_charge_twh[r] = charge_twh
        battery_discharge_twh[r] = discharge_twh

        # Reduce curtailment by charged amount (we assume it would otherwise be wasted)
        curtailed_twh[r] -= charge_twh

    total_curtailed_twh = sum(curtailed_twh.values())
    total_batt_discharge_twh = sum(battery_discharge_twh.values())

    # Supply balance: used VRE + battery discharge + gas generation must meet demand
    # Assume VRE used in-region + imports cover demand; remaining served by gas
    gas_twh = {}
    unserved_twh = {}
    for r in REGIONS:
        supply_non_gas = used_vre_twh[r] + battery_discharge_twh[r]
        remaining = demand_twh[r] - supply_non_gas
        if remaining <= 0:
            gas_twh[r] = 0.0
            unserved_twh[r] = 0.0
        else:
            # Gas energy limited by installed gas capacity with a maximum annual output (availability)
            gas_gw = state["installed"][r]["Gas CCGT"]
            gas_max_twh = gas_gw * 8760 * 0.90 / 1e6
            gas_gen = min(remaining, gas_max_twh)
            gas_twh[r] = gas_gen
            unserved_twh[r] = max(0.0, remaining - gas_gen)

    total_gas_twh = sum(gas_twh.values())
    total_unserved_twh = sum(unserved_twh.values())

    # Reliability proxy via firm capacity margin
    total_peak_gw = sum(BASE_PEAK_GW.values())  # keep simple
    firm_gw = 0.0
    for r in REGIONS:
        firm_gw += state["installed"][r]["Gas CCGT"]
        firm_gw += state["installed"][r]["Battery"] * BATTERY_FIRMNESS
    margin = (firm_gw / total_peak_gw) - 1.0
    lolp = margin_to_lolp(margin)

    # Penalize reliability if unserved energy exists
    if total_unserved_twh > 0.01:
        lolp = min(0.35, lolp + 0.10)

    # Carbon intensity (g/kWh)
    total_supply_twh = total_demand_twh - total_unserved_twh
    if total_supply_twh <= 0:
        carbon_intensity = 999.0
    else:
        emissions_g = total_gas_twh * 1e9 * EMISSIONS["Gas CCGT"] / 1e3  # TWh -> kWh (1e9); * g/kWh
        carbon_intensity = emissions_g / (total_supply_twh * 1e9)  # g/kWh

    # Costs (very simplified)
    # Annualised capex + fixed opex + variable costs + fuel
    annual_cost_m = 0.0
    for r in REGIONS:
        for a in ["Offshore wind", "Onshore wind", "Solar", "Gas CCGT", "Battery", "Transmission upgrade"]:
            cap = state["installed"][r][a]
            if cap <= 0:
                continue
            annual_cost_m += cap * COST[a]["capex_m_per_gw"] * ANNUITY
            annual_cost_m += cap * COST[a]["fix_opex_m_per_gw_yr"]

    # Variable + fuel
    # Convert TWh -> MWh (1 TWh = 1,000,000 MWh)
    gas_fuel = BASE_GAS_FUEL_COST_PER_MWH * (GAS_SPIKE_MULTIPLIER if mods.gas_spike_remaining > 0 else 1.0)
    annual_cost_m += total_gas_twh * 1_000_000 * (COST["Gas CCGT"]["var_cost_per_mwh"] + gas_fuel) / 1e6
    annual_cost_m += total_batt_discharge_twh * 1_000_000 * COST["Battery"]["var_cost_per_mwh"] / 1e6

    # Curtailment "waste cost" penalty (optional but makes constraints bite)
    annual_cost_m += total_curtailed_twh * 1_000_000 * 5 / 1e6  # £5/MWh penalty

    # Policy levies (per household)
    cfd = state["policy"]["CfD"]
    network = state["policy"]["Network"]
    levy = POLICY_EFFECTS["CfD"][cfd]["levy_per_hh"] + POLICY_EFFECTS["Network"][network]["extra_bill_per_hh"]

    # Convert system cost into £/hh add-on: spread annual cost across households.
    # We take a baseline cost constant so bills aren't insane; show delta relative to a reference.
    # We'll map annual_cost_m into a "system add-on" centred at ~£0.
    # Reference annual system cost in this simplified world:
    ref_annual_cost_m = 120_000  # arbitrary anchor to keep outputs stable
    delta_m = annual_cost_m - ref_annual_cost_m
    add_on = delta_m * 1e6 / HOUSEHOLDS_UK  # £ per household
    bill = BASE_BILL_PER_HH + add_on + levy

    # Curtailment rate
    curtail_rate = (total_curtailed_twh / total_vre_twh) if total_vre_twh > 0 else 0.0

    # Warnings
    warnings = []
    if curtail_rate > 0.15:
        warnings.append("Constraint stress: high curtailment suggests insufficient transmission/export capability.")
    if total_gas_twh / max(1e-6, total_supply_twh) > 0.35:
        warnings.append("Gas dependence is high: exposed to fuel price shocks and carbon outcomes.")
    if lolp > 0.02:
        warnings.append("Security of supply risk: firm capacity margin is tight (higher LOLP).")
    if carbon_intensity > 100:
        warnings.append("Carbon trajectory risk: emissions intensity remains high.")

    return {
        "year": year,
        "demand_twh": demand_twh,
        "total_demand_twh": total_demand_twh,
        "total_vre_twh": total_vre_twh,
        "total_curtailed_twh": total_curtailed_twh,
        "curtail_rate": curtail_rate,
        "total_gas_twh": total_gas_twh,
        "total_unserved_twh": total_unserved_twh,
        "lolp": lolp,
        "margin": margin,
        "carbon_intensity_g_per_kwh": carbon_intensity,
        "annual_cost_m": annual_cost_m,
        "bill_per_hh": bill,
        "warnings": warnings,
        "mods": asdict(mods),
    }

def advance_one_year(state: Dict[str, Any]) -> None:
    # 1) Commission projects ready this year
    commissioned = commission_ready_projects(state)

    # 2) Event draw (one per year)
    event = draw_event(state)
    state["last_event"] = event
    if event:
        apply_event(state, event)

    # 3) If event is JR delay, apply it deterministically
    jr_event_applied = False
    if event and event["name"] == "Judicial review delay":
        jr_event_applied = apply_jr_event_delay(state)

    # 4) Otherwise, apply probabilistic JR risk based on planning
    jr_risk_applied = False
    if not jr_event_applied:
        jr_risk_applied = maybe_apply_jr_delay(state)

    # 5) Simulate year outcomes
    result = simulate_year(state)

    # 6) Update political capital based on outcomes (simple)
    # High bills, high LOLP, high curtailment, high carbon all hurt.
    pc = state["political_capital"]
    pc -= clamp((result["bill_per_hh"] - BASE_BILL_PER_HH) / 80, 0, 8)
    pc -= 30 * result["lolp"]
    pc -= 10 * result["curtail_rate"]
    pc -= clamp((result["carbon_intensity_g_per_kwh"] - 50) / 100, 0, 6)
    if jr_event_applied or jr_risk_applied:
        pc -= 2.0
    pc += 1.5  # small recovery each year
    state["political_capital"] = clamp(pc, 0, 100)

    # 7) Record history
    result["commissioned"] = commissioned
    result["event"] = event
    result["jr_delay_applied"] = (jr_event_applied or jr_risk_applied)
    state["history"].append(result)

    # 8) Tick down timed modifiers for next year
    tick_modifiers(state)

    # 9) Reset annual budget
    state["budget_billion"] = 20.0

    # 10) Advance year
    state["year"] += 1


# ----------------------------
# UI
# ----------------------------

st.set_page_config(page_title="Energy Systems Builder (MVP)", layout="wide")

st.title("⚡ Energy Systems Builder (MVP)")
st.caption("A simplified UK-style energy system game: build assets, set policy, survive shocks, hit clean power targets.")

if "state" not in st.session_state:
    st.session_state.state = default_state()
    ensure_deck(st.session_state.state)

state = st.session_state.state

# Sidebar controls
with st.sidebar:
    st.header("Game Controls")

    colA, colB = st.columns(2)
    with colA:
        if st.button("🔄 New Game"):
            st.session_state.state = default_state()
            ensure_deck(st.session_state.state)
            st.rerun()

    with colB:
        if st.button("⏪ Undo 1 Year") and state["history"]:
            # crude undo: reload from saved snapshot at end of prev year is not stored,
            # so we reconstruct by replaying history from scratch.
            # For MVP, we just pop last result, decrement year and revert installed by recompute.
            # Simpler: full reset + reapply queued builds and history is messy.
            # We'll do a safe approach: load last saved checkpoint if present.
            st.warning("Undo is not supported in this MVP. Use Save/Load instead.")

    st.divider()
    st.subheader("Policy levers")
    state["policy"]["CfD"] = st.selectbox("CfD generosity", CFD_LEVELS, index=CFD_LEVELS.index(state["policy"]["CfD"]))
    state["policy"]["Planning"] = st.selectbox("Planning speed", PLANNING_LEVELS, index=PLANNING_LEVELS.index(state["policy"]["Planning"]))
    state["policy"]["Network"] = st.selectbox("Network investment", NETWORK_LEVELS, index=NETWORK_LEVELS.index(state["policy"]["Network"]))

    st.divider()
    st.subheader("Save / Load")
    save_blob = json.dumps(state, indent=2)
    st.download_button("💾 Download Save (.json)", data=save_blob, file_name="energy_game_save.json", mime="application/json")

    up = st.file_uploader("Load Save (.json)", type=["json"])
    if up is not None:
        try:
            loaded = json.load(up)
            # minimal validation
            if "year" in loaded and "installed" in loaded and "policy" in loaded:
                st.session_state.state = loaded
                ensure_deck(st.session_state.state)
                st.success("Loaded save.")
                st.rerun()
            else:
                st.error("Invalid save file.")
        except Exception as e:
            st.error(f"Failed to load: {e}")

# Top status row
col1, col2, col3, col4, col5 = st.columns([1.1, 1.1, 1.3, 1.3, 1.2])
with col1:
    st.metric("Year", f"{state['year']}")
with col2:
    st.metric("Budget (this year)", f"£{state['budget_billion']:.2f}bn")
with col3:
    st.metric("Political capital", f"{state['political_capital']:.0f}/100")
with col4:
    mods = ActiveModifiers(**state["modifiers"])
    gas_spike = "Yes" if mods.gas_spike_remaining > 0 else "No"
    st.metric("Gas spike active?", gas_spike)
with col5:
    st.metric("Build queue", f"{len(state['build_queue'])} projects")

st.divider()

# Main layout: Build + System view + Results
left, mid, right = st.columns([1.1, 1.4, 1.2])

with left:
    st.subheader("🏗 Build")
    region = st.selectbox("Region", REGIONS)

    asset = st.selectbox("Asset", ASSETS)
    cap = st.number_input("Capacity (GW)", min_value=0.0, max_value=20.0, value=0.5, step=0.1)

    if st.button("Add to build queue"):
        ok, msg = enqueue_build(state, asset, region, cap)
        (st.success if ok else st.error)(msg)

    st.caption("Notes: Capex is charged immediately to this year's budget. Build times depend on policy levers.")

    st.divider()
    st.subheader("📋 Build queue")
    if not state["build_queue"]:
        st.write("No projects queued.")
    else:
        for i, item in enumerate(state["build_queue"], start=1):
            st.write(f"{i}. **{item['asset']}** — {item['capacity_gw']:.2f} GW in *{item['region']}* "
                     f"(started {item['start_year']}, ready {item['ready_year']})")

with mid:
    st.subheader("🗺 Regions (stylised)")
    st.caption("Click regions in the Build panel; this shows installed capacity and export headroom proxy.")

    # Region cards
    for r in REGIONS:
        with st.container(border=True):
            c1, c2 = st.columns([1.4, 1.0])
            with c1:
                st.markdown(f"### {r}")
                inst = state["installed"][r]
                mix = {k: v for k, v in inst.items() if v > 0.01 and k != "Transmission upgrade"}
                if mix:
                    st.write("Installed (GW): " + ", ".join([f"{k}: {v:.2f}" for k, v in mix.items()]))
                else:
                    st.write("Installed (GW): —")

                st.write(f"Transmission upgrades (GW): {inst['Transmission upgrade']:.2f}")
            with c2:
                base = BASE_EXPORT_LIMIT_GW[r]
                current = state["export_limits"][r]
                st.write("Export headroom (GW)")
                st.progress(clamp(current / (base + 10), 0.0, 1.0))
                st.caption(f"{current:.1f} GW (base {base:.1f})")

    st.divider()
    st.subheader("▶️ Simulate")
    if state["year"] > END_YEAR:
        st.success("Game complete (end year reached). Load a save or start a new game to replay.")
    else:
        if st.button("Simulate this year"):
            advance_one_year(state)
            st.rerun()

with right:
    st.subheader("📈 Results")

    if not state["history"]:
        st.info("Simulate the first year to see outcomes.")
    else:
        latest = state["history"][-1]

        k1, k2 = st.columns(2)
        with k1:
            st.metric("Bill per household", f"£{latest['bill_per_hh']:.0f}")
            st.metric("Carbon intensity", f"{latest['carbon_intensity_g_per_kwh']:.0f} g/kWh")
        with k2:
            st.metric("LOLP (proxy)", f"{latest['lolp']*100:.1f}%")
            st.metric("Curtailment rate", f"{latest['curtail_rate']*100:.1f}%")

        if latest.get("event"):
            ev = latest["event"]
            st.markdown(f"**Event:** {ev['name']}  \n_{ev['desc']}_ (Severity: {ev['severity']})")
        if latest.get("jr_delay_applied"):
            st.warning("A project was delayed due to planning/JR risk this year.")

        if latest["warnings"]:
            for w in latest["warnings"]:
                st.warning(w)
        else:
            st.success("No major warnings this year.")

        st.divider()
        st.subheader("Trends")

        years = [h["year"] for h in state["history"]]
        bills = [h["bill_per_hh"] for h in state["history"]]
        carbon = [h["carbon_intensity_g_per_kwh"] for h in state["history"]]
        lolp = [h["lolp"] * 100 for h in state["history"]]
        curt = [h["curtail_rate"] * 100 for h in state["history"]]

        # Streamlit line charts accept dict-like
        st.line_chart(
            {"Bill (£/hh)": bills, "Carbon (g/kWh)": carbon},
            x=years,
            height=220
        )
        st.line_chart(
            {"LOLP (%)": lolp, "Curtailment (%)": curt},
            x=years,
            height=220
        )

        st.divider()
        st.subheader("This year breakdown")

        st.write(f"Demand: **{latest['total_demand_twh']:.1f} TWh**")
        st.write(f"VRE produced: **{latest['total_vre_twh']:.1f} TWh**")
        st.write(f"Curtailment: **{latest['total_curtailed_twh']:.1f} TWh**")
        st.write(f"Gas generation: **{latest['total_gas_twh']:.1f} TWh**")
        if latest["total_unserved_twh"] > 0:
            st.error(f"Unserved energy: **{latest['total_unserved_twh']:.2f} TWh** (blackout risk)")
        else:
            st.write("Unserved energy: **0.00 TWh**")

st.divider()
st.caption(
    "MVP disclaimer: this is a simplified model designed for intuition (constraints, trade-offs, delivery risk), "
    "not an operational planning tool."
)
