export type Caption = { start: number; end: number; text: string };
export type Clip = {
  id: string; type: string; start: number; end: number; score: number;
  title: string; description: string; tags: string[]; text: string;
  threshold_lowered: boolean; low_confidence: boolean; signals: Record<string, unknown>;
  captions: Caption[]; export_state: "pending" | "ready" | "failed" | "stale";
  mp4_url?: string; srt_url?: string; output_format?: string; burn_captions?: boolean; crop_center?: number; export_height?: 720 | 1080;
};
export type Stream = {
  id: string; source: string; title: string; duration: number; state: string; message: string;
  clips: Clip[]; timings: Record<string, number>; chat_status: string; timing_quality: string;
  created: string; error?: string; output_format: string; burn_captions: boolean; export_height?: 720 | 1080;
};
export const activeStates = new Set(["queued", "downloading", "recording", "transcribing", "scoring", "exporting"]);
export const clipTypes = ["NEW_MECHANIC", "FIRST_REACTION", "RAGE", "SHOCK", "FUNNY", "OTHER"];

export function parseCaptions(value: string, duration: number): Caption[] {
  return value.split("\n").filter(line => line.trim()).map((line, index) => {
    const match = /^\s*(\d+(?:\.\d+)?)\s*-->\s*(\d+(?:\.\d+)?)\s*\|\s*(.+)\s*$/.exec(line);
    if (!match) throw new Error(`Caption line ${index + 1}: use start --> end | text.`);
    const start = Number(match[1]), end = Number(match[2]);
    if (!match[3].trim() || !Number.isFinite(start) || !Number.isFinite(end) || end <= start || end > duration + 0.01) {
      throw new Error(`Caption line ${index + 1}: times must fit within the clip (${duration.toFixed(2)}s).`);
    }
    return { start, end, text: match[3].trim() };
  });
}

export function formatTime(value: number) {
  const seconds = Math.max(0, Math.floor(value || 0));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
