# Fast streamer/game composition

Approved direction: detect a persistent baked webcam panel once per clean source,
stack that complete panel above the largest unobstructed gameplay rectangle, and
fit both without stretching. Default output is 720x1280 with a 35% upper panel.

1. Add a small layout module with validated normalized rectangles, conservative
   sparse face/boundary detection, a once-per-source cache, manual metadata/JSON
   override, and a reusable ffmpeg graph. Test wrong/no-face cases and exclusion.
2. Resolve the plan once before parallel fast renders. Persist it in task source
   metadata and each clip source map; apply the graph in the existing final encode.
   Place captions below the webcam and keep clean source and word timing unchanged.
3. Reuse the persisted plan in editor preparation as layout.mp4. Keep clean.mp4
   uncropped; automatic framing uses layout.mp4 while original framing uses clean.
   No panel dragging UI is claimed; normalized metadata is the manual override.
4. Run regression tests and retained real gameplay visual QA, including panel bounds,
   no webcam in gameplay, caption placement, clean editor asset, and sparse-pass time.

No deployment. Fresh URL end-to-end timing remains the root agent's benchmark.
