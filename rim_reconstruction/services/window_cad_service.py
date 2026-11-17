"""Main-thread import, preview, persistent polar arrays and native Part cuts."""
import json
from pathlib import Path
import threading
import math
from .model_service import _adapter
from .window_service import _manifest,_hash,_parameters
from ..adapters.cpp_worker_adapter import _read_json


def _context(parameters):
    if threading.current_thread() is not threading.main_thread():raise RuntimeError('FreeCAD operations require main thread')
    import FreeCAD as App
    doc=App.getDocument(parameters['document_name']) if parameters.get('document_name') else App.ActiveDocument
    if doc is None:raise ValueError('No active document')
    return App,doc


def list_spoke_objects(parameters):
    _parameters(parameters,(),('document_name',))
    App,doc=_context(parameters)
    return {'status':'success','document_name':doc.Name,'objects':[{'name':o.Name,'label':o.Label,
        'type':o.TypeId,'source_fit_path':getattr(o,'SourceFitPath',None),'window_id':getattr(o,'WindowId',None),
        'visible':bool(o.ViewObject.Visibility) if App.GuiUp else None,
        'bbox_mm':{'minimum':[o.Shape.BoundBox.XMin,o.Shape.BoundBox.YMin,o.Shape.BoundBox.ZMin],
                   'maximum':[o.Shape.BoundBox.XMax,o.Shape.BoundBox.YMax,o.Shape.BoundBox.ZMax]},
        'solid_count':len(o.Shape.Solids),'volume_mm3':float(o.Shape.Volume)} for o in doc.Objects if hasattr(o,'Shape') and not o.Shape.isNull()]}


def build_window_loft_cutter(parameters,*,adapter=None):
    if threading.current_thread() is not threading.main_thread():raise RuntimeError('FreeCAD operations require main thread')
    from .window_loft_service import build_window_loft_cutter as build
    return build(parameters,adapter=adapter)


def show_spoke_objects(parameters):
    _parameters(parameters,(),('document_name','object_names','isolate'))
    App,doc=_context(parameters)
    names=parameters.get('object_names')
    if names is not None and (not isinstance(names,list) or not names or any(not isinstance(n,str) for n in names)):
        raise ValueError('object_names must be a nonempty list of actual object names')
    objects=[doc.getObject(n) for n in names] if names else [o for o in doc.Objects
        if o.TypeId=='PartDesign::Body' or hasattr(o,'SourceCutterReport') or hasattr(o,'SourceBlank') or o.TypeId=='Part::Cut']
    if not objects or any(o is None or not hasattr(o,'Shape') or o.Shape.isNull() for o in objects):
        raise ValueError('No displayable reconstruction objects; list document objects first')
    if type(parameters.get('isolate',False)) is not bool:raise ValueError('isolate must be a boolean')
    if App.GuiUp and parameters.get('isolate'):
        for obj in doc.Objects:
            if hasattr(obj,'Shape'):obj.ViewObject.Visibility=False
    from ..cad.view_utils import show_objects
    displayed=show_objects(doc,objects,translucent=[o.Name for o in objects if hasattr(o,'SourceCutterReport')])
    return {'status':'success','document_name':doc.Name,'object_names':[o.Name for o in objects],
            'view_updated':displayed,'warnings':[] if displayed else ['FreeCAD GUI is unavailable; geometry was not modified']}


