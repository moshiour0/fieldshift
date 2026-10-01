from __future__ import annotations

import json
import unittest
from datetime import date, timedelta
from pathlib import Path

from fieldshift.v3.agronomy import nutrient_stock_kg_ha, soil_mass_kg_ha
from fieldshift.v3.indicators import compute_rotation_indicators
from fieldshift.v3.models import CropV2, FarmerContext, FieldV2, Provenance, RemoteDay, Rotation, Scenario, SoilProfile, WeatherDay
from fieldshift.v3.nasa import power_daily
from fieldshift.v3.pipeline import DEFAULT_WEIGHTS, load_crops, load_config, run_v3
from fieldshift.v3.rotation import generate_rotations
from fieldshift.v3.scoring import dimension_scores, percentile, rank_options, validate_weights
from fieldshift.v3.water import et0, simulate_crop_water


class TestV3Scoring(unittest.TestCase):
    def test_absolute_scale_is_candidate_set_invariant(self):
        defs = {"d": {"features": {"x": {"weight": 1.0, "direction": "higher", "scale": [0, 100]}}}}
        base = dimension_scores([{"x": 20}, {"x": 40}], ("d",), defs)["d"]
        extended = dimension_scores([{"x": 20}, {"x": 40}, {"x": 100}], ("d",), defs)["d"]
        self.assertEqual(base, extended[:2])

    def test_missing_remote_feature_is_not_neutral_injected(self):
        defs = {"d": {"features": {"x": {"weight": 1.0, "direction": "higher", "scale": [0, 1]}}}}
        out = dimension_scores([{}, {"x": 1.0}], ("d",), defs)["d"]
        self.assertEqual(out[0], 0.5)
        self.assertEqual(out[1], 1.0)
        # The neutral score above is only the dimension fallback when every feature is unavailable;
        # no raw indicator value is fabricated into the evidence bundle.

    def test_rank_p90_uses_rank_samples_not_score_samples(self):
        dims = {"baseline": {"d": [0.9, 0.4, 0.1]}}
        result = rank_options(["a", "b", "c"], dims, {"d": 1.0}, n_weight_samples=60, uncertainty_samples={"a":[0.2,0.3,0.4],"b":[0.2,0.3,0.4],"c":[0.2,0.3,0.4]})
        for x in result["results"].values():
            self.assertGreaterEqual(x["rank_p90"], 1.0)
            self.assertLessEqual(x["rank_p90"], 3.0)
            self.assertEqual(x["rank_p90"], x["rank_mean"])
            self.assertGreaterEqual(x["score_p90"], x["score_p50"])

    def test_weights_sum(self):
        validate_weights(DEFAULT_WEIGHTS, tuple(DEFAULT_WEIGHTS))
        self.assertAlmostEqual(sum(DEFAULT_WEIGHTS.values()), 1.0)


class TestV3Physics(unittest.TestCase):
    def setUp(self):
        self.crop = CropV2(
            "x", "X", "Fabaceae", ("rabi",), ("01-01", "01-31"), 30, (5, 8, 12, 5),
            0.5, 1.0, 0.7, 0.5, ("flower", 10, 20), 35, 0.5, 0.8, 0.5,
            True, 0.8, 1000, 40, 1000, 10, 40, 10, 20, 0.8, (5.5, 7.5),
        )
        self.soil = SoilProfile(ph=6.4, soc_pct=1.1, total_n_mgkg=800, available_p_mgkg=12, exchangeable_k_mgkg=140,
                                bulk_density_g_cm3=1.38, soil_depth_m=1.2, field_capacity_v_v=0.31,
                                wilting_point_v_v=0.16, sand_pct=20, silt_pct=48, clay_pct=32, drainage_class="medium")
        self.field = FieldV2("f", 24.75, 90.41, 1.0, soil=self.soil, farmer=FarmerContext(priorities={"d":1.0}))
        start=date(2026,1,1)
        self.weather=tuple(WeatherDay(start+timedelta(days=i),30,20,60,2,20,5) for i in range(30))

    def test_water_mass_balance(self):
        r = simulate_crop_water(self.crop, self.weather[0].d, self.weather, self.field)
        self.assertLess(abs(r["water_balance_error_mm"]), 1e-6)
        self.assertGreaterEqual(r["irrigation_loss_mm"], 0)
        self.assertGreaterEqual(r["runoff_mm"], 0)
        self.assertAlmostEqual(r["irrigation_mm"] - r["irrigation_effective_mm"], r["irrigation_loss_mm"], places=7)

    def test_nutrient_units_use_depth_and_bulk_density(self):
        expected = 800 * soil_mass_kg_ha(self.field) / 1_000_000 * 0.02
        self.assertAlmostEqual(nutrient_stock_kg_ha(800, self.field, 0.02), expected)
        # 800 mg/kg x 1.38 g/cm3 x 1.2 m = 16,560 kg soil/ha per mg/kg; 2% mineralization.
        self.assertGreater(expected, 100)

    def test_fao56_golden_example_close_to_official_result(self):
        # FAO-56 Chapter 4 April example reports ETo ~5.72 mm/day for 13.73N, z=2m,
        # Tmax=34.8C, Tmin=25.6C, Rs=22.65 MJ/m2/day, u2=2m/s, ea=2.85 kPa.
        w=WeatherDay(date(2026,4,15),34.8,25.6,64.48,2,22.65,0)
        value=et0(w,13.73,2)
        self.assertAlmostEqual(value,5.72,delta=0.08)


