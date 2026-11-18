import pytest
from src.services.fast_webcam import manual_streamer_layout, intersection_area
from src.media.common import VALID_OUTPUT_FORMATS


def test_explicit_streamer_is_equal_height_and_game_excludes_webcam():
    plan = manual_streamer_layout()
    assert "vertical_streamer" in VALID_OUTPUT_FORMATS
    assert plan["webcam"] == pytest.approx([0, .55, .30, .45])
    assert plan["top_fraction"] == .5
    assert plan["method"] == "manual"
    assert intersection_area(plan["webcam"], plan["game"]) == 0
    assert plan["game"] == [.30, 0, .70, 1]


@pytest.mark.parametrize("crop", [[-1, 0, .3, .4], [0, 0, 2, .4], [0,0,float("nan"),.4], [0,0,.01,.4], [0,0,1,1], [], "bad", [False, 0, .3, .4], ["0", 0, .3, .4]])
def test_explicit_streamer_rejects_invalid_crops(crop):
    with pytest.raises(ValueError, match="streamer_webcam"):
        manual_streamer_layout(crop)


def test_crop_persists_when_retry_settings_are_merged():
    from src.api.routes.tasks import _merge_task_source_metadata
    crop = [.02, .5, .25, .45]
    saved = _merge_task_source_metadata({}, output_format="vertical_streamer", streamer_webcam=crop)
    retried = _merge_task_source_metadata(saved, add_subtitles=False)
    assert retried["streamer_webcam"] == crop
    assert retried["output_format"] == "vertical_streamer"
