"""Disposable child core for lifecycle tests. All OS/network operations are fake."""
import json
from pathlib import Path
import signal
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
from relay.core import CoreRuntime
from relay.core_service import CoreService
from relay.lifecycle import inhibited
from test_diagnostics import FakeMac, config
from test_windows import FakeEvents


def main():
    directory = Path(sys.argv[1])
    if inhibited(directory):
        return
    mac = FakeMac()
    runtime = CoreRuntime(directory, platform='darwin', runner=mac, config=config(mac, company=True),
        events_factory=FakeEvents, guard_threaded=False, execution_directory=directory / 'execution')
    event = threading.Event()
    service = CoreService(runtime, stop_requested=event, launch_id=sys.argv[2])
    for kind in (signal.SIGTERM, signal.SIGINT):
        signal.signal(kind, lambda *_: event.set())
    try:
        runtime.restore_guard()
        service.start()
        event.wait()
    finally:
        service.close()
        (directory / 'fixture-result.json').write_text(json.dumps({'commands': mac.calls, 'writes': mac.mutations}))


if __name__ == '__main__':
    main()