def import_window_cutter(parameters,*,adapter=None):
    _parameters(parameters,('cutter_report',),('document_name',))
    App,doc=_context(parameters);adapter=_adapter(adapter)
    path=Path(parameters['cutter_report']).resolve()
    if not path.is_relative_to(adapter.call_root.resolve()) or path.name!='geometry.json':raise ValueError('Use a generated cutter report')
    p=_read_json(path.parent/'cutter_provenance.json',1024*1024)
    if _hash(path)!=p['report_sha256'] or _hash(path.parent/'geometry.brep')!=p['geometry_sha256']:raise ValueError('Cutter artifact was modified')
    data=_manifest(p['analysis_path'],adapter);report=_read_json(path,adapter.max_artifact_bytes)
    if (report.get('loft_path') or report.get('method') not in ('four_fitted_surface_intersection','fitted_surface_intersection')
            or report.get('fitted_side_surface_count') not in (3,4) or report.get('fitted_side_face_count',0)<report.get('fitted_side_surface_count',99)
            or report.get('cap_face_count')!=2 or report.get('side_surface_continuity')!='C1'):
        raise ValueError('Rebuild this cutter through CadAgent from three or four actual fitted surfaces and two caps; legacy loft/prism cutters are not permitted.')
    if data.get('windows') and data.get('outer_radius_mm'):
        from ..geometry.window_regions import is_spoke_window
        observed=next(w for w in data['windows'] if w['window_id']==p['window_id'])
        if not is_spoke_window(observed,data['outer_radius_mm']):raise ValueError('Artifact describes a central hole, not a spoke window')
    if report.get('operation')!='build_window_cutter':raise ValueError('Artifact is not a cutter')
    import Part
    from ..cad.window_features import solid
    shape=Part.Shape();shape.read(str(path.parent/'geometry.brep'))
    if not solid(shape):raise ValueError('Imported cutter is not a valid solid')
    doc.openTransaction('Import window cutting solid')
    try:
        obj=doc.addObject('Part::Feature','WindowCutter');obj.Shape=shape
        for name,value in (('SourceCutterReport',str(path)),('SourceAnalysisPath',p['analysis_path']),
                           ('WindowId',p['window_id']),('AxisRecord',json.dumps(data['axis']))):
            obj.addProperty('App::PropertyString',name,'Source');setattr(obj,name,value)
        doc.recompute()
        from ..cad.view_utils import show_objects
        display=[o for o in doc.Objects if o.TypeId=='PartDesign::Body']+[obj]
        show_objects(doc,display,translucent=[obj.Name])
        doc.commitTransaction()
    except Exception:doc.abortTransaction();raise
    return {'status':'success','document_name':doc.Name,'cutter_name':obj.Name,'window_id':p['window_id'],
        'solid_count':1,'volume_mm3':shape.Volume,'axis':data['axis']}


def _objects(parameters,doc):
    blank=doc.getObject(parameters['blank_name']);cutter=doc.getObject(parameters['cutter_name'])
    from ..cad.window_features import solid
    if blank is None or cutter is None or not hasattr(blank,'Shape') or not hasattr(cutter,'AxisRecord'):raise ValueError('Missing blank or generated cutter')
    doc.recompute()
    if not solid(blank.Shape) or not solid(cutter.Shape):raise ValueError('Blank/cutter must be valid single solids')
    # Tool coordinates are source-global. Reject moved/reoriented blanks instead
    # of silently aligning to bounding-box centers or estimating another axis.
    source=blank
    if not getattr(source,'SourceFitPath','') and getattr(blank,'Tip',None) is not None:source=blank.Tip
    fit_path=getattr(source,'SourceFitPath','')
    if not fit_path:raise ValueError('Blank must have a reconstruction SourceFitPath')
    report_path=getattr(cutter,'SourceCutterReport','')
    if report_path:
        report=_read_json(Path(report_path),16*1024*1024)
        if (report.get('loft_path') or report.get('method') not in ('four_fitted_surface_intersection','fitted_surface_intersection')
                or report.get('fitted_side_surface_count') not in (3,4) or report.get('fitted_side_face_count',0)<report.get('fitted_side_surface_count',99)
                or report.get('cap_face_count')!=2 or report.get('side_surface_continuity')!='C1'):
            raise ValueError('This cutter is not certified as actual fitted main surfaces and two caps; rebuild it through CadAgent')
    request=_read_json(Path(fit_path).resolve().parent.parent/'request.json',1024*1024)
    axis=json.loads(cutter.AxisRecord)
    if any(abs(a-b)>1e-6 for a,b in zip(axis['origin'],request['axis']['origin'])) or any(abs(a-b)>1e-8 for a,b in zip(axis['direction'],request['axis']['direction'])):
        raise ValueError('Blank and cutter refer to different source axes')
    import FreeCAD as App
    actual=source.getGlobalPlacement();origin=App.Vector(*axis['origin']);direction=App.Vector(*axis['direction'])
    if (actual.Base-origin).Length>1e-5 or abs(actual.Rotation.multVec(App.Vector(1,0,0)).dot(direction)-1)>1e-8:
        raise ValueError('Blank placement changed; explicit source alignment required')
    return blank,cutter,axis


