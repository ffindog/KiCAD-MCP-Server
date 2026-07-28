"""Regression tests for DynamicSymbolLoader.find_library_file.

Covers the global-sym-lib-table fallback and the quoted-URI parsing path.
The bug these guard against: libraries registered via KiCad's GUI
(Preferences > Manage Symbol Libraries > Global) live in the user-global
sym-lib-table only; the loader previously consulted only the project-local
table and a hardcoded list of bundled symbol directories, so any company
library mounted from OneDrive / a network share / a custom path was invisible
to add_schematic_component.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.dynamic_symbol_loader import DynamicSymbolLoader


def _write_lib(tmp_path: Path, name: str, symbols: list) -> Path:
    """Write a minimal .kicad_sym file with the given symbol names."""
    parts = [f"(kicad_symbol_lib (version 20231120) (generator test)"]
    for sym in symbols:
        parts.append(f'  (symbol "{sym}" (pin_numbers (hide yes)) (pin_names (hide yes))')
        parts.append(f'    (property "Reference" "R" (at 0 0 0))')
        parts.append(f'    (property "Value" "{sym}" (at 0 0 0))')
        parts.append(f"  )")
    parts.append(")")
    path = tmp_path / f"{name}.kicad_sym"
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def _write_table(table_path: Path, libs: list) -> None:
    """Write a sym-lib-table file.

    libs = list of (name, uri, quote_uri) or (name, uri, quote_uri, type).
    type defaults to "KiCad"; pass "Table" for an included-table entry.
    """
    lines = ["(sym_lib_table"]
    for entry in libs:
        name, uri, quote = entry[0], entry[1], entry[2]
        lib_type = entry[3] if len(entry) > 3 else "KiCad"
        uri_str = f'"{uri}"' if quote else uri
        lines.append(
            f'  (lib (name "{name}")(type "{lib_type}")(uri {uri_str})(options "")(descr ""))'
        )
    lines.append(")")
    table_path.parent.mkdir(parents=True, exist_ok=True)
    table_path.write_text("\n".join(lines), encoding="utf-8")


def _global_table_path(fake_home: Path, version: str = "9.0") -> Path:
    if os.name == "nt":
        return fake_home / "AppData" / "Roaming" / "kicad" / version / "sym-lib-table"
    return fake_home / ".config" / "kicad" / version / "sym-lib-table"


def test_global_sym_lib_table_resolves_library(monkeypatch, tmp_path):
    """Library registered only in the user-global table must resolve."""
    # Lay out a fake user home with the library in a non-standard location
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    lib_dir = tmp_path / "external_libs"
    lib_dir.mkdir()
    lib_file = _write_lib(lib_dir, "MyCompanyLib", ["R_220"])

    # Place the user-global sym-lib-table where Windows KiCad keeps it
    if os.name == "nt":
        global_table = fake_home / "AppData" / "Roaming" / "kicad" / "9.0" / "sym-lib-table"
    else:
        global_table = fake_home / ".config" / "kicad" / "9.0" / "sym-lib-table"
    _write_table(global_table, [("MyCompanyLib", str(lib_file), True)])

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    loader = DynamicSymbolLoader(project_path=None)

    resolved = loader.find_library_file("MyCompanyLib")
    assert resolved is not None
    assert Path(resolved).resolve() == lib_file.resolve()


def test_quoted_uri_with_spaces(monkeypatch, tmp_path):
    """URIs containing spaces (e.g. OneDrive paths) must be parsed correctly."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    # Path with embedded space, like 'OneDrive - Company'
    lib_dir = tmp_path / "OneDrive - Company" / "Documents" / "KiCad" / "9.0" / "symbols"
    lib_dir.mkdir(parents=True)
    lib_file = _write_lib(lib_dir, "SpacedLib", ["R_390"])

    if os.name == "nt":
        global_table = fake_home / "AppData" / "Roaming" / "kicad" / "9.0" / "sym-lib-table"
    else:
        global_table = fake_home / ".config" / "kicad" / "9.0" / "sym-lib-table"
    _write_table(global_table, [("SpacedLib", str(lib_file), True)])

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    loader = DynamicSymbolLoader(project_path=None)

    resolved = loader.find_library_file("SpacedLib")
    assert resolved is not None
    assert Path(resolved).resolve() == lib_file.resolve()
    # And the symbol can be extracted end-to-end through the resolved path
    block = loader.extract_symbol_from_library("SpacedLib", "R_390")
    assert block is not None
    assert "R_390" in block


