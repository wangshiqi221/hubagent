"""Window hypotheses and reproducible four-wall regions from transverse loops.

These are candidates, not certified semantic faces. Degenerate/missing side
supports are reported and cannot be promoted to cutting solids.
"""
import math
import numpy as np
from scipy.spatial import cKDTree
from ..adapters.cpp_worker_adapter import check_cancelled


def _inside(point, polygon):
    x,y=point;p=np.asarray(polygon);q=np.roll(p,-1,axis=0)
    hit=(p[:,1]>y)!=(q[:,1]>y)
    return bool(np.count_nonzero(hit & (x < (q[:,0]-p[:,0])*(y-p[:,1])/(q[:,1]-p[:,1]+1e-300)+p[:,0]))%2)


def partition_window_candidates(windows):
    """Separate a clearly smaller feature family before A/B clustering.

    A relative gap is evidence, not a semantic certification. If no clear gap
    exists, preserve all candidates for review instead of inventing a cutoff.
    """
    ordered=sorted(windows,key=lambda w:w['radial_span_mm'])
    gaps=[(ordered[i+1]['radial_span_mm']/max(ordered[i]['radial_span_mm'],1e-9),i+1)
          for i in range(len(ordered)-1) if len(ordered)-i-1>=3]
    ratio,split=max(gaps,default=(0,0))
    if ratio<2:return list(windows),[]
    small=ordered[:split];large=ordered[split:]
    # Require an independent area gap as well; do not discard narrow but long
    # spoke openings merely because one descriptor differs.
    if min(w['area_mm2'] for w in large)<2*max(w['area_mm2'] for w in small):
        return list(windows),[]
    small_ids={id(w) for w in small}
    return [w for w in windows if id(w) not in small_ids],small


def polygon_centroid(points):
    p=np.asarray(points);q=np.roll(p,-1,axis=0)
    cross=p[:,0]*q[:,1]-q[:,0]*p[:,1]
    return np.sum((p+q)*cross[:,None],axis=0)/(3*cross.sum()) if abs(cross.sum())>1e-12 else p.mean(axis=0)


def is_spoke_window(window, outer_radius):
    """A spoke opening must span a hub-to-rim radial band, not just be a hole."""
    boundary=np.asarray(window['boundary_uv'])
    return (window['radial_span_mm']>=.25*outer_radius
            and np.linalg.norm(boundary,axis=1).max()>=.7*outer_radius)


def main_wall_indices(window):
    """A short tip transition is not an extra main wall of a triangular window."""
    lengths=np.asarray([np.linalg.norm(np.diff(np.asarray(a),axis=0),axis=1).sum() for a in window['side_arcs_uv']])
    shortest=int(np.argmin(lengths))
    if len(lengths)==4 and lengths[shortest]<.15*np.median(np.delete(lengths,shortest)):
        return [i for i in range(4) if i!=shortest]
    return list(range(len(lengths)))


