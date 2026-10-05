"""Container liveness probe; never sends credentials or contacts AWS."""
import json
import sys
from urllib.request import urlopen


def main():
    try:
        with urlopen('http://127.0.0.1:8000/health/live', timeout=3) as response:
            return 0 if response.status == 200 and json.load(response).get('status') == 'ok' else 1
    except Exception:
        return 1


if __name__ == '__main__':
    sys.exit(main())