def _angles(values):
    if not isinstance(values,list) or not 1<=len(values)<=360 or any(type(a) not in (int,float) or not math.isfinite(a) for a in values):raise ValueError('Provide 1..360 finite instance angles')
    result=[float(a%360) for a in values]
    if len({round(a,9) for a in result})!=len(result):raise ValueError('Duplicate angular instances')
    return result


def preview_window_pattern(parameters):
    _parameters(parameters,('blank_name','cutter_name','angles_degrees'),('document_name',))
    App,doc=_context(parameters);blank,cutter,axis=_objects(parameters,doc)
    origin=App.Vector(*axis['origin']);direction=App.Vector(*axis['direction'])
    blank_depths=[(v.Point-origin).dot(direction) for v in blank.Shape.Vertexes]
    cutter_depths=[(v.Point-origin).dot(direction) for v in cutter.Shape.Vertexes]
    target=[min(blank_depths),max(blank_depths)]
    if getattr(cutter,'SourceCutterReport',''):
        report=_read_json(Path(cutter.SourceCutterReport),16*1024*1024)
        if report.get('axial_scope')=='observed_front_window_band':target=report['required_axial_range_mm']
    extension=max(0.,min(cutter_depths)-target[0],target[1]-max(cutter_depths))
    results=[]
    for angle in _angles(parameters['angles_degrees']):
        shape=cutter.Shape.copy();shape.rotate(App.Vector(*axis['origin']),App.Vector(*axis['direction']),angle)
        overlap=blank.Shape.common(shape)
        # Preview also checks each single cut; intersections alone are insufficient.
        result=blank.Shape.cut(shape)
        from ..cad.window_features import solid
        results.append({'angle_degrees':angle,'intersection_volume_mm3':float(overlap.Volume),
            'single_cut_valid':solid(result),'remaining_volume_mm3':float(result.Volume)})
    return {'status':'success','document_name':doc.Name,'instances':results,
        'pattern_ready':extension<=1e-7 and all(r['intersection_volume_mm3']>1e-8 and r['single_cut_valid'] for r in results),
        'additional_axial_margin_needed_mm':extension,
        'warnings':['This verifies geometry intersection and connectivity, not engineering strength or correspondence to every STL window.']}


def create_window_pattern(parameters):
    _parameters(parameters,('blank_name','cutter_name','angles_degrees'),('document_name',))
    App,doc=_context(parameters);blank,cutter,axis=_objects(parameters,doc);angles=_angles(parameters['angles_degrees'])
    preview=preview_window_pattern(parameters)
    if not preview['pattern_ready']:raise ValueError('One or more cutter instances are empty or break the blank; inspect preview')
    from ..cad.window_features import CutterPattern
    doc.openTransaction('Create window cutter pattern')
    try:
        obj=doc.addObject('Part::FeaturePython','WindowPattern');CutterPattern(obj)
        obj.SourceCutter=cutter;obj.AxisOrigin=App.Vector(*axis['origin']);obj.AxisDirection=App.Vector(*axis['direction']);obj.Angles=angles
        obj.addProperty('App::PropertyLink','SourceBlank','Source');obj.SourceBlank=blank
        doc.recompute()
        if obj.Shape.isNull() or not obj.Shape.isValid():raise ValueError('Invalid cutter pattern')
        doc.commitTransaction()
    except Exception:doc.abortTransaction();raise
    return {'status':'success','document_name':doc.Name,'pattern_name':obj.Name,'instance_count':len(angles)}


