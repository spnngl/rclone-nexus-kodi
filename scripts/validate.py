#!/usr/bin/env python3
"""Run lightweight checks that do not require a Kodi installation."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PYTHON_FILES = [ROOT / "addon.py", ROOT / "service.py", *sorted((ROOT / "resources/lib").rglob("*.py"))]
SETTINGS = ROOT / "resources/settings.xml"
XML_FILES = [ROOT / "addon.xml", SETTINGS]
LANGUAGE_DIR = ROOT / "resources/language"
SOURCE_LANGUAGE = "resource.language.en_gb"

# String ID range owned by each source file (documented in resources/lib/ariostv/i18n.py).
ID_RANGES = {
    SETTINGS: range(30000, 30100),
    ROOT / "addon.py": range(30100, 30500),
    ROOT / "service.py": range(30500, 30600),
    ROOT / "resources/lib/ariostv/library_manager.py": range(30600, 30800),
    ROOT / "resources/lib/ariostv/rclone_backend.py": range(30800, 31000),
}
PO_ENTRY = re.compile(
    r'^msgctxt "#(\d+)"\nmsgid "((?:[^"\\]|\\.)*)"\nmsgstr "((?:[^"\\]|\\.)*)"$', re.M
)
PLACEHOLDER = re.compile(r"(?<!\{)\{(\w*)\}(?!\})")

# Spanish text must only live in the es_es catalogue.
SPANISH_MARKERS = (
    "No se ", "Ajustes", "Biblioteca", "Contenido nuevo", "Favoritos", "Herramientas",
    "Sincroniz", "Diagnóstico", "Carpeta", "Películas", "NUEVO", "Añadidos",
    "Eliminados", "Limpieza", "Arquitectura elegida", "Último reporte",
)
ALLOWED_COMPATIBILITY_MARKERS = ("'si'", "'sí'", "'automatico'", "'automático'")


def load_catalogue(path: Path, errors: list[str]) -> dict[int, tuple[str, str]]:
    text = path.read_text(encoding="utf-8")
    entries: dict[int, tuple[str, str]] = {}
    for string_id, msgid, msgstr in PO_ENTRY.findall(text):
        if int(string_id) in entries:
            errors.append(
                f"Duplicate string ID {string_id} in {path.relative_to(ROOT)}"
            )
        entries[int(string_id)] = (msgid, msgstr)
    if text.count("msgctxt ") != len(entries):
        errors.append(
            f"Malformed entries in {path.relative_to(ROOT)} (msgid/msgstr must be single-line)"
        )
    return entries


def used_string_ids(
    source: dict[int, tuple[str, str]], errors: list[str]
) -> set[int]:
    """Collect IDs passed to tr() and check each call's fields match the msgid placeholders."""
    used: set[int] = set()
    for path in PYTHON_FILES:
        allowed = ID_RANGES.get(path, range(0))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "tr"
            ):
                continue
            where = f"{path.relative_to(ROOT)}:{node.lineno}"
            first = node.args[0] if len(node.args) == 1 else None
            if not (isinstance(first, ast.Constant) and type(first.value) is int):
                errors.append(f"{where}: tr() needs exactly one literal integer ID")
                continue
            if any(keyword.arg is None for keyword in node.keywords):
                errors.append(f"{where}: tr() fields must be explicit keywords")
                continue
            string_id = first.value
            used.add(string_id)
            if string_id not in allowed:
                errors.append(f"{where}: string ID {string_id} is outside this file's range")
            if string_id in source:
                expected = set(PLACEHOLDER.findall(source[string_id][0]))
                given = {keyword.arg for keyword in node.keywords}
                if expected != given:
                    errors.append(
                        f"{where}: tr({string_id}) fields {sorted(given)} != placeholders {sorted(expected)}"
                    )
    for element in ET.parse(SETTINGS).getroot().iter():
        label = element.attrib.get("label")
        if label is None:
            continue
        if not label.isdigit() or int(label) not in ID_RANGES[SETTINGS]:
            errors.append(
                f"Settings label must be a string ID in {ID_RANGES[SETTINGS]}: {label!r}"
            )
        else:
            used.add(int(label))
    return used


def check_languages(errors: list[str]) -> None:
    source = load_catalogue(LANGUAGE_DIR / SOURCE_LANGUAGE / "strings.po", errors)
    used = used_string_ids(source, errors)
    for string_id in sorted(used - source.keys()):
        errors.append(
            f"String ID {string_id} is used but missing from {SOURCE_LANGUAGE}"
        )
    for string_id in sorted(source.keys() - used):
        errors.append(f"String ID {string_id} is defined but never used")

    source_path = (LANGUAGE_DIR / SOURCE_LANGUAGE / "strings.po").resolve()
    for directory in sorted(LANGUAGE_DIR.iterdir()):
        if not directory.is_dir():
            continue
        path = directory / "strings.po"
        if not path.is_file():
            errors.append(f"{directory.name} has no strings.po (or a broken symlink)")
            continue
        # Regional variants are symlinks to a full catalogue; English ones need no msgstr.
        if path.resolve() == source_path:
            continue
        catalogue = load_catalogue(path, errors)
        if catalogue.keys() != source.keys():
            errors.append(f"{directory.name} string IDs differ from {SOURCE_LANGUAGE}")
        for string_id, (msgid, msgstr) in catalogue.items():
            if string_id not in source:
                continue
            if msgid != source[string_id][0]:
                errors.append(
                    f"{directory.name} #{string_id}: msgid differs from {SOURCE_LANGUAGE}"
                )
            if not msgstr:
                errors.append(f"{directory.name} #{string_id}: missing translation")
            elif set(PLACEHOLDER.findall(msgid)) != set(PLACEHOLDER.findall(msgstr)):
                errors.append(
                    f"{directory.name} #{string_id}: placeholders differ from msgid"
                )


def main() -> int:
    errors: list[str] = []
    for path in PYTHON_FILES:
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except Exception as exc:
            errors.append(f"Python parse error in {path.relative_to(ROOT)}: {exc}")

    for path in XML_FILES:
        try:
            ET.parse(path)
        except Exception as exc:
            errors.append(f"XML parse error in {path.relative_to(ROOT)}: {exc}")

    manifest = ET.parse(ROOT / "addon.xml").getroot()
    if manifest.attrib.get("id") != "plugin.ariostv":
        errors.append("addon.xml has an unexpected add-on id")

    check_languages(errors)

    for path in [
        *PYTHON_FILES,
        SETTINGS,
        LANGUAGE_DIR / SOURCE_LANGUAGE / "strings.po",
        ROOT / "README.md",
        ROOT / "CHANGELOG.md",
    ]:
        text = path.read_text(encoding="utf-8")
        scrubbed = text
        for marker in ALLOWED_COMPATIBILITY_MARKERS:
            scrubbed = scrubbed.replace(marker, "")
        for marker in SPANISH_MARKERS:
            if marker in scrubbed:
                errors.append(f"Possible untranslated text in {path.relative_to(ROOT)}: {marker!r}")

    if errors:
        print("Validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print("Validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
