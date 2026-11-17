"""Closed, measured window sections as an alternative to unbounded wall fits."""
import json
from pathlib import Path
import uuid
import numpy as np

from .model_service import _adapter
from .window_service import _manifest,_save,_hash,_parameters,_positive
from ..adapters.cpp_worker_adapter import check_cancelled


def observed_window_layers(data,depths,cancel_event=None):
    from ..geometry.transverse_sections import load_stl,section_triangles
    from ..geometry.window_regions import window_candidates
    triangles=load_stl(data['source_stl'],cancel_event)
    sections=section_triangles(triangles,data['axis']['origin'],data['axis']['direction'],depths,cancel_event=cancel_event)
    layers=[]
    for section in sections['sections']:
        check_cancelled(cancel_event)
        layers.append(window_candidates([section],data['outer_radius_mm'],spoke_only=True))
    return layers


def matched_window_layers(data, window, depths, cancel_event=None, candidate_layers=None):
    layers=[]
    for candidates in (candidate_layers if candidate_layers is not None else observed_window_layers(data,depths,cancel_event)):
        if not candidates:continue
        distance=lambda w:abs((w['angle_degrees']-window['angle_degrees']+180)%360-180)
        candidate=min(candidates,key=distance)
        if distance(candidate)>9:continue
        layers.append(candidate)
    return layers


def prepare_window_loft(parameters,*,adapter=None,cancel_event=None,candidate_layers=None):
    _parameters(parameters,('analysis_path','window_id'),('layer_spacing_mm',))
    adapter=_adapter(adapter);data=_manifest(parameters['analysis_path'],adapter)
    from ..geometry.window_regions import is_spoke_window
    window=next((w for w in data['windows'] if w['window_id']==parameters['window_id']),None)
    if window is None or not is_spoke_window(window,data['outer_radius_mm']):
        raise ValueError('Select a real hub-to-rim spoke opening, not a central hole')
    spacing=_positive(parameters.get('layer_spacing_mm',2.),'layer_spacing_mm')
    depths=data['parameters']['depths_mm'];low=min(depths);high=max(depths)
    count=int(np.ceil((high-low)/spacing))+1
    if count>128:raise ValueError('More than 128 layers requested; increase spacing')
    layers=matched_window_layers(data,window,np.linspace(low,high,count).tolist(),cancel_event,candidate_layers)
    if len(layers)<2:raise ValueError('At least two observed closed spoke-window sections are required')
    root=adapter.call_root/'window_lofts'/('loft_'+uuid.uuid4().hex);root.mkdir(parents=True)
    path=root/'loft_sections.json'
    artifact={'algorithm':'observed_window_loft_v1','analysis_path':str(Path(parameters['analysis_path']).resolve()),
              'source_sha256':data['source_sha256'],'window_id':window['window_id'],'axis':data['axis'],'layers':layers,
              'layer_spacing_mm':spacing}
    _save(path,artifact);path.with_suffix('.sha256').write_text(_hash(path),encoding='ascii')
    return {'status':'success','loft_path':str(path),'window_id':window['window_id'],
            'layer_count':len(layers),'observed_depth_range_mm':[layers[0]['depth_mm'],layers[-1]['depth_mm']],
            'warnings':['Only observed closed window sections are lofted; end sections continue axially outside the observed range.']}


def prepare_window_lofts(parameters,*,adapter=None,cancel_event=None):
    _parameters(parameters,('analysis_path','window_ids'),('layer_spacing_mm',))
    ids=parameters['window_ids']
    if not isinstance(ids,list) or not 1<=len(ids)<=64 or len(set(ids))!=len(ids):raise ValueError('Use 1..64 distinct observed window IDs')
    adapter=_adapter(adapter);data=_manifest(parameters['analysis_path'],adapter)
    spacing=_positive(parameters.get('layer_spacing_mm',2.),'layer_spacing_mm');depths=data['parameters']['depths_mm']
    count=int(np.ceil((max(depths)-min(depths))/spacing))+1
    if count>128:raise ValueError('More than 128 layers requested')
    candidates=observed_window_layers(data,np.linspace(min(depths),max(depths),count).tolist(),cancel_event)
    results=[prepare_window_loft({'analysis_path':parameters['analysis_path'],'window_id':wid,'layer_spacing_mm':spacing},
                 adapter=adapter,cancel_event=cancel_event,candidate_layers=candidates) for wid in ids]
    return {'status':'success','analysis_path':parameters['analysis_path'],'prepared_lofts':results}