def window_candidates(sections, outer_radius, *, minimum_radius_ratio=.15, excluded_candidates=None, spoke_only=False):
    candidates=[]
    for section in sections:
        loops=[c for c in section['contours'] if c['closed'] and c['area_mm2'] and c['area_mm2']>outer_radius**2*5e-4]
        for loop in loops:
            p=np.asarray(loop['points_uv']);center=polygon_centroid(p);radius=float(np.linalg.norm(center))
            # Nesting, not just loop area, distinguishes windows from material islands.
            nesting=sum(_inside(p[0],other['points_uv']) for other in loops if other is not loop)
            if nesting%2!=1 or not minimum_radius_ratio*outer_radius<radius<.95*outer_radius:continue
            radial=center/radius;tangent=np.array([-radial[1],radial[0]])
            local=np.c_[p@radial,p@tangent]
            scaled=(local-local.mean(axis=0))/np.maximum(np.ptp(local,axis=0),1e-9)
            anchors=[int(np.argmax(scaled@direction)) for direction in
                     (np.array([-1,-1]),np.array([-1,1]),np.array([1,1]),np.array([1,-1]))]
            if len(set(anchors))!=4:
                anchors=[int(np.argmin(local[:,0])),int(np.argmax(local[:,1])),int(np.argmax(local[:,0])),int(np.argmin(local[:,1]))]
            if len(set(anchors))!=4:continue
            anchors=sorted(anchors)
            arcs=[]
            for start,end in zip(anchors,anchors[1:]+[anchors[0]+len(p)]):
                ids=np.arange(start,end+1)%len(p);arcs.append(p[ids].tolist())
            angle=float(math.degrees(math.atan2(center[1],center[0]))%360)
            candidates.append({'depth_mm':section['depth_mm'],'angle_degrees':angle,
                'radius_mm':radius,'area_mm2':loop['area_mm2'],'radial_span_mm':float(np.ptp(local[:,0])),
                'tangential_span_mm':float(np.ptp(local[:,1])), 'boundary_uv':p.tolist(),
                'side_arcs_uv':arcs,'frame':section['frame']})
    # Keep the layer with the most complete distinct windows. Other layers are
    # used as independent positional evidence, never silently merged geometry.
    depths=sorted({c['depth_mm'] for c in candidates})
    if not depths:return []
    if spoke_only:
        small=[c for c in candidates if not is_spoke_window(c,outer_radius)]
        if excluded_candidates is not None:
            excluded_candidates.extend({**c,'exclusion_reason':'does_not_span_hub_to_rim'} for c in small)
        candidates=[c for c in candidates if is_spoke_window(c,outer_radius)]
        depths=sorted({c['depth_mm'] for c in candidates})
        if not depths:return []
    layers={z:partition_window_candidates([c for c in candidates if c['depth_mm']==z]) for z in depths}
    best=max(depths,key=lambda z:(len(layers[z][0]),-abs(z)))
    chosen=sorted(layers[best][0],key=lambda c:c['angle_degrees'])
    if excluded_candidates is not None:
        excluded_candidates.extend({**c,'exclusion_reason':'separate_small_span_and_area_family'} for c in layers[best][1])
    for i,c in enumerate(chosen):
        c['window_id']=f'window_{i+1:03d}'
        c['main_wall_indices']=main_wall_indices(c)
        c['main_wall_count']=len(c['main_wall_indices'])
        c['supporting_depths_mm']=sorted({other['depth_mm'] for other in candidates
            if min(abs(other['angle_degrees']-c['angle_degrees']),360-abs(other['angle_degrees']-c['angle_degrees']))<max(2.,90/max(1,len(chosen)))
            and abs(other['radius_mm']-c['radius_mm'])<.15*outer_radius})
    return chosen


