from pathlib import Path
import re


def test_real_rebuild_acceptance_has_no_drive_like_variable_interpolation():
    script = Path("scripts/real_rebuild_acceptance.ps1").read_text(encoding="utf-8")
    # In a double-quoted PowerShell string, `$name:` is parsed like a scoped/drive
    # variable reference and can fail at parse time. Use `${name}:` instead.
    unsafe = re.findall(r'\$[A-Za-z_][A-Za-z0-9_]*:', script)
    assert unsafe == []


def test_real_rebuild_acceptance_cost_queries_use_same_window():
    script = Path("scripts/real_rebuild_acceptance.ps1").read_text(encoding="utf-8")
    assert script.count('/api/ai/cost?limit=200') == 2
