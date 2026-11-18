import json
from pathlib import Path

import numpy as np
import pytest

from src.services import fast_webcam as layout


def test_largest_game_rectangle_never_includes_the_webcam():
    plan = layout.validated_layout({'webcam':[0,0,.25,.3], 'top_fraction':.35})
    assert plan['game'] == [.25,0,.75,1]
    assert plan['top_fraction'] == .35
    assert layout.intersection_area(plan['webcam'], plan['game']) == 0


@pytest.mark.parametrize('webcam', [[0,0,1,1], [-.1,0,.3,.3], [0,0,float('nan'),.3], [0,0,0,.2]])
def test_invalid_or_whole_source_boxes_cannot_create_a_webcam(webcam):
    assert layout.validated_layout({'webcam':webcam}) is None


@pytest.mark.parametrize('confidence',[float('nan'),'invalid',None])
def test_invalid_confidence_is_rejected_in_manual_metadata(confidence):
    assert layout.validated_layout({'webcam':[0,0,.25,.3],'confidence':confidence}) is None


def test_pixel_rounding_never_puts_gameplay_back_inside_the_webcam():
    plan = layout.validated_layout({'webcam':[0,0,.2390625,.2388888889]})
    camera = layout._pixels(plan['webcam'],1280,720)
    game = layout._pixels(plan['game'],1280,720,inward=True)
    assert layout.intersection_area(camera,game) == 0


def test_detected_panel_contains_the_full_inset_not_just_the_face():
    frames = []
    for index in range(4):
        rng = np.random.default_rng(index)
        frame = rng.integers(0,150,(360,640,3),dtype=np.uint8)
        frame[:108,:160] = (25,25,25)
        frame[35:80,50:90] = (140+index,130,120)
        frames.append(frame)
    samples = [[(50,35,40,45)] for _ in frames]
    plan = layout.detect_layout_from_frames(frames, samples)
    assert plan is not None
    x,y,w,h = plan['webcam']
    assert x == 0 and y == 0
    assert w == pytest.approx(.25,abs=.015)
    assert h == pytest.approx(.3,abs=.02)
    assert layout.intersection_area(plan['webcam'],plan['game']) == 0


def test_absent_or_moving_faces_do_not_duplicate_content():
    frames = [np.zeros((360,640,3),dtype=np.uint8) for _ in range(4)]
    assert layout.detect_layout_from_frames(frames,[[] for _ in frames]) is None
    assert layout.detect_layout_from_frames(frames,[[(20+i*100,30,40,45)] for i in range(4)]) is None


def test_source_map_retains_layout_and_other_edit_metadata(tmp_path):
    clip = tmp_path/'clip.mp4'
    path = clip.with_suffix('.source_map.json')
    path.write_text(json.dumps({'source_ranges':[{'start':20,'end':30}], 'output_offsets':[0]}))
    plan = layout.validated_layout({'webcam':[0,0,.25,.3]})
    layout.save_clip_layout(clip,plan)
    assert layout.load_clip_layout(clip)['webcam'] == [0,0,.25,.3]
    assert json.loads(path.read_text())['source_ranges'] == [{'start':20,'end':30}]


def test_override_is_validated_and_does_not_need_detection(monkeypatch,tmp_path):
    source = tmp_path/'source.mp4'
    source.write_bytes(b'clean original')
    override = {'webcam':[.75,.7,.25,.3], 'top_fraction':.4}
    monkeypatch.setattr(layout,'sample_source_frames',lambda *a: pytest.fail('Manual override must bypass detection'))
    plan = layout.resolve_streamer_layout(source,override=override)
    assert plan['method'] == 'manual'
    assert plan['top_fraction'] == .4
    assert layout.resolve_streamer_layout(source,override={'enabled':False}) is None


def _color_source(path):
    import subprocess
    subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i',
        'color=c=red:s=1280x720:r=30:d=3','-f','lavfi','-i','sine=frequency=440:duration=3',
        '-vf','drawbox=x=0:y=0:w=320:h=216:color=blue:t=fill',
        '-c:v','libx264','-preset','ultrafast','-c:a','aac','-shortest',str(path)],check=True)


def test_fast_render_stacks_the_complete_webcam_and_excludes_it_from_gameplay(tmp_path):
    import cv2,hashlib
    from src.services.fast_render import render_fast_clip
    source = tmp_path/'source.mp4'
    _color_source(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    plan = layout.validated_layout({'webcam':[0,0,.25,.3]})
    clip = render_fast_clip(source,{'start_time':'00:00','end_time':'00:02','source_ranges':[(0,2)]},
        0,tmp_path,streamer_layout=plan,add_subtitles=False)
    cap = cv2.VideoCapture(clip['path']);ok,frame=cap.read();cap.release()
    assert ok and frame.shape[:2] == (1280,720)
    assert frame[224,360,0] > 180 and frame[224,360,2] < 70
    assert frame[864,360,2] > 180 and frame[864,360,0] < 70
    assert layout.load_clip_layout(Path(clip['path']))['webcam'] == plan['webcam']
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_editor_composition_preserves_uncropped_clean_source(tmp_path):
    import hashlib
    from src.services.editor_service import bake_editor_layout
    source = tmp_path/'clean.mp4'
    _color_source(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    plan = layout.validated_layout({'webcam':[0,0,.25,.3]})
    assert bake_editor_layout(source,'vertical',source,[(0,3)],True,streamer_layout=plan)
    assert (tmp_path/'layout.mp4').is_file()
    assert json.loads((tmp_path/'layout.json').read_text())['streamerLayout']['webcam'] == plan['webcam']
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_fast_cache_profile_changes_with_ai_selection_provider_and_model(monkeypatch):
    from types import SimpleNamespace
    from src.services import task_service
    config = SimpleNamespace(fast_highlights_enabled=True,fast_ai_selection_enabled=False,
                             fast_ai_selection_model='model-a')
    monkeypatch.setattr(task_service,'get_config',lambda:config)
    local = task_service.TaskService._build_cache_key('url','youtube','fast')
    config.fast_ai_selection_enabled = True
    ai = task_service.TaskService._build_cache_key('url','youtube','fast')
    assert ai != local
    config.fast_ai_selection_model = 'model-b'
    assert task_service.TaskService._build_cache_key('url','youtube','fast') != ai
