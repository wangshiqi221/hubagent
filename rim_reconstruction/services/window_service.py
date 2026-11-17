"""Artifact-backed window analysis and C++ geometry tools, without GUI access."""
import hashlib
import json
from pathlib import Path
import uuid
import numpy as np
from .model_service import _adapter, _load_call
from ..adapters.cpp_worker_adapter import CppWorkerAdapter, _read_json, check_cancelled


def _hash(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(4*1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def _save(path,data):
    path=Path(path);temp=path.with_suffix('.tmp');temp.write_text(json.dumps(data,ensure_ascii=False,allow_nan=False),encoding='utf-8');temp.replace(path)


def _parameters(p,required,optional=()):
    if not isinstance(p,dict) or not set(required)<=p.keys() or p.keys()-set(required)-set(optional):raise ValueError('Missing or unknown parameters')


def _positive(v,name,zero=False):
    if type(v) not in (int,float) or not np.isfinite(v) or (v<0 if zero else v<=0):raise ValueError('Invalid '+name)
    return float(v)


class WindowWorkerAdapter(CppWorkerAdapter):
    @classmethod
    def from_base(cls,base):
        executable=base.base_directory/'stlload_hub/build_surface/closed_cutter/geometry_worker.exe'
        return cls(executable if executable.is_file() else base.executable,base.call_root,
            base_directory=base.base_directory,timeout_seconds=max(base.timeout_seconds,300),
            max_result_bytes=base.max_result_bytes,max_artifact_bytes=base.max_artifact_bytes)

    def _validate_result(self,result,request,call):
        self._identity(result,request)
        if result.get('status')!='success' or result.get('unit')!='mm' or result.get('error') is not None:raise ValueError('Invalid window result')
        if request['operation'] not in ('fit_window_surface','build_window_cutter'):raise ValueError('Unsupported window operation')
        artifacts=result.get('artifacts',[])
        if len(artifacts)!=2 or not isinstance(result.get('metrics'),dict):raise ValueError('Missing geometry artifacts')
        for artifact in artifacts:
            p=Path(artifact['path']).resolve()
            if not p.is_relative_to(call.resolve()) or not p.is_file():raise ValueError('Geometry artifact outside call directory')
        report=next(a for a in artifacts if a['type']=='window_report')
        data=_read_json(Path(report['path']),self.max_artifact_bytes)
        if data.get('task_id')!=request['task_id'] or data.get('operation')!=request['operation']:raise ValueError('Window artifact identity mismatch')
        if request['operation']=='build_window_cutter' and (data.get('solid_count')!=1 or not data.get('closed_solid') or data.get('volume_mm3',0)<=0):raise ValueError('Not a valid single cutter')


def _manifest(path,adapter,allow_legacy=False):
    path=Path(path).resolve()
    if not path.is_relative_to(adapter.call_root.resolve()) or path.name!='windows.json':raise ValueError('Use an actual window analysis artifact')
    data=_read_json(path,adapter.max_artifact_bytes)
    if data.get('algorithm') not in (('window_regions_v1','window_regions_v2') if allow_legacy else ('window_regions_v2',)):
        raise ValueError('Window selection version is outdated; rerun rim_analyze_window_regions or rim_refine_surface_region')
    if _hash(data['source_stl'])!=data['source_sha256']:raise ValueError('Source mesh changed; rerun window analysis')
    if _hash(path)!=path.with_suffix('.sha256').read_text(encoding='ascii').strip():raise ValueError('Window analysis was modified')
    return data


def analyze_window_regions(parameters,*,adapter=None,cancel_event=None):
    _parameters(parameters,('axis_call_directory','depths_mm'),('band_mm','type_count','normal_axis_limit'))
    adapter=_adapter(adapter);request,result=_load_call(parameters['axis_call_directory'],'estimate_axis',adapter)
    from ..geometry.transverse_sections import load_stl,section_triangles
    from ..geometry.window_regions import window_candidates,classify_pattern,select_side_regions
    band=_positive(parameters.get('band_mm',3.),'band_mm');limit=parameters.get('normal_axis_limit',.85)
    if type(limit) not in (int,float) or not 0<limit<1:raise ValueError('normal_axis_limit must be between 0 and 1')
    types=parameters.get('type_count',2)
    if type(types)!=int or not 1<=types<=8:raise ValueError('type_count must be 1..8')
    axis=result['metrics']['axis_candidate'];source=Path(request['stl_path']);tri=load_stl(source,cancel_event)
    sections=section_triangles(tri,axis['origin'],axis['direction'],parameters['depths_mm'],cancel_event=cancel_event)
    radial=(tri.reshape(-1,3)-np.asarray(axis['origin']));d=np.asarray(axis['direction']);d=d/np.linalg.norm(d)
    outer=float(np.linalg.norm(radial-(radial@d)[:,None]*d,axis=1).max())
    excluded=[]
    windows=window_candidates(sections['sections'],outer,excluded_candidates=excluded,spoke_only=True);regions=[]
    for window in windows:
        check_cancelled(cancel_event);regions.extend(select_side_regions(tri,window,axis,band,normal_axis_limit=limit,cancel_event=cancel_event))
    root=adapter.call_root/'window_jobs'/('job_'+uuid.uuid4().hex);root.mkdir(parents=True)
    data={'algorithm':'window_regions_v2','source_stl':str(source.resolve()),'source_sha256':_hash(source),
        'task_id':request['task_id'],'axis':axis,'parameters':parameters,'axis_call_directory':parameters['axis_call_directory'],
        'axial_range_mm':sections['axial_range_mm'],'outer_radius_mm':outer,'windows':windows,'regions':regions,
        'pattern':classify_pattern(windows,types),'excluded_feature_candidates':excluded}
    path=root/'windows.json';check_cancelled(cancel_event);_save(path,data);path.with_suffix('.sha256').write_text(_hash(path),encoding='ascii')
    # Simple candidate preview is inspectable without any external image service.
    paths=[]
    for w in windows:
        pts=' '.join(f'{x:.5f},{-y:.5f}' for x,y in w['boundary_uv'])
        paths.append(f'<polygon points="{pts}" fill="none" stroke="black" stroke-width="0.5"/>')
        c=np.mean(w['boundary_uv'],axis=0);paths.append(f'<text x="{c[0]:.5f}" y="{-c[1]:.5f}" font-size="5">{w["window_id"]}</text>')
    (root/'windows.svg').write_text(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-outer} {-outer} {2*outer} {2*outer}">'+''.join(paths)+'</svg>',encoding='utf-8')
    return {'status':'success','analysis_status':'candidates_found' if windows else 'needs_review','analysis_path':str(path),
        'preview_path':str(root/'windows.svg'),'window_count':len(windows),
        'excluded_feature_count':len(excluded),
        'excluded_feature_count_scope':'per-depth contour observations; the same feature may appear at multiple depths',
        'excluded_feature_summary':[{k:v for k,v in w.items() if k not in ('boundary_uv','side_arcs_uv','frame')} for w in excluded],
        'windows':[{k:v for k,v in w.items() if k not in ('boundary_uv','side_arcs_uv','frame')} for w in windows],
        'regions':[{k:v for k,v in r.items() if k!='triangle_indices'} for r in regions],
        'pattern':data['pattern'],'warnings':['Four-wall regions and A/B groups are hypotheses. Validate fits, cutter placement, and each boolean. No final wheel has been built.']}


def refine_surface_region(parameters,*,adapter=None,cancel_event=None):
    _parameters(parameters,('analysis_path','window_id','band_mm'),('normal_axis_limit',))
    adapter=_adapter(adapter);old=_manifest(parameters['analysis_path'],adapter,allow_legacy=True)
    if parameters['window_id'] not in [w['window_id'] for w in old['windows']]:raise ValueError('Unknown window')
    # Generate a new immutable analysis with fresh geometry selection, never edit
    # the previous artifact or reuse fits made against a different region.
    p={**old['parameters'],'band_mm':parameters['band_mm']}
    if 'normal_axis_limit' in parameters:p['normal_axis_limit']=parameters['normal_axis_limit']
    return analyze_window_regions(p,adapter=adapter,cancel_event=cancel_event)


def analyze_window_pattern(parameters,*,adapter=None,cancel_event=None):
    _parameters(parameters,('analysis_path',),('type_count',))
    adapter=_adapter(adapter);data=_manifest(parameters['analysis_path'],adapter)
    from ..geometry.window_regions import classify_pattern
    count=parameters.get('type_count',data['parameters'].get('type_count',2))
    if type(count)!=int or not 1<=count<=8:raise ValueError('type_count must be 1..8')
    return {'status':'success','analysis_path':parameters['analysis_path'],**{'pattern':classify_pattern(data['windows'],count)}}


def prepare_window_loft(parameters,*,adapter=None,cancel_event=None):
    from .window_loft_service import prepare_window_loft as prepare
    return prepare(parameters,adapter=adapter,cancel_event=cancel_event)


def prepare_window_lofts(parameters,*,adapter=None,cancel_event=None):
    from .window_loft_service import prepare_window_lofts as prepare
    return prepare(parameters,adapter=adapter,cancel_event=cancel_event)


def refine_axis_symmetry(parameters,*,adapter=None,cancel_event=None):
    from .model_service import refine_model_axis
    return refine_model_axis(parameters,adapter=adapter,cancel_event=cancel_event)


def fit_window_surface(parameters,*,adapter=None,cancel_event=None):
    _parameters(parameters,('analysis_path','region_id'),('tolerance_mm','max_iterations'))
    adapter=_adapter(adapter);data=_manifest(parameters['analysis_path'],adapter)
    from ..geometry.window_regions import partition_window_candidates
    _,excluded=partition_window_candidates(data['windows'])
    regions=[r for r in data['regions'] if r['region_id']==parameters['region_id']]
    if len(regions)!=1 or regions[0]['status']!='candidate':raise ValueError('Region is missing or needs refinement')
    from ..geometry.window_regions import is_spoke_window
    window=next(w for w in data['windows'] if w['window_id']==regions[0]['window_id'])
    if not is_spoke_window(window,data['outer_radius_mm']):raise ValueError('Selected hole does not span the hub-to-rim spoke region; rerun window analysis')
    if regions[0]['window_id'] in {w['window_id'] for w in excluded}:
        raise ValueError('Region belongs to the separate small-feature family, not a spoke-window candidate. Rerun window analysis before fitting.')
    from ..geometry.transverse_sections import load_stl
    region=regions[0];tri=load_stl(data['source_stl'],cancel_event)[region['triangle_indices']]
    points=np.concatenate([tri.reshape(-1,3),tri.mean(axis=1),((tri+np.roll(tri,-1,axis=1))/2).reshape(-1,3)])
    tolerance=_positive(parameters.get('tolerance_mm',.5),'tolerance_mm')
    budget=parameters.get('max_iterations',8)
    if type(budget)!=int or not 1<=budget<=32:raise ValueError('max_iterations must be 1..32')
    sample_path=Path(parameters['analysis_path']).parent/('samples_'+uuid.uuid4().hex+'.json')
    _save(sample_path,{'samples':points.tolist(),'region_id':region['region_id'],'source_sha256':data['source_sha256']})
    worker=WindowWorkerAdapter.from_base(adapter)
    response=worker.run({'schema_version':1,'operation':'fit_window_surface','task_id':data['task_id'],'unit':'mm',
        'stl_path':data['source_stl'],'samples_path':str(sample_path),'tolerance_mm':tolerance,'max_iterations':budget},cancel_event=cancel_event)
    report=next(a['path'] for a in response.result['artifacts'] if a['type']=='window_report')
    record=Path(report).parent/'provenance.json';_save(record,{'analysis_path':parameters['analysis_path'],
        'region_id':region['region_id'],'window_id':region['window_id'],'geometry_sha256':_hash(Path(report).parent/'geometry.brep'),
        'report_sha256':_hash(report)})
    return {'status':'success','surface_report':report,**response.result['metrics']}


def _surface_report(path,adapter):
    path=Path(path).resolve()
    if not path.is_relative_to(adapter.call_root.resolve()) or path.name!='geometry.json':raise ValueError('Invalid surface report')
    provenance=_read_json(path.parent/'provenance.json',1024*1024)
    if provenance['report_sha256']!=_hash(path) or provenance['geometry_sha256']!=_hash(path.parent/'geometry.brep'):raise ValueError('Surface artifact changed')
    return _read_json(path,adapter.max_artifact_bytes),provenance


def build_window_cutter(parameters,*,adapter=None,cancel_event=None):
    _parameters(parameters,('surface_reports',),('fillet_radius_mm','axial_margin_mm'))
    adapter=_adapter(adapter);paths=parameters['surface_reports']
    if not isinstance(paths,list) or len(paths)!=4 or len(set(paths))!=4:raise ValueError('Select exactly four distinct surface reports')
    reports=[_surface_report(p,adapter) for p in paths];provenances=[p for _,p in reports]
    if len({p['analysis_path'] for p in provenances})!=1 or len({p['window_id'] for p in provenances})!=1 or len({p['region_id'] for p in provenances})!=4:raise ValueError('Four supports must belong to one window and analysis')
    data=_manifest(provenances[0]['analysis_path'],adapter)
    from ..geometry.window_regions import is_spoke_window
    window=next(w for w in data['windows'] if w['window_id']==provenances[0]['window_id'])
    if not is_spoke_window(window,data['outer_radius_mm']):raise ValueError('Central-hole supports cannot form a spoke cutter')
    from ..geometry.window_regions import partition_window_candidates
    _,excluded=partition_window_candidates(data['windows'])
    if provenances[0]['window_id'] in {w['window_id'] for w in excluded}:
        raise ValueError('Small-feature supports cannot be used for a spoke-window cutter. Rerun window analysis.')
    margin=_positive(parameters.get('axial_margin_mm',max(1.,.02*(data['axial_range_mm'][1]-data['axial_range_mm'][0]))),'axial_margin_mm')
    worker=WindowWorkerAdapter.from_base(adapter)
    response=worker.run({'schema_version':1,'operation':'build_window_cutter','unit':'mm','task_id':data['task_id'],
        'stl_path':data['source_stl'],'surface_reports':paths,'axis':data['axis'],
        'minimum_depth_mm':data['axial_range_mm'][0]-margin,'maximum_depth_mm':data['axial_range_mm'][1]+margin,
        'tolerance_mm':max(r['tolerance_mm'] for r,_ in reports),
        'fillet_radius_mm':_positive(parameters.get('fillet_radius_mm',0.),'fillet_radius_mm',True)},cancel_event=cancel_event)
    report=next(a['path'] for a in response.result['artifacts'] if a['type']=='window_report')
    _save(Path(report).parent/'cutter_provenance.json',{'analysis_path':provenances[0]['analysis_path'],
        'window_id':provenances[0]['window_id'],'surface_reports':paths,'report_sha256':_hash(report),
        'geometry_sha256':_hash(Path(report).parent/'geometry.brep')})
    return {'status':'success','cutter_report':report,**response.result['metrics']}
