"""Terminal keyboard handling."""

import sys
import time


def wait_for_key_or_timeout(seconds: float) -> bool:
    if sys.platform == "win32":
        import msvcrt

        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                msvcrt.getch()
                return True
            time.sleep(0.05)
        return False
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        ready, _, _ = select.select([sys.stdin], [], [], seconds)
        if ready:
            sys.stdin.read(1)
            return True
        return False
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
