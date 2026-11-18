import math
import re


def validate(clip, campaign=''):
    errors = []
    start, end = clip.get('start'), clip.get('end')
    if not all(isinstance(x, (float, int)) and not isinstance(x, bool) and math.isfinite(x) for x in (start, end)) or not 0 <= start < end:
        return ['timestamps']
    duration = clip.get('probe', {}).get('duration', end - start)
    if (not isinstance(duration, (int, float)) or isinstance(duration, bool)
            or not math.isfinite(duration) or not 20 <= duration <= 55):
        errors.append('duration')
    text = ' '.join(str(clip.get(k, '')) for k in ('title', 'description'))
    if len(clip.get('title', '')) > 100:
        errors.append('title too long')
    if campaign == 'tjr':
        if '#tjr' not in clip.get('description', '').lower():
            errors.append('missing #TJR')
        if re.search('[\u3040-\u30ff]', text):
            errors.append('Japanese metadata')
    return errors
