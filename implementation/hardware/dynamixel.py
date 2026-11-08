"""Official SDK transport and explicit model/protocol tables, isolated from hardware core.

Only bounded single-turn POSITION commands are implemented on simulated transport.
Physical connections are read-only except best-effort torque-disable emergency stop.
No mode changes, EEPROM writes, raw torque equivalence or automatic port scanning.
"""
import math
import time
from .core import Capabilities,Channel,HardwareError

PROFILES={
 (310,1.0):{'name':'MX-64 Protocol 1','firmware':2,'torque':24,'goal':(30,2),'position':(36,2),
            'temperature':43,'limits':(6,8),'source':'https://emanual.robotis.com/docs/en/dxl/mx/mx-64/'},
 (311,2.0):{'name':'MX-64 Protocol 2','firmware':6,'torque':64,'goal':(116,4),'position':(132,4),
            'temperature':146,'mode':11,'source':'https://emanual.robotis.com/docs/en/dxl/mx/mx-64-2/'}}

class SDKTransport:
    physical=True
    def __init__(self,port,protocol,baudrate=57600,timeout_s=.1,transport='TTL'):
        if protocol not in (1.,2.) or transport not in ('TTL','RS485') or not .02<=timeout_s<=1:
            raise ValueError('Explicit supported protocol, transport and bounded timeout required')
        import dynamixel_sdk as sdk
        self.sdk=sdk;self.port=sdk.PortHandler(port);self.packet=sdk.PacketHandler(protocol)
        self.protocol=protocol;self.baudrate=baudrate;self.timeout=timeout_s;self.transport=transport
        self.opened=False
        self._stop_registers={}
    def open(self):
        if not self.port.openPort():raise HardwareError('Serial port unavailable')
        self.opened=True
        if not self.port.setBaudRate(self.baudrate):self.close();raise HardwareError('Baudrate setup failed')
        self.port.ser.write_timeout=self.timeout
    def _result(self,method,*args):
        start=time.monotonic();result=method(self.port,*args)
        if time.monotonic()-start>self.timeout:raise HardwareError('Communication deadline exceeded')
        if result[-2]!=self.sdk.COMM_SUCCESS or result[-1]!=0:
            raise HardwareError('SDK communication/device fault: %s/%s'%tuple(result[-2:]))
        return result[0] if len(result)==3 else None
    def ping(self,identifier):return self._result(self.packet.ping,identifier)
    def read(self,identifier,address,width):
        return self._result(getattr(self.packet,f'read{width}ByteTxRx'),identifier,address)
    def write(self,identifier,address,width,value):
        if self.physical and (address,width,value)!=(self._stop_registers.get(identifier),1,0):
            raise HardwareError('Real SDK writes are restricted to identified torque-disable registers')
        return self._result(getattr(self.packet,f'write{width}ByteTxRx'),identifier,address,value)
    def close(self):
        if self.opened:self.port.closePort();self.opened=False

class DynamixelDevice:
    def __init__(self,transport,identifier,protocol,expected_firmware):
        if type(identifier)is not int or not 0<=identifier<=252 or protocol not in (1.,2.) or type(expected_firmware)is not int:
            raise ValueError('Unicast ID, explicit protocol and exact expected firmware required')
        if getattr(transport,'protocol',protocol)!=protocol:raise ValueError('Transport protocol mismatch')
        self.transport=transport;self.identifier=identifier;self.protocol=protocol;self.expected_firmware=expected_firmware
        self.state='DISCONNECTED';self.profile=None;self.capabilities=Capabilities(str(identifier),{}, {},getattr(transport,'physical',True))
    def connect(self):
        self.transport.open()
        try:
            model=self.transport.ping(self.identifier)
            profile=PROFILES.get((model,self.protocol))
            if profile is None:raise HardwareError('Unknown model/protocol table: read-only identification only')
            firmware=self.transport.read(self.identifier,profile['firmware'],1)
            if firmware!=self.expected_firmware:raise HardwareError('Firmware identity mismatch')
            self.profile=profile
            if isinstance(self.transport,SDKTransport):
                self.transport._stop_registers[self.identifier]=profile['torque']
            if 'limits'in profile:
                lo,hi=[self.transport.read(self.identifier,a,2) for a in profile['limits']]
                if (lo,hi)!=(0,4095) or self.transport.read(self.identifier,22,1)!=1:
                    raise HardwareError('Only standard single-turn mode/resolution supported; wheel/multi-turn rejected')
            elif self.transport.read(self.identifier,profile['mode'],1)!=3:
                raise HardwareError('Only existing position mode supported; no automatic mode changes')
            self.capabilities=Capabilities(f'dynamixel:{self.identifier}:{model}:{firmware}',
                {'position':{'position':Channel('rad',0,4095*2*math.pi/4096)}},
                {'position':'rad','temperature':'degC'},getattr(self.transport,'physical',True),
                'Best-effort torque disable; holding torque is lost. External stop/interlock required.',
                ['Model/protocol/firmware matched','No calibrated homing transform','No torque/current/velocity command support',
                 'Real motion disabled; host stop is not a safety-rated emergency stop'])
            self.identity={'model_number':model,'firmware':firmware,'protocol':self.protocol,
                           'transport':getattr(self.transport,'transport','SIMULATED'),'table_source':profile['source']}
            self.state='CONNECTED_READ_ONLY' if self.capabilities.physical else 'READY'
            return {'identity':self.identity,'capabilities':self.capabilities.describe(),'state':self.state}
        except Exception:self.state='FAULT';self.transport.close();raise
    def command(self,mode,values,units):
        if self.capabilities.physical:raise HardwareError('PHYSICAL_MOTION_DISABLED')
        if self.state!='READY' or mode!='position' or set(values)!={'position'} or units!={'position':'rad'}:
            raise HardwareError('Unsupported state/mode/channel/unit')
        self.capabilities.modes[mode]['position'].validate(values['position'],units['position'])
        ticks=round(values['position']*4096/(2*math.pi))
        try:
            # Transport is explicitly simulated; never enables real physical torque.
            self.transport.write(self.identifier,*self.profile['goal'],ticks)
            return {'position_ticks':ticks,'physical_motion':False}
        except Exception:self.emergency_stop('Command fault');raise
    def feedback(self):
        if not self.profile or self.state not in ('READY','CONNECTED_READ_ONLY'):raise HardwareError('Device not connected')
        try:
            q=self.transport.read(self.identifier,*self.profile['position'])
            temp=self.transport.read(self.identifier,self.profile['temperature'],1)
            if not 0<=q<=4095 or not 0<=temp<=80:raise HardwareError('Position/temperature feedback outside supported safe range')
            return {'values':{'position':q*2*math.pi/4096,'temperature':temp},
                    'units':self.capabilities.feedback_units,'timestamp_monotonic':time.monotonic(),
                    'source':'HARDWARE_UNVALIDATED' if self.capabilities.physical else 'SIMULATED_TRANSPORT'}
        except Exception:
            if self.capabilities.physical:self.state='FAULT'  # Do not drop gravity-supported loads automatically.
            else:self.emergency_stop('Feedback fault')
            raise
    def emergency_stop(self,reason):
        self.state='ESTOP_LATCHED';confirmed=False
        try:
            if self.profile:
                self.transport.write(self.identifier,self.profile['torque'],1,0)
                confirmed=self.transport.read(self.identifier,self.profile['torque'],1)==0
        except Exception:confirmed=False
        return {'state':self.state,'reason':reason,'torque_disable_readback':confirmed,
                'physical_stop_verified':False,'operator_action':'External equipment-specific stop/power isolation may be required'}
