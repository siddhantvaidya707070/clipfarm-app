import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import HighlightsPage from "./page";
import type { Stream } from "./model";

vi.mock("@/lib/auth-client", () => ({ useSession: () => ({ data: { user: { id: "owner" } }, isPending: false }) }));
const stream: Stream = {
  id: "stream", source: "https://youtube.com/watch?v=example", title: "A recording", duration: 120,
  state: "completed", message: "Review your highlights", created: "2026-11-15", timings: { scoring: 1.2 },
  chat_status: "no chat data", timing_quality: "approximate", output_format: "original", burn_captions: true,
  clips: [{ id: "clip", title: "Big reaction", type: "SHOCK", start: 10, end: 20, score: 0.8,
    description: "A sudden twist", tags: ["reaction"], text: "What just happened?", threshold_lowered: true,
    low_confidence: true, signals: { audio_energy: 0.7 }, captions: [{ start: 0, end: 2, text: "What?" }], export_state: "ready" }],
};
beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => init?.method ? Response.json(stream) : Response.json({ streams: [structuredClone(stream)], config: { model: "gpt-transcribe" } })));
});
afterEach(() => vi.unstubAllGlobals());

it("shows timing and confidence disclosures with protected downloads", async () => {
  render(<HighlightsPage />);
  expect(await screen.findByText("No chat data")).toBeInTheDocument();
  expect(screen.getByText("Approximate caption timing")).toBeInTheDocument();
  expect(screen.getByText("Lowered score threshold")).toBeInTheDocument();
  expect(screen.getByText(/Low confidence/)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Download MP4" })).toHaveAttribute("href", "/api/highlights/stream/files/clip.mp4");
  expect(screen.getByRole("link", { name: "Download SRT" })).toHaveAttribute("href", "/api/highlights/stream/files/clip.srt");
});

it("submits the bounded source with export settings", async () => {
  render(<HighlightsPage />);
  await screen.findByText("A recording", { selector: "h2" });
  const form = screen.getByRole("form", { name: "Start highlight analysis" });
  fireEvent.change(within(form).getByLabelText("YouTube video or livestream URL"), { target: { value: "https://www.youtube.com/watch?v=abc" } });
  fireEvent.change(within(form).getByLabelText(/Live capture limit/), { target: { value: "300" } });
  fireEvent.change(within(form).getByLabelText("Export format"), { target: { value: "vertical" } });
  fireEvent.click(within(form).getByLabelText("Burn captions into video"));
  fireEvent.submit(form);
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/highlights", expect.objectContaining({ method: "POST", body: JSON.stringify({ source: "https://www.youtube.com/watch?v=abc", live_seconds: 300, output_format: "vertical", export_height: 720, burn_captions: false }) })));
});

it("validates caption edits before saving relative times and metadata", async () => {
  render(<HighlightsPage />);
  const form = await screen.findByRole("form", { name: "Edit Big reaction", hidden: true });
  const captions = within(form).getByLabelText("Captions");
  fireEvent.change(within(form).getByLabelText("Out point (source seconds)"), { target: { value: "14" } });
  fireEvent.change(within(form).getByLabelText("Export resolution"), { target: { value: "1080" } });
  fireEvent.change(captions, { target: { value: "0 --> 99 | Too long" } });
  fireEvent.submit(form);
  expect(await screen.findByText(/times must fit within the clip/)).toBeInTheDocument();
  expect(vi.mocked(fetch).mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(0);
  fireEvent.change(captions, { target: { value: "0 --> 2.5 | Really?" } });
  fireEvent.submit(form);
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/highlights/stream/clips/clip", expect.objectContaining({ method: "PATCH", body: expect.stringContaining('"captions":[{"start":0,"end":2.5,"text":"Really?"}]') })));
  const patch = vi.mocked(fetch).mock.calls.find(([, init]) => init?.method === "PATCH")?.[1];
  expect(JSON.parse(String(patch?.body)).type).toBe("SHOCK");
  expect(JSON.parse(String(patch?.body)).export_height).toBe(1080);
});

it("requires an inline delete confirmation and sends a protected mutation", async () => {
  render(<HighlightsPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Delete highlight…" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete highlight" }));
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/highlights/stream/clips/clip", expect.objectContaining({ method: "DELETE" })));
});

