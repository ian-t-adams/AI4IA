"use client";

// Photo avatar gallery (Phase 1): create a fictional avatar from a text
// description, watch its status, preview it, report a problem and delete it.
// ChatApp offers it only while GET /api/photo-avatars/config reports the
// feature enabled. That is display posture, never enforcement: every route
// re-checks the gate, policy, limits, cost and attestation on the server.
import {
  Fragment,
  useEffect,
  useEffectEvent,
  useId,
  useRef,
  useState,
  type CSSProperties,
  type FormEvent,
  type KeyboardEvent,
} from "react";

import {
  PHOTO_AVATAR_ATTRIBUTE_KEYS,
  PHOTO_AVATAR_ATTRIBUTE_LABELS,
  PHOTO_AVATAR_POLL_BUDGET_MS,
  PhotoAvatarApiError,
  attestationRecognized,
  createPhotoAvatar,
  deletePhotoAvatar,
  formatPhotoAvatarPrice,
  formatPhotoAvatarTime,
  getPhotoAvatar,
  getPhotoAvatarConfig,
  isPendingPhotoAvatar,
  listPhotoAvatars,
  newerPhotoAvatar,
  photoAvatarAttributeLabel,
  photoAvatarErrorMessage,
  photoAvatarPollDelay,
  photoAvatarReportReasonLabel,
  photoAvatarStatusText,
  photoAvatarUnavailableText,
  reportPhotoAvatar,
  safeReportUrl,
  validatePhotoAvatarDraft,
  type PhotoAvatar,
  type PhotoAvatarAttributeKey,
  type PhotoAvatarConfig,
  type PhotoAvatarDraftIssue,
  type PhotoAvatarFeedback,
  type PhotoAvatarLimits,
  type PhotoAvatarReportReason,
} from "@/lib/photoAvatars";
import { primaryBtn, secondaryBtn } from "./builderStyles";
import { ModalShell } from "./ModalShell";
import { PhotoAvatarPreview } from "./PhotoAvatarPreview";

const compactBtn: CSSProperties = {
  ...secondaryBtn,
  minHeight: 36,
  padding: "6px 12px",
  fontSize: "0.85rem",
};
const dangerBtn: CSSProperties = {
  ...compactBtn,
  border: "1px solid var(--danger)",
  background: "var(--danger)",
  color: "var(--danger-fg)",
};

type ListState =
  | { phase: "loading" }
  | { phase: "ready" }
  | { phase: "error"; message: string };

type ItemNotice = { message: string; tone: "warn" | "danger"; blockedUntil: number | null };

function omit<T>(record: Record<string, T>, key: string): Record<string, T> {
  if (!(key in record)) return record;
  const next = { ...record };
  delete next[key];
  return next;
}

function joinList(parts: string[]): string {
  if (parts.length <= 1) return parts.join("");
  return `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}

function usageText(limits: PhotoAvatarLimits | null): string | null {
  if (!limits) return null;
  return `${limits.avatarCount} of ${limits.maxAvatars} avatars · ${limits.creationsInLastDay} of ${limits.maxCreationsPerDay} creations in the last 24 hours`;
}

/** Why "New avatar" is unavailable, or null when a create may be attempted. */
function creationBlockedReason(
  config: PhotoAvatarConfig | null,
  configError: string | null,
): string | null {
  if (!config) return configError ? "Creation settings couldn't be loaded." : null;
  if (!config.enabled || !config.available) return photoAvatarUnavailableText(config.reason);
  const limits = config.limits;
  if (limits && limits.avatarCount >= limits.maxAvatars) {
    return `You have ${limits.avatarCount} of ${limits.maxAvatars} avatars, the most you can keep. Delete one to create another.`;
  }
  if (limits && limits.creationsInLastDay >= limits.maxCreationsPerDay) {
    const next = formatPhotoAvatarTime(limits.nextCreationAt);
    return `You've used ${limits.creationsInLastDay} of ${limits.maxCreationsPerDay} creations in the last 24 hours.${next ? ` You can create another after ${next}.` : ""}`;
  }
  if (!config.canCreate) return "Creating avatars is unavailable right now.";
  if (!limits || !config.attributes || !config.attestation) {
    return "Creation settings are incomplete. Close and reopen photo avatars to try again.";
  }
  return null;
}

