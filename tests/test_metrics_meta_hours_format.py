from project_health.site.metrics_meta import M0_METRICS


def test_hours_metric_big_number_uses_day_units_like_days_cards():
    meta = M0_METRICS["time_to_first_response_gh_issue"]
    assert meta.value_kind == "hours"
    assert meta.format_value(1330.7) == "55.4 days"
    assert meta.format_value(5.0) == "5.0 h"
    assert meta.format_value(0.5) == "30 min"
