"""Necessary Gmsh-INP to CalculiX model/load conversion, never a mesh/FE solver.

Only the proposed rectangular-bar reference scope is supported. Parsing or
writing an input deck never grants physical qualification or launches a tool.
"""
import argparse
from collections import Counter,defaultdict
import hashlib
import itertools
import json
import math
from pathlib import Path


def finite(value):
    if type(value) not in (int,float) or not math.isfinite(value): raise ValueError('FINITE_SOLID_SI_PARAMETER_REQUIRED')
    return float(value)


def sub(a,b): return [a[i]-b[i] for i in range(3)]
def cross(a,b): return [a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]
def dot(a,b): return math.fsum(x*y for x,y in zip(a,b))
def near(a,b): return math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-12)


def parse_gmsh_inp(path):
    path = Path(path)
    if path.stat().st_size > 33554432: raise ValueError('BOUNDED_NATIVE_GMSH_INP_REQUIRED')
    nodes,elements = {},{}; section = None
    for raw in path.read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if not line or line.startswith('**'): continue
        if line.startswith('*'):
            fields = [field.strip().upper() for field in line.split(',')]
            section = fields[0]
            if section == '*ELEMENT' and 'TYPE=C3D4' not in fields:
                raise ValueError('ONLY_NATIVE_LINEAR_TETRAHEDRA_IN_THIS_CONVERTER')
            if section not in ('*HEADING','*NODE','*ELEMENT','*NSET','*ELSET'):
                raise ValueError('UNSUPPORTED_GMSH_INP_CARD')
            continue
        values = [field.strip() for field in line.split(',') if field.strip()]
        if section == '*NODE':
            if len(values) != 4: raise ValueError('THREE_COORDINATES_PER_NATIVE_NODE_REQUIRED')
            key = int(values[0])
            if key <= 0 or key in nodes: raise ValueError('UNIQUE_POSITIVE_NODE_IDS_REQUIRED')
            nodes[key] = tuple(finite(float(v)) for v in values[1:])
        elif section == '*ELEMENT':
            if len(values) != 5: raise ValueError('FOUR_NODES_PER_TETRAHEDRON_REQUIRED')
            key = int(values[0]); connectivity = tuple(int(v) for v in values[1:])
            if key <= 0 or key in elements or len(set(connectivity)) != 4: raise ValueError('UNIQUE_VALID_TETRAHEDRA_REQUIRED')
            elements[key] = connectivity
    if not nodes or not elements or len(nodes)>50000 or len(elements)>150000:
        raise ValueError('FINITE_NATIVE_VOLUME_MESH_REQUIRED')
    if set(itertools.chain.from_iterable(elements.values())) != set(nodes):
        raise ValueError('ALL_NATIVE_NODES_MUST_BELONG_TO_VOLUME_ELEMENTS')
    if len({tuple(sorted(v)) for v in elements.values()}) != len(elements): raise ValueError('DUPLICATE_VOLUME_CELL')
    return nodes,elements


def inspect_mesh(nodes,elements,dimensions):
    length,width,height = map(finite,dimensions)
    if min(length,width,height) <= 0: raise ValueError('POSITIVE_SI_BAR_DIMENSIONS_REQUIRED')
    bounds = (length,width,height)
    for xyz in nodes.values():
        if any(not -1e-12 <= value <= bounds[i]+1e-12 for i,value in enumerate(xyz)):
            raise ValueError('MESH_OUTSIDE_REQUESTED_GEOMETRY_OR_UNITS')
    counts = Counter(); volume = 0.
    for element in elements.values():
        a,b,c,d = (nodes[key] for key in element)
        determinant = dot(sub(b,a),cross(sub(c,a),sub(d,a)))
        if determinant <= 1e-24: raise ValueError('DEGENERATE_OR_INVERTED_NATIVE_TETRAHEDRON')
        volume += determinant/6
        for face in itertools.combinations(element,3): counts[tuple(sorted(face))] += 1
    if any(count > 2 for count in counts.values()) or not near(volume,length*width*height):
        raise ValueError('NONMANIFOLD_OR_INCOMPLETE_BAR_MESH')
    left = sorted(key for key,xyz in nodes.items() if near(xyz[0],0))
    right = sorted(key for key,xyz in nodes.items() if near(xyz[0],length))
    origin = [key for key,xyz in nodes.items() if all(near(v,0) for v in xyz)]
    anchor = [key for key,xyz in nodes.items() if near(xyz[0],0) and near(xyz[1],width) and near(xyz[2],0)]
    if not left or not right or len(origin)!=1 or len(anchor)!=1:
        raise ValueError('GEOMETRIC_BOUNDARY_AND_RIGID_MODE_ANCHORS_REQUIRED')
    weights = defaultdict(float); area = 0.
    for face,count in counts.items():
        if count != 1 or not all(key in right for key in face): continue
        a,b,c = (nodes[key] for key in face)
        normal = cross(sub(b,a),sub(c,a)); triangle_area = math.sqrt(dot(normal,normal))/2
        if triangle_area <= 0: raise ValueError('DEGENERATE_LOAD_FACE')
        area += triangle_area
        for key in face: weights[key] += triangle_area/3
    if not near(area,width*height) or set(weights) != set(right): raise ValueError('COMPLETE_NATIVE_RIGHT_FACE_REQUIRED')
    return {'volume_m3':volume,'right_area_m2':area,'right_nodal_areas_m2':dict(weights),
            'left':left,'right':right,'origin':origin[0],'anchor':anchor[0]}


def nset(name,ids):
    return ['*NSET,NSET='+name]+[','.join(str(v) for v in ids[i:i+16]) for i in range(0,len(ids),16)]


