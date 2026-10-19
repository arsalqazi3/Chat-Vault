"""The inspector must describe structure without ever printing content."""

import json
from pathlib import Path

from chat_vault.data.inspect_export import main

PRIVATE_TEXT = "my-bank-pin-is-4821 and other private words"


def test_inspector_never_prints_values(tmp_path: Path, capsys) -> None:
    export = [
        {
            "uuid": "11111111-2222-3333-4444-555555555555",
            "name": PRIVATE_TEXT,
            "chat_messages": [
                {
                    "sender": "human",
                    "text": PRIVATE_TEXT,
                    "content": [{"type": "text", "text": PRIVATE_TEXT}],
                },
                # A non-identifier value in an enum field must also stay hidden.
                {"sender": PRIVATE_TEXT, "text": "x"},
            ],
            # A dict keyed by data (like ChatGPT's id -> node mapping).
            "mapping": {PRIVATE_TEXT: {"role": "user"}},
        }
    ]
    (tmp_path / "conversations.json").write_text(json.dumps(export), encoding="utf-8")

    main([str(tmp_path)])
    out = capsys.readouterr().out

    assert "my-bank-pin" not in out
    assert "private words" not in out
    assert "11111111" not in out
    # ...but the structure is described.
    assert "$[*].chat_messages[*].text" in out
    assert "human(1)" in out
    assert "mapping(1 keys hidden)" in out


def test_uuid_keys_starting_with_a_letter_are_hidden(tmp_path: Path, capsys) -> None:
    # ChatGPT's message tree is keyed by ids; some start with a letter.
    export = [{"mapping": {"aaa1b2c3-d4e5-4f60-8a7b-123456789abc": {"parent": None}}}]
    (tmp_path / "conversations.json").write_text(json.dumps(export), encoding="utf-8")
    main([str(tmp_path)])
    out = capsys.readouterr().out
    assert "aaa1b2c3" not in out
    assert "$[*].mapping{*}.parent" in out


def test_inspector_reads_zip(tmp_path: Path, capsys) -> None:
    import zipfile

    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("conversations.json", json.dumps([{"name": PRIVATE_TEXT}]))
    main([str(archive)])
    out = capsys.readouterr().out
    assert "Export zip contains 1 file(s)" in out
    assert "$[*].name: string x1" in out
    assert "my-bank-pin" not in out


def test_many_files_with_ids_are_grouped_once(tmp_path: Path, capsys) -> None:
    import zipfile

    archive = tmp_path / "projects-000.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for i in range(3):
            uid = f"0000000{i}-1111-2222-3333-444444444444"
            z.writestr(f"projects/{uid}.json", json.dumps({"name": PRIVATE_TEXT}))
    main([str(archive)])
    out = capsys.readouterr().out
    assert "3 x projects/<id>.json" in out
    assert out.count("=== projects/<id>.json") == 1  # described once, combined
    assert "$.name: string x3" in out
    assert "1111-2222" not in out and "my-bank-pin" not in out
