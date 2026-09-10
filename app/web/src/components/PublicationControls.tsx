"use client";

import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { ApiError, apiErrorDetail, listModels } from "@/lib/api";
import * as publishing from "@/lib/publishing";
import type { AssetVersionRef, ModelEntry, UserAgent } from "@/lib/types";
import { checkRow, fieldset, inputStyle, labelStyle, primaryBtn, secondaryBtn } from "./builderStyles";

type SavedAsset = Pick<UserAgent, "name" | "userId" | "revision" | "incarnation" | "updatedAt" | "enabled">;
interface PublicationControlsProps {
  kind: publishing.PublicationKind;
  saved: SavedAsset | null;
  dirty: boolean;
  busy: boolean;
  ownerId?: string;
  models?: ModelEntry[];
  defaultModelId?: string | null;
}

type ReadState<T> = { phase: "loading" } | { phase: "ready"; value: T } | { phase: "error"; message: string };
type WriteState = { phase: "idle" } | { phase: "busy" } | { phase: "success"; message: string } |
  { phase: "error"; message: string; stale: boolean };

const stack: React.CSSProperties = { display: "flex", flexDirection: "column", gap: 12, minWidth: 0 };
const actions: React.CSSProperties = { display: "flex", flexWrap: "wrap", gap: 8 };
const hint: React.CSSProperties = { ...labelStyle, margin: 0 };

function useRead<T>(load: (signal: AbortSignal) => Promise<T>) {
  const [state, setState] = useState<ReadState<T>>({ phase: "loading" });
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal).then(
      (value) => { if (!controller.signal.aborted) setState({ phase: "ready", value }); },
      (error: unknown) => {
        if (!controller.signal.aborted) setState({ phase: "error", message: apiErrorDetail(error) });
      },
    );
    return () => controller.abort();
  }, [load, generation]);
  const refresh = () => {
    setState({ phase: "loading" });
    setGeneration((value) => value + 1);
  };
  return { state, refresh };
}

function useWrite<T = unknown>(onSuccess: (value: T) => void) {
  const [state, setState] = useState<WriteState>({ phase: "idle" });
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => { active.current?.abort(); }, []);
  const run = async (operation: (signal: AbortSignal) => Promise<T>, message: string) => {
    if (active.current) return;
    const controller = new AbortController();
    active.current = controller;
    setState({ phase: "busy" });
    try {
      const value = await operation(controller.signal);
      if (controller.signal.aborted) return;
      setState({ phase: "success", message });
      onSuccess(value);
    } catch (error) {
      if (controller.signal.aborted) return;
      const conflict = error instanceof ApiError && (error.status === 409 || error.status === 412);
      const unknown = !(error instanceof ApiError) || error.status >= 500;
      const context = conflict
        ? "Conflict: the saved source or publication changed. Refresh, then reload or save the draft before retrying."
        : unknown ? "Outcome unknown. Refresh before retrying; the request may have been accepted."
          : "Publication request failed.";
      setState({ phase: "error", stale: conflict || unknown, message: `${context} ${apiErrorDetail(error)}` });
    } finally {
      if (active.current === controller) active.current = null;
    }
  };
  return {
    state, run,
    busy: state.phase === "busy",
    stale: state.phase === "error" && state.stale,
    reset: () => setState({ phase: "idle" }),
  };
}

function ReadNotice<T>({ state, label, refresh }: {
  state: ReadState<T>; label: string; refresh: () => void;
}) {
  if (state.phase === "loading") return <p role="status" style={hint}>Loading {label}...</p>;
  if (state.phase === "error") return <div style={stack}>
    <p role="alert" style={{ ...hint, color: "var(--danger)" }}>{label} unavailable: {state.message}</p>
    <button type="button" style={secondaryBtn} onClick={refresh}>Retry {label}</button>
  </div>;
  return null;
}

function WriteNotice({ state }: { state: WriteState }) {
  if (state.phase === "idle") return null;
  return <p role={state.phase === "error" ? "alert" : "status"}
    style={{ ...hint, color: state.phase === "error" ? "var(--danger)" : "var(--fg-muted)" }}>
    {state.phase === "busy" ? "Saving publication change..." : state.message}
  </p>;
}

