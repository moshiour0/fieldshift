from __future__ import annotations

import csv
import hashlib
import json
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from .models import Provenance, RemoteDay, WeatherDay
from .provenance import now_iso

NASA_POWER_BASE = "https://power.larc.nasa.gov/api/temporal/daily/point"
POWER_PARAMS = "T2M_MAX,T2M_MIN,RH2M,WS2M,ALLSKY_SFC_SW_DWN,PRECTOTCORR"


class NasaDataError(RuntimeError):
    pass


def _http_json(url: str, timeout: int = 30, retries: int = 3) -> dict:
    last: Exception | None = None
    for attempt in range(max(1, retries)):
        req = urllib.request.Request(url, headers={"User-Agent": "FieldShift-V3/3.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # pragma: no cover - network failure is exercised by integration users
            last = exc
            if attempt + 1 < retries:
                time.sleep(0.8 * (2 ** attempt))
    raise NasaDataError(f"NASA data request failed after {retries} attempts: {last}") from last


def _cache_path(cache_dir: Path, url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return cache_dir / f"power_{digest}.json"


def _fill_missing(series: dict[str, float | int | None]) -> tuple[dict[str, float], int]:
    keys = sorted(series)
    vals = [None if series[k] in (None, -999, -999.0) else float(series[k]) for k in keys]
    missing = sum(v is None for v in vals)
    known = [(i, v) for i, v in enumerate(vals) if v is not None]
    if not known:
        raise NasaDataError("NASA POWER parameter has no usable values")
    for i, v in enumerate(vals):
        if v is not None:
            continue
        left = max((x for x in known if x[0] < i), default=None, key=lambda x: x[0])
        right = min((x for x in known if x[0] > i), default=None, key=lambda x: x[0])
        if left and right:
            li, lv = left
            ri, rv = right
            frac = (i - li) / max(1, ri - li)
            vals[i] = lv + frac * (rv - lv)
        elif left:
            vals[i] = left[1]
        else:
            vals[i] = right[1]  # type: ignore[index]
    return {k: float(v) for k, v in zip(keys, vals)}, missing


def power_daily(
    lat: float,
    lon: float,
    start: date,
    end: date,
    timeout: int = 30,
    retries: int = 3,
    cache_dir: str | Path | None = None,
) -> tuple[WeatherDay, ...]:
    query = urllib.parse.urlencode({
        "parameters": POWER_PARAMS,
        "community": "AG",
        "longitude": f"{lon:.6f}",
        "latitude": f"{lat:.6f}",
        "start": start.strftime("%Y%m%d"),
        "end": end.strftime("%Y%m%d"),
        "format": "JSON",
        "time-standard": "LST",
    })
    url = f"{NASA_POWER_BASE}?{query}"
    payload: dict
    cache_path = _cache_path(Path(cache_dir), url) if cache_dir else None
    if cache_path and cache_path.exists():
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        cache_status = "cache"
    else:
        payload = _http_json(url, timeout=timeout, retries=retries)
        cache_status = "live"
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        params = payload["properties"]["parameter"]
    except KeyError as exc:
        raise NasaDataError("unexpected NASA POWER payload") from exc
    dates = sorted(params["T2M_MAX"].keys())
    filled: dict[str, dict[str, float]] = {}
    missing_total = 0
    for name in POWER_PARAMS.split(","):
        filled[name], missing = _fill_missing(params[name])
        missing_total += missing
    prov = Provenance(
        source="NASA POWER Daily API",
        source_type="observed/model-derived",
        retrieval_date=now_iso(),
        temporal_resolution="daily",
        spatial_resolution="point",
        citation="https://power.larc.nasa.gov/docs/services/api/temporal/daily/",
        confidence=0.95 if missing_total == 0 else 0.90,
    )
    out = []
    for key in dates:
        y, m, d = int(key[:4]), int(key[4:6]), int(key[6:8])
        out.append(WeatherDay(
            date(y, m, d), filled["T2M_MAX"][key], filled["T2M_MIN"][key], filled["RH2M"][key], filled["WS2M"][key],
            filled["ALLSKY_SFC_SW_DWN"][key], max(0.0, filled["PRECTOTCORR"][key]),
            source=f"NASA POWER ({cache_status}; {missing_total} values imputed)" if missing_total else f"NASA POWER ({cache_status})",
            provenance=prov,
        ))
    return tuple(out)


def power_json_file(path: str | Path) -> tuple[WeatherDay, ...]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    params = payload["properties"]["parameter"]
    dates = sorted(params["T2M_MAX"].keys())
    filled = {name: _fill_missing(params[name])[0] for name in POWER_PARAMS.split(",")}
    prov = Provenance(
        source=f"NASA POWER cached file: {Path(path).name}", source_type="observed/model-derived",
        temporal_resolution="daily", spatial_resolution="point",
        citation="https://power.larc.nasa.gov/docs/services/api/temporal/daily/", confidence=0.95,
    )
    return tuple(
        WeatherDay(date(int(k[:4]), int(k[4:6]), int(k[6:8])), filled["T2M_MAX"][k], filled["T2M_MIN"][k],
                   filled["RH2M"][k], filled["WS2M"][k], filled["ALLSKY_SFC_SW_DWN"][k], max(0.0, filled["PRECTOTCORR"][k]),
                   source="NASA POWER cache", provenance=prov)
        for k in dates
    )


def load_timeseries_csv(path: str | Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def remote_csv(path: str | Path, source: str, source_type: str = "satellite") -> tuple[RemoteDay, ...]:
    rows = load_timeseries_csv(path)
    out: list[RemoteDay] = []
    prov = Provenance(source=source, source_type=source_type, temporal_resolution="native", citation=None, confidence=0.85)
    fields = ("ndvi", "evi", "ndmi", "smap_surface_m3_m3", "smap_rootzone_m3_m3", "modis_et_mm", "gpm_rain_mm")
    for r in rows:
        d = date.fromisoformat(r["date"])
        kw = {k: float(r[k]) for k in fields if r.get(k) not in (None, "", "NA", "-999", "-999.0")}
        out.append(RemoteDay(d=d, source=source, provenance=prov, **kw))
    return tuple(out)


def load_gpm_csv(path: str | Path) -> tuple[RemoteDay, ...]:
    return remote_csv(path, "NASA GPM IMERG", "satellite")


def load_smap_csv(path: str | Path) -> tuple[RemoteDay, ...]:
    return remote_csv(path, "NASA SMAP", "satellite")


def load_hls_csv(path: str | Path) -> tuple[RemoteDay, ...]:
    return remote_csv(path, "NASA HLS", "satellite")


def load_modis_et_csv(path: str | Path) -> tuple[RemoteDay, ...]:
    return remote_csv(path, "NASA MODIS MOD16", "satellite")


def load_nex_gddp_csv(path: str | Path, scenario_name: str) -> tuple[WeatherDay, ...]:
    rows = load_timeseries_csv(path)
    prov = Provenance(
        source=f"NASA NEX-GDDP-CMIP6 ({scenario_name})", source_type="modeled",
        temporal_resolution="daily", spatial_resolution="downscaled grid",
        citation="https://www.nccs.nasa.gov/nex-gddp/", confidence=0.80,
    )
    return tuple(
        WeatherDay(
            d=date.fromisoformat(r["date"]), tmax_c=float(r["tmax_c"]), tmin_c=float(r["tmin_c"]),
            rh_mean_pct=float(r.get("rh_mean_pct", 70)), wind_2m_ms=float(r.get("wind_2m_ms", 1.5)),
            solar_rad_mj_m2_day=float(r.get("solar_rad_mj_m2_day", 18)), rain_mm=float(r["rain_mm"]),
            source=f"NEX-GDDP-CMIP6:{scenario_name}", provenance=prov,
        ) for r in rows
    )
