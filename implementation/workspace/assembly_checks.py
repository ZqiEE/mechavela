"""Neutral-pose assembly measurements using installed OCCT, not a mate solver.

Project-authored rules are diagnostic assertions, never owner acceptance. Instance
and face selectors are meaningful only for the explicitly hashed STEP artifact.
"""
import math,re
from collections import defaultdict

SCOPE='Project-defined neutral-pose checks only; not a mate solver, finite shaft engagement, swept clearance, strength or owner requirements acceptance.'
MAX_CHECKS=1024


def feature_catalog(parts,source_sha256,geometry=None):
    if geometry is not None:return geometry.features(parts,source_sha256)
    import build123d as b
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    rows=[];truncated=False
    for i,part in enumerate(parts):
        for j,face in enumerate(part.faces()):
            if face.geom_type!=b.GeomType.CYLINDER:continue
            if len(rows)>=20000:truncated=True;break
            axis=BRepAdaptor_Surface(face.wrapped).Cylinder();line=axis.Axis();box=face.bounding_box()
            rows.append({'part':'part_'+str(i),'name':part.label,'face':j,'type':'cylinder','radius_mm':float(axis.Radius()),
                         'axis_origin_mm':list(line.Location().Coord()),'axis_direction':list(line.Direction().Coord()),
                         'bounds_mm':[list(box.min),list(box.max)],'orientation':str(face.wrapped.Orientation())})
        if truncated:break
    return {'schema':'rlsok.cad-features/1','cad_sha256':source_sha256,'units':'mm','frame':'STEP global placement',
            'features':rows,'truncated':truncated,'limit':20000,'scope':'Actual OCCT analytic cylinders. Face indices/instance IDs are artifact-local, not persistent topology IDs.'}


def number(value,name,maximum=None):
    if type(value) not in (int,float) or not math.isfinite(value) or value<0 or maximum is not None and value>maximum:
        raise ValueError(name+' must be finite and nonnegative'+(' and <= '+str(maximum) if maximum is not None else ''))
    return float(value)


