from pathlib import Path
from datetime import date
import json
import os
import tempfile

class JsonStore:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "data.json"

    def _read(self):
        if not self.path.exists():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("stored document must be an object")
        return value

    def _write(self, value):
        self.root.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".data-", suffix=".json", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + " must be a nonempty string")
    return value.strip()

def positive(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(label + " must be a positive integer")
    return value

def calendar_date(value, label):
    # Strict calendar date in YYYY-MM-DD: the string is trimmed, then matched
    # digit-for-digit (ASCII only) before datetime.date validates the real
    # calendar day, so alternate formats and impossible dates are both rejected.
    # The value is caller-supplied; the system clock is never read.
    if not isinstance(value, str):
        raise ValueError(label + " must be a YYYY-MM-DD date string")
    value = value.strip()
    digits = value[:4] + value[5:7] + value[8:]
    ascii_digits = all("0" <= char <= "9" for char in digits)
    if len(value) != 10 or value[4] != "-" or value[7] != "-" or not ascii_digits:
        raise ValueError(label + " must be a YYYY-MM-DD date string")
    year, month, day = int(value[:4]), int(value[5:7]), int(value[8:])
    try:
        date(year, month, day)
    except ValueError:
        raise ValueError(label + " must be a real calendar date") from None
    return value
