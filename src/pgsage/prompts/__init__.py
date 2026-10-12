from importlib.resources import files

SYSTEM_PROMPT = (files(__package__) / "system.md").read_text(encoding="utf-8")
