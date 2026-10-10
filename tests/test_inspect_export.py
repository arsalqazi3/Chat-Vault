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
