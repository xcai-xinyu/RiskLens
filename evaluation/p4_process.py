"""WSL fork-free hard deadline for an evaluation process and its descendants."""
import os
import signal
import sys
import time


def bounded(command,timeout):
    pid=os.posix_spawn('/usr/bin/setsid',['setsid',*command],dict(os.environ))
    deadline=time.monotonic()+timeout
    while True:
        waited,status=os.waitpid(pid,os.WNOHANG)
        if waited:return os.waitstatus_to_exitcode(status)
        if time.monotonic()>=deadline:
            try:os.killpg(pid,signal.SIGKILL)
            except ProcessLookupError:
                try:os.kill(pid,signal.SIGKILL)
                except ProcessLookupError:pass
            os.waitpid(pid,0)
            return 124
        time.sleep(.02)


def launch(timeout):
    return bounded([sys.executable,'-m','evaluation.run_eval',*sys.argv[1:],'--internal-worker'],timeout)
