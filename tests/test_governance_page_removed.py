"""The 'removed in v2' note lists each removed exemption once (issue #93 follow-up)."""

from project_health.governance.policy import load_policy
from project_health.site.governance_page import _removed_exemptions


def test_each_removed_exemption_listed_once_with_all_its_rules():
    removed = _removed_exemptions(load_policy())
    ids = [r.exemption_id for r in removed]
    assert sorted(ids) == sorted(set(ids)), ids
    by_id = {r.exemption_id: r for r in removed}
    assert set(by_id) == {"ninja", "submodule-repin"}
    assert by_id["ninja"].rule_ids == ["reviewer-present", "jira-ticket-referenced"]
    # the full reason wins over the short cross-reference one
    assert "No official source" in by_id["ninja"].reason
    assert "see reviewer-present" not in by_id["ninja"].reason
