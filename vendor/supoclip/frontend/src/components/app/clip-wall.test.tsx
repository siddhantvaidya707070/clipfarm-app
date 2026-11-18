import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import messages from "../../../messages/en/task.json";

import { ClipFocus, ClipTile, ProcessingPanel, clipProgressFromMessage, type WallClip } from "./clip-wall";
import { GenerationCard } from "./generation-card";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { GenerationSummary } from "@/lib/generations";

function clip(overrides: Partial<WallClip> = {}): WallClip {
  return {
    id: "clip-1",
    filename: "clip.mp4",
    start_time: "12:04",
    end_time: "13:20",
    duration: 76,
    text: "",
    relevance_score: 1,
    clip_order: 2,
    video_url: "/tasks/t/clips/clip-1/file",
    virality_score: 0,
    hook_score: 0,
    engagement_score: 0,
    value_score: 0,
    shareability_score: 0,
    hook_type: null,
    hook_title: null,
    ...overrides,
  };
}

const tileProps = { taskId: "t", busy: false, best: false, editable: false, onOpen: vi.fn(), onDownload: vi.fn() };

describe("workflow progress", () => {
  it("does not turn a fixed download milestone into a growing completion estimate", () => {
    const message = "Downloading audio: 20 MiB / ~60 MiB · 3 MiB/s · fragment 4/10";
    const { rerender } = render(<ProcessingPanel status="processing" progress={10}
      message={message} elapsedSeconds={300} />);

    expect(screen.queryByText("About 45 min left")).not.toBeInTheDocument();
    expect(screen.getByText(messages.eta.estimating)).toBeVisible();
    expect(screen.getByText(message)).toBeVisible();

    rerender(<ProcessingPanel status="processing" progress={10}
      message={message} elapsedSeconds={600} />);
    expect(screen.queryByText(/\d+ min left|Less than a minute left/)).not.toBeInTheDocument();
    expect(screen.getByText(messages.eta.estimating)).toBeVisible();
  });

  it.each([
    [30, "Transcribing audio..."],
    [55, "Finding the best moments..."],
    [80, "Creating clip 1/3..."],
  ])("does not estimate unmeasured stage durations at %s percent", (progress, message) => {
    render(<ProcessingPanel status="processing" progress={progress}
      message={message} elapsedSeconds={300} />);
    expect(screen.getByText(messages.eta.estimating)).toBeVisible();
    expect(screen.queryByText(/\d+ min left|Less than a minute left/)).not.toBeInTheDocument();
  });

  it("reads clip, part, recap and trailer counters", () => {
    expect(clipProgressFromMessage("Creating clip 3/5...")).toEqual({ current: 3, total: 5 });
    expect(clipProgressFromMessage("Creating part 2 / 4")).toEqual({ current: 2, total: 4 });
    expect(clipProgressFromMessage("Creating recap 1/1")).toEqual({ current: 1, total: 1 });
    expect(clipProgressFromMessage("Mapping the story")).toBeNull();
  });

  it("names the steps for what the generation makes", () => {
    render(<ProcessingPanel status="processing" progress={50} message="Mapping the story" workflow="series" />);
    const current = screen.getByText("Mapping the story", { selector: "li span" });
    expect(current.closest("li")).toHaveClass("font-medium");
    expect(screen.getByText("Cutting parts")).toBeInTheDocument();
  });

  it("counts parts while a series renders", () => {
    render(<ProcessingPanel status="processing" progress={80} message="Creating part 2/4" workflow="series" />);
    expect(screen.getByText("Part 2 of 4")).toBeInTheDocument();
    expect(screen.getByText("Cutting parts").closest("li")).toHaveClass("font-medium");
  });

  it("stays on the planning step whatever the searched topic says", () => {
    const message = "Finding where 'render farm clip 3/4' comes up...";
    render(<ProcessingPanel status="processing" progress={54} message={message} workflow="topic" />);
    expect(screen.getByText("Finding moments about your topic").closest("li")).toHaveClass("font-medium");
    expect(clipProgressFromMessage(message)).toBeNull();
    expect(screen.queryByText(/Clip 3 of 4/)).not.toBeInTheDocument();
  });

  it("keeps the highlights labels", () => {
    render(<ProcessingPanel status="processing" progress={80} message="Creating clip 1/3" />);
    expect(screen.getByText("Clip 1 of 3")).toBeInTheDocument();
    expect(screen.getByText("Finding moments")).toBeInTheDocument();
  });
});