def validate_window_pattern(parameters,*,adapter=None):
    """Compare rotated cutter boundary against observed target-wall samples."""
    _parameters(parameters,('blank_name','cutter_name','angles_degrees'),('document_name','match_tolerance_mm'))
    App,doc=_context(parameters);blank,cutter,axis=_objects(parameters,doc);adapter=_adapter(adapter)
    from .window_service import _positive
    tolerance=_positive(parameters.get('match_tolerance_mm',.5),'match_tolerance_mm')
    data=_manifest(cutter.SourceAnalysisPath,adapter)
    source_report=_read_json(Path(cutter.SourceCutterReport),adapter.max_artifact_bytes)
    sides=source_report['fitted_side_surface_count']
    source=next(w for w in data['windows'] if w['window_id']==cutter.WindowId)
    from ..geometry.transverse_sections import load_stl
    import numpy as np
    import Part
    tri=load_stl(data['source_stl']);instances=[];selected=set()
    for angle in _angles(parameters['angles_degrees']):
        target_angle=(source['angle_degrees']+angle)%360
        distance=lambda w:min(abs(w['angle_degrees']-target_angle),360-abs(w['angle_degrees']-target_angle))
        target=min(data['windows'],key=distance)
        shape=cutter.Shape.copy();shape.rotate(App.Vector(*axis['origin']),App.Vector(*axis['direction']),angle)
        shell=Part.makeShell(shape.Faces);distances=[]
        regions=[r for r in data['regions'] if r['window_id']==target['window_id']]
        if sides==3:
            from ..geometry.window_regions import main_wall_indices
            main_sides=set(main_wall_indices(target));regions=[r for r in regions if r['side_index'] in main_sides]
        for region in regions:
            ids=region['triangle_indices']
            if not ids:continue
            sample_ids=np.asarray(ids)[np.linspace(0,len(ids)-1,min(16,len(ids)),dtype=int)]
            for point in tri[sample_ids].mean(axis=1):
                distances.append(float(shell.distToShape(Part.Vertex(App.Vector(*point)))[0]))
        # Also verify the complete observed section, including a short corner
        # transition not used as a main fitted wall. Omitting a fit must not hide
        # geometric mismatch at that transition.
        frame=target['frame'];o=np.asarray(frame['origin']);u=np.asarray(frame['u_direction']);v=np.asarray(frame['v_direction'])
        boundary=np.asarray(target['boundary_uv']);boundary=boundary[np.linspace(0,len(boundary)-1,min(128,len(boundary)),dtype=int)]
        for point in o+boundary[:,0,None]*u+boundary[:,1,None]*v:
            distances.append(float(shell.distToShape(Part.Vertex(App.Vector(*point)))[0]))
        maximum=max(distances) if distances else None
        matched=(maximum is not None and maximum<=tolerance and distance(target)<=max(2.,90/max(len(data['windows']),1))
                 and target['window_id'] not in selected)
        matched=matched and len(regions)==sides and all(r['status']=='candidate' for r in regions)
        selected.add(target['window_id'])
        instances.append({'angle_degrees':angle,'target_window_id':target['window_id'],
            'maximum_sample_distance_mm':maximum,'sample_count':len(distances),'matched':matched})
    return {'status':'success','document_name':doc.Name,'instances':instances,'match_tolerance_mm':tolerance,
        'stl_match_passed':all(i['matched'] for i in instances),
        'warnings':['Sample distances cover selected wall regions only, not continuous surfaces or engineering strength.']}


