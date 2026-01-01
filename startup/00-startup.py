# Make ophyd listen to pyepics.
print(f"Loading file {__file__!r} ...")

import asyncio
import datetime
import logging
import os
import subprocess
import time as ttime
import warnings

import bluesky.plan_stubs as bps
import bluesky.plans as bp
import epicscorelibs.path.pyepics
import nslsii
import redis
from bluesky.callbacks.broker import post_run, verify_files_saved
from bluesky.callbacks.tiled_writer import TiledWriter
from bluesky.plans import count, scan
from bluesky.run_engine import RunEngine, autoawait_in_bluesky_event_loop
from bluesky_queueserver import is_re_worker_active
from IPython import get_ipython
from ophyd.sim import det, motor
from redis_json_dict import RedisJSONDict
from tiled.client import from_uri
from tiled.server import SimpleTiledServer

DEBUG = True

if DEBUG:
    RE = RunEngine()
else:
    RE = RunEngine(RedisJSONDict(redis.Redis("info.tst.nsls2.bnl.gov"), prefix=""))

if not is_re_worker_active():
    autoawait_in_bluesky_event_loop()


# Set up Tiled for data storage - wrapped in try/except for environments
# where SimpleTiledServer may not work (e.g., tiled 0.2.1 has known issues)
try:
    tiled_server = SimpleTiledServer()
    tiled_client = from_uri(tiled_server.uri)
    tiled_writer = TiledWriter(tiled_client)
    RE.subscribe(tiled_writer)
except Exception as e:
    print(f"Warning: Tiled setup failed ({type(e).__name__}: {e})")
    print("Data will not be persisted to Tiled. Configure an external Tiled server if needed.")
    tiled_server = None
    tiled_client = None
    tiled_writer = None


def dump_doc_to_stdout(name, doc):
    print("========= Emitting Doc =============")
    print(f"{name = }")
    print(f"{doc = }")
    print("============ Done ============")


RE.subscribe(dump_doc_to_stdout)


def now():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


class FileLoadingTimer:

    def __init__(self):
        self.start_time = 0
        self.loading = False

    def start_timer(self, filename):
        if self.loading:
            raise Exception("File already loading!")

        print(f"Loading {filename}...")
        self.start_time = ttime.time()
        self.loading = True

    def stop_timer(self, filename):

        elapsed = ttime.time() - self.start_time
        print(f"Done loading {filename} in {elapsed} seconds.")
        self.loading = False


warnings.filterwarnings("ignore")


def warmup_hdf5_plugins(detectors):
    """
    Warm-up the hdf5 plugins.
    This is necessary for when the corresponding IOC restarts we have to trigger one image
    for the hdf5 plugin to work correctly, else we get file writing errors.
    Parameter:
    ----------
    detectors: list
    """
    for det in detectors:
        _array_size = det.hdf5.array_size.get()
        if 0 in [_array_size.height, _array_size.width] and hasattr(det, "hdf5"):
            print(
                f"\n  Warming up HDF5 plugin for {det.name} as the array_size={_array_size}..."
            )
            det.hdf5.warmup()
            print(
                f"  Warming up HDF5 plugin for {det.name} is done. array_size={det.hdf5.array_size.get()}\n"
            )
        else:
            print(
                f"\n  Warming up of the HDF5 plugin is not needed for {det.name} as the array_size={_array_size}."
            )


def show_env():
    # this is not guaranteed to work as you can start IPython without hacking
    # the path via activate
    proc = subprocess.Popen(["conda", "list"], stdout=subprocess.PIPE)
    out, err = proc.communicate()
    a = out.decode("utf-8")
    b = a.split("\n")
    print(b[0].split("/")[-1][:-1])


TST_PROPOSAL_DIR_ROOT = "/nsls2/data/tst/legacy/mock-proposals"

RUNNING_IN_NSLS2_CI = os.environ.get("RUNNING_IN_NSLS2_CI", "NO") == "YES" or DEBUG

if RUNNING_IN_NSLS2_CI:
    print("Running in CI, using mock mode when initializing devices...")

file_loading_timer = FileLoadingTimer()
