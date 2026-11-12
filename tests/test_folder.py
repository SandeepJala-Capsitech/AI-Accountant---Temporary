import hashlib

from types import SimpleNamespace

from ledgersync.folder import account_for, new_files


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_import_skips_known_contents_and_lists_unreadable_files(tmp_path):
    (tmp_path / "statement.csv").write_bytes(b"Date,Amount\n01/09/2026,-12.00\n")
    (tmp_path / "statement copy.csv").write_bytes(b"Date,Amount\n01/09/2026,-12.00\n")
    (tmp_path / "old.csv").write_bytes(b"Date,Amount\n02/09/2026,-5.00\n")
    (tmp_path / ".DS_Store").write_bytes(b"x"); (tmp_path / "backup.zip").write_bytes(b"PK")
    scan = new_files(tmp_path, known={sha256(b"Date,Amount\n02/09/2026,-5.00\n")})
    assert [p.name for p, _, _ in scan.new] == ["statement copy.csv"]     # sorted path order: " " sorts before "."
    assert scan.skipped == ["old.csv", "statement.csv"]
    assert scan.unreadable == ["backup.zip"]


def test_subfolders_are_read_and_hidden_ones_are_not(tmp_path):
    (tmp_path / "2026").mkdir(); (tmp_path / ".cache").mkdir()
    (tmp_path / "2026" / "Sept.CSV").write_bytes(b"a")
    (tmp_path / ".cache" / "hidden.csv").write_bytes(b"b")
    (tmp_path / "notes.docx").write_bytes(b"c")
    scan = new_files(tmp_path, known=set())
    assert [(p.relative_to(tmp_path).as_posix(), data, digest) for p, data, digest in scan.new] == [
        ("2026/Sept.CSV", b"a", sha256(b"a"))]
    assert (scan.skipped, scan.unreadable) == ([], ["notes.docx"])


def test_a_files_name_points_at_the_account_or_card_its_statement_is_for():
    accounts = [SimpleNamespace(code="1200", name="Starling current", kind="current"),
                SimpleNamespace(code="1210", name="Starling savings", kind="savings"),
                SimpleNamespace(code="1240", name="Amex card", kind="credit_card")]
    where = {name: account_for(name, accounts, "1200") for name in (
        "starling-current-q1-2026.csv", "amex-card-jan-feb-2026.csv", "savings-march.pdf", "statement.pdf",
        "starling.csv")}
    assert where == {"starling-current-q1-2026.csv": "1200", "amex-card-jan-feb-2026.csv": "1240",
                     "savings-march.pdf": "1210", "statement.pdf": "1200",   # nothing stands out: the one chosen
                     "starling.csv": "1200"}                                # two accounts as likely: the one chosen
    assert account_for("statement.pdf", accounts, None) is None
