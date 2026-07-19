import json
import os
import tempfile

DEFAULT_STATE_PATH = "state.json"


def write_state(path, snapshot):
    directory = os.path.dirname(os.path.abspath(path))
    handle, temp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as temp_file:
            json.dump(snapshot, temp_file)
        os.replace(temp_path, path)
    except Exception:
        os.unlink(temp_path)
        raise


def read_state(path):
    try:
        with open(path) as state_file:
            return json.load(state_file)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
