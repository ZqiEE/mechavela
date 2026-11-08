"""Units, modes and arbitrary channels; no motor vendor, joint or morphology assumptions."""
from dataclasses import dataclass, field, asdict
import math
import time
import threading
import uuid
from typing import Protocol

class HardwareError(RuntimeError): pass

@dataclass(frozen=True)
class Channel:
    unit: str
    minimum: float
    maximum: float
    def validate(self,value,unit):
        if unit!=self.unit or type(value) not in (int,float) or not math.isfinite(value) or not self.minimum<=value<=self.maximum:
            raise HardwareError('Command unit/value outside declared channel capability')

@dataclass
class Capabilities:
    device_id: str
    modes: dict[str,dict[str,Channel]]
    feedback_units: dict[str,str]
    physical: bool=False
    stop_effect: str='Zero simulated outputs; no physical safety claim'
    constraints: list[str]=field(default_factory=list)
    def describe(self): return asdict(self)

class Device(Protocol):
    capabilities: Capabilities
    state: str
    def command(self,mode:str,values:dict,units:dict): ...
    def feedback(self)->dict: ...
    def emergency_stop(self,reason:str)->dict: ...

class SimulatedDevice:
    """Command transport for a solver/custom simulator. Does not invent physical dynamics."""
    def __init__(self,capabilities):
        if capabilities.physical:raise HardwareError('Physical capabilities cannot use simulated transport')
        self.capabilities=capabilities;self.state='DISCONNECTED';self.outputs={};self.observation={}
    def connect(self):self.state='READY';return self.capabilities.describe()
    def command(self,mode,values,units):
        if self.state!='READY':raise HardwareError('Device is not ready')
        channels=self.capabilities.modes.get(mode)
        if channels is None or set(values)!=set(channels) or set(units)!=set(channels):
            raise HardwareError('Unsupported mode or incomplete channels')
        for name,spec in channels.items():spec.validate(values[name],units[name])
        self.outputs=dict(values);return dict(self.outputs)
    def observe(self,values):
        if set(values)!=set(self.capabilities.feedback_units):raise HardwareError('Feedback schema mismatch')
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in values.values()):
            self.emergency_stop('Nonfinite simulated feedback');raise HardwareError('Nonfinite feedback')
        self.observation={'values':dict(values),'units':dict(self.capabilities.feedback_units),
                          'timestamp_monotonic':time.monotonic(),'source':'SIMULATOR'}
    def feedback(self):
        if not self.observation:raise HardwareError('No simulator observation available')
        return self.observation
    def emergency_stop(self,reason):
        self.outputs={k:0. for k in self.outputs};self.state='ESTOP_LATCHED'
        return {'state':self.state,'reason':reason,'effect':self.capabilities.stop_effect,'confirmed':True}

class SafetySession:
    """Explicit operator grant, short lease, watchdog and latched faults for test transports.

    No web/AI tool grants this lease. Real equipment requires equipment-specific
    interlock verification; this release deliberately refuses physical arming.
    """
    def __init__(self,device,timeout_s=.2):
        if not .02<=timeout_s<=2:raise ValueError('Watchdog timeout must be .02..2s')
        self.device=device;self.timeout=timeout_s;self.permit=None;self.deadline=0.;self.closed=threading.Event()
        self.thread=threading.Thread(target=self._watch,daemon=True);self.thread.start()
    def authorize(self,operator,config):
        if self.device.capabilities.physical:
            raise HardwareError('PHYSICAL_MOTION_DISABLED: equipment-specific safe configuration not verified')
        if not operator or config.get('device_id')!=self.device.capabilities.device_id or config.get('mode') not in self.device.capabilities.modes or not config.get('stop_reviewed'):
            raise HardwareError('Explicit operator and matching reviewed test configuration required')
        if self.device.state!='READY':raise HardwareError('Reconnect/review required after fault')
        self.permit={'id':uuid.uuid4().hex,'operator':operator,'mode':config['mode']}
        self.deadline=time.monotonic()+self.timeout
        return dict(self.permit)
    def command(self,permit,mode,values,units):
        if not self.permit or permit!=self.permit['id'] or mode!=self.permit['mode'] or time.monotonic()>self.deadline:
            self.stop('Missing/expired operator authorization');raise HardwareError('Authorization required')
        try:result=self.device.command(mode,values,units)
        except Exception:
            self.stop('Command/communication fault');raise
        self.deadline=time.monotonic()+self.timeout
        return result
    def stop(self,reason):
        self.permit=None
        return self.device.emergency_stop(reason)
    def _watch(self):
        while not self.closed.wait(.01):
            if self.permit and time.monotonic()>self.deadline:
                try:self.stop('Host command watchdog expired')
                except Exception:self.permit=None
    def close(self):
        self.closed.set();self.thread.join(1)
        if self.permit:self.stop('Session closed')