def test_project_table_still_takes_precedence(monkeypatch, tmp_path):
    """Project-local sym-lib-table must override the global one."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    # Two libraries with the same nickname but different content
    project_lib_dir = tmp_path / "proj_libs"
    project_lib_dir.mkdir()
    project_lib = _write_lib(project_lib_dir, "DualLib", ["FROM_PROJECT"])

    global_lib_dir = tmp_path / "global_libs"
    global_lib_dir.mkdir()
    global_lib = _write_lib(global_lib_dir, "DualLib", ["FROM_GLOBAL"])

    project_path = tmp_path / "project"
    project_path.mkdir()
    _write_table(project_path / "sym-lib-table", [("DualLib", str(project_lib), True)])

    if os.name == "nt":
        global_table = fake_home / "AppData" / "Roaming" / "kicad" / "9.0" / "sym-lib-table"
    else:
        global_table = fake_home / ".config" / "kicad" / "9.0" / "sym-lib-table"
    _write_table(global_table, [("DualLib", str(global_lib), True)])

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    loader = DynamicSymbolLoader(project_path=str(project_path))

    resolved = loader.find_library_file("DualLib")
    assert Path(resolved).resolve() == project_lib.resolve()
    # Confirm content came from project, not global
    block = loader.extract_symbol_from_library("DualLib", "FROM_PROJECT")
    assert block is not None


def test_unknown_library_returns_none(monkeypatch, tmp_path):
    """Looking up a library that isn't in any table returns None, not a path."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    loader = DynamicSymbolLoader(project_path=None)
    assert loader.find_library_file("NoSuchLib") is None


def test_included_table_entry_is_followed(monkeypatch, tmp_path):
    """A (type "Table") entry names another sym-lib-table and must be followed.

    This is KiCad 10's global layout: the user table lists no libraries at all,
    only a Table entry pointing at the bundled template table. Not following it
    made the newest version's table look empty, so lookups fell through to an
    older KiCad's table and cached that version's symbols.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    lib_dir = tmp_path / "kicad10" / "symbols"
    lib_dir.mkdir(parents=True)
    lib_file = _write_lib(lib_dir, "Device", ["R_Small"])

    # The bundled template table holds the real entries
    template = tmp_path / "kicad10" / "template" / "sym-lib-table"
    _write_table(template, [("Device", str(lib_file), True)])

    # The user-global table only points at it
    _write_table(
        _global_table_path(fake_home, "10.0"),
        [("KiCad", str(template), True, "Table")],
    )

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    loader = DynamicSymbolLoader(project_path=None)

    resolved = loader.find_library_file("Device")
    assert resolved is not None, "Table indirection was not followed"
    assert Path(resolved).resolve() == lib_file.resolve()


def test_direct_entry_wins_over_included_table(monkeypatch, tmp_path):
    """A direct entry in a table beats one reached through its Table include."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    direct_dir = tmp_path / "direct"
    direct_dir.mkdir()
    direct_lib = _write_lib(direct_dir, "DualLib", ["FROM_DIRECT"])

    included_dir = tmp_path / "included"
    included_dir.mkdir()
    included_lib = _write_lib(included_dir, "DualLib", ["FROM_INCLUDED"])

    template = tmp_path / "template" / "sym-lib-table"
    _write_table(template, [("DualLib", str(included_lib), True)])

    # Include listed FIRST, direct entry second -- order must not decide it
    _write_table(
        _global_table_path(fake_home, "10.0"),
        [
            ("KiCad", str(template), True, "Table"),
            ("DualLib", str(direct_lib), True),
        ],
    )

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    loader = DynamicSymbolLoader(project_path=None)

    assert Path(loader.find_library_file("DualLib")).resolve() == direct_lib.resolve()


def test_self_referencing_table_does_not_recurse_forever(monkeypatch, tmp_path):
    """A table that includes itself must terminate and return None."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    table = _global_table_path(fake_home, "10.0")
    table.parent.mkdir(parents=True, exist_ok=True)
    _write_table(table, [("Loop", str(table), True, "Table")])

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    loader = DynamicSymbolLoader(project_path=None)

    assert loader.find_library_file("Whatever") is None


def test_mutually_recursive_tables_terminate(monkeypatch, tmp_path):
    """Two tables including each other must terminate rather than hang."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    a = tmp_path / "a" / "sym-lib-table"
    b = tmp_path / "b" / "sym-lib-table"
    _write_table(a, [("B", str(b), True, "Table")])
    _write_table(b, [("A", str(a), True, "Table")])
    _write_table(_global_table_path(fake_home, "10.0"), [("A", str(a), True, "Table")])

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    loader = DynamicSymbolLoader(project_path=None)

    assert loader.find_library_file("Missing") is None
