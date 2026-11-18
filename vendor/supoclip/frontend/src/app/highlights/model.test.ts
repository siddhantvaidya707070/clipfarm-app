import { formatTime, parseCaptions } from "./model";

describe("highlight captions", () => {
  it("parses relative caption seconds and preserves punctuation", () => {
    expect(parseCaptions("0 --> 1.5 | What?\n\n1.5 --> 3 | Wait | really?", 3)).toEqual([
      { start: 0, end: 1.5, text: "What?" }, { start: 1.5, end: 3, text: "Wait | really?" },
    ]);
  });
  it.each(["bad input", "-1 --> 2 | text", "2 --> 1 | text", "0 --> 5 | text", "0 --> 1 | "])("rejects invalid captions: %s", value => {
    expect(() => parseCaptions(value, 3)).toThrow(/Caption line 1/);
  });
  it("allows removing all captions", () => expect(parseCaptions("", 3)).toEqual([]));
  it("formats source times", () => expect(formatTime(125.8)).toBe("2:05"));
});