it("starts only the selected export", async () => {
  render(<HighlightsPage />);
  fireEvent.click(await screen.findByRole("checkbox", { name: "Big reaction" }));
  fireEvent.click(screen.getByRole("button", { name: "Export selected (1)" }));
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/highlights/stream/export", expect.objectContaining({ method: "POST", body: '{"clip_ids":["clip"]}' })));
});

it("adds a manual moment after validating the source range", async () => {
  render(<HighlightsPage />);
  await screen.findByText("A recording", { selector: "h2" });
  const form = screen.getByRole("form", { name: "Add manual highlight", hidden: true });
  fireEvent.change(within(form).getByLabelText("Start (source seconds)"), { target: { value: "30" } });
  fireEvent.change(within(form).getByLabelText("End (source seconds)"), { target: { value: "45" } });
  fireEvent.change(within(form).getByLabelText("Title"), { target: { value: "A manual moment" } });
  fireEvent.submit(form);
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/highlights/stream/clips", expect.objectContaining({ method: "POST", body: '{"start":30,"end":45,"type":"NEW_MECHANIC","title":"A manual moment"}' })));
});

it("offers the six backend moment types and retains an existing SHOCK selection", async () => {
  render(<HighlightsPage />);
  const edit = await screen.findByRole("form", { name: "Edit Big reaction", hidden: true });
  const manual = screen.getByRole("form", { name: "Add manual highlight", hidden: true });
  for (const form of [edit, manual]) {
    const select = within(form).getByLabelText("Moment type") as HTMLSelectElement;
    expect(Array.from(select.options, option => option.value)).toEqual(["NEW_MECHANIC", "FIRST_REACTION", "RAGE", "SHOCK", "FUNNY", "OTHER"]);
  }
  expect(within(edit).getByLabelText("Moment type")).toHaveValue("SHOCK");
});

it.each([
  { clipHeight: 1080, streamHeight: 720, expected: "1080" },
  { clipHeight: undefined, streamHeight: 1080, expected: "1080" },
  { clipHeight: undefined, streamHeight: undefined, expected: "720" },
] as const)("defaults clip resolution from clip, recording, or 720p: $expected", async ({ clipHeight, streamHeight, expected }) => {
  const source = structuredClone(stream);
  source.export_height = streamHeight;
  source.clips[0].export_height = clipHeight;
  vi.mocked(fetch).mockResolvedValue(Response.json({ streams: [source] }));
  render(<HighlightsPage />);
  const edit = await screen.findByRole("form", { name: "Edit Big reaction", hidden: true });
  expect(within(edit).getByLabelText("Export resolution")).toHaveValue(expected);
  const start = screen.getByRole("form", { name: "Start highlight analysis" });
  expect(within(start).getByLabelText("Export resolution")).toHaveValue("720");
});

it("merges exactly two selected moments", async () => {
  const two = structuredClone(stream);
  two.clips.push({ ...two.clips[0], id: "clip2", title: "Second reaction", start: 25, end: 35 });
  vi.mocked(fetch).mockImplementation(async (_url, init) => init?.method ? Response.json(two) : Response.json({ streams: [two] }));
  render(<HighlightsPage />);
  fireEvent.click(await screen.findByRole("checkbox", { name: "Big reaction" }));
  expect(screen.getByRole("button", { name: "Merge two selected" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: "Second reaction" }));
  fireEvent.click(screen.getByRole("button", { name: "Merge two selected" }));
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/highlights/stream/merge", expect.objectContaining({ method: "POST", body: '{"clip_ids":["clip","clip2"]}' })));
});

it("regenerates untouched captions when bounds change and refreshes the editor", async () => {
  const saved = structuredClone(stream);
  vi.mocked(fetch).mockImplementation(async (_url, init) => {
    if (init?.method === "PATCH") {
      saved.clips[0].end = 11;
      saved.clips[0].captions = [{ start: 0, end: 0.8, text: "Regenerated caption" }];
      return Response.json(saved);
    }
    return Response.json({ streams: [saved] });
  });
  render(<HighlightsPage />);
  const initial = await screen.findByRole("form", { name: "Edit Big reaction", hidden: true });
  fireEvent.change(within(initial).getByLabelText("Out point (source seconds)"), { target: { value: "11" } });
  fireEvent.submit(initial);
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/highlights/stream/clips/clip", expect.objectContaining({ method: "PATCH" })));
  const patch = vi.mocked(fetch).mock.calls.find(([, init]) => init?.method === "PATCH")?.[1];
  expect(JSON.parse(String(patch?.body))).not.toHaveProperty("captions");
  await waitFor(() => {
    const updated = screen.getByRole("form", { name: "Edit Big reaction", hidden: true });
    expect(updated).not.toBe(initial);
    expect(within(updated).getByLabelText("Captions")).toHaveValue("0 --> 0.8 | Regenerated caption");
    expect(within(updated).getByLabelText("Out point (source seconds)")).toHaveValue(11);
  });
});