describe("ClipTile", () => {
  it.each(["local_heuristic", "ai_transcript"])("shows %s selection relevance without labeling it virality", (hook_type) => {
    render(<ClipTile {...tileProps} clip={clip({ hook_type, relevance_score: .437, virality_score: 0 })} />);
    expect(screen.getByText("44% relevance")).toBeVisible();
    expect(screen.getByRole("img", { name: "44% relevance" })).toBeVisible();
    expect(screen.queryByRole("img", { name: /virality/i })).not.toBeInTheDocument();
  });

  it.each(["part", "recap", "trailer"] as const)("keeps planned %s outputs unscored even with local selection metadata", (workflow_role) => {
    render(<ClipTile {...tileProps} clip={clip({ workflow_role, hook_type: "local_heuristic", relevance_score: .8 })} />);
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.queryByText("80% relevance")).not.toBeInTheDocument();
  });

  it("badges parts with their number instead of a score", () => {
    render(<ClipTile {...tileProps} partTotal={5} clip={clip({ workflow_role: "part", part_number: 2, virality_score: 70 })} />);
    expect(screen.getByText("Part 2 of 5")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Part 2" })).toBeInTheDocument();
    expect(screen.queryByText("70")).not.toBeInTheDocument();
  });

  it("titles a part without repeating the number its badge shows", () => {
    render(<ClipTile {...tileProps} partTotal={5} clip={clip({ workflow_role: "part", part_number: 2, hook_title: "Part 2: The bank calls back" })} />);
    expect(screen.getByText("Part 2 of 5")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "The bank calls back" })).toBeInTheDocument();
  });

  it("keeps the score ring for topic clips", () => {
    render(<TooltipProvider><ClipTile {...tileProps} clip={clip({ workflow_role: "topic", virality_score: 70, workflow_meta: { why: "Explains pricing tiers" } })} /></TooltipProvider>);
    expect(screen.getByText("70")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Clip 2" })).toBeInTheDocument();
  });
});

describe("ClipFocus", () => {
  it("shows local relevance without invented virality or semantic subscores", () => {
    render(<ClipFocus clip={clip({ hook_type: "local_heuristic", relevance_score: .437 })}
      taskId="t" index={0} total={1} busy={false} editable={false}
      onNavigate={vi.fn()} onClose={vi.fn()} onDownload={vi.fn()} hookTypeLabel={() => "Local heuristic"} />);
    expect(screen.getByText("44% relevance")).toBeVisible();
    expect(screen.getByRole("img", { name: "44% relevance" })).toBeVisible();
    expect(screen.queryByText("Virality score")).not.toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /virality/i })).not.toBeInTheDocument();
    expect(screen.queryByText("0/25")).not.toBeInTheDocument();
    expect(screen.queryByText("Local heuristic · 44% relevance")).not.toBeInTheDocument();
  });

  it("lists the source moments of a stitched clip and what a part delivers", () => {
    render(
      <ClipFocus
        clip={clip({
          workflow_role: "part",
          part_number: 2,
          hook_title: "Part 2: The bank calls back",
          workflow_meta: { ranges: [[724.31, 801.92], [815.1, 840.55]], delivers: "Why the loan fell through", ends_on: "The call nobody expected" },
        })}
        taskId="t"
        index={1}
        total={4}
        busy={false}
        editable={false}
        onNavigate={vi.fn()}
        onClose={vi.fn()}
        onDownload={vi.fn()}
        hookTypeLabel={() => ""}
      />,
    );
    expect(screen.getByText("Source moments")).toBeInTheDocument();
    expect(screen.getByText("12:04 – 13:21")).toBeInTheDocument();
    expect(screen.getByText("13:35 – 14:00")).toBeInTheDocument();
    expect(screen.getByText("Why the loan fell through")).toBeInTheDocument();
    expect(screen.getByText("Ends on: The call nobody expected")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "The bank calls back" })).toBeInTheDocument();
    expect(screen.getByText("Part 2")).toBeInTheDocument();
  });

  it("numbers a part against the series it belongs to", () => {
    render(
      <ClipFocus
        clip={clip({ workflow_role: "part", part_number: 3, hook_title: "Teil 3 – Die Bank ruft an", workflow_meta: { part_count: 4 } })}
        taskId="t"
        index={2}
        total={4}
        busy={false}
        editable={false}
        onNavigate={vi.fn()}
        onClose={vi.fn()}
        onDownload={vi.fn()}
        hookTypeLabel={() => ""}
      />,
    );
    expect(screen.getByText("Part 3 of 4")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Die Bank ruft an" })).toBeInTheDocument();
  });
});