function draftHint(issues: PhotoAvatarDraftIssue[], limits: PhotoAvatarLimits | null): string | null {
  if (issues.includes("config")) {
    return "Creation settings are unavailable. Close and reopen photo avatars to try again.";
  }
  if (issues.includes("attestationUnrecognized")) {
    return "This version of AI4IA doesn't recognize the current confirmation statements. Reload the page to create an avatar.";
  }
  const parts: string[] = [];
  if (issues.includes("displayName")) parts.push("add a name");
  if (issues.includes("displayNameTooLong")) {
    parts.push(`shorten the name to ${limits?.displayNameMaxChars ?? 60} characters`);
  }
  if (issues.includes("prompt")) parts.push("add a description");
  if (issues.includes("promptTooLong")) {
    parts.push(`shorten the description to ${limits?.promptMaxChars.toLocaleString() ?? "the limit"} characters`);
  }
  if (issues.includes("attribute")) parts.push("choose the optional details again");
  if (issues.includes("attestation")) parts.push("confirm each statement");
  return parts.length ? `To create an avatar, ${joinList(parts)}.` : null;
}

/**
 * Polls one pending record with backoff until it is ready or failed, for at
 * most PHOTO_AVATAR_POLL_BUDGET_MS of wall time. It never polls while the tab
 * is hidden, and stops as soon as the item unmounts (the panel closed or the
 * avatar was removed).
 */
