"""NumPy triangle/plane intersections; no FreeCAD or C++ worker dependency."""
from pathlib import Path
import struct
from itertools import product
import numpy as np
from ..adapters.cpp_worker_adapter import check_cancelled


def load_stl(path, cancel_event=None):
    path = Path(path)
    with path.open('rb') as stream:
        header = stream.read(84)
    count = struct.unpack('<I', header[80:84])[0] if len(header) == 84 else 0
    if len(header) == 84 and path.stat().st_size == 84+50*count:
        dtype = np.dtype([('normal','<f4',(3,)),('vertices','<f4',(3,3)),('attribute','<u2')])
        data = np.fromfile(path, dtype=dtype, offset=84, count=count)
        triangles = data['vertices'].astype(float)
    else:
        vertices = []
        with path.open('r', encoding='ascii') as stream:
            for i,line in enumerate(stream):
                if i % 10000 == 0:
                    check_cancelled(cancel_event)
                parts=line.split()
                if parts and parts[0].lower() == 'vertex':
                    if len(parts) != 4:
                        raise ValueError('Invalid ASCII STL vertex')
                    vertices.append([float(v) for v in parts[1:]])
        if len(vertices) % 3:
            raise ValueError('Incomplete ASCII STL triangle')
        triangles=np.asarray(vertices,dtype=float).reshape(-1,3,3)
    check_cancelled(cancel_event)
    if not len(triangles) or not np.isfinite(triangles).all():
        raise ValueError('STL must contain finite triangles')
    return triangles


def _contours(segments, tolerance, cancel_event):
    """Weld neighboring endpoints, deduplicate edges, preserve open/branch nodes."""
    vertices=[]; buckets={}; edges=set()
    def vertex(p):
        cell=tuple(np.floor(p/tolerance).astype(np.int64))
        for offset in product((-1,0,1),repeat=2):
            for i in buckets.get(tuple(a+b for a,b in zip(cell,offset)),[]):
                if np.linalg.norm(vertices[i]-p) <= tolerance:
                    return i
        i=len(vertices); vertices.append(p.copy()); buckets.setdefault(cell,[]).append(i)
        return i
    for i,(a,b) in enumerate(segments):
        if i%1024 == 0: check_cancelled(cancel_event)
        x,y=vertex(a),vertex(b)
        if x!=y: edges.add(tuple(sorted((x,y))))
    adjacency={i:[] for i in range(len(vertices))}
    for a,b in sorted(edges):
        adjacency[a].append(b); adjacency[b].append(a)
    unused=set(edges); paths=[]
    def walk(start,neighbor):
        path=[start]; current=start; nxt=neighbor
        while True:
            unused.remove(tuple(sorted((current,nxt))))
            path.append(nxt); current=nxt
            if current==start or len(adjacency[current])!=2: break
            following=[v for v in adjacency[current] if tuple(sorted((current,v))) in unused]
            if not following: break
            nxt=min(following)
        closed=path[-1]==path[0]
        if closed:path.pop()
        p=np.asarray([vertices[i] for i in path])
        area=abs(float(np.sum(p[:,0]*np.roll(p[:,1],-1)-p[:,1]*np.roll(p[:,0],-1)))/2) if closed else None
        paths.append({'closed':closed,'points_uv':p.tolist(),'point_count':len(p),'area_mm2':area})
    for start in sorted(adjacency):
        if len(adjacency[start])!=2:
            for neighbor in sorted(adjacency[start]):
                if tuple(sorted((start,neighbor))) in unused:walk(start,neighbor)
    while unused:
        a,b=min(unused);walk(a,b)
    return paths,sum(len(v)>2 for v in adjacency.values())


def section_triangles(triangles, origin, axis_direction, depths_mm, *, tolerance_mm=1e-5, cancel_event=None):
    tri=np.asarray(triangles,dtype=float); origin=np.asarray(origin,dtype=float); axis=np.asarray(axis_direction,dtype=float)
    if tri.ndim!=3 or tri.shape[1:]!=(3,3) or not np.isfinite(tri).all() or not len(tri):raise ValueError('Expected finite (N,3,3) triangles')
    if origin.shape!=(3,) or axis.shape!=(3,) or not np.isfinite(origin).all() or not np.isfinite(axis).all() or np.linalg.norm(axis)<1e-12:raise ValueError('Invalid section frame')
    if type(tolerance_mm) not in (int,float) or not np.isfinite(tolerance_mm) or tolerance_mm<=0:raise ValueError('Invalid tolerance')
    if not isinstance(depths_mm,list) or not 1<=len(depths_mm)<=64 or any(type(d) not in (int,float) or not np.isfinite(d) for d in depths_mm) or len(set(depths_mm))!=len(depths_mm):raise ValueError('Provide 1..64 unique finite depths')
    axis=axis/np.linalg.norm(axis); ref=np.eye(3)[np.argmin(np.abs(axis))]
    u=ref-axis*np.dot(ref,axis);u/=np.linalg.norm(u);v=np.cross(axis,u)
    axial=(tri-origin)@axis
    bounds=[float(axial.min()),float(axial.max())]
    output=[]
    for depth in depths_mm:
        check_cancelled(cancel_event)
        selected=np.flatnonzero((axial.min(axis=1)<=depth+tolerance_mm)&(axial.max(axis=1)>=depth-tolerance_mm))
        segments=[]; coplanar={}; coplanar_count=0
        for step,index in enumerate(selected):
            if step%1024==0:check_cancelled(cancel_event)
            points=tri[index];d=axial[index]-depth;d[np.abs(d)<=tolerance_mm]=0
            uv=np.c_[(points-origin)@u,(points-origin)@v]
            if np.all(d==0):
                coplanar_count+=1
                for a,b in zip(uv,np.roll(uv,-1,axis=0)):
                    key=tuple(sorted((tuple(a),tuple(b))))
                    coplanar[key]=coplanar.get(key,0)+1
                continue
            hits=[]
            for i,j in ((0,1),(1,2),(2,0)):
                if d[i]==0:hits.append(uv[i])
                if d[i]*d[j]<0:hits.append(uv[i]+(uv[j]-uv[i])*d[i]/(d[i]-d[j]))
            unique=[]
            for p in hits:
                if not any(np.linalg.norm(p-q)<=tolerance_mm for q in unique):unique.append(p)
            if len(unique)==2:segments.append(unique)
        segments.extend([[np.asarray(a),np.asarray(b)] for (a,b),count in coplanar.items() if count==1])
        contours,branches=_contours(segments,tolerance_mm,cancel_event)
        center=origin+depth*axis
        for i,c in enumerate(contours):
            c['contour_index']=i
            c['points_xyz']=(center+np.asarray(c['points_uv'])[:,:1]*u+np.asarray(c['points_uv'])[:,1:]*v).tolist()
        output.append({'depth_mm':float(depth),'unit':'mm','frame':{'origin':center.tolist(),'u_direction':u.tolist(),'v_direction':v.tolist(),'normal':axis.tolist()},
            'contours':contours,'branch_node_count':branches,'coplanar_triangle_count':coplanar_count,
            'status':'empty' if not contours else ('needs_review' if branches or any(not c['closed'] for c in contours) else 'closed_contours')})
    return {'schema_version':1,'unit':'mm','coordinate_order':['u','v'],'axial_range_mm':bounds,'sections':output,
        'warnings':['Closed loops do not identify windows or nesting; open chains and branch nodes are not force-closed. Coplanar mesh faces contribute boundary edges only.']}