def build_deck(mesh_path,plan,kind,force_factor=1.,conductivity_factor=1.):
    if kind not in ('structural','thermal'): raise ValueError('EXPLICIT_SOLID_ANALYSIS_KIND_REQUIRED')
    if plan.get('schema') != 'rlsok.solid-reference-preparation/1': raise ValueError('EXPLICIT_REFERENCE_MODEL_AND_MATERIAL_REQUIRED')
    nodes,elements = parse_gmsh_inp(mesh_path)
    dimensions = plan['geometry']['dimensions_m']; geometry = inspect_mesh(nodes,elements,dimensions)
    material = plan['material']; young,poisson,k = map(finite,(material['young_modulus_Pa'],material['poisson_ratio'],material['conductivity_W_per_m_K']))
    force_factor,conductivity_factor = finite(force_factor),finite(conductivity_factor)
    if min(young,k,force_factor,conductivity_factor)<=0 or not -1 < poisson < .5:
        raise ValueError('POSITIVE_STABLE_ISOTROPIC_REFERENCE_PARAMETERS_REQUIRED')
    lines = ['*HEADING','RLSOK native Gmsh mesh / CalculiX input conversion. SI units. Physical result NOT_RUN.','*NODE']
    lines += [str(key)+','+','.join(format(v,'.17g') for v in nodes[key]) for key in sorted(nodes)]
    lines += ['*ELEMENT,TYPE='+('C3D4' if kind=='structural' else 'DC3D4')+',ELSET=DOMAIN']
    lines += [str(key)+','+','.join(str(v) for v in elements[key]) for key in sorted(elements)]
    lines += nset('NALL',sorted(nodes))+nset('LEFT',geometry['left'])+nset('RIGHT',geometry['right'])
    lines += ['*MATERIAL,NAME=ASSUMED_REFERENCE','*ELASTIC',format(young,'.17g')+','+str(poisson),
              '*CONDUCTIVITY',format(k*conductivity_factor,'.17g'),'*DENSITY',str(material['density_kg_per_m3']),
              '*SPECIFIC HEAT',str(material['specific_heat_J_per_kg_K']),'*SOLID SECTION,ELSET=DOMAIN,MATERIAL=ASSUMED_REFERENCE']
    if kind == 'structural':
        force = finite(plan['structural']['force_x_N'])*force_factor
        pressure = force/geometry['right_area_m2']
        loads = {key:pressure*area for key,area in geometry['right_nodal_areas_m2'].items()}
        if not near(math.fsum(loads.values()),force): raise ValueError('LOAD_TRANSFER_MUST_CONSERVE_FORCE')
        lines += ['*BOUNDARY','LEFT,1,1,0',str(geometry['origin'])+',2,3,0',str(geometry['anchor'])+',3,3,0',
                  '*STEP','*STATIC','*CLOAD']
        lines += [str(key)+',1,'+format(load,'.17g') for key,load in sorted(loads.items())]
        lines += ['*NODE PRINT,NSET=NALL','U,RF','*EL PRINT,ELSET=DOMAIN','S',
                  '*NODE FILE','U,RF','*EL FILE','S','*END STEP']
        reference = {'stress_xx_Pa':force/geometry['right_area_m2'],
                     'right_displacement_x_m':force*dimensions[0]/(young*geometry['right_area_m2']),
                     'left_total_reaction_x_N':-force,'total_applied_force_x_N':math.fsum(loads.values())}
    else:
        cold,hot = map(finite,(plan['thermal']['left_temperature_K'],plan['thermal']['right_temperature_K']))
        lines += ['*INITIAL CONDITIONS,TYPE=TEMPERATURE','NALL,'+str(cold),'*STEP','*HEAT TRANSFER,STEADY STATE','1.,1.',
                  '*BOUNDARY','LEFT,11,11,'+str(cold),'RIGHT,11,11,'+str(hot),
                  '*NODE PRINT,NSET=NALL','NT,RFL','*EL PRINT,ELSET=DOMAIN','HFL',
                  '*NODE FILE','NT','*EL FILE','HFL','*END STEP']
        flux = -k*conductivity_factor*(hot-cold)/dimensions[0]
        reference = {'left_temperature_K':cold,'right_temperature_K':hot,'heat_flux_x_W_per_m2':flux,
                     'heat_rate_magnitude_W':abs(flux)*geometry['right_area_m2']}
    return '\n'.join(lines)+'\n',{'status':'INPUT_DECK_PREPARED_NOT_SIMULATED','kind':kind,
        'nodes':len(nodes),'elements':len(elements),'geometry':geometry,'analytical_reference_not_native':reference,
        'professional_solver_executions':0,'physical_validation':'NOT_RUN','platform_goal_achieved':False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mesh',required=True); p.add_argument('--plan',required=True)
    p.add_argument('--kind',choices=['structural','thermal'],required=True); p.add_argument('--out',required=True)
    p.add_argument('--force-factor',type=float,default=1.); p.add_argument('--conductivity-factor',type=float,default=1.)
    args = p.parse_args(); out = Path(args.out)
    if out.exists() or out.with_suffix('.conversion.json').exists(): raise ValueError('FRESH_CONVERTED_DESIGN_ONLY')
    text,report = build_deck(args.mesh,json.loads(Path(args.plan).read_text()),args.kind,args.force_factor,args.conductivity_factor)
    report['source_mesh_sha256'] = hashlib.sha256(Path(args.mesh).read_bytes()).hexdigest()
    report['source_plan_sha256'] = hashlib.sha256(Path(args.plan).read_bytes()).hexdigest()
    out.write_text(text,encoding='utf-8')
    report['input_deck_sha256'] = hashlib.sha256(out.read_bytes()).hexdigest()
    out.with_suffix('.conversion.json').write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps(report,allow_nan=False))


if __name__ == '__main__': main()
