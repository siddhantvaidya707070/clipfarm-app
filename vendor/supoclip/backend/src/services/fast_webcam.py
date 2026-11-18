"""Conservative once-per-source webcam-panel detection and clean split layouts.

Rectangles are normalized x/y/width/height. They describe the complete baked
camera panel, never a face crop. Uncertain sources keep the ordinary renderer.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import subprocess
import uuid

import cv2
import numpy as np

VERSION = 1


def intersection_area(a, b):
    return max(0, min(a[0]+a[2], b[0]+b[2])-max(a[0], b[0])) * max(
        0, min(a[1]+a[3], b[1]+b[3])-max(a[1], b[1]))


def _rectangle(value):
    try:
        x,y,w,h = [float(v) for v in value]
        if all(math.isfinite(v) for v in (x,y,w,h)) and x >= 0 and y >= 0 and w >= .04 and h >= .04 and x+w <= 1.000001 and y+h <= 1.000001:
            return [x,y,min(w,1-x),min(h,1-y)]
    except (TypeError, ValueError):
        pass
    return None


def validated_layout(value):
    if not isinstance(value, dict) or value.get('enabled') is False:
        return None
    webcam = _rectangle(value.get('webcam'))
    if not webcam or webcam[2]*webcam[3] > .5:
        return None
    x,y,w,h = webcam
    available = [[0,0,x,1], [x+w,0,1-x-w,1], [0,0,1,y], [0,y+h,1,1-y-h]]
    available = [r for r in available if r[2] >= .15 and r[3] >= .15]
    if not available:
        return None
    game = max(available, key=lambda r:r[2]*r[3])
    try:
        fraction = float(value.get('top_fraction', .35))
        confidence = float(value.get('confidence',1))
        if not math.isfinite(fraction) or not .2 <= fraction <= .5 or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            return None
    except (TypeError, ValueError):
        return None
    return {'version':VERSION, 'kind':'streamer_game', 'webcam':webcam, 'game':game,
            'top_fraction':fraction, 'method':value.get('method','manual'),
            'confidence':confidence}


def _peaks(values, start, end, count=4):
    start,end = max(0,int(start)),min(len(values),int(end))
    if end-start < 4:
        return []
    region = values[start:end]
    baseline = float(np.median(region))
    mad = float(np.median(np.abs(region-baseline)))
    threshold = max(7., baseline*1.6, baseline+2*mad)
    result = []
    for index in np.argsort(region)[::-1]:
        position = start+int(index)+1
        if region[index] < threshold:
            break
        if all(abs(position-other) > 6 for other in result):
            result.append(position)
            if len(result) >= count:
                break
    return result


def _panel_for_face(frames, face):
    height,width = frames[0].shape[:2]
    fx,fy,fw,fh = face
    cx = fx+fw/2
    left = cx < width*.35
    if not left and cx <= width*.65:
        return None
    gray = np.stack([cv2.cvtColor(f,cv2.COLOR_BGR2GRAY).astype(np.float32) for f in frames])
    # Signed temporal consensus suppresses moving gameplay edges; fixed panel
    # boundaries retain their contrast. Use full line support, not face edges.
    gx = np.abs(np.median(np.diff(gray,axis=2),axis=0))
    gy = np.abs(np.median(np.diff(gray,axis=1),axis=0))
    ya,yb = max(0,int(fy-fh*.4)),min(height,int(fy+fh*1.4))
    vertical = gx[ya:yb].mean(axis=0)
    inner = _peaks(vertical,fx+fw*1.2,min(width*.55,fx+fw*7)) if left else _peaks(vertical,max(width*.45,fx-fw*6),fx-fw*.2)
    candidates = []
    for edge in inner:
        x0,x1 = (0,edge) if left else (edge,width)
        if not width*.12 <= x1-x0 <= width*.5:
            continue
        horizontal = gy[:,x0:x1].mean(axis=1)
        tops = [0]+_peaks(horizontal,max(0,fy-fh*4),fy-fh*.2)
        bottoms = [height]+_peaks(horizontal,fy+fh*1.15,min(height,fy+fh*4))
        for y0 in tops:
            for y1 in bottoms:
                if not height*.12 <= y1-y0 <= height*.85:
                    continue
                if not (x0 <= fx and fx+fw <= x1 and y0 <= fy and fy+fh <= y1):
                    continue
                if fw/(x1-x0) > .75 or fh/(y1-y0) > .85:
                    continue
                line = float(gx[y0:y1,edge-1].mean())
                horizontal_edges = [float(horizontal[y-1]) for y in (y0,y1) if y not in (0,height)]
                if not horizontal_edges or min([line]+horizontal_edges) < 7:
                    continue
                score = min([line]+horizontal_edges)
                candidates.append((score,[x0/width,y0/height,(x1-x0)/width,(y1-y0)/height]))
    if not candidates:
        return None
    return max(candidates,key=lambda item:item[0])[1]


def detect_layout_from_frames(frames, samples, top_fraction=.35):
    if len(frames) < 3 or len(samples) != len(frames):
        return None
    height,width = frames[0].shape[:2]
    seeds = [box[:4] for boxes in samples for box in boxes
             if .0008 < box[2]*box[3]/(width*height) < .06
             and (box[0]+box[2]/2 < width*.35 or box[0]+box[2]/2 > width*.65)]
    for seed in sorted(seeds,key=lambda b:b[2]*b[3],reverse=True):
        sx,sy = seed[0]+seed[2]/2,seed[1]+seed[3]/2
        matched = []
        for boxes in samples:
            nearby = [box[:4] for box in boxes if abs(box[0]+box[2]/2-sx) < width*.035
                      and abs(box[1]+box[3]/2-sy) < height*.045
                      and .5 < box[2]/seed[2] < 2]
            if nearby:
                matched.append(min(nearby,key=lambda b:abs(b[0]+b[2]/2-sx)+abs(b[1]+b[3]/2-sy)))
        if len(matched) < max(3,math.ceil(len(frames)*.75)):
            continue
        boxes = np.asarray(matched)
        centres = boxes[:,:2]+boxes[:,2:]/2
        if np.std(centres[:,0]) > width*.02 or np.std(centres[:,1]) > height*.025:
            continue
        panel = _panel_for_face(frames,np.median(boxes,axis=0))
        if panel:
            return validated_layout({'webcam':panel,'top_fraction':top_fraction,
                'method':'persistent_face_and_panel_edges','confidence':round(len(matched)/len(frames),3)})
    return None


def sample_source_frames(source):
    cap = cv2.VideoCapture(str(source))
    frames = []
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        duration = cap.get(cv2.CAP_PROP_FRAME_COUNT)/fps
        for fraction in (.08,.36,.64,.9):
            cap.set(cv2.CAP_PROP_POS_MSEC,max(0,duration-.2)*fraction*1000)
            ok,frame = cap.read()
            if ok:
                height,width = frame.shape[:2]
                frames.append(cv2.resize(frame,(640,max(1,round(height*640/width))),interpolation=cv2.INTER_AREA))
    finally:
        cap.release()
    return frames


def resolve_streamer_layout(source:Path,override=None,top_fraction=.35):
    source = Path(source)
    manual = source.with_suffix('.webcam_override.json')
    if override is None and manual.is_file():
        try:
            override = json.loads(manual.read_text())
        except (OSError,ValueError):
            return None
    if override is not None:
        return validated_layout({**override,'method':'manual'}) if isinstance(override,dict) else None
    if not source.is_file() or source.stat().st_size < 1024:
        return None
    stat = source.stat()
    cache = source.with_suffix('.webcam_layout.json')
    identity = [VERSION,stat.st_size,stat.st_mtime_ns,top_fraction]
    try:
        stored = json.loads(cache.read_text())
        if stored.get('source') == identity:
            return validated_layout(stored.get('layout'))
    except (OSError,ValueError,AttributeError):
        pass
    frames = sample_source_frames(source)
    from ..media.face_detection import open_yunet_detector
    detector = open_yunet_detector(max_input_width=640)
    samples = [[box[:4] for box in detector.detect(frame) if box[4] >= .7] for frame in frames] if detector else []
    plan = detect_layout_from_frames(frames,samples,top_fraction)
    try:
        cache.write_text(json.dumps({'source':identity,'layout':plan}),encoding='utf-8')
    except OSError:
        pass
    return plan


def _pixels(rect,width,height,*,inward=False):
    x,y,w,h = rect
    start = math.ceil if inward else math.floor
    finish = math.floor if inward else math.ceil
    px,py = max(0,start(x*width/2)*2),max(0,start(y*height/2)*2)
    right = min(width//2*2,finish((x+w)*width/2)*2)
    bottom = min(height//2*2,finish((y+h)*height/2)*2)
    return px,py,max(2,right-px),max(2,bottom-py)


def layout_filter(plan,width,height,out_width=720,out_height=1280,input_label='0:v:0',output_label='stacked'):
    plan = validated_layout(plan)
    if plan is None:
        raise ValueError('Invalid streamer/game layout')
    top = round(out_height*plan['top_fraction']/2)*2
    bottom = out_height-top
    camera = _pixels(plan['webcam'],width,height)
    game = _pixels(plan['game'],width,height,inward=True)
    def crop(rect):
        x,y,w,h = rect
        return f'crop={w}:{h}:{x}:{y}'
    return (f'[{input_label}]split=2[webcam_src][game_src];'
        f'[webcam_src]{crop(camera)},scale={out_width}:{top}:force_original_aspect_ratio=decrease:flags=fast_bilinear,'
        f'pad={out_width}:{top}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[webcam_panel];'
        f'[game_src]{crop(game)},split=2[game_full][game_blur_src];'
        f'[game_full]scale={out_width}:{bottom}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=fast_bilinear,setsar=1[game_fit];'
        f'[game_blur_src]scale=180:{max(2,bottom//4//2*2)}:force_original_aspect_ratio=increase:flags=fast_bilinear,'
        f'crop=180:{max(2,bottom//4//2*2)},gblur=sigma=10:steps=1,'
        f'scale={out_width}:{bottom}:flags=fast_bilinear,setsar=1[game_background];'
        '[game_background][game_fit]overlay=(W-w)/2:(H-h)/2[game_panel];'
        f'[webcam_panel][game_panel]vstack=inputs=2[{output_label}]')


def save_clip_layout(clip,plan):
    path = Path(clip).with_suffix('.source_map.json')
    payload = json.loads(path.read_text()) if path.is_file() else {}
    payload['streamer_layout'] = validated_layout(plan)
    path.write_text(json.dumps(payload),encoding='utf-8')


def load_clip_layout(clip):
    try:
        return validated_layout(json.loads(Path(clip).with_suffix('.source_map.json').read_text()).get('streamer_layout'))
    except (OSError,ValueError,AttributeError):
        return None


def render_layout_preview(clean,destination,plan):
    from ..media.ffmpeg import ffprobe_video_size
    width,height = ffprobe_video_size(Path(clean))
    graph = layout_filter(plan,width,height)
    result = subprocess.run(['ffmpeg','-v','error','-nostdin','-y','-threads','1',
        '-filter_complex_threads','1','-i',str(clean),'-filter_complex',graph,
        '-map','[stacked]','-map','0:a:0','-c:v','libx264','-preset','ultrafast','-crf','20',
        '-threads','2','-pix_fmt','yuv420p','-c:a','copy',str(destination)],capture_output=True,timeout=90)
    if result.returncode or not Path(destination).is_file():
        Path(destination).unlink(missing_ok=True)
        raise RuntimeError('Could not prepare the streamer/game editor preview')


def manual_streamer_layout(webcam=None):
    """Build the explicit equal-height layout, rejecting invalid manual crops."""
    value = [0, .55, .30, .45] if webcam is None else webcam
    if not isinstance(value, (list, tuple)) or len(value) != 4 or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value):
        raise ValueError('streamer_webcam must be an array of four finite numbers')
    plan = validated_layout({'webcam': value, 'top_fraction': .5, 'method': 'manual'})
    if plan is None:
        raise ValueError('streamer_webcam must be normalized [x,y,width,height] within the source, with dimensions at least 0.04 and area at most 0.5')
    return plan
