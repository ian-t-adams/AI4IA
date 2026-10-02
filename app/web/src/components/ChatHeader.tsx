"use client";

import type { RefObject } from "react";

import { EditableSessionTitle } from "./EditableSessionTitle";
import { Icon } from "./Icon";

// Conversation identity and meaningful activity state only: the title, the
// model that will actually answer (opening its settings), whether a reply is
// being generated, and the way into the conversation inspector.
export function ChatHeader({
  title,
  onRename,
  locked,
  lockReason,
  lockReasonId,
  modelLabel,
  onOpenModel,
  streaming,
  onOpenSidebar,
  sidebarButtonRef,
  inspectorOpen,
  onOpenInspector,
  inspectorButtonRef,
}: {
  /** Null for a conversation that has not been created yet. */
  title: string | null;
  onRename?: (title: string) => Promise<void>;
  locked: boolean;
  lockReason?: string;
  lockReasonId: string;
  modelLabel: string | null;
  onOpenModel: () => void;
  streaming: boolean;
  /** Present on phones, where the navigation sidebar is a drawer. */
  onOpenSidebar?: () => void;
  sidebarButtonRef?: RefObject<HTMLElement | null>;
  inspectorOpen: boolean;
  onOpenInspector: () => void;
  inspectorButtonRef?: RefObject<HTMLElement | null>;
}) {
  return (
    <header className="chat-header">
      {onOpenSidebar ? (
        <button
          ref={(element) => {
            if (element && sidebarButtonRef) sidebarButtonRef.current = element;
          }}
          type="button"
          className="icon-btn"
          onClick={onOpenSidebar}
          aria-label="Open conversation sidebar"
          title="Conversations and destinations"
        >
          <Icon name="menu" />
        </button>
      ) : null}
      <div className="chat-header-title">
        {title !== null && onRename ? (
          <>
            <EditableSessionTitle
              title={title}
              onSave={onRename}
              disabled={locked}
              disabledReasonId={lockReasonId}
            />
            {locked && lockReason ? (
              <span id={lockReasonId} role="status" className="visually-hidden">
                {lockReason}
              </span>
            ) : null}
          </>
        ) : (
          <strong>New conversation</strong>
        )}
      </div>
      {modelLabel ? (
        <button
          type="button"
          className="model-chip"
          onClick={onOpenModel}
          aria-label={`Model: ${modelLabel}. Change it in conversation settings`}
          title="Change the model in conversation settings"
        >
          <span>{modelLabel}</span>
          <Icon name="chevron-down" size={14} />
        </button>
      ) : null}
      <div
        className="chat-activity"
        role="status"
        aria-live="polite"
        aria-atomic="true"
        data-busy={streaming ? "true" : "false"}
      >
        {streaming ? "Generating…" : ""}
      </div>
      {!inspectorOpen ? (
        <button
          ref={(element) => {
            if (element && inspectorButtonRef) inspectorButtonRef.current = element;
          }}
          type="button"
          className="icon-btn"
          onClick={onOpenInspector}
          aria-label="Open conversation inspector"
          title="Conversation settings"
        >
          <Icon name="sliders" />
        </button>
      ) : null}
    </header>
  );
}
