"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";

import * as api from "@/lib/api";
import type {
  ImageEditRegion,
  ImageEditSource,
  ImageModelOption,
  ImageOptionsResponse,
  Message,
} from "@/lib/types";

import { ModalShell } from "./ModalShell";

// Edges in percent of the rendered image. Keyboard users set them with the
// sliders; pointer users can also drag a rectangle over the preview.
export interface RegionPercent {
  left: number;
  top: number;
  width: number;
  height: number;
}

const DEFAULT_REGION: RegionPercent = { left: 25, top: 25, width: 50, height: 50 };
const MAX_PROMPT_CHARS = 4000;

function clampPercent(value: number): number {
  return Math.min(100, Math.max(0, value));
}

function roundTenth(value: number): number {
  return Math.round(value * 10) / 10;
}

/**
 * The rectangle between two pointer positions, in percent. The edges are rounded
 * and the size derived from them: rounding a size on its own can push the far
 * edge past 100%, and the server refuses a region outside the image.
 */
export function regionFromDrag(
  start: { x: number; y: number },
  point: { x: number; y: number },
): RegionPercent {
  const left = roundTenth(Math.min(start.x, point.x));
  const top = roundTenth(Math.min(start.y, point.y));
  return {
    left,
    top,
    width: roundTenth(roundTenth(Math.max(start.x, point.x)) - left),
    height: roundTenth(roundTenth(Math.max(start.y, point.y)) - top),
  };
}

export function toFractions(region: RegionPercent): ImageEditRegion {
  const fraction = (value: number) => Number((value / 100).toFixed(4));
  return {
    x: fraction(region.left),
    y: fraction(region.top),
    width: fraction(region.width),
    height: fraction(region.height),
  };
}

function defaultControl(values: string[]): string {
  return values.includes("auto") ? "auto" : (values[0] ?? "");
}

// Edits accept their own lists; `sizes`/`qualities` describe generation only.
function editSizes(model: ImageModelOption | null | undefined): string[] {
  return model?.editSizes ?? [];
}

function editQualities(model: ImageModelOption | null | undefined): string[] {
  return model?.editQualities ?? [];
}

export function editingModels(options: ImageOptionsResponse): ImageModelOption[] {
  return options.models.filter((model) => model.editing === true);
}

