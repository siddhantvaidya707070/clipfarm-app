"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

type Capture = { id: string; source: string; state: string; message: string; recorded_seconds?: number; task_id?: string; task_ids?: string[] };

export function LiveCapture({ taskOptions = {}, disabled = false }: { taskOptions?: Record<string, unknown>; disabled?: boolean }) {
  const [mode, setMode] = useState<"record" | "replay">("record");
  const [continuousParts, setContinuousParts] = useState(true);
  const [rows, setRows] = useState<Capture[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const refresh = useCallback(async () => {
    try {
      const result = await fetch("/api/capture");
      if (!result.ok) throw new Error("Capture service unavailable");
      const data: unknown = await result.json();
      setRows(Array.isArray(data) ? data : []);
    } catch (e) { setError((e as Error).message); }
  }, []);
  useEffect(() => {
    void refresh();
    const timer = setInterval(() => { void refresh(); }, 5000);
    return () => clearInterval(timer);
  }, [refresh]);
  async function send(body: object) {
    setBusy(true); setError("");
    try {
      const response = await fetch("/api/capture", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : data.error || "Capture request failed");
      await refresh();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  function start(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const options = mode === "record" && continuousParts ? {
      ...taskOptions,
      continuous_parts: true,
      workflow: "highlights",
      clip_length: "auto",
      cut_long_pauses: false,
      remove_filler_words: false,
      filtered_words: [],
      include_broll: false,
    } : taskOptions;
    void send({ source: data.get("source"), start: data.get("start"), end: data.get("end"), time_basis: "recording", mode, task_options: options });
  }
  return (
    <details className="my-6 rounded-xl border border-border p-4">
      <summary className="cursor-pointer font-medium">Capture a livestream</summary>
      <p className="mt-3 text-sm text-muted-foreground">Record from now creates a first task after 60 seconds of video, then another every 3 minutes while recording continues. Processing takes additional time. Framing and caption settings selected below apply to each task.</p>
      <form onSubmit={start} className="mt-4 space-y-3">
        <fieldset className="flex flex-wrap gap-4 text-sm" disabled={busy || disabled}>
          <legend className="sr-only">Source mode</legend>
          <label className="flex items-center gap-2"><input type="radio" name="capture-mode" checked={mode === "record"} onChange={() => setMode("record")} />Record from now (rolling clips)</label>
          <label className="flex items-center gap-2"><input type="radio" name="capture-mode" checked={mode === "replay"} onChange={() => setMode("replay")} />Existing replay</label>
        </fieldset>
        {mode === "record" && <div className="space-y-1">
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={continuousParts} disabled={busy || disabled} onChange={(event) => setContinuousParts(event.target.checked)} />Continuous one-minute parts — keep all footage</label>
          <p className="text-sm text-muted-foreground">{continuousParts ? "Keeps every recorded moment in consecutive parts of about one minute. No highlight filtering, silence deletion, word removal, or B-roll. Your selected framing and captions are preserved." : "Uses your selected workflow and highlight settings to choose clips."}</p>
        </div>}
        {mode === "replay" && <p className="text-sm text-muted-foreground">Processes the whole replay URL as a normal video using your current settings. No recording or capture timestamps.</p>}
        <label className="block text-sm">Stream URL<Input className="mt-1" name="source" type="url" required placeholder="https://www.twitch.tv/channel" /></label>
        {mode === "record" && <div className="grid grid-cols-2 gap-3">
          <label className="text-sm">Start (HH:MM:SS)<Input className="mt-1" name="start" defaultValue="00:00:00" required /></label>
          <label className="text-sm">End (HH:MM:SS)<Input className="mt-1" name="end" defaultValue="01:00:00" required /></label>
        </div>}
        <Button type="submit" disabled={disabled || busy || (mode === "record" && rows.some(r => ["queued", "recording", "ready", "handing_off"].includes(r.state)))}>{mode === "record" ? "Start rolling capture" : "Process replay"}</Button>
      </form>
      {error && <p role="alert" className="mt-3 text-sm text-destructive">{error}</p>}
      <ul className="mt-4 space-y-3" aria-live="polite">
        {rows.slice(0, 5).map(row => <li key={row.id} className="border-t pt-3 text-sm">
          <p className="break-all font-medium">{row.source}</p>
          <p>{row.state}{row.recorded_seconds !== undefined && ` · ${Math.floor(row.recorded_seconds)}s recorded`}</p>
          <p className="text-muted-foreground">{row.message}</p>
          {Array.from(new Set([...(Array.isArray(row.task_ids) ? row.task_ids : []), ...(row.task_id ? [row.task_id] : [])])).map((taskId, index, ids) => <Link key={taskId} className="mr-4 inline-block underline" href={`/tasks/${taskId}`}>{taskId === row.task_id || index === ids.length - 1 ? "Open latest task" : `Open batch ${index + 1}`}</Link>)}
          {["queued", "recording", "ready"].includes(row.state) && <Button className="mt-2" size="sm" variant="outline" disabled={busy} onClick={() => void send({ action: "stop", id: row.id })}>Stop capture</Button>}
        </li>)}
      </ul>
    </details>
  );
}
