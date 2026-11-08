"""Geometry adapter for the external FreeCAD host, not a CAD engine."""
import math
from dataclasses import dataclass

@dataclass
class NativePart:
    label:str
    shape:object
    colors:list
    def faces(self):return self.shape.Faces

def coordinates(v):return [float(v.x),float(v.y),float(v.z)]
def face_palette(colors,count):
    """FreeCAD import returns one color for a uniform part, or a per-face list.
    Import.export's explicit tuple callback addresses Face1..FaceN individually.
    Broadcast a common color before passing it to that callback.
    """
    palette=colors or [(.6,.65,.7,1.0)]
    return palette*count if len(palette)==1 else palette

def bounds(shape):
    b=shape.optimalBoundingBox(False)
    return [[b.XMin,b.YMin,b.ZMin],[b.XMax,b.YMax,b.ZMax]]

class FreeCADGeometry:
    def __init__(self):
        import FreeCAD,Part
        self.App=FreeCAD;self.Part=Part
    def cylinder(self,face):
        s=face.Surface
        if not isinstance(s,self.Part.Cylinder):raise ValueError('Selected face is not an actual cylinder; inspect feature catalogue')
        return s.Center,s.Axis
    def distance(self,a,z):
        distance,points,_=a.shape.distToShape(z.shape)
        if not points:raise ValueError('Native distance calculation did not complete')
        overlap=float(a.shape.common(z.shape).Volume);inside=overlap>1e-9
        return {'clearance_mm':0. if inside else float(distance),'native_surface_distance_mm':float(distance),'inner_solution':inside,
                'nearest_points_mm':[coordinates(p) for p in points[0]],'common_volume_mm3':overlap,'provider':'FreeCAD Part.distToShape / common'}
    def joint(self,joint):return self.App.Vector(*(x*1000 for x in joint['world_position'])),self.App.Vector(*joint['world_axis'])
    def axes(self,a,z):
        p,u=a;q,v=z;cross=u.cross(v);delta=q-p
        distance=abs(delta.dot(cross))/cross.Length if cross.Length>1e-12 else q.distanceToLine(p,u)
        angle=math.degrees(u.getAngle(v))
        return {'axis_distance_mm':float(distance),'axis_angle_deg':min(angle,180-angle),'axis_origins_mm':[coordinates(p),coordinates(q)],'scope':'Infinite axis lines only; finite engagement and axial offsets are not certified'}
    def features(self,parts,sha):
        rows=[];truncated=False
        for i,part in enumerate(parts):
            for j,face in enumerate(part.faces()):
                if not isinstance(face.Surface,self.Part.Cylinder):continue
                if len(rows)>=20000:truncated=True;break
                origin,axis=self.cylinder(face)
                rows.append({'part':'part_'+str(i),'name':part.label,'face':j,'type':'cylinder','radius_mm':float(face.Surface.Radius),
                             'axis_origin_mm':coordinates(origin),'axis_direction':coordinates(axis),'bounds_mm':bounds(face),'orientation':str(face.Orientation)})
            if truncated:break
        return {'schema':'rlsok.cad-features/1','cad_sha256':sha,'units':'mm','frame':'STEP global placement','features':rows,'truncated':truncated,'limit':20000,
                'scope':'Actual FreeCAD/OCCT analytic cylinders. Face indices/instance IDs are artifact-local, not persistent topology IDs.'}
