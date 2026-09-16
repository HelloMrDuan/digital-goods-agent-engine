from pathlib import Path
import re


_VALID_SCOPES = {"$env:", "$script:", "$global:", "$local:", "$private:"}


def test_real_rebuild_acceptance_has_no_drive_like_variable_interpolation():
    script = Path("scripts/real_rebuild_acceptance.ps1").read_text(encoding="utf-8")
    # In a double-quoted PowerShell string, `$name:` can be parsed like a scoped/drive
    # variable reference and fail at parse time. Valid PowerShell scopes such as
    # `$env:` and `$script:` are intentional; ordinary variables must use `${name}:`.
    matches = re.findall(r'\$[A-Za-z_][A-Za-z0-9_]*:', script)
    unsafe = [match for match in matches if match.lower() not in _VALID_SCOPES]
    assert unsafe == []


def test_real_rebuild_acceptance_cost_queries_use_same_window():
    script = Path("scripts/real_rebuild_acceptance.ps1").read_text(encoding="utf-8")
    assert script.count('/api/ai/cost?limit=200') == 2