def classify_pattern(windows, type_count=2):
    """Deterministic descriptor clustering and explicit angular-instance lists."""
    windows=[{**w,'angle_degrees':float(np.degrees(np.arctan2(*polygon_centroid(w['boundary_uv'])[::-1]))%360)}
             if 'boundary_uv' in w else w for w in windows]
    windows,excluded=partition_window_candidates(windows)
    if len(windows)<max(2,type_count):return {'status':'needs_review','reason':'Insufficient windows','groups':[]}
    values=np.asarray([[w['area_mm2'],w['radial_span_mm'],w['tangential_span_mm'],w['radius_mm']] for w in windows])
    scale=np.maximum(np.mean(np.abs(values),axis=0),1e-9);features=values/scale
    seeds=[0]
    while len(seeds)<type_count:
        distances=np.min(np.sum((features[:,None]-features[seeds][None])**2,axis=2),axis=1)
        for i in seeds:distances[i]=-1
        seeds.append(int(np.argmax(distances)))
    centers=features[seeds].copy();labels=np.zeros(len(windows),dtype=int)
    for _ in range(20):
        labels=np.argmin(np.sum((features[:,None]-centers[None])**2,axis=2),axis=1)
        if any(not np.any(labels==i) for i in range(type_count)):
            return {'status':'needs_review','reason':'Requested types are not separable by observed descriptors','groups':[]}
        updated=np.asarray([features[labels==i].mean(axis=0) for i in range(type_count)])
        if np.allclose(updated,centers):break
        centers=updated
    groups=[]
    for i in range(type_count):
        members=[w for w,label in zip(windows,labels) if label==i]
        representative=min(members,key=lambda w:(-len(w['supporting_depths_mm']),w['angle_degrees']))
        angles=sorted(w['angle_degrees'] for w in members)
        gaps=np.diff(angles+[angles[0]+360])
        groups.append({'type_id':chr(ord('A')+i),'window_ids':[w['window_id'] for w in members],
            'representative_window_id':representative['window_id'],'angles_degrees':angles,
            'relative_angles_degrees':[(a-representative['angle_degrees'])%360 for a in angles],
            'uniform_relative_angles_degrees':[(j-angles.index(representative['angle_degrees']))*360/len(angles)%360 for j in range(len(angles))],
            'period_degrees':float(360/len(angles)),
            'equal_spacing_max_error_degrees':float(np.max(np.abs(gaps-360/len(angles))))})
    regular=all(g['equal_spacing_max_error_degrees']<=.15*g['period_degrees'] for g in groups)
    return {'status':'hypotheses' if regular else 'needs_review','groups':groups,'requires_3d_validation':True,
        'excluded_window_ids':[w['window_id'] for w in excluded],
        'reason':None if regular else 'Groups do not form consistent angular patterns; do not build arrays from these groups',
        'warning':'Types and angular phases are geometric hypotheses; verify imported cutters before cutting.'}


