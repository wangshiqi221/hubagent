"""One registry for schemas, dispatch and background/main-thread separation."""
import json
from .tool_handlers import _reject_constant,_unique_object
from .adapters.cpp_worker_adapter import WorkerError,WorkerCancelledError,check_cancelled

BACKGROUND={
 'rim_analyze_window_regions':'analyze_window_regions',
 'rim_refine_surface_region':'refine_surface_region',
 'rim_analyze_window_pattern':'analyze_window_pattern',
 'rim_fit_surface_patch':'fit_window_surface',
 'rim_build_window_cutting_solid':'build_window_cutter'}
MAIN={
 'rim_list_spoke_objects':'list_spoke_objects',
 'rim_import_window_cutter':'import_window_cutter',
 'rim_preview_window_pattern':'preview_window_pattern',
 'rim_validate_window_pattern':'validate_window_pattern',
 'rim_create_cutter_pattern':'create_window_pattern',
 'rim_cut_spoke_windows':'cut_window_patterns'}


def dispatch_window_tool(name,args_json,*,cancel_event=None):
    try:
        p=json.loads(args_json,parse_constant=_reject_constant,object_pairs_hook=_unique_object)
        if name in BACKGROUND:
            from .services import window_service
            check_cancelled(cancel_event)
            result=getattr(window_service,BACKGROUND[name])(p,cancel_event=cancel_event)
            check_cancelled(cancel_event)
        elif name in MAIN:
            from .services import window_cad_service
            result=getattr(window_cad_service,MAIN[name])(p)
        else:raise ValueError('Unknown window tool')
    except WorkerError as exc:
        detail=(getattr(exc,'worker_result',None) or {}).get('error',{}).get('details')
        result={'status':'cancelled' if isinstance(exc,WorkerCancelledError) else 'error','error':{'code':exc.code,'message':str(exc),'details':detail},
                'call_directory':str(exc.call_directory) if exc.call_directory else None}
    except (ValueError,TypeError,KeyError) as exc:
        result={'status':'error','error':{'code':'WINDOW_INPUT_INVALID','message':str(exc)}}
    except Exception as exc:
        result={'status':'error','error':{'code':'WINDOW_OPERATION_FAILED','message':str(exc)}}
    return json.dumps(result,ensure_ascii=False,allow_nan=False)


def handler(name):
    def invoke(args_json,*,cancel_event=None):return dispatch_window_tool(name,args_json,cancel_event=cancel_event)
    invoke.__name__=name
    return invoke


def schemas():
    text={'type':'string'};number={'type':'number'}
    defs=[]
    def add(name,description,properties,required):
        defs.append({'type':'function','function':{'name':name,'description':description,
            'parameters':{'type':'object','properties':properties,'required':required,'additionalProperties':False}}})
    add('rim_analyze_window_regions','Analyze multi-depth transverse loops, propose window IDs, four wall region IDs, previews and type hypotheses. No solid generated.',
        {'axis_call_directory':text,'depths_mm':{'type':'array','items':number,'minItems':1,'maxItems':64},
         'band_mm':{'type':'number','exclusiveMinimum':0},'type_count':{'type':'integer','minimum':1,'maximum':8},
         'normal_axis_limit':{'type':'number','exclusiveMinimum':0,'exclusiveMaximum':1}},['axis_call_directory','depths_mm'])
    add('rim_refine_surface_region','Regenerate candidate wall regions with a new band/normal filter. Old fits become unrelated; returns new analysis.',
        {'analysis_path':text,'window_id':text,'band_mm':{'type':'number','exclusiveMinimum':0},'normal_axis_limit':number},['analysis_path','window_id','band_mm'])
    add('rim_analyze_window_pattern','Return geometric A/B grouping and explicit angle hypotheses. No guessed count or automatic cut.',
        {'analysis_path':text,'type_count':{'type':'integer','minimum':1,'maximum':8}},['analysis_path'])
    add('rim_fit_surface_patch','Fit one actual region with C++ SurfaceFit. Accepts region IDs only, no generated coordinates. Returns surface_report and fit error.',
        {'analysis_path':text,'region_id':text,'tolerance_mm':{'type':'number','exclusiveMinimum':0},'max_iterations':{'type':'integer','minimum':1,'maximum':32,'default':8}},['analysis_path','region_id'])
    add('rim_build_window_cutting_solid','Build an intermediate closed cutter from four distinct fitted supports of the same window. Does not array or subtract.',
        {'surface_reports':{'type':'array','items':text,'minItems':4,'maxItems':4,'uniqueItems':True},'fillet_radius_mm':{'type':'number','minimum':0},'axial_margin_mm':{'type':'number','exclusiveMinimum':0}},['surface_reports'])
    add('rim_list_spoke_objects','List real document/object IDs, solids and reconstruction sources before selecting the blank.',{'document_name':text},[])
    add('rim_import_window_cutter','Import a verified C++ cutter BREP into FreeCAD and validate the solid.',{'document_name':text,'cutter_report':text},['cutter_report'])
    props={'document_name':text,'blank_name':text,'cutter_name':text,'angles_degrees':{'type':'array','items':number,'minItems':1,'maxItems':360,'uniqueItems':True}}
    add('rim_preview_window_pattern','Test each angular cutter intersection and single cut without modifying the blank. Not an engineering approval.',props,['blank_name','cutter_name','angles_degrees'])
    add('rim_validate_window_pattern','Compare rotated cutter boundaries against actual STL target-wall samples. Required evidence before accepting angle/type hypotheses.',
        {**props,'match_tolerance_mm':{'type':'number','exclusiveMinimum':0}},['blank_name','cutter_name','angles_degrees'])
    add('rim_create_cutter_pattern','Create a persistent recomputable polar cutter array after valid per-instance preview.',props,['blank_name','cutter_name','angles_degrees'])
    add('rim_cut_spoke_windows','Native FreeCAD Part cuts, in transaction. All patterns must refer to the same blank; requires valid connected volume reduction.',
        {'document_name':text,'blank_name':text,'pattern_names':{'type':'array','items':text,'minItems':1,'maxItems':8,'uniqueItems':True}},['blank_name','pattern_names'])
    return defs
