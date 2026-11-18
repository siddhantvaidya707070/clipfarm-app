"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { useSession } from "@/lib/auth-client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { activeStates, clipTypes, formatTime, parseCaptions, type Clip, type Stream } from "./model";

const control = "mt-1 min-h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";
const badge = "rounded-md border px-2 py-1 text-xs font-medium";
type Write = (path: string, method?: string, body?: object) => Promise<boolean>;

export default function HighlightsPage() {
  const { data: session, isPending } = useSession();
  const [streams, setStreams] = useState<Stream[]>([]);
  const [activeId, setActiveId] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const controller = useRef<AbortController | null>(null);
  const userId = session?.user?.id;
  const current = streams.find(stream => stream.id === activeId);
  const hasActive = streams.some(stream => activeStates.has(stream.state));

  const refresh = useCallback(async () => {
    if (!userId) return;
    try {
      const response = await fetch("/api/highlights", { signal: controller.current?.signal, cache: "no-store" });
      const data = await response.json().catch(() => ({ error: "Highlight service unavailable. Try again shortly." }));
      if (!response.ok) throw new Error(data.error || "Could not load highlights.");
      if (!Array.isArray(data.streams)) throw new Error("Could not load highlights. Try refreshing.");
      setStreams(data.streams);
      setActiveId(id => id ?? data.streams[0]?.id);
    } catch (e) {
      if (!controller.current?.signal.aborted) setError((e as Error).message);
    } finally { if (!controller.current?.signal.aborted) setLoading(false); }
  }, [userId]);

  useEffect(() => {
    controller.current = new AbortController();
    if (userId) void refresh();
    return () => controller.current?.abort();
  }, [userId, refresh]);
  useEffect(() => {
    if (!hasActive) return;
    const timer = setInterval(() => void refresh(), 2000);
    return () => clearInterval(timer);
  }, [hasActive, refresh]);
  useEffect(() => { setSelected([]); }, [activeId]);

  const write: Write = async (path, method = "POST", body = {}) => {
    setBusy(true); setError(""); setNotice("");
    try {
      const response = await fetch(`/api/highlights${path}`, {
        method, headers: { "Content-Type": "application/json" },
        ...(method !== "DELETE" ? { body: JSON.stringify(body) } : {}), signal: controller.current?.signal,
      });
      const data = await response.json().catch(() => ({ error: "Highlight service unavailable. Try again shortly." }));
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : data.error || "Could not update highlights.");
      if (data.id) setActiveId(data.id);
      await refresh();
      setNotice(path.endsWith("/export") ? "Export started. Downloads appear when rendering finishes." : "Changes saved.");
      return true;
    } catch (e) {
      if (!controller.current?.signal.aborted) setError((e as Error).message);
      return false;
    } finally { if (!controller.current?.signal.aborted) setBusy(false); }
  };

  function start(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    void write("", "POST", {
      source: String(data.get("source")).trim(), live_seconds: Number(data.get("live_seconds")),
      output_format: data.get("output_format"), export_height: Number(data.get("export_height")), burn_captions: data.has("burn_captions"),
    });
  }

  if (isPending) return <main className="mx-auto w-full max-w-6xl p-6"><Skeleton className="h-24 w-full" /></main>;
  if (!userId) return <main className="mx-auto max-w-xl p-8"><h1 className="text-2xl font-semibold">Highlights</h1><p className="mt-3">Sign in to find and edit highlights from YouTube videos and livestreams.</p><Button asChild className="mt-4"><Link href="/sign-in">Sign in</Link></Button></main>;

  return <main className="mx-auto w-full max-w-6xl min-w-0 space-y-6 p-4 md:p-8">
    <header className="flex flex-wrap items-start justify-between gap-3">
      <div><h1 className="text-2xl font-semibold tracking-tight">Highlights</h1><p className="mt-1 max-w-2xl text-sm text-muted-foreground">Find reactions in a YouTube recording or bounded livestream, refine each moment, then export clips and captions.</p></div>
      <Button variant="outline" size="sm" disabled={busy} onClick={() => void refresh()}>Refresh highlights</Button>
    </header>

    <form onSubmit={start} className="space-y-4 rounded-xl border bg-background p-4 md:p-5" aria-label="Start highlight analysis">
      <label className="block text-sm font-medium">YouTube video or livestream URL<Input className="mt-1" name="source" type="url" required placeholder="https://www.youtube.com/watch?v=…" disabled={busy} /></label>
      <div className="flex flex-wrap items-end gap-4">
        <label className="min-w-40 flex-1 text-sm">Live capture limit (seconds)<Input className="mt-1" name="live_seconds" type="number" min="15" max="10800" defaultValue="600" required disabled={busy} /><span className="mt-1 block text-xs text-muted-foreground">15 seconds to 3 hours. Only used when the video is live.</span></label>
        <label className="min-w-40 flex-1 text-sm">Export format<select name="output_format" className={control} disabled={busy}><option value="original">Original framing</option><option value="vertical">Vertical 9:16</option></select></label>
        <label className="min-w-40 flex-1 text-sm">Export resolution<select name="export_height" className={control} defaultValue="720" disabled={busy}><option value="720">720p · faster</option><option value="1080">1080p · more detail</option></select></label>
        <label className="flex min-h-10 items-center gap-2 text-sm"><input name="burn_captions" type="checkbox" defaultChecked disabled={busy} className="size-4 accent-current" />Burn captions into video</label>
        <Button type="submit" disabled={busy}>{busy ? "Working…" : "Find highlights"}</Button>
      </div>
      <p className="text-xs text-muted-foreground">Transcription uses gpt-transcribe. Live recordings stop at the limit or when you stop capture.</p>
      <p className="text-xs text-muted-foreground">Highlight videos export at 30 fps, with captions optimized for faster rendering. Native editor exports keep their existing settings.</p>
    </form>

    {error && <p role="alert" className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">{error}</p>}
    {notice && <p role="status" className="text-sm">{notice}</p>}
    {loading ? <div aria-label="Loading highlights" className="space-y-3"><Skeleton className="h-16 w-full" /><Skeleton className="h-48 w-full" /></div> : !streams.length ? <div className="py-10 text-center"><h2 className="font-medium">Your first highlight starts with a video</h2><p className="mt-2 text-sm text-muted-foreground">Paste a YouTube URL above. Your progress and editable clips will appear here.</p></div> : <div className="grid min-w-0 gap-6 lg:grid-cols-[220px_minmax(0,1fr)]">
      <nav aria-label="Highlight recordings" className="min-w-0"><h2 className="mb-3 text-sm font-semibold">Recordings ({streams.length})</h2><ul className="flex gap-2 overflow-x-auto pb-2 lg:flex-col">
        {streams.map(stream => <li key={stream.id} className="min-w-44 lg:min-w-0"><button type="button" onClick={() => { setActiveId(stream.id); setNotice(""); }} aria-current={stream.id === activeId ? "true" : undefined} className={`w-full rounded-lg border p-3 text-left text-sm transition-colors focus-visible:ring-2 focus-visible:ring-ring ${stream.id === activeId ? "border-foreground/30 bg-background" : "border-transparent hover:bg-accent"}`}><span className="line-clamp-2 break-words font-medium">{stream.title || "YouTube recording"}</span><span className="mt-1 block text-xs capitalize text-muted-foreground">{stream.state} · {stream.clips.length} clips</span></button></li>)}
      </ul></nav>

      {current && <section className="min-w-0 space-y-5" aria-label="Selected recording">
        <header className="space-y-3 border-b pb-5">
          <h2 className="break-words text-xl font-semibold">{current.title || "YouTube recording"}</h2>
          <div className="flex flex-wrap gap-2"><span className={`${badge} capitalize`} role="status">{current.state}</span><span className={badge}>{formatTime(current.duration)} source</span><span className={badge}>{current.chat_status === "available" ? "Chat signals available" : current.chat_status === "pending" ? "Chat signals pending" : "No chat data"}</span>{current.timing_quality !== "word" && <span className={badge}>Approximate caption timing</span>}</div>
          <p className="text-sm" aria-live="polite">{current.message}</p>
          {current.error && <p role="alert" className="break-words text-sm text-destructive">{current.error}</p>}
          {activeStates.has(current.state) && <progress aria-label={`${current.state} in progress`} className="h-1 w-full accent-current" />}
          {Object.keys(current.timings || {}).length > 0 && <dl className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground">{Object.entries(current.timings).map(([stage, seconds]) => <div key={stage} className="flex gap-1"><dt className="capitalize">{stage.replaceAll("_", " ")}:</dt><dd>{typeof seconds === "number" ? `${seconds.toFixed(1)}s` : String(seconds)}</dd></div>)}</dl>}
          <div className="flex flex-wrap gap-2">
            {current.state === "failed" && <Button size="sm" disabled={busy} onClick={() => void write(`/${current.id}/retry`)}>Retry analysis</Button>}
            {current.state === "recording" && <Button size="sm" variant="outline" disabled={busy} onClick={() => void write(`/${current.id}/stop`)}>Stop capture</Button>}
            <Button size="sm" variant="outline" disabled={busy || activeStates.has(current.state) || !current.clips.length} onClick={() => void write(`/${current.id}/export`, "POST", { clip_ids: selected })}>{selected.length ? `Export selected (${selected.length})` : "Re-export all clips"}</Button>
            <Button size="sm" variant="outline" disabled={busy || selected.length !== 2 || activeStates.has(current.state)} onClick={() => void write(`/${current.id}/merge`, "POST", { clip_ids: selected }).then(ok => { if (ok) setSelected([]); })}>Merge two selected</Button>
            <Button asChild size="sm" variant="ghost"><a href={`/api/highlights/${current.id}/manifest`} download>Download manifest</a></Button>
          </div>
          <p className="text-xs text-muted-foreground">Select two moments to merge. Saving edits marks an existing export as stale; re-export to apply them.</p>
        </header>

        {!current.clips.length && !activeStates.has(current.state) && <p className="py-5 text-sm text-muted-foreground">No highlights yet. Add a moment manually or retry a failed analysis.</p>}
        <div className="divide-y">{current.clips.map(clip => <ClipEditor key={`${clip.id}:${clip.start}:${clip.end}`} clip={clip} stream={current} busy={busy || activeStates.has(current.state)} selected={selected.includes(clip.id)} onSelect={checked => setSelected(ids => checked ? [...ids, clip.id] : ids.filter(id => id !== clip.id))} write={write} />)}</div>

        <details className="rounded-xl border bg-background p-4"><summary className="cursor-pointer font-medium">Add a moment manually</summary><form aria-label="Add manual highlight" className="mt-4 space-y-3" onSubmit={event => {
          event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
          const start = Number(data.get("start")), end = Number(data.get("end"));
          if (end <= start || (current.duration > 0 && end > current.duration)) { setError("Choose an end after the start, within the source duration."); return; }
          void write(`/${current.id}/clips`, "POST", { start, end, type: data.get("type"), title: data.get("title") }).then(ok => { if (ok) form.reset(); });
        }}>
          <div className="grid gap-3 sm:grid-cols-2"><label className="text-sm">Start (source seconds)<Input name="start" type="number" min="0" step="0.001" required /></label><label className="text-sm">End (source seconds)<Input name="end" type="number" min="0.01" step="0.001" max={current.duration || undefined} required /></label><label className="text-sm">Moment type<select className={control} name="type">{clipTypes.map(type => <option key={type} value={type}>{type}</option>)}</select></label><label className="text-sm">Title<Input name="title" required maxLength={69} /></label></div>
          <Button type="submit" size="sm" disabled={busy || activeStates.has(current.state)}>Add highlight</Button>
        </form></details>
      </section>}
    </div>}
  </main>;
}

function ClipEditor({ clip, stream, busy, selected, onSelect, write }: { clip: Clip; stream: Stream; busy: boolean; selected: boolean; onSelect: (value: boolean) => void; write: Write }) {
  const [error, setError] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [dirty, setDirty] = useState(false);
  const base = `/${stream.id}/clips/${clip.id}`;
  const fileBase = `/api/highlights/${stream.id}/files/${clip.id}`;
  const captionText = (clip.captions || []).map(caption => `${caption.start} --> ${caption.end} | ${caption.text}`).join("\n");

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError("");
    const data = new FormData(event.currentTarget);
    const start = Number(data.get("start")), end = Number(data.get("end"));
    try {
      if (end <= start || (stream.duration > 0 && end > stream.duration)) throw new Error("Choose an end after the start, within the source duration.");
      const editedCaptions = String(data.get("captions"));
      const regenerateCaptions = (start !== clip.start || end !== clip.end) && editedCaptions === captionText;
      const captionUpdate = regenerateCaptions ? {} : { captions: parseCaptions(editedCaptions, end - start) };
      const saved = await write(base, "PATCH", {
        start, end, type: data.get("type"), title: data.get("title"), description: data.get("description"),
        tags: String(data.get("tags")).split(",").map(tag => tag.trim()).filter(Boolean), ...captionUpdate,
        output_format: data.get("output_format"), export_height: Number(data.get("export_height")), burn_captions: data.has("burn_captions"), crop_center: Number(data.get("crop_center")),
      });
      if (saved) setDirty(false);
    } catch (e) { setError((e as Error).message); }
  }

  return <article className="min-w-0 space-y-3 py-5 first:pt-0" aria-label={clip.title || `Highlight ${formatTime(clip.start)}`}>
    <div className="flex flex-wrap items-center justify-between gap-3"><label className="flex min-w-0 items-start gap-2 text-sm font-semibold"><input type="checkbox" checked={selected} onChange={event => onSelect(event.target.checked)} disabled={busy} className="mt-0.5 size-4 shrink-0 accent-current" /><span className="break-words">{clip.title || "Untitled highlight"}</span></label><span className="text-xs tabular-nums text-muted-foreground">{formatTime(clip.start)}–{formatTime(clip.end)} · score {Number(clip.score || 0).toFixed(2)}</span></div>
    <div className="flex flex-wrap gap-2"><span className={badge}>{clip.type}</span><span className={`${badge} capitalize`}>{clip.export_state === "stale" ? "Export needs refresh" : `Export ${clip.export_state}`}</span>{clip.threshold_lowered && <span className={badge}>Lowered score threshold</span>}{clip.low_confidence && <span className={badge}>Low confidence: review this moment</span>}</div>
    {clip.export_state === "ready" && <video controls preload="none" src={`${fileBase}.mp4`} className="max-h-80 w-full rounded-lg bg-black" aria-label={`Preview ${clip.title}`} />}
    {clip.text && <p className="max-w-prose whitespace-pre-wrap text-sm text-muted-foreground">{clip.text}</p>}
    {Object.keys(clip.signals || {}).length > 0 && <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">Scoring signals</summary><dl className="mt-2 flex flex-wrap gap-3">{Object.entries(clip.signals).map(([key, value]) => <div key={key}><dt className="inline">{key.replaceAll("_", " ")}: </dt><dd className="inline break-all">{typeof value === "object" ? JSON.stringify(value) : String(value)}</dd></div>)}</dl></details>}
    <div className="flex flex-wrap items-center gap-3 text-sm">
      {clip.export_state === "ready" && <><a href={`${fileBase}.mp4`} download className="font-medium underline underline-offset-4">Download MP4</a><a href={`${fileBase}.srt`} download className="font-medium underline underline-offset-4">Download SRT</a></>}
      <Button size="sm" variant="outline" disabled={busy || dirty} onClick={() => void write(`/${stream.id}/export`, "POST", { clip_ids: [clip.id] })}>{clip.export_state === "failed" ? "Retry clip export" : "Re-export clip"}</Button>
    </div>
    <details className="rounded-lg border bg-background p-3"><summary className="cursor-pointer text-sm font-medium">Edit timing, captions and details{dirty ? " (unsaved changes)" : ""}</summary><form aria-label={`Edit ${clip.title}`} onSubmit={save} onChange={() => setDirty(true)} className="mt-4 space-y-3">
      <fieldset disabled={busy} className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="text-sm">In point (source seconds)<Input name="start" type="number" min="0" step="0.001" defaultValue={clip.start} required /></label>
          <label className="text-sm">Out point (source seconds)<Input name="end" type="number" min="0.01" max={stream.duration || undefined} step="0.001" defaultValue={clip.end} required /></label>
          <label className="text-sm">Moment type<select name="type" className={control} defaultValue={clip.type}>{clipTypes.map(type => <option key={type} value={type}>{type}</option>)}</select></label>
          <label className="text-sm">Title<Input name="title" defaultValue={clip.title} maxLength={69} required /></label>
        </div>
        <label className="block text-sm">Description<textarea name="description" className={control} rows={2} defaultValue={clip.description} /></label>
        <label className="block text-sm">Tags (comma separated)<Input name="tags" defaultValue={(clip.tags || []).join(", ")} /></label>
        <label className="block text-sm">Captions<textarea name="captions" className={`${control} font-mono text-xs`} rows={5} defaultValue={captionText} aria-describedby={`caption-help-${clip.id}`} /></label>
        <p id={`caption-help-${clip.id}`} className="text-xs text-muted-foreground">One line per caption: start --&gt; end | text. Times are seconds relative to the clip, starting at 0. Changing clip boundaries regenerates untouched captions from the transcript. If you edit captions too, adjust their times to fit the new clip.</p>
        <div className="grid gap-3 sm:grid-cols-2"><label className="text-sm">Export format<select name="output_format" className={control} defaultValue={clip.output_format || stream.output_format}><option value="original">Original framing</option><option value="vertical">Vertical 9:16</option></select></label><label className="text-sm">Export resolution<select name="export_height" className={control} defaultValue={clip.export_height || stream.export_height || 720}><option value="720">720p · faster</option><option value="1080">1080p · more detail</option></select></label><label className="text-sm">Horizontal crop center (0 to 1)<Input name="crop_center" type="number" min="0" max="1" step="0.01" defaultValue={clip.crop_center ?? 0.5} required /><span className="mt-1 block text-xs text-muted-foreground">Vertical output only: 0 is left, 0.5 center, 1 right.</span></label></div>
        <label className="flex items-center gap-2 text-sm"><input name="burn_captions" type="checkbox" defaultChecked={clip.burn_captions ?? stream.burn_captions} className="size-4 accent-current" />Burn captions into video</label>
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        <Button size="sm" type="submit">Save clip changes</Button>
      </fieldset>
    </form></details>
    {deleting ? <div className="flex flex-wrap items-center gap-2 text-sm"><p>Delete this highlight and its exports?</p><Button size="sm" variant="destructive" disabled={busy} onClick={() => void write(base, "DELETE")}>Delete highlight</Button><Button size="sm" variant="ghost" disabled={busy} onClick={() => setDeleting(false)}>Keep highlight</Button></div> : <Button size="sm" variant="ghost" disabled={busy} onClick={() => setDeleting(true)}>Delete highlight…</Button>}
  </article>;
}