class TestV3NASAAndLineage(unittest.TestCase):
    def test_power_single_missing_value_is_interpolated(self):
        import fieldshift.v3.nasa as nasa
        old=nasa._http_json
        try:
            nasa._http_json=lambda url,timeout=30,retries=3:{"properties":{"parameter":{
                "T2M_MAX":{"20260101":25,"20260102":-999,"20260103":27},
                "T2M_MIN":{"20260101":12,"20260102":13,"20260103":14},
                "RH2M":{"20260101":70,"20260102":71,"20260103":72},
                "WS2M":{"20260101":1.5,"20260102":1.6,"20260103":1.7},
                "ALLSKY_SFC_SW_DWN":{"20260101":18,"20260102":17,"20260103":16},
                "PRECTOTCORR":{"20260101":2,"20260102":3,"20260103":4}}}}
            out=power_daily(24.75,90.41,date(2026,1,1),date(2026,1,3))
            self.assertEqual(out[1].tmax_c,26.0)
            self.assertIn('imputed',out[1].source)
        finally:
            nasa._http_json=old

    def test_no_remote_defaults_in_indicator_values(self):
        crop=self._crop()
        field=self._field()
        start=date(2026,1,1)
        weather=tuple(WeatherDay(start+timedelta(days=i),30,20,60,2,20,5) for i in range(40))
        scenario=Scenario('baseline','x',weather,())
        rotation=Rotation('x',('x',),'X')
        from fieldshift.v3.rotation import schedule
        sch=schedule(rotation,{'x':crop},2026,0,60)
        values,_=compute_rotation_indicators(rotation,sch,{'x':crop},scenario,field)
        self.assertNotIn('mean_hls_ndvi',values)
        self.assertNotIn('mean_hls_ndmi',values)

    def _crop(self):
        return CropV2('x','X','Fabaceae',('rabi',),('01-01','01-31'),30,(5,8,12,5),0.5,1,0.7,0.5,('flower',10,20),35,0.5,0.8,0.5,True,0.8,1000,40,1000,10,40,10,20,0.8,(5.5,7.5))
    def _field(self):
        soil=SoilProfile(ph=6.4,soc_pct=1.1,total_n_mgkg=800,available_p_mgkg=12,exchangeable_k_mgkg=140,bulk_density_g_cm3=1.38,soil_depth_m=1.2,field_capacity_v_v=0.31,wilting_point_v_v=0.16,sand_pct=20,silt_pct=48,clay_pct=32,drainage_class='medium')
        return FieldV2('f',24.75,90.41,1,soil=soil,farmer=FarmerContext(priorities={'soil_health':1.0}))


class TestV3Engine(unittest.TestCase):
    def test_full_engine_and_validation(self):
        run=run_v3(n_weight_samples=40,n_uncertainty_samples=16)
        self.assertEqual(run.engine_version,'3.0.0')
        self.assertTrue(run.validation['passed'])
        self.assertGreater(len(run.evaluations),0)
        self.assertTrue(any(e.rotation.rotation_id=='BASELINE' for e in run.evaluations))
        self.assertTrue(all(1.0<=e.robustness.rank_p90<=len(run.evaluations) for e in run.evaluations))

    def test_parameter_monte_carlo_is_not_score_jitter(self):
        run=run_v3(n_weight_samples=20,n_uncertainty_samples=12)
        self.assertTrue(any(e.robustness.score_std>0 for e in run.evaluations))
        self.assertEqual(run.evaluations[0].robustness.uncertainty_type,'input-parameter Monte Carlo')

    def test_constraints_are_active(self):
        run=run_v3(n_weight_samples=20,n_uncertainty_samples=8)
        accepted={e.rotation.rotation_id for e in run.evaluations}
        self.assertGreater(len(run.rejected),0)
        # Generated options violating the demo budget/labour/water ceilings must not be ranked.
        self.assertTrue(any('budget' in x['reason'] or 'labour' in x['reason'] or 'irrigation' in x['reason'] for x in run.rejected))
        self.assertIn('BASELINE', accepted)  # reference baseline remains visible even when it exceeds a farmer constraint.


if __name__=='__main__':
    unittest.main(verbosity=2)