def evaluate(parts,source_sha256,spec,scene=None,geometry=None):
    report={'schema':'rlsok.assembly-measurements/1','status':'NOT_DECLARED','cad_sha256':source_sha256,
            'scope':SCOPE,'authority':'PROJECT_AUTHORED_NOT_OWNER_ACCEPTANCE','checks':[]}
    if spec is None:return report
    try:
        if not isinstance(spec,dict) or set(spec)-{'schema','cad_sha256','physics_sha256','checks'} or spec.get('schema')!='rlsok.assembly-checks/1':
            raise ValueError('Expected rlsok.assembly-checks/1 with cad_sha256 and checks')
        if not isinstance(spec.get('cad_sha256'),str) or not re.fullmatch('[a-f0-9]{64}',spec['cad_sha256']):raise ValueError('Explicit STEP SHA256 required')
        checks=spec.get('checks')
        if not isinstance(checks,list) or not 1<=len(checks)<=MAX_CHECKS:raise ValueError('Provide 1..'+str(MAX_CHECKS)+' project-defined checks')
        ids=[c.get('id') for c in checks if isinstance(c,dict)]
        if len(ids)!=len(checks) or not all(isinstance(x,str) and re.fullmatch('[A-Za-z0-9_.-]{1,80}',x) for x in ids) or len({x.casefold() for x in ids})!=len(ids):
            raise ValueError('Checks require unique short IDs')
        if spec['cad_sha256']!=source_sha256:
            report.update(status='STALE_CHECKS',error='STEP changed; reselect/review features against the current artifact. No checks evaluated.');return report
        if any(c.get('kind')=='joint_axis' for c in checks):
            if not scene or spec.get('physics_sha256')!=scene.get('model_sha256'):
                report.update(status='STALE_CHECKS',error='Native physics hash missing or changed; review joint selectors. No checks evaluated.');return report
    except (ValueError,TypeError) as e:
        report.update(status='INVALID_SPEC',error=str(e));return report
    if geometry is None:geometry=OCPGeometry()
    by_id={'part_'+str(i):p for i,p in enumerate(parts)};by_name=defaultdict(list)
    for i,p in enumerate(parts):by_name[p.label].append('part_'+str(i))
    def select(value,cylinder=False):
        selectors=[{'part','face'},{'name','face'}] if cylinder else [{'part'},{'name'}]
        if not isinstance(value,dict) or set(value) not in selectors:
            raise ValueError('Select an artifact instance with part ID or an explicit unique name; include face for a cylinder')
        key=value.get('part',value.get('name'))
        if not isinstance(key,str):raise ValueError('Part selector must be text')
        if 'name' in value:
            matches=by_name.get(key,[])
            if len(matches)!=1:raise ValueError('Unknown or ambiguous part; use exact artifact instance ID: '+key)
            key=matches[0]
        if key not in by_id:raise ValueError('Unknown artifact instance ID: '+key)
        part=by_id[key]
        if not cylinder:return key,part
        index=value['face']
        if type(index) is not int or index<0:raise ValueError('Face index must be a nonnegative integer')
        faces=part.faces()
        if index>=len(faces):raise ValueError('Selected face is not an actual cylinder; inspect feature catalogue')
        return key,geometry.cylinder(faces[index])
    for check in checks:
        row={'id':check['id'],'kind':check.get('kind'),'status':'ERROR','parts':[],'criteria':check}
        try:
            kind=check.get('kind')
            expected={'id','kind','a','b','minimum_mm'} if kind=='clearance' else {'id','kind','a','b','max_distance_mm','max_angle_deg'} if kind=='coaxial' else {'id','kind','a','joint','max_distance_mm','max_angle_deg'} if kind=='joint_axis' else set()
            if not expected or set(check)!=expected:raise ValueError('Unknown check kind or fields')
            if kind=='clearance':
                minimum=number(check['minimum_mm'],'minimum_mm');ia,a=select(check['a']);ib,z=select(check['b']);row['parts']=[ia,ib]
                if ia==ib:raise ValueError('Choose two different instances')
                row['measured']=geometry.distance(a,z);distance=row['measured']['clearance_mm'];inside=row['measured']['inner_solution']
                passed=distance>=minimum and not inside
            else:
                maximum=number(check['max_distance_mm'],'max_distance_mm');angle_max=number(check['max_angle_deg'],'max_angle_deg',90)
                ia,a=select(check['a'],True);row['parts']=[ia]
                if kind=='coaxial':
                    ib,z=select(check['b'],True);row['parts'].append(ib)
                    if ia==ib and check['a']['face']==check['b']['face']:raise ValueError('Choose two different cylindrical features')
                else:
                    joint=next((j for j in scene.get('joints',[]) if j['name']==check['joint']),None)
                    if not joint or joint['type'] not in ('hinge','slide'):raise ValueError('Choose an actual native hinge/slide joint')
                    z=geometry.joint(joint)
                row['measured']=geometry.axes(a,z);distance=row['measured']['axis_distance_mm'];angle=row['measured']['axis_angle_deg']
                passed=distance<=maximum and angle<=angle_max
            row['status']='PASS' if passed else 'FAIL'
        except Exception as e:
            row['error']=type(e).__name__+': '+str(e)[:1000]
        report['checks'].append(row)
    statuses=[r['status'] for r in report['checks']]
    report['counts']={s:statuses.count(s) for s in ['PASS','FAIL','ERROR']}
    report['status']='PROJECT_CHECKS_ERROR' if 'ERROR' in statuses else 'PROJECT_CHECKS_FAILED' if 'FAIL' in statuses else 'PROJECT_CHECKS_PASSED'
    return report


class OCPGeometry:
    """Legacy provider kept lazy; a FreeCAD inspection never imports OCP."""
    def cylinder(self,face):
        import build123d as b
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        if face.geom_type!=b.GeomType.CYLINDER:raise ValueError('Selected face is not an actual cylinder; inspect feature catalogue')
        return BRepAdaptor_Surface(face.wrapped).Cylinder().Axis()
    def distance(self,a,z):
        from OCP.BRepExtrema import BRepExtrema_DistShapeShape
        tool=BRepExtrema_DistShapeShape(a.wrapped,z.wrapped)
        if not tool.IsDone() or not tool.NbSolution():raise ValueError('OCCT minimum-distance calculation did not complete')
        inside=bool(tool.InnerSolution())
        return {'clearance_mm':0. if inside else float(tool.Value()),'native_surface_distance_mm':float(tool.Value()),'inner_solution':inside,'nearest_points_mm':[list(tool.PointOnShape1(1).Coord()),list(tool.PointOnShape2(1).Coord())]}
    def joint(self,joint):
        from OCP.gp import gp_Lin,gp_Pnt,gp_Dir
        return gp_Lin(gp_Pnt(*(x*1000 for x in joint['world_position'])),gp_Dir(*joint['world_axis'])).Position()
    def axes(self,a,z):
        from OCP.gp import gp_Lin
        angle=math.degrees(a.Direction().Angle(z.Direction()))
        return {'axis_distance_mm':float(gp_Lin(a).Distance(gp_Lin(z))),'axis_angle_deg':min(angle,180-angle),'axis_origins_mm':[list(a.Location().Coord()),list(z.Location().Coord())],'scope':'Infinite axis lines only; finite engagement and axial offsets are not certified'}
