"""Python-only transverse STL sections using an existing estimated axis."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid
from .model_service import _adapter, _load_call
from ..adapters.cpp_worker_adapter import check_cancelled


def process_transverse_sections(parameters, *, adapter=None, cancel_event=None):
    required={'axis_call_directory','depths_mm'}
    if not isinstance(parameters,dict) or not required<=parameters.keys() or parameters.keys()-required-{'tolerance_mm'}:
        raise ValueError('Only axis_call_directory, depths_mm and optional tolerance_mm are accepted')
    adapter=_adapter(adapter)
    request,result=_load_call(parameters['axis_call_directory'],'estimate_axis',adapter)
    if request.get('unit')!='mm':raise ValueError('Source unit must be mm')
    axis=result['metrics']['axis_candidate'];source=Path(request['stl_path'])
    if not source.is_file() or source.suffix.lower()!='.stl':raise ValueError('Source STL does not exist')
    check_cancelled(cancel_event)
    from ..geometry.transverse_sections import load_stl,section_triangles
    triangles=load_stl(source,cancel_event)
    geometry=section_triangles(triangles,axis['origin'],axis['direction'],parameters['depths_mm'],
        tolerance_mm=parameters.get('tolerance_mm',1e-5),cancel_event=cancel_event)
    digest=hashlib.sha256()
    with source.open('rb') as stream:
        for block in iter(lambda:stream.read(4*1024*1024),b''):
            check_cancelled(cancel_event);digest.update(block)
    parent=Path(parameters['axis_call_directory']).resolve()/'transverse'
    parent.mkdir(exist_ok=True)
    run='transverse_'+uuid.uuid4().hex;destination=parent/run
    full={**geometry,'operation':'transverse_sections','task_id':request['task_id'],
        'axis_source_run_id':result.get('run_id'),'axis_call_directory':str(parent.parent),
        'axis':axis,'source_stl':str(source.resolve()),'source_sha256':digest.hexdigest(),
        'parameters':parameters}
    with tempfile.TemporaryDirectory(dir=parent) as directory:
        path=Path(directory)/'sections.json'
        path.write_text(json.dumps(full,ensure_ascii=False,allow_nan=False),encoding='utf-8')
        check_cancelled(cancel_event)
        os.replace(directory,destination)
    return {'status':'success','operation':'transverse_sections','result_path':str(destination/'sections.json'),
        'axis_call_directory':str(parent.parent),'unit':'mm','axis_validated':False,
        'axial_range_mm':geometry['axial_range_mm'],'warnings':geometry['warnings'],
        'sections':[{'depth_mm':s['depth_mm'],'status':s['status'],'contour_count':len(s['contours']),
            'closed_contour_count':sum(c['closed'] for c in s['contours']),
            'branch_node_count':s['branch_node_count'],'coplanar_triangle_count':s['coplanar_triangle_count']} for s in geometry['sections']]}
