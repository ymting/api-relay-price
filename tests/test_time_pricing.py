"""分时计价（闲时/忙时、工作日与周末）解析与换算的回归测试。

只使用合成表达式，不联网、不执行远端表达式：解析结果全部来自 integrations/pricing.py
里的白名单求值 + 7×24 小时静态抽样。
"""

import unittest

import fetch_prices as fp
from integrations.pricing import (build_peak_windows, parse_clock_span,
                                  parse_time_pricing, scale_time_pricing)

SITE = {"id": "fixture", "name": "Fixture"}

# 真实站点上抓到的两种写法
HOUR_FACTORS = (
    '(tier("空闲期", p * 1.5 + c * 4.5 + cr * 0.05))'
    ' * (hour("Asia/Shanghai") >= 14 && hour("Asia/Shanghai") < 18 ? 2 : 1)'
    ' * (hour("Asia/Shanghai") >= 9 && hour("Asia/Shanghai") < 12 ? 2 : 1)'
)
WEEKDAY_FACTORS = (
    '(tier("base", p * 1.5 + c * 4.5 + cr * 0.05))'
    ' * (weekday("Asia/Shanghai") >= 1 && weekday("Asia/Shanghai") < 6'
    ' && hour("Asia/Shanghai") >= 9 && hour("Asia/Shanghai") < 12 ? 2 : 1)'
    ' * (weekday("Asia/Shanghai") >= 1 && weekday("Asia/Shanghai") < 6'
    ' && hour("Asia/Shanghai") >= 14 && hour("Asia/Shanghai") < 18 ? 2 : 1)'
)
TIME_TERNARY = (
    '((hour("Asia/Shanghai") >= 9 && hour("Asia/Shanghai") < 12)'
    ' || (hour("Asia/Shanghai") >= 14 && hour("Asia/Shanghai") < 18))'
    ' ? (tier("peak", p * 9 + c * 27 + cr * 0.3))'
    ' : (tier("off_peak", p * 4.5 + c * 13.5 + cr * 0.15))'
)


class HourFactorTests(unittest.TestCase):
    def test_two_peak_windows_merge_into_four_segments(self):
        info = parse_time_pricing(HOUR_FACTORS)
        self.assertIsNotNone(info)
        self.assertEqual(info["tz"], "Asia/Shanghai")
        # 09-12 ×2、14-18 ×2、其余 ×1；18:00 到次日 09:00 同价要合成一段（跨零点）
        self.assertEqual([(w["start"], w["end"], w["multiplier"]) for w in info["windows"]],
                         [(540, 720, 2.0), (720, 840, 1.0), (840, 1080, 2.0), (1080, 540, 1.0)])
        self.assertTrue(info["windows"][-1]["wrap"])
        # 主表用最低时段价：忙时 ×2 不能盖住底价
        self.assertAlmostEqual(info["min"]["input"], 1.5)
        self.assertAlmostEqual(info["max"]["input"], 3.0)
        self.assertTrue(info["variants"])

    def test_peak_window_is_labelled_peak_not_the_base_tier_name(self):
        # 表达式里 tier 标签是「空闲期」，但这一段时间实际是 ×2，展示要按倍率说
        labels = [w["label"] for w in parse_time_pricing(HOUR_FACTORS)["windows"]]
        self.assertEqual(labels, ["高峰", "闲时", "高峰", "闲时"])


class WeekdayFactorTests(unittest.TestCase):
    def test_weekend_has_no_peak(self):
        info = parse_time_pricing(WEEKDAY_FACTORS)
        self.assertIsNotNone(info)
        weekday = [w for w in info["windows"] if w["days"] == [1, 2, 3, 4, 5]]
        weekend = [w for w in info["windows"] if w["days"] == [6, 7]]
        self.assertEqual(len(weekday), 4)
        self.assertEqual(len(weekend), 1)
        self.assertEqual(weekend[0]["day_label"], "周六、周日")
        self.assertEqual(weekend[0]["multiplier"], 1.0)
        self.assertEqual((weekend[0]["start"], weekend[0]["end"]), (0, 1440))


class TimeTernaryTests(unittest.TestCase):
    def test_branches_become_peak_and_off_peak_tiers(self):
        info = parse_time_pricing(TIME_TERNARY)
        self.assertIsNotNone(info)
        peak = [w for w in info["windows"] if w["multiplier"] > 1]
        off = [w for w in info["windows"] if w["multiplier"] == 1]
        self.assertEqual(len(peak), 2)
        self.assertEqual(len(off), 2)
        self.assertAlmostEqual(peak[0]["input"], 9.0)     # 分叉里的绝对值 9，不是 4.5×2
        self.assertAlmostEqual(off[0]["input"], 4.5)
        self.assertEqual(off[0]["label"], "闲时")          # off_peak 标签要译成中文
        self.assertAlmostEqual(info["min"]["input"], 4.5)


class RejectionTests(unittest.TestCase):
    def test_unparsable_time_expression_stays_reference_price(self):
        for expr in (
            'hour("Asia/Shanghai") >= 9.5 ? tier("peak", p * 9) : tier("off", p * 4.5)',  # 非整点
            'hour("Asia/Shanghai") >= 9 ? tier("peak", p * 9) : tier("off", rand())',      # 未知函数
            'hour("Asia/Shanghai") >= 9 ? tier("peak", p * 9) : tier("off", p * 4.5)'
            ' * (hour("UTC") >= 9 ? 2 : 1)',                                               # 两个时区
            'hour("Asia/Shanghai") >= 9 ? tier("peak", p * c) : tier("off", p * 4.5)',     # 非线性
        ):
            with self.subTest(expr=expr):
                self.assertIsNone(parse_time_pricing(expr))

    def test_expressions_without_time_terms_are_left_to_the_old_parser(self):
        self.assertIsNone(parse_time_pricing('len >= 272000 ? tier("a", p * 4) : tier("b", p * 2)'))


