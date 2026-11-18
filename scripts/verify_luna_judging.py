"""Small metered judge regression; no media upload or publication."""
import asyncio
from src import ai
from src.config import get_config

SAMPLES = [
    'Today we are changing the game settings. Open the menu, choose the display tab and adjust the resolution. These are the usual settings and we will discuss more options later.',
    'LOUD! LOUD! LOUD! AAA! AAA! Again! Again! Loud noise! Loud noise! Nothing happened. Just shouting the same words again. AAA! AAA!',
    'I just sold my only tournament-winning card for ONE coin. Wait, ONE?! I thought that button said a million! NO! NO! My opponent bought it! [laughter] He just beat me with my own card. I cannot believe this!',
    'You told me to press X to win. I pressed X and my whole team fell into the lava! Wait, you meant the controller? I am using a keyboard! [both laughing] That is why the bridge exploded. We did it again!',
]


async def main():
    cfg = get_config()
    assert cfg.llm == cfg.rerank_llm == 'openai:gpt-6-luna'
    candidates = [ai.TranscriptSegment(start_time=f'{i:02d}:00', end_time=f'{i:02d}:30',
                                      text=text, reaction_moment=i >= 2)
                  for i, text in enumerate(SAMPLES)]
    # Call the real agent directly so a rerank failure cannot silently fall back
    # to first-pass order and masquerade as a successful verification.
    result = await ai.get_rerank_agent().run(ai.build_candidate_rerank_prompt(candidates,
        clip_signals='[02:00 - 02:30] audio energy peak; [03:00 - 03:30] audio energy peak'))
    ranking = result.output.ranking
    assert {r.candidate_id for r in ranking} == set(range(4))
    assert ranking[0].candidate_id in (2, 3), 'Grounded reaction should lead this fixture'
    scores = {r.candidate_id: r.score for r in ranking}
    assert min(scores[2], scores[3]) > scores[1], 'Noise alone must not outrank grounded reactions'
    transcript = '\n'.join(f'[{i:02d}:00 - {i:02d}:30] {text}' for i, text in enumerate(SAMPLES))
    selection = await ai.get_transcript_agent().run(ai.build_transcript_analysis_prompt(transcript))
    assert any(s.reaction_moment for s in selection.output.most_relevant_segments)
    print('Live GPT-6 Luna judging passed. Candidate order:', [r.candidate_id for r in ranking])
    print('Reaction scores:', scores[2], scores[3], '; noise-only score:', scores[1])
    print('Initial selection preserves reaction openings; response contracts parsed successfully.')


if __name__ == '__main__':
    asyncio.run(main())
