"""Cooperative pause and bounded, checkpointed CPU batch execution."""

from concurrent.futures import ProcessPoolExecutor,wait,FIRST_COMPLETED
from multiprocessing import get_context
from pathlib import Path
import os


def check_pause(path):
    if path and Path(path).exists():
        raise InterruptedError("Paused after saving the completed batch")


def batches(jobs,function,save,workers,pause_file,initializer=None,initargs=()):
    names = ("OPENBLAS_NUM_THREADS","OMP_NUM_THREADS","NUMBA_NUM_THREADS")
    previous = {name:os.environ.get(name) for name in names}
    try:
        for name in names:os.environ[name] = "1"
        return _run_batches(jobs,function,save,workers,pause_file,initializer,initargs)
    finally:
        for name,value in previous.items():
            if value is None:os.environ.pop(name,None)
            else:os.environ[name] = value


def _run_batches(jobs,function,save,workers,pause_file,initializer=None,initargs=()):
    """Keep only one running job per worker; save all in-flight work on pause."""
    iterator = iter(jobs)
    stopped = False
    with ProcessPoolExecutor(max_workers=workers,mp_context=get_context("spawn"),
                             initializer=initializer,initargs=initargs) as pool:
        pending = {}
        def submit():
            nonlocal stopped
            if pause_file and Path(pause_file).exists():
                stopped = True
                return
            job = next(iterator,None)
            if job is not None:
                args,key = job
                pending[pool.submit(function,*args)] = key
        for _ in range(workers):submit()
        while pending:
            done,_ = wait(pending,return_when=FIRST_COMPLETED)
            for future in done:
                key = pending.pop(future)
                save(key,future.result())
                submit()
    if stopped:check_pause(pause_file)
