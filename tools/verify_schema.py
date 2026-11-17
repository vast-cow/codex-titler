"""Verify the used wire fields against an explicitly selected installed Codex binary."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

FIELDS = {
    "ThreadStartParams": [
        "ephemeral",
        "cwd",
        "model",
        "modelProvider",
        "allowProviderModelFallback",
        "config",
        "approvalPolicy",
        "sandbox",
        "environments",
        "dynamicTools",
    ],
    "TurnStartParams": ["threadId", "input", "effort", "outputSchema", "environments"],
    "ThreadTurnsListParams": [
        "threadId",
        "cursor",
        "limit",
        "itemsView",
        "sortDirection",
    ],
    "ThreadReadParams": ["threadId", "includeTurns"],
    "ThreadSetNameParams": ["threadId", "name"],
    "ThreadUnsubscribeParams": ["threadId"],
    "ModelListParams": ["modelProvider", "includeHidden", "cursor"],
    "ConfigReadParams": ["includeLayers", "cwd"],
}


def main():
    binary = sys.argv[1] if len(sys.argv) > 1 else "codex"
    version = subprocess.check_output([binary, "--version"], text=True).strip()
    with tempfile.TemporaryDirectory() as directory:
        subprocess.run(
            [
                binary,
                "app-server",
                "generate-json-schema",
                "--experimental",
                "--out",
                directory,
            ],
            check=True,
        )
        root = Path(directory) / "v2"
        for name, fields in FIELDS.items():
            schema = json.loads((root / f"{name}.json").read_text())
            assert set(fields) <= schema["properties"].keys(), name
        schema = json.loads(
            (root / "ThreadTokenUsageUpdatedNotification.json").read_text()
        )
        assert (
            "modelContextWindow"
            in schema["definitions"]["ThreadTokenUsage"]["properties"]
        )
        schema = json.loads((root / "ModelReroutedNotification.json").read_text())
        assert {"threadId", "turnId", "fromModel", "toModel", "reason"} <= schema[
            "properties"
        ].keys()
        schema = json.loads((root / "ThreadTurnsListResponse.json").read_text())
        assert any(
            "full" in view.get("enum", [])
            for view in schema["definitions"]["TurnItemsView"]["oneOf"]
        )
    print(f"Verified used protocol fields: {version}")


if __name__ == "__main__":
    main()