def _resampled_loop(values,count):
    from ..geometry.window_regions import polygon_centroid
    p=np.asarray(values,dtype=float)
    area=np.sum(p[:,0]*np.roll(p[:,1],-1)-np.roll(p[:,0],-1)*p[:,1])
    if area<0:p=p[::-1]
    center=polygon_centroid(p);radial=center/np.linalg.norm(center)
    start=int(np.argmax((p-center)@radial));p=np.roll(p,-start,axis=0)
    closed=np.vstack([p,p[0]]);length=np.r_[0,np.cumsum(np.linalg.norm(np.diff(closed,axis=0),axis=1))]
    keep=np.r_[True,np.diff(length)>1e-10];length=length[keep];closed=closed[keep]
    t=np.linspace(0,length[-1],count,endpoint=False)
    return np.column_stack([np.interp(t,length,closed[:,i]) for i in range(2)])


def build_window_loft_cutter(parameters,*,adapter=None):
    _parameters(parameters,('loft_path',),('tolerance_mm','axial_margin_mm'))
    adapter=_adapter(adapter);path=Path(parameters['loft_path']).resolve()
    if not path.is_relative_to(adapter.call_root.resolve()) or path.name!='loft_sections.json':raise ValueError('Use a prepared loft artifact')
    if _hash(path)!=path.with_suffix('.sha256').read_text(encoding='ascii').strip():raise ValueError('Loft artifact changed')
    record=json.loads(path.read_text(encoding='utf-8'));data=_manifest(record['analysis_path'],adapter)
    if record['source_sha256']!=data['source_sha256']:raise ValueError('Loft source mismatch')
    tolerance=_positive(parameters.get('tolerance_mm',.5),'tolerance_mm')
    margin=_positive(parameters.get('axial_margin_mm',2.),'axial_margin_mm')
    import FreeCAD as App
    import Part
    from ..cad.window_features import solid
    wires=[];maximum=0.;sample_count=0
    for layer in record['layers']:
        frame=layer['frame'];origin=np.asarray(frame['origin']);u=np.asarray(frame['u_direction']);v=np.asarray(frame['v_direction'])
        actual=np.asarray(layer['boundary_uv']);chosen=None
        for count in (64,128,256,512):
            uv=_resampled_loop(actual,count);xyz=origin+uv[:,0,None]*u+uv[:,1,None]*v
            curve=Part.BSplineCurve();curve.interpolate(Points=[App.Vector(*p) for p in xyz],PeriodicFlag=True,Tolerance=1e-7)
            wire=Part.Wire([curve.toShape()])
            errors=[wire.distToShape(Part.Vertex(App.Vector(*(origin+p[0]*u+p[1]*v))))[0] for p in actual]
            error=max(errors)
            if wire.isClosed() and wire.isValid() and error<=tolerance:chosen=wire;break
        if chosen is None:raise ValueError('Closed section fitting did not reach the requested tolerance')
        wires.append(chosen);maximum=max(maximum,error);sample_count+=len(actual)
    direction=App.Vector(*data['axis']['direction'])
    front=wires[0].copy();back=wires[-1].copy()
    minimum_depth=data['axial_range_mm'][0]-margin
    maximum_depth=record['layers'][-1]['depth_mm']+margin
    # Window cutters clear the front disc, not the entire rear barrel. Forcing
    # their outer profiles through the rear rim would slot the preserved rim.
    front.translate(direction*(minimum_depth-record['layers'][0]['depth_mm']))
    back.translate(direction*(maximum_depth-record['layers'][-1]['depth_mm']))
    shape=Part.makeLoft([front]+wires+[back],True,True)
    if not solid(shape):raise ValueError('Measured closed sections did not produce a valid single loft solid')
    root=path.parent/('cutter_'+uuid.uuid4().hex);root.mkdir()
    geometry=root/'geometry.brep';shape.exportBrep(str(geometry));report=root/'geometry.json'
    output={'operation':'build_window_cutter','method':'observed_closed_section_loft','task_id':data['task_id'],'unit':'mm',
            'window_id':record['window_id'],
            'source_sha256':data['source_sha256'],'axis':data['axis'],'closed_solid':True,'solid_count':1,
            'volume_mm3':shape.Volume,'geometry_path':str(geometry),'tolerance_mm':tolerance,
            'maximum_section_deviation_mm':maximum,'section_sample_count':sample_count,
            'observed_depth_range_mm':[record['layers'][0]['depth_mm'],record['layers'][-1]['depth_mm']],
            'required_axial_range_mm':[minimum_depth,maximum_depth],'axial_scope':'observed_front_window_band',
            'loft_path':str(path),'warning':'Front section is extended outward; back cap clears the last observed window layer. The rear barrel is not cut through.'}
    _save(report,output)
    _save(root/'cutter_provenance.json',{'analysis_path':record['analysis_path'],'window_id':record['window_id'],
        'loft_path':str(path),'report_sha256':_hash(report),'geometry_sha256':_hash(geometry)})
    return {'status':'success','cutter_report':str(report),**output}
