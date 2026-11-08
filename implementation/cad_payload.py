"""Actual CadQuery/OCCT adapter for standalone parametric coupons.

Executed only by the existing resource-controlled sandbox. Does not import any
robot code or process robot designs. Scope: single solid, not assembly/strength.
"""
import collections
import hashlib
import json
import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, '/tool/lib/python3.10/site-packages')
import cadquery as cq


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


request = json.loads(Path('/parameters.json').read_text())
if set(request) != {'shape', 'length_mm', 'width_mm', 'height_mm', 'hole_diameter_mm'} or request['shape'] != 'box':
    raise ValueError('supported schema: one parameterized rectangular solid with optional through-hole')
for key in ('length_mm', 'width_mm', 'height_mm'):
    value = request[key]
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 1000:
        raise ValueError('positive finite dimensions <=1000mm required')
length, width, height = (request[k] for k in ('length_mm', 'width_mm', 'height_mm'))
hole = request['hole_diameter_mm']
if type(hole) not in (int, float) or not math.isfinite(hole) or not 0 <= hole < min(length, width):
    raise ValueError('through-hole diameter must fit the solid')
expected = length * width * height - math.pi * (hole / 2)**2 * height
shape = cq.Workplane('XY').box(length, width, height, centered=(False, False, False))
if hole:
    shape = shape.faces('>Z').workplane(centerOption='CenterOfMass').hole(hole)
solid = shape.val()
if not solid.isValid() or len(solid.Solids()) != 1 or not math.isclose(solid.Volume(), expected, rel_tol=1e-8, abs_tol=1e-7):
    raise ValueError('CAD kernel solid validity/analytic-volume mismatch')
cq.exporters.export(shape, '/work/model.step')
cq.exporters.export(shape, '/work/model.stl', tolerance=0.01, angularTolerance=0.1)
roundtrip = cq.importers.importStep('/work/model.step').val()
box = roundtrip.BoundingBox()
if not roundtrip.isValid() or len(roundtrip.Solids()) != 1 or not math.isclose(roundtrip.Volume(), expected, rel_tol=1e-8, abs_tol=1e-7):
    raise ValueError('STEP reread failed validity or analytic-volume check')
if any(not math.isclose(a, b, rel_tol=1e-7, abs_tol=1e-6) for a, b in zip((box.xlen, box.ylen, box.zlen), (length, width, height))):
    raise ValueError('STEP dimensions mismatch')
data = Path('/work/model.stl').read_bytes()
if len(data) < 84:
    raise ValueError('STL unreadable')
triangles = struct.unpack_from('<I', data, 80)[0]
if len(data) != 84 + 50 * triangles or not 0 < triangles <= 1000000:
    raise ValueError('binary STL length/triangle count invalid')
edges = collections.Counter()
for index in range(triangles):
    numbers = struct.unpack_from('<12fH', data, 84 + 50 * index)
    vertices = [tuple(numbers[3 + k*3:6 + k*3]) for k in range(3)]
    if any(not math.isfinite(x) for vertex in vertices for x in vertex) or len(set(vertices)) != 3:
        raise ValueError('STL has nonfinite or degenerate triangle')
    for i in range(3):
        edges[tuple(sorted((vertices[i], vertices[(i+1) % 3])))] += 1
if any(count != 2 for count in edges.values()):
    raise ValueError('STL mesh not closed: edge count differs from two')
Path('/work/parameters.json').write_text(json.dumps(request, indent=2))
result = {'status': 'PASS_SINGLE_SOLID_GEOMETRY', 'cadquery_version': cq.__version__,
          'input_sha256': sha('/parameters.json'), 'solid_count': 1,
          'analytic_volume_mm3': expected, 'roundtrip_volume_mm3': roundtrip.Volume(),
          'step_valid': True, 'step_dimensions_mm': [box.xlen, box.ylen, box.zlen],
          'stl_reread': True, 'stl_closed_edge_check': True, 'stl_triangles': triangles,
          'files': {name: sha('/work/' + name) for name in ('model.step', 'model.stl', 'parameters.json')},
          'scope': 'single parameterized solid geometry/STEP/STL only; no strength, assembly, tolerancing or manufacturing release'}
Path('/work/cad-checks.json').write_text(json.dumps(result, indent=2))
