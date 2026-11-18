import pytest
from supervisor.store import Store


def fixture_clip(tmp_path, campaign=''):
    s = Store('sqlite:///' + str(tmp_path / 'publish.db'))
    s.put('creator', 'test', {'id':'test','channel':'gaming','campaign':campaign,'permission':''})
    s.put('channel','gaming',{'id':'gaming','youtube_id':'verified-channel','paused':True,'autopublish':False})
    job = s.queue('test','https://youtube.com/watch?v=sample')
    c = s.add_clip(job['id'],{'start':0,'end':30,'path':'/data/render.mp4','title':'A real highlight','description':'Source video'})
    return s,c


def test_single_publish_is_atomic_review_and_idempotent(tmp_path):
    s,c = fixture_clip(tmp_path)
    result = s.request_manual_publication(c['id'])
    assert result['state'] == 'publish_queued'
    assert result['manual_publish'] and result['permission_confirmed']
    assert s.request_manual_publication(c['id']) == result
    assert s.review_count('gaming') == 1
    assert s.get('channel','gaming')['paused']


def test_single_publish_does_not_override_campaign_requirements(tmp_path):
    s,c = fixture_clip(tmp_path, 'tjr')
    with pytest.raises(ValueError):
        s.request_manual_publication(c['id'])
    assert s.get('clip',c['id'])['state'] == 'review'


def test_integration_test_never_publishes(tmp_path):
    s,c = fixture_clip(tmp_path)
    s.update_clip(c['id'],integration_test=True)
    with pytest.raises(ValueError):
        s.request_manual_publication(c['id'])


def test_start_only_range_does_not_reuse_whole_job(tmp_path):
    s = Store('sqlite:///' + str(tmp_path/'jobs.db'))
    a = s.queue('gaming','https://youtube.com/watch?v=sample')
    b = s.queue('gaming','https://youtube.com/watch?v=sample',source_offset=60)
    assert a['id'] != b['id']


def test_new_effects_get_new_render_but_only_one_variant_can_publish(tmp_path):
    s,c = fixture_clip(tmp_path)
    original_job = s.get_job(c['job_id'])
    variant_job = s.queue('test', original_job['source'], render_options={'preset':'punchy','music':'','music_rights_confirmed':False,'narration':False})
    assert variant_job['id'] != original_job['id']
    variant = s.add_clip(variant_job['id'], {'start':0,'end':30,'path':'/data/variant.mp4','title':'A revised highlight','description':'Source video'})
    assert variant['id'] != c['id']
    s.request_manual_publication(variant['id'])
    with pytest.raises(ValueError, match='Another version'):
        s.request_manual_publication(c['id'])


def test_manual_publisher_releases_without_scheduled_slot(tmp_path, monkeypatch):
    import supervisor.worker as worker
    s,c = fixture_clip(tmp_path)
    s.request_manual_publication(c['id'])
    s.update_clip(c['id'], publication_error='Old transient error')
    uploaded=[]; released=[]
    def upload(store,ident,channel):
        uploaded.append(ident)
        store.update_clip(ident,state='private_uploaded',youtube_id='once')
    def release(store,clip,channel):
        released.append(clip['id'])
        store.update_clip(clip['id'],state='published')
        return True
    monkeypatch.setattr(worker,'upload_private',upload)
    monkeypatch.setattr(worker,'release',release)
    worker.publish_jobs(s); worker.publish_jobs(s); worker.publish_jobs(s)
    assert uploaded == [c['id']]
    assert released == [c['id']]
    assert not s.get('clip', c['id']).get('publication_error')
