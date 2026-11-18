import { describe, expect, it } from "vitest";

import { parseCreateSettings } from "./create-settings";

describe("parseCreateSettings", () => {
  it("remembers explicit streamer framing and a bounded normalized webcam crop", () => {
    const settings = { outputFormat: "vertical_streamer", streamerWebcam: [0, 0.55, 0.3, 0.45] };
    expect(parseCreateSettings(settings)).toEqual(settings);
    for (const crop of [[0, 0, 0.03, 0.4], [0, 0, 1, 1], [0.8, 0, 0.3, 0.4], [0, 0, 0, 0.4], [0, -0.1, 0.3, 0.4], [0, 0, 1, 1, 0], [0, 0, NaN, 1]]) {
      expect(parseCreateSettings({streamerWebcam: crop})).toEqual({});
    }
  });
  it("keeps every well-formed setting", () => {
    const settings = {
      workflow: "trailer",
      captionTemplate: "minimal",
      addSubtitles: false,
      outputFormat: "original",
      includeBroll: true,
      cutLongPauses: false,
      pauseThresholdMs: 800,
      removeFillerWords: false,
      filteredWords: "um",
      lengths: { series: "long" },
      advanced: { trailer: { spoilers: "strict", target_seconds: 40, hook_title: false } },
    };
    expect(parseCreateSettings(settings)).toEqual(settings);
  });

  it("drops anything else, including keys that aren't settings", () => {
    expect(parseCreateSettings({
      workflow: "montage",
      topic: "secret",
      instructions: "secret",
      outputFormat: "square",
      pauseThresholdMs: 10,
      filteredWords: "x".repeat(2001),
      lengths: { highlights: "long", recap: "huge" },
      advanced: { montage: { a: true }, series: { "bad key": true, cold_open: { nested: true }, part_labels: false } },
    })).toEqual({ advanced: { series: { part_labels: false } } });
    for (const value of [null, "x", 3, []]) expect(parseCreateSettings(value)).toEqual({});
  });
});