def select_side_regions(triangles, window, axis, band_mm, *, normal_axis_limit=.85, axial_band_mm=None, corner_exclusion_mm=None, normal_spread_degrees=35., cancel_event=None):
    """Restricted local wall candidates; interior triangle samples stay traceable."""
    tri=np.asarray(triangles);origin=np.asarray(axis['origin']);direction=np.asarray(axis['direction'],dtype=float);direction/=np.linalg.norm(direction)
    frame=window['frame'];u=np.asarray(frame['u_direction']);v=np.asarray(frame['v_direction'])
    centers=tri.mean(axis=1);delta=centers-origin
    uv=np.c_[delta@u,delta@v];depth=delta@direction
    vertex_depth=(tri-origin)@direction
    crosses_section=(vertex_depth.min(axis=1)<=window['depth_mm']+1e-6)&(vertex_depth.max(axis=1)>=window['depth_mm']-1e-6)
    normals=np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]);length=np.linalg.norm(normals,axis=1)
    good=length>1e-12;alignment=np.ones(len(tri));alignment[good]=np.abs(normals[good]@direction)/length[good]
    boundary=np.asarray(window['boundary_uv']);low=boundary.min(axis=0)-band_mm;high=boundary.max(axis=0)+band_mm
    axial_band=max(2*band_mm,.25*window['radial_span_mm']) if axial_band_mm is None else axial_band_mm
    mask=good&(alignment<normal_axis_limit)&np.all((uv>=low)&(uv<=high),axis=1)&(np.abs(depth-window['depth_mm'])<=axial_band)
    ids=np.flatnonzero(mask);regions=[]
    main_sides=main_wall_indices(window)
    arcs=[]
    for values in window['side_arcs_uv']:
        a=np.asarray(values);parts=[]
        for first,last in zip(a[:-1],a[1:]):
            count=max(1,int(np.ceil(np.linalg.norm(last-first)/(band_mm*.25))))
            if count>100000:raise ValueError('Selection band too small for boundary sampling')
            parts.append(first+(last-first)*np.linspace(0,1,count,endpoint=False)[:,None])
        arcs.append(np.vstack(parts+[a[-1:]]))
    if not len(ids):return []
    distances=np.column_stack([cKDTree(a).query(uv[ids])[0] for a in arcs])
    nearest=np.argmin(distances,axis=1)
    for side in range(4):
        check_cancelled(cancel_event)
        selected=ids[(nearest==side)&(distances[:,side]<=band_mm)]
        # Exclude corners, where fillets and two walls meet.
        if len(selected):
            endpoint_distance=np.minimum(np.linalg.norm(uv[selected]-arcs[side][0],axis=1),np.linalg.norm(uv[selected]-arcs[side][-1],axis=1))
            exclusion=min(band_mm*.5,.1*np.linalg.norm(arcs[side][-1]-arcs[side][0])) if corner_exclusion_mm is None else corner_exclusion_mm
            selected=selected[endpoint_distance>exclusion]
        initial_count=len(selected)
        seeds=selected[crosses_section[selected]]
        if len(seeds):
            midpoint=arcs[side][len(arcs[side])//2]
            seed_distance=np.linalg.norm(uv[seeds]-midpoint,axis=1)
            seeds=seeds[seed_distance<=seed_distance.min()+max(.25,band_mm*.5)]
        selected,quality=coherent_surface_patch(tri,selected,seed_indices=seeds,normal_limit_degrees=normal_spread_degrees)
        regions.append({'region_id':f"{window['window_id']}_side_{side+1}",'window_id':window['window_id'],
            'role':'main_wall' if side in main_sides else 'short_corner_transition',
            'side_index':side,'triangle_indices':selected.tolist(),'triangle_count':len(selected),
            'selection_quality':quality,'initial_triangle_count':initial_count,
            'status':'candidate' if len(seeds) and len(selected)>=2 and quality['retained_fraction']>=.2 else 'needs_review'})
    return regions


def coherent_surface_patch(triangles,indices,*,seed_indices=None,normal_limit_degrees=35.):
    """One connected, bounded-normal wall patch; never fit separate walls as one.
    Every exclusion is recorded. Fitting/extension is not validated outside it.
    """
    ids=np.asarray(indices,dtype=int)
    if not len(ids):return ids,{'retained_fraction':0.,'component_count':0,'normal_limit_degrees':35.}
    tri=np.asarray(triangles)[ids];vectors=np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]);areas=np.linalg.norm(vectors,axis=1)
    normals=vectors/np.maximum(areas[:,None],1e-30)
    bins=np.rint(normals/.2).astype(int);groups={}
    seed_mask=np.isin(ids,seed_indices) if seed_indices is not None else np.ones(len(ids),dtype=bool)
    for i,key in enumerate(map(tuple,bins)):
        if seed_mask[i]:groups.setdefault(key,[]).append(i)
    if not groups:return ids[:0],{'retained_fraction':0.,'component_count':0,'normal_limit_degrees':35.,'seed_triangle_count':0}
    group=max(groups.values(),key=lambda values:float(areas[values].sum()))
    reference=np.sum(normals[group]*areas[group,None],axis=0);reference/=max(np.linalg.norm(reference),1e-30)
    eligible=np.flatnonzero((normals@reference>=np.cos(np.radians(normal_limit_degrees)))&(areas>1e-12))
    adjacency={int(i):set() for i in eligible};edges={}
    for i in eligible:
        for a,b in ((0,1),(1,2),(2,0)):
            key=tuple(sorted((tuple(tri[i,a]),tuple(tri[i,b]))));edges.setdefault(key,[]).append(int(i))
    for incident in edges.values():
        if len(incident)==2:
            a,b=incident;adjacency[a].add(b);adjacency[b].add(a)
    components=[];unseen=set(adjacency)
    while unseen:
        first=min(unseen);unseen.remove(first);todo=[first];component=[]
        while todo:
            current=todo.pop();component.append(current)
            for neighbor in adjacency[current]:
                if neighbor in unseen:unseen.remove(neighbor);todo.append(neighbor)
        components.append(component)
    best=max(components,key=lambda values:(int(seed_mask[values].sum()),float(areas[values].sum()))) if components else []
    selected=ids[sorted(best)]
    return selected,{'retained_fraction':len(selected)/len(ids),'component_count':len(components),
        'normal_limit_degrees':normal_limit_degrees,'excluded_triangle_count':len(ids)-len(selected),
        'seed_triangle_count':int(seed_mask[best].sum()) if best else 0}
