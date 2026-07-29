import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_doctor_accepts_and_reports_all_five_declared_components():
    """A doctor script that rejects the fifth submodule must fail this governance contract."""
    doctor = subprocess.run(
        ["./scripts/doctor.sh"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    output = doctor.stdout + doctor.stderr

    assert doctor.returncode == 0, (
        "workspace governance does not yet accept the fifth component"
    )
    assert ".gitmodules declares exactly 5 submodules" in output
    assert "Doctor completed with 0 failure(s)" in output
