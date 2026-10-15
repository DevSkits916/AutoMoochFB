"""Cross-process ownership of the shared Auto Mooch browser profile."""
from contextlib import asynccontextmanager
from pathlib import Path
import os


class ProfileBusyError(RuntimeError):
    pass


@asynccontextmanager
async def profile_lease(profile):
    profile = Path(profile)
    profile.parent.mkdir(parents=True, exist_ok=True)
    handle = (profile.parent / '.browser-profile.lock').open('a+b')
    acquired = False
    try:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ProfileBusyError('The shared browser profile is in use. Press Stop in the other Auto Mooch app and wait for its browser to close, then try again.') from error
        acquired = True
        yield profile
    finally:
        if acquired:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
