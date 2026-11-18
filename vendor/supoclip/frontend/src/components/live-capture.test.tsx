import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LiveCapture } from "./live-capture";

afterEach(() => vi.unstubAllGlobals());

function captureFetch(rows: unknown = []) {
  const fetcher = vi.fn(async (_url: unknown, init?: RequestInit) => ({ ok: true, json: async () => init?.method === "POST" ? {} : rows }));
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}

describe("LiveCapture", () => {
  it("tolerates a non-array list and sends all current task settings with recording mode", async () => {
    const fetcher = captureFetch({});
    const taskOptions = { workflow: "series", workflow_options: { part_count: 2 }, caption_template: "default", output_format: "vertical_streamer", streamer_webcam: [0, 0, 0.3, 0.4], clip_length: "short", include_broll: false, add_subtitles: true };
    render(<LiveCapture taskOptions={taskOptions} />);
    fireEvent.click(screen.getByRole("checkbox", { hidden: true }));
    fireEvent.change(screen.getByLabelText("Stream URL"), { target: { value: "https://youtube.com/watch?v=example" } });
    fireEvent.click(screen.getByRole("button", { name: "Start rolling capture", hidden: true }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith("/api/capture", expect.objectContaining({ method: "POST" })));
    const call = fetcher.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(call![1]!.body as string)).toMatchObject({ mode: "record", time_basis: "recording", start: "00:00:00", end: "01:00:00", task_options: taskOptions });
  });

  it("defaults recording to consecutive parts while preserving framing and captions", async () => {
    const fetcher = captureFetch();
    const taskOptions = { output_format: "vertical_streamer", streamer_webcam: [0, 0.55, 0.3, 0.45], caption_template: "default", add_subtitles: true, clip_length: "long", workflow: "recap", cut_long_pauses: true, remove_filler_words: true, filtered_words: ["test"], include_broll: true };
    render(<LiveCapture taskOptions={taskOptions} />);
    expect(screen.getByRole("checkbox", { hidden: true })).toBeChecked();
    fireEvent.change(screen.getByLabelText("Stream URL"), { target: { value: "https://youtube.com/watch?v=example" } });
    fireEvent.click(screen.getByRole("button", { name: "Start rolling capture", hidden: true }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith("/api/capture", expect.objectContaining({ method: "POST" })));
    const call = fetcher.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(call![1]!.body as string).task_options).toEqual({ ...taskOptions, continuous_parts: true, workflow: "highlights", clip_length: "auto", cut_long_pauses: false, remove_filler_words: false, filtered_words: [], include_broll: false });
  });

  it("hides timestamps for whole replay processing and shows all rolling batch links", async () => {
    const fetcher = captureFetch([{ id: "capture", source: "stream", state: "recording", message: "Recording", task_ids: ["one", "two"], task_id: "two" }]);
    render(<LiveCapture taskOptions={{ add_subtitles: false }} />);
    await waitFor(() => expect(screen.getAllByRole("link", { hidden: true })).toHaveLength(2));
    expect(screen.getByText("Open latest task")).toHaveAttribute("href", "/tasks/two");
    fireEvent.click(screen.getByLabelText("Existing replay"));
    expect(screen.queryByLabelText("Start (HH:MM:SS)")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Stream URL"), { target: { value: "https://youtube.com/watch?v=replay" } });
    fireEvent.click(screen.getByRole("button", { name: "Process replay", hidden: true }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith("/api/capture", expect.objectContaining({ method: "POST" })));
    const call = fetcher.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(call![1]!.body as string)).toMatchObject({ mode: "replay", start: null, end: null, task_options: { add_subtitles: false } });
  });
});
