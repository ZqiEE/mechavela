"""Bound a trusted native tool and all descendants using a Windows Job Object.

Create the process suspended, assign it before any tool code runs, then resume.
No elevation, shell, policy changes, detached children or guessed exit codes.
"""
import ctypes
from ctypes import wintypes as w
import msvcrt
import os
import subprocess
import time
from pathlib import Path

kernel = ctypes.WinDLL('kernel32', use_last_error=True)
HANDLE = w.HANDLE
SIZE = ctypes.c_size_t
LL = ctypes.c_longlong
ULL = ctypes.c_ulonglong

class Limits(ctypes.Structure):
    _fields_ = [('process_time',LL),('job_time',LL),('flags',w.DWORD),('min_ws',SIZE),('max_ws',SIZE),('active_limit',w.DWORD),('affinity',SIZE),('priority',w.DWORD),('schedule',w.DWORD)]
class IO(ctypes.Structure):
    _fields_ = [(name,ULL) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
class Extended(ctypes.Structure):
    _fields_ = [('basic',Limits),('io',IO),('process_memory',SIZE),('job_memory',SIZE),('peak_process_memory',SIZE),('peak_job_memory',SIZE)]
class Accounting(ctypes.Structure):
    _fields_ = [('user',LL),('system',LL),('period_user',LL),('period_system',LL),('page_faults',w.DWORD),('total',w.DWORD),('active',w.DWORD),('terminated',w.DWORD)]
class Startup(ctypes.Structure):
    _fields_ = [('cb',w.DWORD),('reserved',w.LPWSTR),('desktop',w.LPWSTR),('title',w.LPWSTR),
                *[(name,w.DWORD) for name in ('x','y','xsize','ysize','xchars','ychars','fill','flags')],
                ('show',w.WORD),('reserved_size',w.WORD),('reserved2',ctypes.POINTER(ctypes.c_byte)),
                ('stdin',HANDLE),('stdout',HANDLE),('stderr',HANDLE)]
class ProcessInfo(ctypes.Structure):
    _fields_ = [('process',HANDLE),('thread',HANDLE),('pid',w.DWORD),('tid',w.DWORD)]

kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR];kernel.CreateJobObjectW.restype=HANDLE
kernel.SetInformationJobObject.argtypes=[HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
kernel.QueryInformationJobObject.argtypes=[HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD,ctypes.c_void_p]
kernel.AssignProcessToJobObject.argtypes=[HANDLE,HANDLE]
kernel.CreateProcessW.argtypes=[w.LPCWSTR,w.LPWSTR,ctypes.c_void_p,ctypes.c_void_p,w.BOOL,w.DWORD,ctypes.c_void_p,w.LPCWSTR,ctypes.POINTER(Startup),ctypes.POINTER(ProcessInfo)]
kernel.ResumeThread.argtypes=[HANDLE];kernel.ResumeThread.restype=w.DWORD
kernel.TerminateJobObject.argtypes=[HANDLE,w.UINT]
kernel.TerminateProcess.argtypes=[HANDLE,w.UINT]
kernel.GetExitCodeProcess.argtypes=[HANDLE,ctypes.POINTER(w.DWORD)]
kernel.CloseHandle.argtypes=[HANDLE]
kernel.WaitForSingleObject.argtypes=[HANDLE,w.DWORD];kernel.WaitForSingleObject.restype=w.DWORD

def checked(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value

def run(argv, directory, cpu_cap, wall_cap, memory_cap=4294967296, env=None, disk_root=None, disk_cap=None, cancel=lambda: False):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=False)
    if cpu_cap <= 0 or wall_cap <= 0 or not Path(argv[0]).is_absolute():
        raise ValueError('absolute tool and positive resource caps required')
    job=checked(kernel.CreateJobObjectW(None,None));info=ProcessInfo();started=False;assigned=False
    limit=Extended();limit.basic.flags=0x2000|0x200;limit.job_memory=memory_cap
    start=time.monotonic();cpu_start=time.process_time();limit_event=None;terminated_at=None
    next_disk_check=0;disk_peak=0
    environment=ctypes.create_unicode_buffer('\0'.join(k+'='+str(v) for k,v in sorted(env.items()))+'\0\0') if env else None
    try:
        checked(kernel.SetInformationJobObject(job,9,ctypes.byref(limit),ctypes.sizeof(limit)))
        with open(os.devnull,'rb') as stdin, (directory/'stdout.log').open('wb') as stdout, (directory/'stderr.log').open('wb') as stderr:
            handles=[msvcrt.get_osfhandle(f.fileno()) for f in (stdin,stdout,stderr)]
            for handle in handles:os.set_handle_inheritable(handle,True)
            startup=Startup();startup.cb=ctypes.sizeof(startup);startup.flags=0x100|0x1;startup.show=0
            startup.stdin,startup.stdout,startup.stderr=handles
            command=ctypes.create_unicode_buffer(subprocess.list2cmdline([str(x) for x in argv]))
            create_begin=time.perf_counter_ns()
            checked(kernel.CreateProcessW(str(argv[0]),command,None,None,True,0x4|0x08000000|0x400,environment,str(directory),ctypes.byref(startup),ctypes.byref(info)))
            create_end=time.perf_counter_ns()
            started=True
            checked(kernel.AssignProcessToJobObject(job,info.process))
            assigned=True
            if kernel.ResumeThread(info.thread)==0xffffffff:raise ctypes.WinError(ctypes.get_last_error())
            resumed=time.perf_counter_ns()
            while True:
                account=Accounting();checked(kernel.QueryInformationJobObject(job,1,ctypes.byref(account),ctypes.sizeof(account),None))
                elapsed=time.monotonic()-start;cpu=(account.user+account.system)/10000000
                disk_exceeded=False
                if disk_root and elapsed>=next_disk_check:
                    disk_used=0
                    for root,dirs,files in os.walk(disk_root,followlinks=False):
                        for name in files:
                            try:disk_used+=os.stat(os.path.join(root,name),follow_symlinks=False).st_size
                            except FileNotFoundError:pass
                    disk_peak=max(disk_peak,disk_used);next_disk_check=elapsed+2
                    disk_exceeded=disk_cap is not None and disk_used>disk_cap
                total_cpu=cpu+time.process_time()-cpu_start
                cancelled = cancel()
                if not limit_event and (cancelled or elapsed>wall_cap or total_cpu>cpu_cap or disk_exceeded):
                    limit_event='CANCELLED' if cancelled else 'DISK_LIMIT' if disk_exceeded else 'WALL_LIMIT' if elapsed>wall_cap else 'CPU_LIMIT'
                    checked(kernel.TerminateJobObject(job,124))
                    terminated_at=time.monotonic()
                if account.active==0:break
                if terminated_at and time.monotonic()-terminated_at>10:
                    raise RuntimeError('job termination not confirmed; keep resource reservation held')
                time.sleep(.05)
            checked(kernel.QueryInformationJobObject(job,9,ctypes.byref(limit),ctypes.sizeof(limit),None))
            code=w.DWORD();checked(kernel.GetExitCodeProcess(info.process,ctypes.byref(code)))
            return {'argv':[str(x) for x in argv],'exit_code':code.value,'cpu_seconds':(account.user+account.system)/10000000,
                    'timing':{'process_creation_begin_ns':create_begin,'process_creation_end_ns':create_end,
                              'process_resumed_ns':resumed,'job_quiescent_ns':time.perf_counter_ns()},
                    'wall_seconds':time.monotonic()-start,'memory_peak_bytes':limit.peak_job_memory,
                    'limit_event':limit_event,'all_job_processes_quiescent':account.active==0,
                    'disk_peak_observed_bytes':disk_peak,'disk_monitor_scope':str(disk_root) if disk_root else None,
                    'disk_monitor_interval_seconds':2 if disk_root else None,
                    'resource_provider':'Windows Job Object, user+kernel CPU, job memory; not cgroup',
                    'stdout':str(directory/'stdout.log'),'stderr':str(directory/'stderr.log')}
    finally:
        # Closing the job kills all descendants if any exceptional path remains.
        kernel.CloseHandle(job)
        if started:
            if not assigned:kernel.TerminateProcess(info.process,125)
            kernel.WaitForSingleObject(info.process,5000)
            kernel.CloseHandle(info.thread);kernel.CloseHandle(info.process)
