"""Project-scoped bridge to existing ngspice, netlist validation, metrics and BOM code."""
import gzip,json,os,sys
from pathlib import Path
from .projects import read,write,encode
ROOT=Path(__file__).resolve().parents[2]

def defaults():
    return {'circuit.json':encode({'dut_body':['R1 vin vout 10k','R2 vout gnd 10k']}).decode(),
        'electrical-requirements.json':encode({'interpretation':'10 V to 5 V into 10k load; reference electronics only, not a robot power supply',
            'ports':['vin','vout','gnd'],'testbench':['VIN in 0 DC 10','XDUT in out 0 dut','RLOAD out 0 10k'],
            'analysis':{'kind':'op'},'vectors':['v(out)','vin#branch'],
            'metrics':{'vout':{'vector':'v(out)','stat':'value','unit':'V'}},
            'checks':[{'id':'vout_band','metric':'vout','op':'between','min':4.5,'max':5.5,'unit':'V'}],
            'operating_conditions':{'input_voltage':{'value':10,'unit':'V'}},'assumptions':['Ideal resistors; fixed testbench load; reference circuit only']}).decode()}

def adapter():
    from implementation import solver_interface as SI
    bundled=ROOT/'thirdparty/ngspice/bin/ngspice.dll'
    if bundled.exists():os.environ['RLSOK_NGSPICE_LIBRARY']=str(bundled)
    os.environ['RLSOK_WORKER_PYTHON']=sys.executable
    return SI.get('ngspice')

def validate(folder):
    from implementation.circuit import spec as S,netlist as N
    a=adapter();req=read(folder/'electrical-requirements.json');S.validate_spec(req,a)
    design=read(folder/'circuit.json')
    N.validate_dut_body(design['dut_body'],req['ports'])
    job={'netlist':N.assemble('RLSOK project electrical analysis',req['testbench'],req['ports'],design['dut_body']),
         'analysis':req['analysis'],'vectors':req['vectors']}
    a.validate(job)
    if len(job['netlist'])>30000:raise ValueError('Circuit size limit')
    return a,job,req,design

def run(folder):
    from implementation.solver_interface import ResourceCaps
    from implementation.circuit import metrics as M,bom as B,netlist as N
    a,job,req,design=validate(folder)
    native=a.run(job,folder/'native',ResourceCaps(cpu_seconds=10,wall_seconds=20,memory_bytes=536870912))
    if native.status!='SUCCEEDED':raise RuntimeError('ngspice: '+str(native.reason))
    with gzip.open(folder/'native/vectors.json.gz','rt',encoding='utf-8') as f:data=json.load(f)
    measured,errors=M.compute_all(req['metrics'],data,req['analysis']['kind'])
    checks=M.evaluate_checks(req['checks'],measured,errors);passed=not errors and all(c['passed'] for c in checks)
    # Use the existing BOM parser; no price/availability or buildability certification inferred.
    dut=N.validate_dut_body(design['dut_body'],req['ports'])
    bom=B.build_bom(dut,0.0)
    (folder/'BOM.csv').write_text(B.bom_csv(bom),encoding='utf-8')
    result={'adapter':'ngspice.circuit/1','solver':native.tool,'criteria':req,'measured':measured,'metric_errors':errors,
        'checks':{c['id']:c['passed'] for c in checks},'check_details':checks,'passed':passed,'warnings':[],
        'levels':{'proposal':'RECORDED','artifact':'GENERATED','static':'PASSED','simulation':'COMPLETED',
            'requirements':'PASSED_SIMULATION' if passed else 'FAILED','manufacturing':'NOT_REVIEWED','physical':'NOT_TESTED'},
        'limitations':['Circuit simulation only; no PCB/ERC/DRC or physical qualification','No automatic power-system integration or component availability guarantee']}
    write(folder/'result.json',result)

def verify(folder,result):
    from implementation.circuit import metrics as M
    req=read(folder/'electrical-requirements.json')
    with gzip.open(folder/'native/vectors.json.gz','rt',encoding='utf-8') as f:data=json.load(f)
    measured,errors=M.compute_all(req['metrics'],data,req['analysis']['kind'])
    checks=M.evaluate_checks(req['checks'],measured,errors)
    if result['measured']!=measured or result['criteria']!=req or result['check_details']!=checks or result['passed']!=(not errors and all(c['passed'] for c in checks)):
        raise ValueError('Electrical native evidence mismatch')