function usePhotoAvatarPolling(
  avatar: PhotoAvatar,
  onRecord: (record: PhotoAvatar) => void,
  onGone: () => void,
): { stalled: boolean; resume: () => void } {
  const pending = isPendingPhotoAvatar(avatar);
  const [session, setSession] = useState(0);
  const [stalledSession, setStalledSession] = useState<number | null>(null);
  const receive = useEffectEvent(onRecord);
  const gone = useEffectEvent(onGone);
  const id = avatar.id;

  useEffect(() => {
    if (!pending) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | null = null;
    let attempt = 0;
    let waitingForVisible = false;
    const startedAt = Date.now();

    const schedule = () => {
      const remaining = PHOTO_AVATAR_POLL_BUDGET_MS - (Date.now() - startedAt);
      const delay = Math.max(0, Math.min(photoAvatarPollDelay(attempt), remaining));
      attempt += 1;
      timer = setTimeout(() => void tick(), delay);
    };
    const tick = async () => {
      if (cancelled) return;
      if (document.visibilityState === "hidden") {
        waitingForVisible = true;
        return;
      }
      controller = new AbortController();
      try {
        const record = await getPhotoAvatar(id, controller.signal);
        if (cancelled) return;
        receive(record);
        if (!isPendingPhotoAvatar(record)) return;
      } catch (error) {
        if (cancelled) return;
        if (error instanceof PhotoAvatarApiError && error.status === 404) {
          gone();
          return;
        }
        // Anything else is transient: keep backing off within the budget.
      }
      if (Date.now() - startedAt >= PHOTO_AVATAR_POLL_BUDGET_MS) {
        setStalledSession(session);
        return;
      }
      schedule();
    };
    const onVisibility = () => {
      if (cancelled || !waitingForVisible || document.visibilityState === "hidden") return;
      waitingForVisible = false;
      void tick();
    };

    document.addEventListener("visibilitychange", onVisibility);
    schedule();
    return () => {
      cancelled = true;
      clearTimeout(timer);
      controller?.abort();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [id, pending, session]);

  return {
    stalled: pending && stalledSession === session,
    resume: () => setSession((value) => value + 1),
  };
}

function statusTone(avatar: PhotoAvatar): { tone: string; glyph: string | null } {
  if (isPendingPhotoAvatar(avatar)) return { tone: "info", glyph: null };
  switch (avatar.status) {
    case "ready":
      return { tone: "success", glyph: "✓" };
    case "failed":
      return { tone: "danger", glyph: "✕" };
    case "deleting":
      return { tone: "warn", glyph: "!" };
    default:
      return { tone: "muted", glyph: "?" };
  }
}

function AvatarItem({
  avatar,
  notice,
  disclosureText,
  reportable,
  focusOnMount,
  onRecord,
  onGone,
  onAnnounce,
  onDelete,
  onReport,
}: {
  avatar: PhotoAvatar;
  notice: ItemNotice | undefined;
  disclosureText: string | null;
  reportable: boolean;
  focusOnMount: boolean;
  onRecord: (record: PhotoAvatar) => void;
  onGone: (avatar: PhotoAvatar) => void;
  onAnnounce: (message: string) => void;
  onDelete: (avatar: PhotoAvatar) => void;
  onReport: (avatar: PhotoAvatar) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const deleteRef = useRef<HTMLButtonElement>(null);
  const keepRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef(false);
  const { stalled, resume } = usePhotoAvatarPolling(
    avatar,
    (record) => {
      if (record.status !== avatar.status) {
        if (record.status === "ready") onAnnounce(`${record.displayName} is ready.`);
        if (record.status === "failed") {
          onAnnounce(`Couldn't create ${record.displayName}. ${record.failure?.message ?? ""}`.trim());
        }
      }
      onRecord(record);
    },
    () => onGone(avatar),
  );

  useEffect(() => {
    if (focusOnMount) headingRef.current?.focus();
  }, [focusOnMount]);
  useEffect(() => {
    if (confirming) {
      keepRef.current?.focus();
    } else if (returnFocusRef.current) {
      returnFocusRef.current = false;
      deleteRef.current?.focus();
    }
  }, [confirming]);

  const keep = () => {
    returnFocusRef.current = true;
    setConfirming(false);
  };
  const onConfirmKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Escape") return;
    // Escape backs out of the confirmation without closing the whole gallery.
    event.preventDefault();
    event.stopPropagation();
    keep();
  };

  const pending = isPendingPhotoAvatar(avatar);
  const { tone, glyph } = statusTone(avatar);
  const ready = avatar.status === "ready";
  const blocked = notice?.blockedUntil != null;
  const price = formatPhotoAvatarPrice(avatar.cost?.estimatedUsd, avatar.cost?.currency, avatar.cost?.known === true);
  const created = formatPhotoAvatarTime(avatar.createdAt);
  const attributes = PHOTO_AVATAR_ATTRIBUTE_KEYS.flatMap((key) => {
    const value = avatar.attributes?.[key];
    return value ? [{ key, value }] : [];
  });

  return (
    <li className="photo-avatar-item" aria-busy={pending || undefined}>
      <div className="photo-avatar-frame" data-status={avatar.status}>
        {ready ? (
          <PhotoAvatarPreview avatar={avatar} />
        ) : (
          <span className="photo-avatar-silhouette" aria-hidden="true" />
        )}
      </div>
      <h4 ref={headingRef} tabIndex={-1} className="photo-avatar-name">
        {avatar.displayName}
      </h4>
      <p className="photo-avatar-status" data-tone={tone}>
        {glyph ? (
          <span aria-hidden="true" className="photo-avatar-status-glyph">
            {glyph}
          </span>
        ) : (
          <span aria-hidden="true" className="activity-spinner" />
        )}
        <span>{photoAvatarStatusText(avatar.status)}</span>
      </p>
      {avatar.status === "failed" && avatar.failure?.message ? (
        <p className="photo-avatar-note">{avatar.failure.message}</p>
      ) : null}
      {avatar.status === "deleting" ? (
        <p className="photo-avatar-note">Delete it again to finish removing it.</p>
      ) : null}
      {stalled ? (
        <p className="photo-avatar-note">
          Still working on it. Automatic checks have stopped.{" "}
          <button
            type="button"
            style={compactBtn}
            onClick={resume}
            aria-label={`Check the status of ${avatar.displayName}`}
          >
            Check status
          </button>
        </p>
      ) : null}
      {notice ? (
        <p role="alert" className="photo-avatar-note" data-tone={notice.tone}>
          {notice.message}
        </p>
      ) : null}
      {ready && !avatar.usable ? (
        <p className="photo-avatar-note">Not available to use right now.</p>
      ) : null}
      {avatar.reported ? <p className="photo-avatar-note">You reported this avatar.</p> : null}
      <details className="photo-avatar-details">
        <summary>Details</summary>
        <dl>
          <dt>Description</dt>
          <dd>{avatar.prompt}</dd>
          {attributes.map(({ key, value }) => (
            <Fragment key={key}>
              <dt>{PHOTO_AVATAR_ATTRIBUTE_LABELS[key]}</dt>
              <dd>{photoAvatarAttributeLabel(value)}</dd>
            </Fragment>
          ))}
          <dt>Created</dt>
          <dd>{created ? <time dateTime={avatar.createdAt}>{created}</time> : "Unknown"}</dd>
          <dt>Estimated cost</dt>
          <dd>{price ?? "Unknown"}</dd>
        </dl>
        {ready && disclosureText ? <p>{disclosureText}</p> : null}
      </details>
      {confirming ? (
        <div
          className="photo-avatar-actions"
          role="group"
          aria-label={`Confirm deleting ${avatar.displayName}`}
          onKeyDown={onConfirmKeyDown}
        >
          <span className="photo-avatar-confirm-text">Delete permanently?</span>
          <button
            type="button"
            style={dangerBtn}
            onClick={() => {
              setConfirming(false);
              onDelete(avatar);
            }}
            aria-label={`Delete ${avatar.displayName} permanently`}
          >
            Delete
          </button>
          <button ref={keepRef} type="button" style={compactBtn} onClick={keep}>
            Keep
          </button>
        </div>
      ) : (
        <div className="photo-avatar-actions">
          {ready && reportable ? (
            <button
              type="button"
              style={compactBtn}
              onClick={() => onReport(avatar)}
              aria-label={`Report a problem with ${avatar.displayName}`}
            >
              Report
            </button>
          ) : null}
          <button
            ref={deleteRef}
            type="button"
            style={compactBtn}
            disabled={blocked}
            onClick={() => setConfirming(true)}
            aria-label={`Delete ${avatar.displayName}`}
          >
            Delete
          </button>
        </div>
      )}
    </li>
  );
}

function CreateAvatarForm({
  formId,
  config,
  open,
  focusRequest,
  onCreated,
  onCancel,
  onRefreshConfig,
  onOutcomeUncertain,
}: {
  formId: string;
  config: PhotoAvatarConfig;
  open: boolean;
  focusRequest: number;
  onCreated: (record: PhotoAvatar) => void;
  onCancel: () => void;
  onRefreshConfig: () => void;
  onOutcomeUncertain: () => void;
}) {
  const [displayName, setDisplayName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [attributes, setAttributes] = useState<Partial<Record<PhotoAvatarAttributeKey, string>>>({});
  const [attested, setAttested] = useState<Record<string, boolean>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const mountedRef = useRef(false);
  const baseId = useId();
  const ids = {
    title: `${baseId}-title`,
    name: `${baseId}-name`,
    nameHint: `${baseId}-name-hint`,
    prompt: `${baseId}-prompt`,
    promptHint: `${baseId}-prompt-hint`,
    counter: `${baseId}-counter`,
    submitHint: `${baseId}-submit-hint`,
  };

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);
  useEffect(() => {
    if (focusRequest > 0 && open) nameRef.current?.focus();
  }, [focusRequest, open]);

  const limits = config.limits;
  const options = config.attributes;
  const attestation = config.attestation;
  const { request, issues } = validatePhotoAvatarDraft(config, {
    displayName,
    prompt,
    attributes,
    attested,
  });
  const canSubmit = config.canCreate && request !== null && !submitting;
  const promptLength = prompt.trim().length;
  const promptMax = limits?.promptMaxChars ?? 0;
  const promptOver = promptLength > promptMax ? promptLength - promptMax : 0;
  const hint = submitting ? null : draftHint(issues, limits);
  const pricing = config.pricing;
  const price = formatPhotoAvatarPrice(pricing?.estimatedUsdPerAvatar, pricing?.currency, pricing?.known === true);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!request || !config.canCreate || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const record = await createPhotoAvatar(request);
      if (mountedRef.current) onCreated(record);
    } catch (reason) {
      if (!mountedRef.current) return;
      if (reason instanceof PhotoAvatarApiError) {
        setError(photoAvatarErrorMessage(reason));
        if (reason.code === "attestation_outdated") setAttested({});
        // Limits, availability or the attestation may have moved under us.
        onRefreshConfig();
      } else {
        // The request may or may not have reached the server. Never repeat it;
        // re-read the gallery so an avatar that was created shows up.
        setError(
          "The request didn't complete, so it isn't known whether the avatar was created. The gallery is refreshing to check before you try again.",
        );
        onOutcomeUncertain();
      }
    } finally {
      if (mountedRef.current) setSubmitting(false);
    }
  };

  return (
    <form
      id={formId}
      hidden={!open}
      className="photo-avatar-form"
      aria-labelledby={ids.title}
      onSubmit={(event) => void submit(event)}
      noValidate
    >
      <h4 id={ids.title}>New avatar</h4>
      <div className="photo-avatar-field">
        <label htmlFor={ids.name}>Name</label>
        <input
          id={ids.name}
          ref={nameRef}
          value={displayName}
          maxLength={limits?.displayNameMaxChars}
          autoComplete="off"
          disabled={submitting}
          aria-describedby={ids.nameHint}
          onChange={(event) => setDisplayName(event.target.value)}
        />
        <span id={ids.nameHint} className="photo-avatar-hint">
          Shown only in AI4IA. Up to {limits?.displayNameMaxChars ?? 60} characters.
        </span>
      </div>
      <div className="photo-avatar-field">
        <label htmlFor={ids.prompt}>Description</label>
        <textarea
          id={ids.prompt}
          rows={4}
          value={prompt}
          disabled={submitting}
          aria-invalid={promptOver > 0 || undefined}
          aria-describedby={`${ids.promptHint} ${ids.counter}`}
          onChange={(event) => setPrompt(event.target.value)}
        />
        <span id={ids.promptHint} className="photo-avatar-hint">
          Describe a fictional adult: appearance, clothing and setting. A head-and-shoulders
          portrait facing the camera on a plain background works best.
        </span>
        <span id={ids.counter} className="photo-avatar-counter" data-over={promptOver > 0 || undefined}>
          {promptLength.toLocaleString()} / {promptMax.toLocaleString()} characters
          {promptOver > 0 ? ` · ${promptOver.toLocaleString()} over the limit` : ""}
        </span>
      </div>
      <fieldset className="photo-avatar-fieldset" disabled={submitting}>
        <legend>Optional details</legend>
        <p className="photo-avatar-hint">Leave any of these unspecified to let the description decide.</p>
        <div className="photo-avatar-attributes">
          {PHOTO_AVATAR_ATTRIBUTE_KEYS.map((key) => (
            <div className="photo-avatar-field" key={key}>
              <label htmlFor={`${baseId}-${key}`}>{PHOTO_AVATAR_ATTRIBUTE_LABELS[key]}</label>
              <select
                id={`${baseId}-${key}`}
                value={attributes[key] ?? ""}
                onChange={(event) =>
                  setAttributes((current) => ({ ...current, [key]: event.target.value }))
                }
              >
                <option value="">Unspecified</option>
                {(options?.[key] ?? []).map((value) => (
                  <option key={value} value={value}>
                    {photoAvatarAttributeLabel(value)}
                  </option>
                ))}
              </select>
            </div>
          ))}
        </div>
      </fieldset>
      <fieldset className="photo-avatar-fieldset" disabled={submitting}>
        <legend>Confirm the character</legend>
        <p className="photo-avatar-hint">Required for every avatar you create.</p>
        {attestationRecognized(attestation)
          ? attestation.statements.map((statement) => (
              <label key={statement.id} className="photo-avatar-check">
                <input
                  type="checkbox"
                  checked={attested[statement.id] === true}
                  onChange={(event) =>
                    setAttested((current) => ({ ...current, [statement.id]: event.target.checked }))
                  }
                />
                <span>{statement.text}</span>
              </label>
            ))
          : null}
      </fieldset>
      <p className="photo-avatar-cost">
        Each creation is billed.{" "}
        {price
          ? `Estimated cost: ${price} per avatar${pricing?.priceVersion ? ` (price list ${pricing.priceVersion})` : ""}.`
          : "The cost per avatar is unknown in this deployment."}
      </p>
      {error ? (
        <p role="alert" className="studio-alert">
          {error}
        </p>
      ) : null}
      <div className="photo-avatar-form-actions">
        <button
          type="submit"
          style={primaryBtn}
          disabled={!canSubmit}
          aria-describedby={hint ? ids.submitHint : undefined}
        >
          {submitting ? "Creating…" : "Create avatar"}
        </button>
        <button type="button" style={secondaryBtn} disabled={submitting} onClick={onCancel}>
          Cancel
        </button>
      </div>
      {hint ? (
        <p id={ids.submitHint} className="photo-avatar-hint">
          {hint}
        </p>
      ) : null}
    </form>
  );
}

