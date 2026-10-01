from __future__ import annotations

from datetime import date
import math

from .agronomy import soil_indicators, yield_adjustment
from .economics import rotation_economics
from .models import CropV2, FieldV2, IndicatorBundle, RemoteDay, Rotation, ScheduledCropV2, Scenario
from .water import simulate_crop_water


def _max_dry_days(days) -> int:
    best = cur = 0
    for d in days:
        if d.rain_mm < 1.0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def compute_rotation_indicators(
    rotation: Rotation,
    schedule: tuple[ScheduledCropV2, ...],
    crops: dict[str, CropV2],
    scenario: Scenario,
    field: FieldV2,
    soc_horizon_years: int = 3,
) -> tuple[dict[str, float], list[dict[str, float]]]:
    remote = {x.d: x for x in scenario.remote}
    water_results = []
    yields = []
    active_days = []
    for sc in schedule:
        crop = crops[sc.crop_id]
        wr = simulate_crop_water(crop, sc.sow_date, scenario.weather, field, remote)
        yf = yield_adjustment(crop, wr, field)
        water_results.append(wr)
        yields.append(yf)
        active_days.extend(d for d in scenario.weather if sc.sow_date <= d.d < sc.harvest_date)
    rotation_crops = tuple(crops[x] for x in rotation.crop_ids)
    soil = soil_indicators(rotation_crops, field, soc_horizon_years)
    econ = rotation_economics(rotation_crops, yields, field)
    irrigation = sum(w["irrigation_gross_mm"] for w in water_results)
    yield_total = sum(y["climate_adjusted_yield_kg_ha"] for y in yields)
    etc_total = sum(w["etc_mm"] for w in water_results)
    water_stress = sum(w["water_stress_fraction"] for w in water_results) / max(1, len(water_results))
    severe = sum(w["severe_water_stress_days"] for w in water_results) / max(1, sum(c.duration_days for c in rotation_crops))
    heat_days = sum(w["heat_stress_days"] for w in water_results)
    heat_dd = sum(w["heat_degree_days"] for w in water_results)
    waterlog_days = sum(w["waterlogging_days"] for w in water_results)
    heavy_days = sum(w["heavy_rain_days"] for w in water_results)
    dry_days = _max_dry_days(active_days)
    taw = sum(w["taw_mm"] for w in water_results) / max(1, len(water_results))
    smap_error_vals = [w["smap_abs_error_m3_m3"] for w in water_results if w["smap_match_count"] > 0]
    modis_vals = [x.modis_et_mm for x in (remote.get(d.d) for d in active_days) if x is not None and x.modis_et_mm is not None]
    ndvi_vals = [x.ndvi for x in (remote.get(d.d) for d in active_days) if x is not None and x.ndvi is not None]
    ndmi_vals = [x.ndmi for x in (remote.get(d.d) for d in active_days) if x is not None and x.ndmi is not None]
    calendar_margins = []
    for sc in schedule:
        _, we = _window(crops[sc.crop_id].sowing_window, sc.sow_date)
        calendar_margins.append((we - sc.sow_date).days)
    calendar_margin = min(calendar_margins) if calendar_margins else 0

    observed_et_error_pct = None
    if modis_vals and etc_total > 0:
        observed_et_mean = sum(modis_vals) / len(modis_vals)
        model_et_mean = etc_total / max(1, len(active_days))
        observed_et_error_pct = abs(model_et_mean - observed_et_mean) / max(observed_et_mean, 0.1) * 100.0

    smap_error = sum(smap_error_vals) / len(smap_error_vals) if smap_error_vals else None
    remote_days = [d for d in active_days if d.d in remote]
    coverage = len(remote_days) / max(1, len(active_days))
    ndvi_mean = sum(ndvi_vals) / len(ndvi_vals) if ndvi_vals else None
    ndmi_mean = sum(ndmi_vals) / len(ndmi_vals) if ndmi_vals else None

    values = {
        **soil,
        **econ,
        "irrigation_mm": irrigation,
        "irrigation_effective_mm": sum(w["irrigation_effective_mm"] for w in water_results),
        "irrigation_loss_mm": sum(w["irrigation_loss_mm"] for w in water_results),
        "etc_total_mm": etc_total,
        "runoff_mm": sum(w["runoff_mm"] for w in water_results),
        "deep_drainage_mm": sum(w["deep_drainage_mm"] for w in water_results),
        "paddy_outflow_mm": sum(w["paddy_outflow_mm"] for w in water_results),
        "water_balance_error_mm": sum(w["water_balance_error_mm"] for w in water_results),
        "water_stress_fraction": water_stress,
        "severe_water_stress_fraction": severe,
        "heat_stress_days": heat_days,
        "heat_degree_days": heat_dd,
        "waterlogging_days": waterlog_days,
        "heavy_rain_days": heavy_days,
        "max_consecutive_dry_days": float(dry_days),
        "soil_water_deficit_mm": water_stress * taw,
        "irrigation_reliability_burden": irrigation * (1 - field.farmer.irrigation_reliability),
        "water_productivity": yield_total / max(irrigation, 1.0),
        "flood_exposure": min(1.0, (heavy_days / max(1, sum(c.duration_days for c in rotation_crops))) * (0.5 + 0.5 * ("low" in field.land_type.lower()))),
        "climate_adjusted_yield_kg_ha": yield_total,
        "yield_factor": sum(y["yield_factor"] for y in yields) / max(1, len(yields)),
        "scenario_yield_cv": 0.0,
        "labour_peak_person_days_ha": max(c.labour_person_days_ha for c in rotation_crops),
        "labour_total_person_days_ha": sum(c.labour_person_days_ha for c in rotation_crops),
        "calendar_margin_days": float(calendar_margin),
        "market_access_burden": max(0.0, min(1.0, 1 - field.farmer.market_access_score)),
        "rotation_length": float(len(rotation.crop_ids)),
        "mean_hls_ndvi": ndvi_mean,
        "mean_hls_ndmi": ndmi_mean,
        "mean_smap_rootzone_moisture": None if smap_error is None else max(0.0, min(1.0, 1.0 - smap_error / 0.15)),
        "smap_model_abs_error_m3_m3": smap_error,
        "model_vs_modis_et_error_pct": observed_et_error_pct,
        "modis_et_mean_mm": sum(modis_vals) / len(modis_vals) if modis_vals else None,
        "remote_observation_coverage_fraction": coverage,
        "hls_observation_count": float(len(ndvi_vals) + len(ndmi_vals)),
        "smap_observation_count": float(sum(w["smap_match_count"] for w in water_results)),
        "modis_observation_count": float(len(modis_vals)),
    }
    # Only observations supported by the input are retained; there are no injected 0.5/0.0 defaults.
    values = {k: v for k, v in values.items() if v is not None}
    return values, yields


def _window(window: tuple[str, str], same: date):
    sm, sd = map(int, window[0].split("-"))
    em, ed = map(int, window[1].split("-"))
    a = date(same.year, sm, sd)
    b = date(same.year, em, ed)
    if b < a:
        b = date(same.year + 1, em, ed)
    return a, b
