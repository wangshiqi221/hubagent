"""One registry for schemas, dispatch and background/main-thread separation."""
import json
from .tool_handlers import _reject_constant,_unique_object
from .adapters.cpp_worker_adapter import WorkerError,WorkerCancelledError,check_cancelled

BACKGROUND={
 'rim_prepare_window_lofts':'prepare_window_lofts',
 'rim_refine_axis_symmetry':'refine_axis_symmetry',
 'rim_prepare_window_loft':'prepare_window_loft',
 'rim_analyze_window_regions':'analyze_window_regions',
 'rim_refine_surface_region':'refine_surface_region',
 'rim_analyze_window_pattern':'analyze_window_pattern',
 'rim_fit_surface_patch':'fit_window_surface',
 'rim_build_window_cutting_solid':'build_window_cutter'}
MAIN={
 'rim_extend_window_cutter_through_disc':'extend_window_cutter_through_disc',
 'rim_create_window_cutter_set':'create_window_cutter_set',
 'rim_build_window_loft_cutter':'build_window_loft_cutter',
 'rim_show_spoke_objects':'show_spoke_objects',
 'rim_list_spoke_objects':'list_spoke_objects',
 'rim_import_window_cutter':'import_window_cutter',
 'rim_preview_window_pattern':'preview_window_pattern',
 'rim_validate_window_pattern':'validate_window_pattern',
 'rim_create_cutter_pattern':'create_window_pattern',
 'rim_cut_spoke_windows':'cut_window_patterns'}

# The user requires fitted-surface intersection. Keep explicit rejection for
# cached conversations, rather than allowing an old loft handler to execute.
DISABLED_RECONSTRUCTION_TOOLS=frozenset({
    'rim_prepare_window_loft','rim_prepare_window_lofts','rim_build_window_loft_cutter',
    'rim_extend_window_cutter_through_disc'})
for _disabled in DISABLED_RECONSTRUCTION_TOOLS:
    BACKGROUND.pop(_disabled,None)
    MAIN.pop(_disabled,None)