function ReportDialog({
  avatar,
  feedback,
  onClose,
  onReported,
}: {
  avatar: PhotoAvatar;
  feedback: PhotoAvatarFeedback;
  onClose: () => void;
  onReported: (avatar: PhotoAvatar) => void;
}) {
  const [reason, setReason] = useState<PhotoAvatarReportReason | "">("");
  const [details, setDetails] = useState("");
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const doneRef = useRef<HTMLButtonElement>(null);
  const mountedRef = useRef(false);
  const baseId = useId();
  const reportUrl = safeReportUrl(feedback.microsoftReportUrl);
  const detailsLength = details.trim().length;
  const detailsOver = detailsLength > feedback.detailsMaxChars;
  const canSend = reason !== "" && !detailsOver && !sending;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);
  useEffect(() => {
    if (sent) doneRef.current?.focus();
  }, [sent]);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSend) return;
    setSending(true);
    setError(null);
    try {
      const trimmed = details.trim();
      await reportPhotoAvatar(avatar.id, trimmed ? { reason, details: trimmed } : { reason });
      if (!mountedRef.current) return;
      setSent(true);
      onReported(avatar);
    } catch (reasonError) {
      if (mountedRef.current) setError(photoAvatarErrorMessage(reasonError));
    } finally {
      if (mountedRef.current) setSending(false);
    }
  };

  const microsoftLink = reportUrl ? (
    <a href={reportUrl} target="_blank" rel="noopener noreferrer">
      report it to Microsoft (opens in new tab)
    </a>
  ) : null;

  return (
    <ModalShell
      ariaLabel={`Report a problem with ${avatar.displayName}`}
      title="Report a problem"
      filename={avatar.displayName}
      closeLabel="Close report"
      onClose={onClose}
      width="min(480px, 94vw)"
      zIndex={60}
    >
      {sent ? (
        <div className="photo-avatar-report">
          <p role="status">Thanks. Your report was recorded with this avatar.</p>
          {microsoftLink ? <p>You can also {microsoftLink}.</p> : null}
          <div className="photo-avatar-form-actions">
            <button ref={doneRef} type="button" style={primaryBtn} onClick={onClose}>
              Done
            </button>
          </div>
        </div>
      ) : (
        <form className="photo-avatar-report" onSubmit={(event) => void submit(event)} noValidate>
          <fieldset className="photo-avatar-fieldset" disabled={sending}>
            <legend>What&apos;s wrong with this avatar?</legend>
            {feedback.reasons.map((value) => (
              <label key={value} className="photo-avatar-check">
                <input
                  type="radio"
                  name={`${baseId}-reason`}
                  value={value}
                  checked={reason === value}
                  onChange={() => setReason(value)}
                />
                <span>{photoAvatarReportReasonLabel(value)}</span>
              </label>
            ))}
          </fieldset>
          <div className="photo-avatar-field">
            <label htmlFor={`${baseId}-details`}>Details (optional)</label>
            <textarea
              id={`${baseId}-details`}
              rows={3}
              value={details}
              disabled={sending}
              aria-invalid={detailsOver || undefined}
              aria-describedby={`${baseId}-details-counter`}
              onChange={(event) => setDetails(event.target.value)}
            />
            <span id={`${baseId}-details-counter`} className="photo-avatar-counter" data-over={detailsOver || undefined}>
              {detailsLength.toLocaleString()} / {feedback.detailsMaxChars.toLocaleString()} characters
            </span>
          </div>
          <p className="photo-avatar-hint">
            Your report is recorded with this avatar.
            {microsoftLink ? <> To report abuse directly, you can also {microsoftLink}.</> : null}
          </p>
          {error ? (
            <p role="alert" className="studio-alert">
              {error}
            </p>
          ) : null}
          <div className="photo-avatar-form-actions">
            <button type="submit" style={primaryBtn} disabled={!canSend}>
              {sending ? "Sending…" : "Send report"}
            </button>
            <button type="button" style={secondaryBtn} disabled={sending} onClick={onClose}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </ModalShell>
  );
}

export function PhotoAvatarsPanel({ onClose }: { onClose: () => void }) {
  const [config, setConfig] = useState<PhotoAvatarConfig | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [avatars, setAvatars] = useState<PhotoAvatar[]>([]);
  const [list, setList] = useState<ListState>({ phase: "loading" });
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [formOpen, setFormOpen] = useState(false);
  const [formKey, setFormKey] = useState(0);
  const [formFocusRequest, setFormFocusRequest] = useState(0);
  const [announcement, setAnnouncement] = useState("");
  const [notices, setNotices] = useState<Record<string, ItemNotice>>({});
  const [reporting, setReporting] = useState<PhotoAvatar | null>(null);
  const [focusId, setFocusId] = useState<string | null>(null);
  const mountedRef = useRef(false);
  const configRequestRef = useRef(0);
  const autoOpenedRef = useRef(false);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const noticeTimersRef = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const baseId = useId();
  const headingId = `${baseId}-heading`;
  const formId = `${baseId}-form`;
  const blockedId = `${baseId}-blocked`;

  useEffect(() => {
    mountedRef.current = true;
    const timers = noticeTimersRef.current;
    return () => {
      mountedRef.current = false;
      for (const timer of timers.values()) clearTimeout(timer);
      timers.clear();
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const request = ++configRequestRef.current;
    void (async () => {
      const [configResult, listResult] = await Promise.allSettled([
        getPhotoAvatarConfig(controller.signal),
        listPhotoAvatars(controller.signal),
      ]);
      if (controller.signal.aborted) return;
      if (request === configRequestRef.current) {
        if (configResult.status === "fulfilled") {
          setConfig(configResult.value);
          setConfigError(null);
        } else {
          setConfigError(photoAvatarErrorMessage(configResult.reason));
        }
      }
      if (configResult.status === "fulfilled" && !configResult.value.enabled) {
        setAvatars([]);
        setList({ phase: "ready" });
        return;
      }
      if (listResult.status === "rejected") {
        setList({ phase: "error", message: photoAvatarErrorMessage(listResult.reason) });
        return;
      }
      setAvatars(listResult.value);
      setList({ phase: "ready" });
      // An empty gallery opens straight onto the form once, to teach the task.
      if (
        !autoOpenedRef.current &&
        listResult.value.length === 0 &&
        configResult.status === "fulfilled" &&
        configResult.value.canCreate
      ) {
        autoOpenedRef.current = true;
        setFormOpen(true);
      }
    })();
    return () => controller.abort();
  }, [loadAttempt]);

  const reload = () => {
    setList({ phase: "loading" });
    setLoadAttempt((value) => value + 1);
  };

  const refreshConfig = async () => {
    const request = ++configRequestRef.current;
    try {
      const next = await getPhotoAvatarConfig();
      if (mountedRef.current && request === configRequestRef.current) {
        setConfig(next);
        setConfigError(null);
      }
    } catch {
      // Keep the last known limits; the server re-checks them on every create.
    }
  };

  const clearNotice = (id: string) => {
    const timers = noticeTimersRef.current;
    clearTimeout(timers.get(id));
    timers.delete(id);
    setNotices((current) => omit(current, id));
  };

  const showNotice = (id: string, message: string, blockedUntil: number | null) => {
    const timers = noticeTimersRef.current;
    clearTimeout(timers.get(id));
    timers.delete(id);
    // A wait the server asked for is not a failure; everything else is.
    const tone = blockedUntil !== null ? "warn" : "danger";
    setNotices((current) => ({ ...current, [id]: { message, tone, blockedUntil } }));
    if (blockedUntil !== null) {
      timers.set(
        id,
        setTimeout(() => {
          timers.delete(id);
          if (mountedRef.current) setNotices((current) => omit(current, id));
        }, Math.max(0, blockedUntil - Date.now())),
      );
    }
  };

  const receive = (record: PhotoAvatar) => {
    setAvatars((current) => {
      const index = current.findIndex((item) => item.id === record.id);
      if (index < 0) return current;
      const next = newerPhotoAvatar(current[index], record);
      if (next === current[index]) return current;
      const copy = current.slice();
      copy[index] = next;
      return copy;
    });
  };

  const forget = (avatar: PhotoAvatar) => {
    setAvatars((current) => current.filter((item) => item.id !== avatar.id));
    clearNotice(avatar.id);
    setAnnouncement(`${avatar.displayName} no longer exists.`);
    void refreshConfig();
  };

  const remove = async (avatar: PhotoAvatar) => {
    const index = avatars.findIndex((item) => item.id === avatar.id);
    clearNotice(avatar.id);
    // Optimistic: the item leaves at once and comes back if the server refuses.
    setAvatars((current) => current.filter((item) => item.id !== avatar.id));
    headingRef.current?.focus();
    try {
      await deletePhotoAvatar(avatar.id);
      if (!mountedRef.current) return;
      setAnnouncement(`Deleted ${avatar.displayName}.`);
      void refreshConfig();
    } catch (error) {
      if (!mountedRef.current) return;
      const unfinished =
        error instanceof PhotoAvatarApiError && error.code === "provider_delete_failed";
      const restored: PhotoAvatar = unfinished ? { ...avatar, status: "deleting" } : avatar;
      setAvatars((current) => {
        if (current.some((item) => item.id === avatar.id)) return current;
        const copy = current.slice();
        copy.splice(Math.min(Math.max(index, 0), copy.length), 0, restored);
        return copy;
      });
      const blockedUntil =
        error instanceof PhotoAvatarApiError &&
        error.code === "avatar_confirming" &&
        error.retryAfterSeconds !== null
          ? Date.now() + error.retryAfterSeconds * 1000
          : null;
      showNotice(avatar.id, photoAvatarErrorMessage(error), blockedUntil);
    }
  };

  const created = (record: PhotoAvatar) => {
    setAvatars((current) => [record, ...current.filter((item) => item.id !== record.id)]);
    setFormOpen(false);
    setFormKey((value) => value + 1);
    setFocusId(record.id);
    setAnnouncement(
      record.status === "failed"
        ? `Couldn't create ${record.displayName}. ${record.failure?.message ?? ""}`.trim()
        : `Creating ${record.displayName}. This can take a minute; you can keep working.`,
    );
    void refreshConfig();
  };

  const reported = (avatar: PhotoAvatar) => {
    setAvatars((current) =>
      current.map((item) => (item.id === avatar.id ? { ...item, reported: true } : item)),
    );
    setAnnouncement(`Report sent for ${avatar.displayName}.`);
  };

  const closeForm = () => {
    setFormOpen(false);
    toggleRef.current?.focus();
  };
  const toggleForm = () => {
    if (formOpen) {
      closeForm();
      return;
    }
    setFormOpen(true);
    setFormFocusRequest((value) => value + 1);
  };

  const disabled = config !== null && !config.enabled;
  const unavailable = config !== null && config.enabled && !config.available;
  const blockedReason = creationBlockedReason(config, configError);
  const canOpenForm = config !== null && list.phase === "ready" && blockedReason === null;
  const usage = usageText(config?.limits ?? null);
  const label = config?.disclosure?.label?.trim() || "AI-generated";

  return (
    <>
      <ModalShell
        ariaLabel="Photo avatars"
        title="Photo avatars"
        closeLabel="Close photo avatars"
        onClose={onClose}
        width="min(760px, 94vw)"
        zIndex={50}
        headingFontSize="1.2em"
        headerGap={0}
      >
        <div className="photo-avatar-panel">
          <p className="photo-avatar-intro">
            Describe a fictional adult and AI generates a portrait of them. Every preview stays
            labelled <strong>{label}</strong>.
          </p>
          <p className="visually-hidden" role="status" aria-live="polite" aria-atomic="true">
            {announcement}
          </p>
          {disabled ? (
            <p className="photo-avatar-notice" role="status">
              {photoAvatarUnavailableText("disabled")}
            </p>
          ) : (
            <>
              {unavailable ? (
                <div className="photo-avatar-notice" role="status">
                  <strong>
                    <span aria-hidden="true">⚠ </span>
                    Creating avatars is unavailable
                  </strong>
                  <p id={blockedId}>{blockedReason}</p>
                  <p>Avatars you already made stay listed below.</p>
                </div>
              ) : null}
              <section className="photo-avatar-gallery" aria-labelledby={headingId}>
                <div className="photo-avatar-toolbar">
                  <div>
                    <h3 id={headingId} ref={headingRef} tabIndex={-1}>
                      Your avatars
                    </h3>
                    {usage ? <p className="photo-avatar-meta">{usage}</p> : null}
                  </div>
                  {list.phase === "ready" ? (
                    <button
                      ref={toggleRef}
                      type="button"
                      style={formOpen ? secondaryBtn : primaryBtn}
                      aria-expanded={formOpen}
                      aria-controls={config ? formId : undefined}
                      aria-describedby={blockedReason ? blockedId : undefined}
                      disabled={!formOpen && !canOpenForm}
                      onClick={toggleForm}
                    >
                      New avatar
                    </button>
                  ) : null}
                </div>
                {blockedReason && !unavailable && list.phase === "ready" ? (
                  <p id={blockedId} className="photo-avatar-blocked">
                    {blockedReason}
                    {configError && !config ? (
                      <>
                        {" "}
                        <button type="button" style={compactBtn} onClick={reload}>
                          Try again
                        </button>
                      </>
                    ) : null}
                  </p>
                ) : null}
                {config ? (
                  <CreateAvatarForm
                    key={formKey}
                    formId={formId}
                    config={config}
                    open={formOpen}
                    focusRequest={formFocusRequest}
                    onCreated={created}
                    onCancel={closeForm}
                    onRefreshConfig={() => void refreshConfig()}
                    onOutcomeUncertain={reload}
                  />
                ) : null}
                {list.phase === "loading" ? (
                  <p role="status" className="photo-avatar-meta">
                    Loading your avatars…
                  </p>
                ) : list.phase === "error" ? (
                  <div role="alert" className="studio-alert photo-avatar-load-error">
                    <span>Your avatars couldn&apos;t be loaded. {list.message}</span>
                    <button type="button" style={compactBtn} onClick={reload}>
                      Try again
                    </button>
                  </div>
                ) : avatars.length === 0 ? (
                  <p className="photo-avatar-empty">
                    No avatars yet. An avatar you create appears here with its status while it
                    generates.
                  </p>
                ) : (
                  <ul className="photo-avatar-grid" aria-labelledby={headingId}>
                    {avatars.map((avatar) => (
                      <AvatarItem
                        key={avatar.id}
                        avatar={avatar}
                        notice={notices[avatar.id]}
                        disclosureText={config?.disclosure?.text ?? null}
                        reportable={Boolean(config?.feedback)}
                        focusOnMount={focusId === avatar.id}
                        onRecord={receive}
                        onGone={forget}
                        onAnnounce={setAnnouncement}
                        onDelete={(item) => void remove(item)}
                        onReport={setReporting}
                      />
                    ))}
                  </ul>
                )}
              </section>
            </>
          )}
        </div>
      </ModalShell>
      {reporting && config?.feedback ? (
        <ReportDialog
          key={reporting.id}
          avatar={reporting}
          feedback={config.feedback}
          onClose={() => setReporting(null)}
          onReported={reported}
        />
      ) : null}
    </>
  );
}