def create_window_cutter_set(parameters,*,adapter=None):
    """A/B groups may contain measured variants when exact polar copies fail."""
    _parameters(parameters,('blank_name','cutter_names'),('document_name','label'))
    App,doc=_context(parameters);names=parameters['cutter_names']
    if not isinstance(names,list) or not 1<=len(names)<=64 or len(set(names))!=len(names):raise ValueError('Use 1..64 distinct imported cutters')
    cutters=[];checks=[];windows=set();analyses=set()
    for name in names:
        p={'document_name':doc.Name,'blank_name':parameters['blank_name'],'cutter_name':name,'angles_degrees':[0.]}
        blank,cutter,axis=_objects(p,doc)
        if cutter.WindowId in windows:raise ValueError('Duplicate observed window in cutter set')
        windows.add(cutter.WindowId);analyses.add(cutter.SourceAnalysisPath)
        check=validate_window_pattern(p,adapter=adapter)
        if not check['stl_match_passed']:raise ValueError('Measured cutter does not match its own observed window within 0.5 mm')
        preview=preview_window_pattern(p)
        if not preview['pattern_ready']:raise ValueError('Measured cutter does not produce a valid single-window cut')
        checks.append({'cutter_name':name,'window_id':cutter.WindowId,'validation':check,'preview':preview});cutters.append(cutter)
    if len(analyses)!=1:raise ValueError('Cutter set must use one source analysis')
    doc.openTransaction('Group individually measured window cutters')
    try:
        obj=doc.addObject('Part::Compound','WindowCutterSet');obj.Links=cutters
        obj.Label=parameters.get('label','Measured window cutter set')
        obj.addProperty('App::PropertyLink','SourceBlank','Source');obj.SourceBlank=blank
        obj.addProperty('App::PropertyString','ReconstructionMethod','Source');obj.ReconstructionMethod='individually_fitted_window_variants'
        doc.recompute()
        if obj.Shape.isNull() or not obj.Shape.isValid():raise ValueError('Invalid cutter-set compound')
        doc.commitTransaction()
    except Exception:doc.abortTransaction();raise
    return {'status':'success','document_name':doc.Name,'pattern_name':obj.Name,'instance_count':len(cutters),
            'method':'individually_fitted_window_variants','checks':checks,
            'warnings':['This group contains independently fitted window variants, not rigid copies of one template.']}


def extend_window_cutter_through_disc(parameters,*,adapter=None):
    """Explicit rejection for legacy calls; do not append replacement walls."""
    raise ValueError('Disabled: additional clearance prisms are not part of the required four fitted-surface cutter')


def cut_window_patterns(parameters):
    _parameters(parameters,('blank_name','pattern_names'),('document_name',))
    App,doc=_context(parameters);blank=doc.getObject(parameters['blank_name'])
    names=parameters['pattern_names']
    if not isinstance(names,list) or not names or len(names)>8 or len(set(names))!=len(names):raise ValueError('Select distinct A/B pattern objects')
    from ..cad.window_features import solid
    if blank is None or not solid(blank.Shape):raise ValueError('Invalid blank')
    patterns=[doc.getObject(name) for name in names]
    if any(p is None or not hasattr(p,'SourceBlank') or p.SourceBlank!=blank for p in patterns):raise ValueError('Patterns must be validated against the same blank')
    for pattern in patterns:
        sources=[pattern.SourceCutter] if hasattr(pattern,'SourceCutter') else list(getattr(pattern,'Links',[]))
        if not sources:raise ValueError('Pattern has no verifiable fitted-surface cutters')
        for cutter in sources:
            _objects({'blank_name':blank.Name,'cutter_name':cutter.Name},doc)
    doc.openTransaction('Cut spoke windows')
    created=[];current=blank
    try:
        for pattern in patterns:
            before=current.Shape.Volume
            obj=doc.addObject('Part::Cut','CutWindows');obj.Base=current;obj.Tool=pattern;doc.recompute()
            if not solid(obj.Shape) or not 0<obj.Shape.Volume<before-1e-8:raise ValueError('Boolean produced an invalid, disconnected or unchanged wheel')
            created.append(obj.Name);current=obj
        doc.commitTransaction()
    except Exception:doc.abortTransaction();raise
    if App.GuiUp:
        for o in doc.Objects:
            if hasattr(o,'Shape'):o.ViewObject.Visibility=False
        from ..cad.view_utils import show_objects
        show_objects(doc,[current])
    return {'status':'success','operation':'cut_spoke_windows','document_name':doc.Name,'object_name':current.Name,
        'cut_names':created,'solid_count':1,'volume_mm3':current.Shape.Volume,'removed_volume_mm3':blank.Shape.Volume-current.Shape.Volume,
        'warnings':['Valid connected geometry does not establish spoke strength, minimum wall thickness or semantic correctness.']}