class AdapterIntegrationTests(unittest.TestCase):
    def newapi(self, expr, ratio=2):
        result = fp.parse_newapi_pricing(SITE, {
            "group_ratio": {"vip": ratio},
            "data": [{"model_name": "fixture-model", "quota_type": 0, "billing_mode": "tiered_expr",
                      "billing_expr": expr, "enable_groups": ["vip"]}],
        })
        return result["offers"][0]

    def test_offer_keeps_lowest_window_price_and_all_windows(self):
        offer = self.newapi(TIME_TERNARY, ratio=2)
        self.assertEqual(offer["pricing_basis"], "time_windows")
        self.assertTrue(offer["pricing_verified"])
        self.assertTrue(offer["time_variant"])
        self.assertAlmostEqual(offer["input"], 9.0)      # 4.5 × 2（分组倍率）
        self.assertAlmostEqual(offer["output"], 27.0)
        self.assertEqual(len(offer["price_windows"]), 4)
        high = [w for w in offer["price_windows"] if w["multiplier"] > 1][0]
        self.assertAlmostEqual(high["input"], 18.0)      # 9 × 2
        self.assertEqual(offer["time_windows"]["tz"], "Asia/Shanghai")

    def test_price_windows_are_not_reference_price(self):
        offer = self.newapi(HOUR_FACTORS)
        self.assertFalse(offer["pricing_verified"] is False)
        self.assertEqual(offer["pricing_basis"], "time_windows")

    def test_unknown_time_expression_still_has_no_price(self):
        offer = self.newapi('hour("Asia/Shanghai") >= 9.5 ? tier("p", p * 9) : tier("o", p * 4)')
        self.assertIsNone(offer["input"])
        self.assertFalse(offer["pricing_verified"])
        self.assertEqual(offer["pricing_basis"], "unknown_expression")

    def test_scaling_multiplies_every_window(self):
        info = scale_time_pricing(parse_time_pricing(TIME_TERNARY), 0.5)
        self.assertAlmostEqual(info["min"]["input"], 2.25)
        self.assertAlmostEqual(max(w["input"] for w in info["windows"]), 4.5)

    def test_sub2api_peak_rate_becomes_windows(self):
        payload = {"groups": [{
            "name": "vip", "rate_multiplier": 1, "peak_rate_enabled": True,
            "peak_start": "09:00", "peak_end": "12:00", "peak_rate_multiplier": 2,
            "models": [{"name": "fixture-model", "pricing": {"input_price": 3e-6, "output_price": 15e-6}}],
        }]}
        offer = fp.parse_sub2api_pricing(SITE, payload)["offers"][0]
        self.assertEqual(offer["pricing_basis"], "time_windows")
        self.assertTrue(offer["pricing_verified"])
        self.assertAlmostEqual(offer["input"], 3.0)          # 闲时 3e-6 × 1e6
        windows = offer["price_windows"]
        self.assertEqual([(w["start"], w["end"], w["multiplier"]) for w in windows],
                         [(0, 540, 1.0), (540, 720, 2.0), (720, 1440, 1.0)])
        peak = [w for w in windows if w["peak"]][0]
        self.assertAlmostEqual(peak["input"], 6.0)
        self.assertTrue(offer["time_windows"]["assumed_timezone"])

    def test_sub2api_peak_without_parsable_span_stays_pending(self):
        payload = {"groups": [{
            "name": "vip", "rate_multiplier": 1, "peak_rate_enabled": True,
            "peak_start": "", "peak_end": "", "peak_rate_multiplier": 2,
            "models": [{"name": "fixture-model", "pricing": {"input_price": 3e-6}}],
        }]}
        offer = fp.parse_sub2api_pricing(SITE, payload)["offers"][0]
        self.assertFalse(offer["pricing_verified"])
        self.assertEqual(offer["pricing_basis"], "base_rate_pending_dynamic")
        self.assertEqual(offer["price_windows"], [])


class ClockSpanTests(unittest.TestCase):
    def test_accepts_hour_and_minute_forms(self):
        self.assertEqual(parse_clock_span("9:00", "18:30"), (540, 1110))
        self.assertEqual(parse_clock_span("09:00:00", "12:00:00"), (540, 720))

    def test_rejects_bad_or_empty_spans(self):
        for start, end in (("", "12:00"), ("09:00", ""), ("09:00", "09:00"),
                           ("25:00", "12:00"), ("09:60", "12:00"), (None, None)):
            with self.subTest(start=start, end=end):
                self.assertIsNone(parse_clock_span(start, end))

    def test_peak_window_wrapping_midnight(self):
        info = build_peak_windows({"input": 1.0}, None, 2, 22 * 60, 6 * 60)
        peak = [w for w in info["windows"] if w["peak"]][0]
        self.assertTrue(peak["wrap"])
        off = [w for w in info["windows"] if not w["peak"]][0]
        self.assertEqual((off["start"], off["end"]), (360, 1320))


if __name__ == "__main__":
    unittest.main()