def dispatch_window_tool(name,args_json,*,cancel_event=None):
    try:
        if name in DISABLED_RECONSTRUCTION_TOOLS:
            raise ValueError('This reconstruction route is disabled. Use the actual fitted main surfaces and rim_build_window_cutting_solid; lofts, layered cutters and added clearance prisms are not permitted.')
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
         'normal_axis_limit':{'type':'number','exclusiveMinimum':0,'exclusiveMaximum':1},'axial_band_mm':{'type':'number','exclusiveMinimum':0},'corner_exclusion_mm':{'type':'number','minimum':0},'normal_spread_degrees':{'type':'number','exclusiveMinimum':0,'exclusiveMaximum':90}},['axis_call_directory','depths_mm'])
    add('rim_refine_axis_symmetry','Refine an existing PCA axis by observed rotational symmetry and held-out surface registration. Invalidates downstream profiles and cutters; rebuild them from the returned axis. Engineering axis accuracy remains unconfirmed.',
        {'axis_call_directory':text,'instance_count':{'type':'integer','minimum':2,'maximum':12}},['axis_call_directory','instance_count'])
    add('rim_refine_surface_region','Regenerate candidate wall regions with a new band/normal filter. Old fits become unrelated; returns new analysis.',
        {'analysis_path':text,'window_id':text,'band_mm':{'type':'number','exclusiveMinimum':0},'normal_axis_limit':number,'axial_band_mm':{'type':'number','exclusiveMinimum':0},'corner_exclusion_mm':{'type':'number','minimum':0},'normal_spread_degrees':{'type':'number','exclusiveMinimum':0,'exclusiveMaximum':90}},['analysis_path','window_id','band_mm'])
    add('rim_analyze_window_pattern','Return geometric A/B grouping and explicit angle hypotheses. No guessed count or automatic cut.',
        {'analysis_path':text,'type_count':{'type':'integer','minimum':1,'maximum':8}},['analysis_path'])
    add('rim_prepare_window_loft','Extract matched closed spoke-window sections from the STL for a bounded loft alternative when four freeform supports fail to enclose a cutter.',
        {'analysis_path':text,'window_id':text,'layer_spacing_mm':{'type':'number','exclusiveMinimum':0}},['analysis_path','window_id'])
    add('rim_prepare_window_lofts','Prepare several real window loft artifacts in one mesh traversal. Use for independently fitted A/B variants when shared templates fail validation.',
        {'analysis_path':text,'window_ids':{'type':'array','items':text,'minItems':1,'maxItems':64,'uniqueItems':True},'layer_spacing_mm':{'type':'number','exclusiveMinimum':0}},['analysis_path','window_ids'])
    add('rim_build_window_loft_cutter','Fit measured closed section wires within tolerance, build a valid solid loft and continue its end profiles along the recorded axis. Returns an intermediate cutter report; requires target-window validation.',
        {'loft_path':text,'tolerance_mm':{'type':'number','exclusiveMinimum':0,'default':.5},'axial_margin_mm':{'type':'number','exclusiveMinimum':0}},['loft_path'])
    add('rim_fit_surface_patch','Fit one actual region with C++ SurfaceFit. Default sample tolerance is 0.5 mm. Accepts region IDs only, no generated coordinates. Returns surface_report and fit error.',
        {'analysis_path':text,'region_id':text,'tolerance_mm':{'type':'number','exclusiveMinimum':0,'maximum':1.0,'default':0.5,'description':'Start at 0.5 mm. Up to 1.0 mm is permitted only after an actual 0.5 mm numerical fit failure for this same region.'},
         'retry_from_call_directory':{'type':'string','description':'Required when tolerance exceeds 0.5 mm: failed same-analysis, same-region 0.5 mm fit call.'},
         'max_iterations':{'type':'integer','minimum':1,'maximum':64,'default':16}},['analysis_path','region_id'])
    add('rim_build_window_cutting_solid','Intersect and trim the actual three or four C1 fitted main side surfaces with two cap planes. Three are allowed only when analysis identifies a short corner transition, not an extra main wall. No lofts, layers, or added prisms.',
        {'surface_reports':{'type':'array','items':text,'minItems':3,'maxItems':4,'uniqueItems':True},'fillet_radius_mm':{'type':'number','minimum':0},'axial_margin_mm':{'type':'number','exclusiveMinimum':0},
         'axial_depth_range_mm':{'type':'array','items':number,'minItems':2,'maxItems':2,'description':'Optional signed cap depths along the recorded axis; derive from actual surface sample coverage and required cutting region. No added prisms.'},
         'boundary_tolerance_mm':{'type':'number','exclusiveMinimum':0,'maximum':1.0,'description':'Explicit trimmed-wall boundary check tolerance, independent of surface fitting. User allows up to 1.0 mm.'}},['surface_reports'])
    add('rim_list_spoke_objects','List real document/object IDs, solids and reconstruction sources before selecting the blank.',{'document_name':text},[])
    add('rim_show_spoke_objects','Show existing reconstruction objects and fit the camera to their bounds. Keeps geometry placement unchanged; available without reconstruction stage prerequisites.',
        {'document_name':text,'object_names':{'type':'array','items':text,'minItems':1},'isolate':{'type':'boolean','default':False,'description':'Show only these objects, hiding old intermediate geometry without deleting it.'}},[])
    add('rim_import_window_cutter','Import a verified C++ cutter BREP into FreeCAD and validate the solid.',{'document_name':text,'cutter_report':text},['cutter_report'])
    add('rim_extend_window_cutter_through_disc','Clear inner-disc remnants behind a measured front-window loft. Derives a radial separator from the actual blank and verifies its annular band contains no material, protecting the rear rim. Returns a new imported cutter.',
        {'document_name':text,'blank_name':text,'cutter_name':text},['blank_name','cutter_name'])
    props={'document_name':text,'blank_name':text,'cutter_name':text,'angles_degrees':{'type':'array','items':number,'minItems':1,'maxItems':360,'uniqueItems':True}}
    add('rim_preview_window_pattern','Test each angular cutter intersection and single cut without modifying the blank. Not an engineering approval.',props,['blank_name','cutter_name','angles_degrees'])
    add('rim_validate_window_pattern','Compare rotated cutter boundaries against actual STL target-wall samples. Required evidence before accepting angle/type hypotheses.',
        {**props,'match_tolerance_mm':{'type':'number','exclusiveMinimum':0}},['blank_name','cutter_name','angles_degrees'])
    add('rim_create_cutter_pattern','Create a persistent recomputable polar cutter array after valid per-instance preview.',props,['blank_name','cutter_name','angles_degrees'])
    add('rim_create_window_cutter_set','When a polar template fails target matching, group independently fitted observed window cutters. Revalidates each at its actual position within 0.5 mm and previews native cuts. Not identical polar copies.',
        {'document_name':text,'blank_name':text,'cutter_names':{'type':'array','items':text,'minItems':1,'maxItems':64,'uniqueItems':True},'label':text},['blank_name','cutter_names'])
    add('rim_cut_spoke_windows','Native FreeCAD Part cuts, in transaction. All patterns must refer to the same blank; requires valid connected volume reduction.',
        {'document_name':text,'blank_name':text,'pattern_names':{'type':'array','items':text,'minItems':1,'maxItems':8,'uniqueItems':True}},['blank_name','pattern_names'])
    return [definition for definition in defs if definition['function']['name'] not in DISABLED_RECONSTRUCTION_TOOLS]
