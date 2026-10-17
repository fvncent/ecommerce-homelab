import pytest

from ecomgen import __version__
from ecomgen.cli import main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"ecomgen {__version__}"


def test_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: ecomgen" in capsys.readouterr().out


def test_init_db_unreachable_does_not_leak_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ECOMMERCE_DATABASE_URL", "postgresql://u:topsecret@127.0.0.1:1/x?connect_timeout=2")
    assert main(["init-db", "config/generator.example.yaml"]) == 1
    err = capsys.readouterr().err
    assert "database error" in err and "topsecret" not in err