describe("GenerationCard", () => {
  const generation: GenerationSummary = {
    id: "g1",
    user_id: "u",
    source_id: "s",
    source_title: "Founder interview",
    source_type: "youtube",
    status: "completed",
    clips_count: 4,
    top_virality_score: 0,
    created_at: "2026-10-31T00:00:00Z",
    updated_at: "2026-10-31T00:00:00Z",
  };

  it("counts parts for a series and moments for a topic", () => {
    const { rerender } = render(<GenerationCard generation={{ ...generation, workflow: "series", part_count: 5 }} />);
    expect(screen.getByText("Series · 5 parts")).toBeInTheDocument();
    rerender(<GenerationCard generation={{ ...generation, workflow: "topic" }} />);
    expect(screen.getByText("Topic · 4 moments")).toBeInTheDocument();
    rerender(<GenerationCard generation={{ ...generation, workflow: "trailer", clips_count: 1 }} />);
    expect(screen.getByText("Trailer")).toBeInTheDocument();
    rerender(<GenerationCard generation={generation} />);
    expect(screen.getByText("4 clips")).toBeInTheDocument();
  });

  it("says what an active workflow is doing", () => {
    render(<GenerationCard generation={{ ...generation, status: "processing", workflow: "recap", clips_count: 0 }} />);
    expect(screen.getByText("Picking the main points…")).toBeInTheDocument();
  });
});

describe("download recovery progress", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shows a casual delay notice without an ETA, extra details, or increased progress", () => {
    vi.spyOn(Math, "random").mockReturnValue(0);
    const { rerender } = render(<ProcessingPanel status="processing" progress={65}
      message="Using a backup video download option..." elapsedSeconds={90} />);

    expect(screen.getByText("Hang tight")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent(messages.recovery.messages.snags);
    expect(screen.getByRole("status")).not.toHaveTextContent(/download|backup|connection|recovery/i);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "65");
    expect(screen.queryByText(/min left|minute left/)).not.toBeInTheDocument();

    rerender(<ProcessingPanel status="processing" progress={70}
      message="Creating clip 1/4..." elapsedSeconds={90} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByText("Hang tight")).not.toBeInTheDocument();
    expect(screen.getByText(messages.eta.estimating)).toBeVisible();
  });

  it.each([
    "Retrying video download with another connection...",
    "Recovering video audio...",
    "Recovering the full video download...",
    "Trying another video download recovery option...",
  ])("shows a generic delay notice for persisted recovery state: %s", (message) => {
    render(<ProcessingPanel status="processing" progress={10} message={message} elapsedSeconds={30} />);
    expect(Object.values(messages.recovery.messages)).toContain(screen.getByRole("status").textContent);
  });

  it("chooses a random sentence once and keeps it through progress and recovery changes", () => {
    const random = vi.spyOn(Math, "random").mockReturnValue(0.4);
    const { rerender } = render(<ProcessingPanel status="processing" progress={10}
      message="Using a backup video download option..." elapsedSeconds={30} />);
    expect(screen.getByRole("status")).toHaveTextContent(messages.recovery.messages.scenicRoute);

    random.mockReturnValue(0.9);
    rerender(<ProcessingPanel status="processing" progress={65}
      message="Trying another video download recovery option..." elapsedSeconds={90} />);
    expect(screen.getByRole("status")).toHaveTextContent(messages.recovery.messages.scenicRoute);
  });

  it("does not expose unknown provider errors or stale recovery while queued", () => {
    const { rerender } = render(<ProcessingPanel status="processing" progress={10}
      message="private provider error" elapsedSeconds={30} />);
    expect(screen.queryByText("private provider error")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    rerender(<ProcessingPanel status="queued" progress={0}
      message="Using a backup video download option..." elapsedSeconds={30} />);
    expect(screen.getByText("Waiting for a worker")).toBeVisible();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