export function ImageEditDialog({
  sessionId,
  source,
  label,
  options,
  onClose,
  onEdited,
}: {
  sessionId: string;
  source: ImageEditSource;
  /** What the user is editing: the image's prompt or the document name. */
  label: string;
  options: ImageOptionsResponse;
  onClose: () => void;
  onEdited: (sessionId: string, messages: Message[]) => void;
}) {
  const models = useMemo(() => editingModels(options), [options]);
  const initialModel =
    models.find((model) => model.id === options.defaultEditModel) ?? models[0] ?? null;
  const [modelId, setModelId] = useState(initialModel?.id ?? "");
  const model = models.find((candidate) => candidate.id === modelId) ?? null;
  const [size, setSize] = useState(defaultControl(editSizes(initialModel)));
  const [quality, setQuality] = useState(defaultControl(editQualities(initialModel)));
  const [prompt, setPrompt] = useState("");
  const [useRegion, setUseRegion] = useState(false);
  const [region, setRegion] = useState<RegionPercent>(DEFAULT_REGION);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewFailed, setPreviewFailed] = useState(false);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const dragStart = useRef<{ x: number; y: number } | null>(null);
  // A drag released over the backdrop must not read as "click outside to close".
  const suppressCloseUntil = useRef(0);

  useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;
    const load =
      source.kind === "library"
        ? api.fetchLibraryImageSource(source.id)
        : api.fetchImageArtifact(source.id);
    load
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setPreviewUrl(objectUrl);
      })
      .catch(() => {
        if (!cancelled) setPreviewFailed(true);
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [source.kind, source.id]);

  const chooseModel = (id: string) => {
    const next = models.find((candidate) => candidate.id === id);
    setModelId(id);
    if (next && !editSizes(next).includes(size)) setSize(defaultControl(editSizes(next)));
    if (next && !editQualities(next).includes(quality)) {
      setQuality(defaultControl(editQualities(next)));
    }
  };

  const setEdge = (edge: keyof RegionPercent, raw: number) => {
    setRegion((current) => {
      const value = clampPercent(raw);
      if (edge === "left") {
        const left = Math.min(value, 99);
        return { ...current, left, width: Math.min(current.width, 100 - left) };
      }
      if (edge === "top") {
        const top = Math.min(value, 99);
        return { ...current, top, height: Math.min(current.height, 100 - top) };
      }
      if (edge === "width") {
        return { ...current, width: Math.max(1, Math.min(value, 100 - current.left)) };
      }
      return { ...current, height: Math.max(1, Math.min(value, 100 - current.top)) };
    });
  };

  const pointer = (event: ReactPointerEvent<HTMLDivElement>) => {
    const rect = stageRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0 || rect.height === 0) return null;
    return {
      x: clampPercent(((event.clientX - rect.left) / rect.width) * 100),
      y: clampPercent(((event.clientY - rect.top) / rect.height) * 100),
    };
  };

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!useRegion || pending) return;
    const start = pointer(event);
    if (!start) return;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    dragStart.current = start;
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const start = dragStart.current;
    const point = start ? pointer(event) : null;
    if (!start || !point) return;
    const next = regionFromDrag(start, point);
    if (next.width >= 1 && next.height >= 1) setRegion(next);
  };

  const endDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!dragStart.current) return;
    dragStart.current = null;
    suppressCloseUntil.current = performance.now() + 250;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  };

  const close = () => {
    if (performance.now() < suppressCloseUntil.current) return;
    onClose();
  };

  const trimmed = prompt.trim();
  const canSubmit = Boolean(model) && trimmed.length > 0 && !pending;

  const submit = async () => {
    if (!model || !trimmed || pending) return;
    setPending(true);
    setError(null);
    try {
      const response = await api.editImage({
        sessionId,
        source,
        prompt: trimmed,
        model: model.id,
        // Omitted when the server advertises no list, so its default applies.
        ...(size ? { size } : {}),
        ...(quality ? { quality } : {}),
        ...(useRegion ? { region: toFractions(region) } : {}),
      });
      onEdited(sessionId, response.messages);
      onClose();
    } catch (reason) {
      setError(api.apiErrorDetail(reason));
    } finally {
      setPending(false);
    }
  };

  const regionHelpId = "image-edit-region-help";

  return (
    <ModalShell
      ariaLabel="Edit image"
      title="Edit image"
      filename={label}
      closeLabel="Close image editor"
      onClose={close}
      width="min(640px, 94vw)"
    >
      <form
        className="image-edit-form"
        aria-busy={pending}
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <figure className="image-edit-preview">
          {previewUrl ? (
            <div
              ref={stageRef}
              className="image-edit-stage"
              data-region={useRegion ? "true" : "false"}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={endDrag}
              onPointerCancel={endDrag}
            >
              {/* eslint-disable-next-line @next/next/no-img-element -- authenticated blob object URL; next/image adds no value */}
              <img src={previewUrl} alt={`Source image: ${label}`} draggable={false} />
              {useRegion ? (
                <div
                  className="image-edit-region"
                  data-testid="image-edit-region"
                  aria-hidden="true"
                  style={{
                    left: `${region.left}%`,
                    top: `${region.top}%`,
                    width: `${region.width}%`,
                    height: `${region.height}%`,
                  }}
                />
              ) : null}
            </div>
          ) : (
            <p className="inspector-note" role={previewFailed ? "alert" : "status"}>
              {previewFailed ? "The source image could not be loaded." : "Loading image…"}
            </p>
          )}
        </figure>

        <label className="image-edit-field">
          Describe the change
          <textarea
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            maxLength={MAX_PROMPT_CHARS}
            rows={3}
            required
            disabled={pending}
            placeholder="Replace the sky with a warm sunset"
          />
        </label>

        <label className="image-edit-field">
          Model
          <select
            value={modelId}
            disabled={pending || models.length === 0}
            onChange={(event) => chooseModel(event.target.value)}
          >
            {models.map((candidate) => (
              <option key={candidate.id} value={candidate.id}>
                {candidate.id === options.defaultEditModel
                  ? `${candidate.displayName} (recommended for editing)`
                  : candidate.displayName}
              </option>
            ))}
          </select>
        </label>

        {model && (editSizes(model).length > 0 || editQualities(model).length > 0) ? (
          <div className="image-output-options">
            {editSizes(model).length > 0 ? (
              <label className="image-edit-field">
                Output size
                <select
                  value={size}
                  disabled={pending}
                  onChange={(event) => setSize(event.target.value)}
                >
                  {editSizes(model).map((value) => (
                    <option key={value} value={value}>{value}</option>
                  ))}
                </select>
              </label>
            ) : null}
            {editQualities(model).length > 0 ? (
              <label className="image-edit-field">
                Quality
                <select
                  value={quality}
                  disabled={pending}
                  onChange={(event) => setQuality(event.target.value)}
                >
                  {editQualities(model).map((value) => (
                    <option key={value} value={value}>{value}</option>
                  ))}
                </select>
              </label>
            ) : null}
          </div>
        ) : null}

        <fieldset className="image-edit-area" disabled={pending}>
          <legend>Area to change</legend>
          <label>
            <input
              type="radio"
              name="image-edit-area"
              checked={!useRegion}
              onChange={() => setUseRegion(false)}
            />
            Whole image
          </label>
          <label>
            <input
              type="radio"
              name="image-edit-area"
              checked={useRegion}
              onChange={() => setUseRegion(true)}
              aria-describedby={regionHelpId}
            />
            Selected region
          </label>
          <p id={regionHelpId} className="inspector-note">
            Drag across the image, or set the region&apos;s edges with the sliders.
          </p>
          {useRegion ? (
            <div className="image-edit-region-controls">
              {(
                [
                  ["left", "Left edge"],
                  ["top", "Top edge"],
                  ["width", "Width"],
                  ["height", "Height"],
                ] as const
              ).map(([edge, name]) => (
                <label key={edge}>
                  <span>
                    {name}: {Math.round(region[edge])}%
                  </span>
                  <input
                    type="range"
                    min={edge === "width" || edge === "height" ? 1 : 0}
                    max={edge === "left" || edge === "top" ? 99 : 100}
                    step={1}
                    // A drag keeps tenths; the slider shows the whole percent its
                    // label reads, so its value always matches its step and the
                    // form's constraint validation never blocks a submit.
                    value={Math.round(region[edge])}
                    aria-valuetext={`${Math.round(region[edge])} percent`}
                    onChange={(event) => setEdge(edge, Number(event.target.value))}
                  />
                </label>
              ))}
            </div>
          ) : null}
        </fieldset>

        <p className="inspector-note">
          Cost estimate unavailable. An unavailable estimate is not free. The
          edited image is added to this conversation as a new image; the source
          stays unchanged.
        </p>

        {error ? (
          <p className="image-edit-error" role="alert">
            {error}
          </p>
        ) : null}

        <div className="image-edit-actions">
          <button type="button" onClick={close}>
            Cancel
          </button>
          <button type="submit" data-primary="true" disabled={!canSubmit}>
            {pending ? "Editing…" : "Edit image"}
          </button>
        </div>
      </form>
    </ModalShell>
  );
}
