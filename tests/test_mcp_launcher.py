import subprocess
import sys


def test_pgsage_mcp_explains_a_missing_extra_instead_of_a_traceback() -> None:
    script = "import sys; sys.modules['mcp'] = None; from pgsage.mcp_main import main; main()"
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 2
    assert "pip install 'pgsage[mcp]'" in result.stderr
    assert "Traceback" not in result.stderr


def test_the_module_entry_point_runs_the_cli() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pgsage", "--version"], capture_output=True, text=True
    )
    assert result.returncode == 0 and result.stdout.startswith("pgsage ")