function Disclosure({ title, children }: { title: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return <details open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{title}</summary>
    {open && <div style={{ ...stack, paddingTop: 12 }}>{children}</div>}
  </details>;
}

export function PublicationControls(props: PublicationControlsProps) {
  const { saved } = props;
  // A new saved snapshot requires new consent. It also retires all in-flight
  // reads/writes, including a late response for an asset with the same name.
  const key = JSON.stringify([props.kind, saved?.userId ?? props.ownerId, saved?.name,
    saved?.incarnation, saved?.revision, saved?.updatedAt]);
  return <PublicationSession key={key} {...props} />;
}

function PublicationSession(props: PublicationControlsProps) {
  const availability = useRead(publishing.getPublicationCapabilities);
  if (availability.state.phase !== "ready") return <div>
    <ReadNotice state={availability.state} label="publication availability" refresh={availability.refresh} />
    {availability.state.phase === "error" && <p style={hint}>Private editing is still available.</p>}
  </div>;
  const capabilities = availability.state.value;
  if (capabilities.enabled !== true) return null;
  const canSubmit = capabilities.actions.includes("submit");
  const canReview = capabilities.actions.includes("review") || capabilities.operatorReviewAvailable === true;
  return <section aria-label="Publication" style={{ ...stack, borderTop: "1px solid var(--border)", paddingTop: 16 }}>
    <h3 style={{ margin: 0, fontSize: "1em" }}>Publication</h3>
    <p style={hint}>Private drafts stay yours. Sharing requires independent review, then your explicit activation.
      Review is not permission to execute tools.</p>
    {props.saved ? <OwnerControls {...props} saved={props.saved} canSubmit={canSubmit} /> :
      canSubmit && <p style={hint}>Save this {props.kind} before submitting it for review.</p>}
    {canReview && <Disclosure title="Independent review inbox">
      <ReviewInbox kind={props.kind} ownerId={props.saved?.userId ?? props.ownerId} />
    </Disclosure>}
    {capabilities.actions.includes("consume") && <Disclosure title="Available publications">
      <PublicationCatalog kind={props.kind} />
    </Disclosure>}
  </section>;
}

function OwnerControls({ saved, kind, dirty, busy, canSubmit, models, defaultModelId }: PublicationControlsProps & {
  saved: SavedAsset; canSubmit: boolean;
}) {
  const load = useCallback(async (signal: AbortSignal) => {
    const head = await publishing.getOwnerPublication(kind, saved.name, signal);
    if (head !== null && head.userId !== saved.userId) throw new Error("Owner publication response does not match this draft.");
    return head;
  }, [kind, saved.name, saved.userId]);
  const owner = useRead(load);
  const write = useWrite(owner.refresh);
  const refresh = () => { write.reset(); owner.refresh(); };
  const knownRevision = typeof saved.revision === "number" && Number.isSafeInteger(saved.revision) && saved.revision >= 0;
  const head = owner.state.phase === "ready" ? owner.state.value : null;
  const changedIncarnation = head !== null && (head.sourceIncarnation ?? null) !== (saved.incarnation ?? null);
  const savedBlocked = dirty || !knownRevision || !saved.enabled;
  const blocked = busy || write.busy || write.stale || owner.state.phase !== "ready";
  return <div style={stack}>
    <ReadNotice state={owner.state} label="publication status" refresh={refresh} />
    {owner.state.phase === "ready" && <>
      {head === null ? <p style={hint}>No publication submitted. This draft is private.</p> : <div style={stack}>
        <p style={hint}>Publication revision {head.revision}. {head.deleted ? "Withdrawn." :
          head.activeSource ? `${publishing.publicationVisibilityLabel(head.visibility)}: active version ${head.activeSource.version}.` : "Not active."}
          {" "}Handle: <code>{head.handle}</code></p>
        {head.pendingSource && <p style={hint}>
          Version {head.pendingSource.version}: {head.reviewDecision === "approved" ? "Approved; awaiting your activation." :
            head.reviewDecision === "rejected" ? "Rejected. Submit a revised version for another review." : "Awaiting independent review."}
          {head.reviewerId && <> Reviewer: <code>{head.reviewerId}</code>.</>}
        </p>}
        {changedIncarnation && <p role="status" style={hint}>This publication belongs to an earlier draft incarnation.
          Submit the current saved draft for a new review; it cannot activate the old version.</p>}
        <div style={actions}>
          {canSubmit && head.reviewDecision === "approved" && head.pendingSource && head.deleted === false &&
            <button type="button" style={primaryBtn} disabled={blocked || savedBlocked || changedIncarnation}
              onClick={() => {
                const source = head.pendingSource;
                if (!source || blocked || savedBlocked || changedIncarnation) return;
                void write.run((signal) => publishing.activatePublication(kind, saved.name, {
                  source, expectedHeadRevision: head.revision,
                }, signal), "Reviewed version activated. Execution remains separately authorized.");
              }}>Activate reviewed version</button>}
          {head.deleted === false && (head.activeSource || head.pendingSource) &&
            <button type="button" style={secondaryBtn} disabled={blocked}
              onClick={() => {
                if (blocked) return;
                void write.run((signal) => publishing.withdrawPublication(kind, saved.name, head, signal),
                  "Publication withdrawn. Immutable review history is retained.");
              }}>Withdraw publication</button>}
        </div>
      </div>}
      <button type="button" style={secondaryBtn} disabled={busy || write.busy} onClick={refresh}>Refresh publication status</button>
    </>}
    <WriteNotice state={write.state} />
    {dirty && <p role="status" style={hint}>Unsaved edits. Save changes first; only saved content can be submitted or activated.</p>}
    {!knownRevision && <p style={hint}>Saved revision unavailable. Save this {kind} before submitting or activating.</p>}
    {!saved.enabled && <p style={hint}>Enable and save this {kind} before submitting or activating.</p>}
    {canSubmit ? <Disclosure title="Submit a saved version for review">
      <SubmissionEditor kind={kind} saved={saved} models={models} defaultModelId={defaultModelId}
        blocked={blocked || savedBlocked} busy={busy || write.busy}
        submit={(input) => write.run((signal) => publishing.submitPublication(kind, saved.name, input, {
          ownerId: saved.userId, sourceIncarnation: saved.incarnation ?? null, headRevision: head?.revision ?? 0,
        }, signal),
          "Submitted for independent review. Nothing is activated automatically.")} />
    </Disclosure> : <p style={hint}>Submission is not available to your account. You can still withdraw a known owned publication.</p>}
  </div>;
}

interface SubmissionProps {
  kind: publishing.PublicationKind;
  saved: SavedAsset;
  models?: ModelEntry[];
  defaultModelId?: string | null;
  blocked: boolean;
  busy: boolean;
  submit: (input: publishing.PublicationSubmit) => Promise<void>;
}

const loadModels = async () => (await listModels()).models;

function SubmissionEditor(props: SubmissionProps) {
  return props.models ? <SubmissionForm {...props} models={props.models} /> : <SubmissionWithModels {...props} />;
}

function SubmissionWithModels(props: SubmissionProps) {
  const models = useRead(loadModels);
  return models.state.phase === "ready" ? <SubmissionForm {...props} models={models.state.value} /> :
    <ReadNotice state={models.state} label="publication models" refresh={models.refresh} />;
}

function SubmissionForm({ kind, saved, models, defaultModelId, blocked, busy, submit }: SubmissionProps & { models: ModelEntry[] }) {
  const id = useId();
  const [visibility, setVisibility] = useState<publishing.PublicationAudience["visibility"]>("shared");
  const [emails, setEmails] = useState("");
  const [groups, setGroups] = useState("");
  const [modelIds, setModelIds] = useState<string[]>(() => defaultModelId && models.some((model) => model.id === defaultModelId) ? [defaultModelId] : []);
  const [modes, setModes] = useState<publishing.PublicationMode[]>([kind === "agent" ? "chat" : "workflow"]);
  const [skillMode, setSkillMode] = useState<publishing.PublicationSkillMode>("versioned");
  const [reviewConsent, setReviewConsent] = useState(false);
  const [operatorConsent, setOperatorConsent] = useState(false);
  const [reviewer, setReviewer] = useState("");
  const [error, setError] = useState<string | null>(null);
  const availableModes: publishing.PublicationMode[] = kind === "agent" ? ["chat", "workflow", "delegation", "voice"] : ["workflow", "workflow_tool"];
  const validModels = modelIds.length > 0 && modelIds.length <= 32 && modelIds.every((id) => models.some((model) => model.id === id));
  return <form style={stack} onSubmit={(event) => {
    event.preventDefault();
    setError(null);
    if (blocked || typeof saved.revision !== "number") { setError("Save the draft and refresh publication status before submitting."); return; }
    if (!reviewConsent || !validModels || modes.length === 0) { setError("Choose catalog models, at least one mode, and explicit review consent."); return; }
    if (reviewer && (reviewer !== reviewer.trim() || reviewer === saved.userId)) { setError("Use an exact independent reviewer's internal user ID, not your own ID."); return; }
    let audience: publishing.PublicationAudience;
    try { audience = publishing.publicationAudience(visibility, emails, groups); }
    catch (error) { setError(apiErrorDetail(error)); return; }
    void submit({ expectedRevision: saved.revision, audience, modelIds, modes,
      reviewConsent: true, operatorReviewConsent: operatorConsent,
      ...(reviewer ? { reviewerUserId: reviewer } : {}), skillMode });
  }}>
    <p style={hint}>Submit saved revision {saved.revision ?? "unknown"}, not unsaved edits. No credentials or user MCP configuration
      are uploaded by these controls. Unsupported tools or dependencies are reported by the server, never silently removed.</p>
    <fieldset style={fieldset} disabled={busy}>
      <legend style={labelStyle}>Reviewed audience</legend>
      <label htmlFor={`${id}-visibility`} style={labelStyle}>Publication audience</label>
      <select id={`${id}-visibility`} style={inputStyle} value={visibility}
        onChange={(event) => setVisibility(event.target.value === "public" ? "public" : "shared")}>
        <option value="shared">Specific people or groups</option>
        <option value="public">Tenant-visible</option>
      </select>
      {visibility === "shared" ? <>
        <label htmlFor={`${id}-emails`} style={labelStyle}>Recipient emails</label>
        <textarea id={`${id}-emails`} style={inputStyle} rows={2} maxLength={25500} value={emails} onChange={(event) => setEmails(event.target.value)} />
        <label htmlFor={`${id}-groups`} style={labelStyle}>Group object IDs</label>
        <textarea id={`${id}-groups`} style={inputStyle} rows={2} maxLength={3700} value={groups} onChange={(event) => setGroups(event.target.value)} />
        <p style={hint}>Separate recipients with commas or newlines. Emails are normalized; group GUIDs stay exact.
          No directory lookup. The server verifies the audience.</p>
      </> : <p style={hint}>Available to authorized signed-in users in your tenant, not anonymous visitors.</p>}
    </fieldset>
    <fieldset style={fieldset} disabled={busy}>
      <legend style={labelStyle}>Reviewed execution contract</legend>
      <label htmlFor={`${id}-models`} style={labelStyle}>Publication models</label>
      <select id={`${id}-models`} multiple style={inputStyle} value={modelIds} size={Math.max(2, Math.min(6, models.length))}
        onChange={(event) => setModelIds(Array.from(event.target.selectedOptions, (option) => option.value))}>
        {models.map((model) => <option key={model.id} value={model.id}>{model.displayName} ({model.category})</option>)}
      </select>
      <p style={hint}>{models.length ? "Select 1-32 catalog models, including any capability or embedding models needed by tools. The server validates the combination."
        : "No catalog models available. Submission is unavailable."}</p>
      <fieldset style={fieldset}>
        <legend style={labelStyle}>Execution modes</legend>
        {availableModes.map((mode) => <label key={mode} style={checkRow}>
          <input type="checkbox" checked={modes.includes(mode)} onChange={(event) => setModes((current) => event.target.checked
            ? [...current, mode] : current.filter((item) => item !== mode))} />{mode === "workflow_tool" ? "Workflow tool" : mode}
        </label>)}
      </fieldset>
      <label htmlFor={`${id}-skills`} style={labelStyle}>Skill profile</label>
      <select id={`${id}-skills`} style={inputStyle} value={skillMode}
        onChange={(event) => setSkillMode(event.target.value === "excluded" ? "excluded" : "versioned")}>
        <option value="versioned">Versioned skills</option>
        <option value="excluded">Explicit no-skill profile</option>
      </select>
      <p style={hint}>{skillMode === "versioned" ? "Skills require versioned discovery. Unknown or unversioned skills block submission; they are never automatically excluded."
        : "You are explicitly requesting a reviewed profile without skills. Skill loading is excluded from this version, so skill-based behavior is unavailable."}</p>
    </fieldset>
    <fieldset style={fieldset} disabled={busy}>
      <legend style={labelStyle}>Independent review consent</legend>
      <label style={checkRow}><input type="checkbox" checked={reviewConsent} onChange={(event) => setReviewConsent(event.target.checked)} />
        I consent to independent review of this submitted source</label>
      <p style={hint}>The submitted source, audience, model and tool contracts become inspectable by an authorized independent reviewer.
        You cannot approve your own submission. This does not grant execution consent.</p>
      <label style={checkRow}><input type="checkbox" checked={operatorConsent} onChange={(event) => setOperatorConsent(event.target.checked)} />
        Also allow operator review of this submission</label>
      <p style={hint}>Optional and off by default. Authorizes an eligible operator to inspect this submitted version for review.</p>
      <label htmlFor={`${id}-reviewer`} style={labelStyle}>Reviewer internal user ID (optional)</label>
      <input id={`${id}-reviewer`} style={inputStyle} maxLength={256} value={reviewer} onChange={(event) => setReviewer(event.target.value)} autoComplete="off" />
      <p style={hint}>Use an exact known internal ID to restrict review to that independent reviewer. No user lookup is performed.</p>
    </fieldset>
    {error && <p role="alert" style={{ ...hint, color: "var(--danger)" }}>{error}</p>}
    <button type="submit" style={primaryBtn} disabled={blocked || busy || !reviewConsent || !validModels || modes.length === 0}>
      {busy ? "Submitting for review..." : "Submit for independent review"}
    </button>
  </form>;
}

function ReviewInbox({ kind, ownerId }: { kind: publishing.PublicationKind; ownerId?: string }) {
  const load = useCallback((signal: AbortSignal) => publishing.listPublicationReviews(kind, signal), [kind]);
  const inbox = useRead(load);
  const [selected, setSelected] = useState<publishing.PublicationReviewSummary | null>(null);
  const [acknowledged, setAcknowledged] = useState<publishing.PublicationReviewSummary[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  return <div style={stack}>
    <p style={hint}>Only owner-consented submissions authorized for your independent review appear here. This is not private-draft access.</p>
    <ReadNotice state={inbox.state} label="review inbox" refresh={inbox.refresh} />
    {message && <p role="status" style={hint}>{message}</p>}
    {inbox.state.phase === "ready" && <>
      {inbox.state.value.items.length === 0 ? <p style={hint}>No submissions awaiting your review.</p> :
        <ul style={{ margin: 0, paddingLeft: 20 }}>
          {inbox.state.value.items.map((item) => {
            const review = item.reviewDecision !== null ? item :
              acknowledged.find((entry) => publishing.sameAssetVersionRef(entry.source, item.source)) ?? item;
            return <li key={JSON.stringify(item.source)} style={{ marginBottom: 8 }}>
              {item.source.ownerId === ownerId ? <span>Your submission: {item.displayName}. An independent reviewer is required.</span> :
                <button type="button" style={secondaryBtn} onClick={() => { setMessage(null); setSelected(review); }}>
                  {review.reviewDecision === null ? "Review" : "View reviewed"} {item.displayName} (version {item.source.version})
                </button>}
            </li>;
          })}
        </ul>}
      {inbox.state.value.truncated && <p style={hint}>Partial inbox: the server limit was reached.</p>}
      <button type="button" style={secondaryBtn} onClick={() => { setSelected(null); setMessage(null); inbox.refresh(); }}>Refresh review inbox</button>
    </>}
    {selected && selected.source.ownerId !== ownerId && <ReviewDetail key={JSON.stringify(selected.source)} source={selected.source}
      acknowledged={selected.reviewDecision === null ? null : selected}
      onDecided={(review) => {
        setAcknowledged((current) => [review,
          ...current.filter((entry) => !publishing.sameAssetVersionRef(entry.source, review.source))].slice(0, 100));
        setSelected(null);
        setMessage("Review recorded. Owner activation is still required; this is not execution consent.");
        inbox.refresh();
      }} />}
  </div>;
}

function Evidence({ title, value }: { title: string; value: unknown }) {
  return <details>
    <summary>{title}</summary>
    <pre tabIndex={0} aria-label={title} style={{ ...inputStyle, fontSize: "0.85em", whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: "24rem", overflow: "auto" }}>
      {JSON.stringify(value, null, 2)}
    </pre>
  </details>;
}

function ReviewDetail({ source, acknowledged, onDecided }: {
  source: AssetVersionRef;
  acknowledged: publishing.PublicationReviewSummary | null;
  onDecided: (review: publishing.PublicationReviewSummary) => void;
}) {
  const load = useCallback((signal: AbortSignal) => publishing.getPublicationReview(source, signal), [source]);
  const detail = useRead(load);
  const write = useWrite<publishing.PublicationReviewSummary>(onDecided);
  const [note, setNote] = useState("");
  const id = useId();
  const refresh = () => { write.reset(); detail.refresh(); };
  if (detail.state.phase !== "ready") return <ReadNotice state={detail.state} label="review snapshot" refresh={refresh} />;
  const { version, headRevision } = detail.state.value;
  const reviewDecision = detail.state.value.reviewDecision ?? acknowledged?.reviewDecision ?? null;
  const reviewerId = detail.state.value.reviewerId ?? acknowledged?.reviewerId;
  return <section aria-label="Review submitted version" style={stack}>
    <h4 style={{ margin: 0 }}>Review {version.source.displayName}, version {source.version}</h4>
    <p style={hint}>Owner: <code>{source.ownerId}</code>. Publication revision {headRevision}. Submitted {version.submittedAt}.</p>
    <Evidence title="Exact source reference" value={source} />
    <Evidence title="Submitted source" value={version.source} />
    <Evidence title="Audience and reviewer consent" value={{
      ...version.audience, visibility: publishing.publicationVisibilityLabel(version.audience.visibility),
      reviewConsent: version.reviewConsent, operatorReviewConsent: version.operatorReviewConsent,
      reviewerUserId: version.reviewerUserId ?? null,
    }} />
    <Evidence title="Model bindings and declared versions" value={version.modelBindings} />
    <Evidence title="Tool contracts, requirements and exclusions" value={version.profiles} />
    <Evidence title="Skills, dependencies and source evidence" value={{
      skillMode: version.skillMode, dependencies: version.dependencies,
      sourceDigest: version.sourceDigest, policyDigest: version.policyDigest,
    }} />
    {version.skillMode === "excluded" && <p style={hint}>Explicit no-skill profile: skill loading is excluded, not silently substituted after a discovery failure.</p>}
    {reviewDecision === null ? <>
      <label htmlFor={`${id}-note`} style={labelStyle}>Review note (optional, up to 1000 characters)</label>
      <textarea id={`${id}-note`} style={inputStyle} rows={3} maxLength={1000} value={note} disabled={write.busy} onChange={(event) => setNote(event.target.value)} />
      <p style={hint}>Decide only after inspecting the submitted source and its material contracts. Approval is not activation or execution consent.</p>
    </> : <p role="status" style={hint}>
      This version was {reviewDecision} by <code>{reviewerId}</code>. The decision is immutable; another decision requires a new submitted version.
    </p>}
    <WriteNotice state={write.state} />
    <div style={actions}>
      {reviewDecision === null && (["approved", "rejected"] as const).map((decision) => <button key={decision} type="button"
        style={decision === "approved" ? primaryBtn : secondaryBtn} disabled={write.busy || write.stale}
        onClick={() => {
          if (write.stale) return;
          void write.run((signal) => publishing.decidePublicationReview({
            source, expectedHeadRevision: headRevision, decision, note,
          }, signal), "Review recorded.");
        }}>{decision === "approved" ? "Approve reviewed version" : "Reject version"}</button>)}
      <button type="button" style={secondaryBtn} disabled={write.busy} onClick={refresh}>Reload review snapshot</button>
    </div>
  </section>;
}

function PublicationCatalog({ kind }: { kind: publishing.PublicationKind }) {
  const load = useCallback((signal: AbortSignal) => publishing.listPublications(kind, signal), [kind]);
  const catalog = useRead(load);
  return <div style={stack}>
    <ReadNotice state={catalog.state} label="publication catalog" refresh={catalog.refresh} />
    {catalog.state.phase === "ready" && <>
      <p style={hint}>Available to you now. Use these handles in existing {kind === "agent" ? "agent mentions" : "workflow pickers"}.
        Availability does not replace execution authorization; source is not copied into your private drafts.</p>
      {catalog.state.value.items.length === 0 ? <p style={hint}>No published {kind === "agent" ? "agents" : "workflows"} available to you.</p> :
        <ul style={{ margin: 0, paddingLeft: 20 }}>
          {catalog.state.value.items.map((item) => <li key={JSON.stringify(item.source)} style={{ marginBottom: 12, overflowWrap: "anywhere" }}>
            <strong>{item.displayName}</strong>{" "}<code>{kind === "agent" ? "@" : ""}{item.handle}</code>
            <p style={hint}>{item.description}</p>
            <p style={hint}>{publishing.publicationVisibilityLabel(item.visibility)}. Version {item.source.version}.
              Modes: {item.modes.join(", ")}. Models: {item.modelIds.join(", ")}.
              {item.skillMode === "excluded" ? " Explicit no-skill profile." : " Versioned skills."}</p>
          </li>)}
        </ul>}
      {catalog.state.value.truncated && <p style={hint}>Partial catalog: the server limit was reached.</p>}
      <button type="button" style={secondaryBtn} onClick={catalog.refresh}>Refresh publication catalog</button>
    </>}
  </div>;
}
