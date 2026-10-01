# V3 data folder

Optional files:

```text
data/v3/
├── field.geojson      # Polygon, MultiPolygon or FeatureCollection
├── soil.json           # SoilProfile-compatible JSON
├── gpm.csv             # date,gpm_rain_mm
├── smap.csv            # date,smap_surface_m3_m3,smap_rootzone_m3_m3
├── hls.csv             # date,ndvi,evi,ndmi
├── modis_et.csv        # date,modis_et_mm
├── ssp245.csv          # date,tmax_c,tmin_c,rh_mean_pct,wind_2m_ms,solar_rad_mj_m2_day,rain_mm
└── ssp585.csv          # same schema
```

NASA POWER cache files are created automatically in `data/v3/cache/` when live API mode is enabled.