it("preserves unsaved edits when refreshing unchanged clip bounds", async () => {
  let reads = 0;
  vi.mocked(fetch).mockImplementation(async () => {
    const refreshed = structuredClone(stream);
    if (++reads > 1) refreshed.title = "Refreshed recording";
    return Response.json({ streams: [refreshed] });
  });
  render(<HighlightsPage />);
  const form = await screen.findByRole("form", { name: "Edit Big reaction", hidden: true });
  fireEvent.change(within(form).getByLabelText("Title"), { target: { value: "Unsaved title" } });
  fireEvent.click(screen.getByRole("button", { name: "Refresh highlights" }));
  await screen.findByText("Refreshed recording", { selector: "h2" });
  expect(within(screen.getByRole("form", { name: "Edit Big reaction", hidden: true })).getByLabelText("Title")).toHaveValue("Unsaved title");
});

it("accepts source bounds and edits with millisecond precision before saving", async () => {
  const source = structuredClone(stream);
  source.clips[0].start = 10.123;
  source.clips[0].end = 40.456;
  vi.mocked(fetch).mockImplementation(async (_url, init) => init?.method ? Response.json(source) : Response.json({ streams: [source] }));
  render(<HighlightsPage />);
  const form = await screen.findByRole("form", { name: "Edit Big reaction", hidden: true });
  const start = within(form).getByLabelText("In point (source seconds)") as HTMLInputElement;
  const end = within(form).getByLabelText("Out point (source seconds)") as HTMLInputElement;
  expect(start.checkValidity()).toBe(true);
  expect(end.checkValidity()).toBe(true);
  fireEvent.change(start, { target: { value: "10.124" } });
  fireEvent.change(end, { target: { value: "40.457" } });
  expect(start.checkValidity()).toBe(true);
  expect(end.checkValidity()).toBe(true);
  expect((form as HTMLFormElement).checkValidity()).toBe(true);
  fireEvent.submit(form);
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/highlights/stream/clips/clip", expect.objectContaining({ method: "PATCH", body: expect.stringContaining('"start":10.124,"end":40.457') })));
});

it("accepts millisecond precision for manual highlight bounds", async () => {
  render(<HighlightsPage />);
  await screen.findByText("A recording", { selector: "h2" });
  const form = screen.getByRole("form", { name: "Add manual highlight", hidden: true });
  const start = within(form).getByLabelText("Start (source seconds)") as HTMLInputElement;
  const end = within(form).getByLabelText("End (source seconds)") as HTMLInputElement;
  fireEvent.change(start, { target: { value: "50.123" } });
  fireEvent.change(end, { target: { value: "60.456" } });
  fireEvent.change(within(form).getByLabelText("Title"), { target: { value: "Precise moment" } });
  expect(start.checkValidity()).toBe(true);
  expect(end.checkValidity()).toBe(true);
  expect((form as HTMLFormElement).checkValidity()).toBe(true);
  fireEvent.submit(form);
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/highlights/stream/clips", expect.objectContaining({ method: "POST", body: expect.stringContaining('"start":50.123,"end":60.456') })));
});

it("limits both manual and edited titles to 69 characters", async () => {
  render(<HighlightsPage />);
  const edit = await screen.findByRole("form", { name: "Edit Big reaction", hidden: true });
  const manual = screen.getByRole("form", { name: "Add manual highlight", hidden: true });
  for (const form of [edit, manual]) expect(within(form).getByLabelText("Title")).toHaveAttribute("maxlength", "69");
});

it.each(["queued", "downloading", "recording"])("offers stop capture only while recording, current state: %s", async state => {
  vi.mocked(fetch).mockResolvedValue(Response.json({ streams: [{ ...stream, state }] }));
  render(<HighlightsPage />);
  await screen.findByText("A recording", { selector: "h2" });
  if (state === "recording") expect(screen.getByRole("button", { name: "Stop capture" })).toBeInTheDocument();
  else expect(screen.queryByRole("button", { name: "Stop capture" })).not.toBeInTheDocument();
});
