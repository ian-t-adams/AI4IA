"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, RefObject } from "react";
import type { DeletionFeedback } from "@/lib/conversationDeletion";
import type { Session } from "@/lib/types";
import type { WorkspaceView } from "@/lib/workspaceView";
import { DOCS_INDEX_URL, STATUS_URL } from "@/lib/docs";
import { groupConversations, matchesConversationQuery } from "@/lib/conversationGroups";
import { AdminLink } from "./AdminLink";
import { EditableSessionTitle } from "./EditableSessionTitle";
import { Icon, type IconName } from "./Icon";
import { UserMenu } from "./UserMenu";
import { useModalFocus, useModalKeyDown } from "./useModalFocus";
import { WorkflowApprovalInboxEntry } from "./WorkflowApprovalInbox";

// The left navigation owns conversations and true destinations. Expanded, it
// shows New chat, the destinations, searchable conversations and one compact
// utility/account area; collapsed, it is an icon rail; as a drawer (narrow
// screens) it is a labelled modal dialog that traps focus and returns it.
export type SidebarMode = "expanded" | "collapsed" | "drawer";

interface Destination {
  view: WorkspaceView;
  label: string;
  icon: IconName;
  // Destinations that can move the conversation (running a workflow, editing
  // an image into the active chat) follow the same navigation lock as
  // switching conversations.
  lockable: boolean;
}

// Show the search field once the list is long enough to need one.
const SEARCH_THRESHOLD = 6;

export function Sidebar({
  mode = "expanded",
  sessions,
  activeId,
  onSelect,
  onNewChat,
  onDelete,
  onRename,
  deletingIds,
  deletionFeedback,
  onDismissDeletionFeedback,
  view = "chat",
  onNavigate,
  libraryAvailable = false,
  photoAvatarsAvailable = false,
  decisionsAvailable = false,
  decisionsAvailabilityError = false,
  onRetryDecisions,
  onOpenDeletionStatus,
  onBeforeSignOut,
  onCollapse,
  onExpand,
  expandLabel = "Expand sidebar",
  openerRef,
  disabled = false,
  disabledReason,
}: {
  mode?: SidebarMode;
  sessions: Session[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNewChat: () => void;
  onDelete: (id: string) => void;
  onRename: (id: string, title: string) => Promise<void>;
  deletingIds?: ReadonlySet<string>;
  /** The outcome of a deletion that didn't complete, shown on its row. */
  deletionFeedback?: ReadonlyMap<string, DeletionFeedback>;
  onDismissDeletionFeedback?: (id: string) => void;
  view?: WorkspaceView;
  onNavigate?: (view: WorkspaceView) => void;
  /** Present only while the deployment offers the document library. */
  libraryAvailable?: boolean;
  /** Present only while the server reports photo avatars enabled. */
  photoAvatarsAvailable?: boolean;
  /** Present only while the authenticated Decision options offer a model. */
  decisionsAvailable?: boolean;
  decisionsAvailabilityError?: boolean;
  onRetryDecisions?: () => void;
  onOpenDeletionStatus: () => void;
  onBeforeSignOut?: () => boolean | void;
  onCollapse?: () => void;
  onExpand?: () => void;
  expandLabel?: string;
  openerRef?: RefObject<HTMLElement | null>;
  disabled?: boolean;
  /** Shown while navigation is locked, explaining why and how to recover. */
  disabledReason?: string;
}) {
  const drawer = mode === "drawer";
  const drawerFocusRef = useModalFocus<HTMLElement>(drawer, openerRef);
  const onDrawerKeyDown = useModalKeyDown<HTMLElement>(onCollapse ?? (() => {}), drawer);
  // Every soft-disabled control points at the visible lock reason, so screen
  // reader users hear the same recovery guidance sighted users read.
  const lockHintId = useId();
  const headingId = useId();
  const describedBy = disabled && disabledReason ? lockHintId : undefined;
  const [query, setQuery] = useState("");
  const [utilitiesOpen, setUtilitiesOpen] = useState(false);
  const utilitiesId = useId();
  const utilitiesButton = useRef<HTMLButtonElement>(null);

  // Deleting asks on the row itself: one proportionate, keyboard-reachable
  // confirmation, never a native dialog (a browser can silently suppress
  // those, which makes Delete do nothing).
  const rowIdBase = useId();
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const deleteButtons = useRef(new Map<string, HTMLButtonElement>());
  const cancelRef = useRef<HTMLButtonElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const removalFocus = useRef<{ id: string; index: number } | null>(null);
  const confirming = confirmingId !== null && sessions.some((session) => session.id === confirmingId)
    ? confirmingId
    : null;
  useEffect(() => {
    if (confirming) cancelRef.current?.focus();
  }, [confirming]);
  useEffect(() => {
    // A deleted row takes its focused control with it: hand focus to the
    // conversation now in its place, or New chat, rather than the page.
    const pending = removalFocus.current;
    if (!pending || sessions.some((session) => session.id === pending.id)) return;
    removalFocus.current = null;
    const container = scrollRef.current;
    const active = document.activeElement;
    if (!container || (active && active !== document.body && container.contains(active))) return;
    const titles = container.querySelectorAll<HTMLElement>(".conversation-row .editable-session-title-text");
    const next = titles[Math.min(pending.index, titles.length - 1)]
      ?? container.querySelector<HTMLElement>(".sidebar-new-chat");
    next?.focus();
  }, [sessions]);

  const deletionBlocked = (id: string) => deletionFeedback?.get(id)?.blocksRemoval === true;
  const startDeletion = (id: string) => {
    if (disabled || deletingIds?.has(id) || deletionBlocked(id)) return;
    setConfirmingId((current) => (current === id ? null : id));
  };
  const cancelDeletion = (id: string) => {
    setConfirmingId(null);
    deleteButtons.current.get(id)?.focus();
  };
  const confirmDeletion = (id: string, index: number) => {
    setConfirmingId(null);
    if (disabled) return;
    removalFocus.current = { id, index };
    deleteButtons.current.get(id)?.focus();
    onDelete(id);
  };
  const onConfirmKeyDown = (event: KeyboardEvent<HTMLDivElement>, id: string) => {
    if (event.key !== "Escape") return;
    // Cancel the question, not the drawer around it.
    event.preventDefault();
    event.stopPropagation();
    cancelDeletion(id);
  };

  const destinations: Destination[] = [
    { view: "chat", label: "Chat", icon: "chat", lockable: false },
    ...(decisionsAvailable
      ? [{ view: "decisions" as const, label: "Decisions", icon: "sliders" as const, lockable: true }]
      : []),
    { view: "studio", label: "Studio", icon: "studio", lockable: true },
  ];
  const resources: Destination[] = [
    ...(libraryAvailable
      ? [{ view: "library" as const, label: "Document library", icon: "library" as const, lockable: true }]
      : []),
    ...(photoAvatarsAvailable
      ? [{ view: "avatars" as const, label: "Photo avatars", icon: "avatar" as const, lockable: false }]
      : []),
  ];

  const visible = useMemo(
    () => sessions.filter((session) => matchesConversationQuery(session.title, query)),
    [query, sessions],
  );
  const groups = useMemo(() => groupConversations(visible), [visible]);

  const go = (destination: Destination) => {
    if (disabled && destination.lockable) return;
    onNavigate?.(destination.view);
  };

  const newChat = () => {
    if (disabled) return;
    onNewChat();
  };

  const destinationButton = (destination: Destination) => (
    <button
      type="button"
      className="sidebar-link"
      aria-label={destination.view === "studio" ? "Studio: Agents & workflows" : undefined}
      onClick={() => go(destination)}
      aria-current={view === destination.view ? "page" : undefined}
      aria-disabled={(disabled && destination.lockable) || undefined}
      aria-describedby={destination.lockable ? describedBy : undefined}
    >
      <Icon name={destination.icon} />
      <span>{destination.label}</span>
    </button>
  );
  const availabilityNotice = decisionsAvailabilityError ? (
    <div className="sidebar-availability" aria-live="polite">
      <p>Decision availability could not be checked.</p>
      <button type="button" className="sidebar-quiet-action" onClick={onRetryDecisions}>
        Retry Decision availability
      </button>
    </div>
  ) : null;

  if (mode === "collapsed") {
    return (
      <nav
        className="app-sidebar"
        data-mode="collapsed"
        aria-label="Chat sessions"
        style={{ overflow: "hidden" }}
      >
        <div className="sidebar-head">
          {/* eslint-disable-next-line @next/next/no-img-element -- small static brand mark */}
          <img src="/ai4ia-mark.png" alt="" aria-hidden="true" width={28} height={28} style={{ borderRadius: 6 }} />
          <button
            ref={(element) => {
              if (element && openerRef) openerRef.current = element;
            }}
            type="button"
            className="icon-btn"
            onClick={onExpand}
            aria-label={expandLabel}
            title={expandLabel}
          >
            <Icon name="panel-left" />
          </button>
        </div>
        <div className="sidebar-rail">
          {disabled && disabledReason ? (
            <p id={lockHintId} role="status" className="visually-hidden">
              {disabledReason}
            </p>
          ) : null}
          <ul className="sidebar-rail-group" aria-label="Destinations">
            <li>
              <button
                type="button"
                className="icon-btn rail-new-chat"
                onClick={newChat}
                aria-disabled={disabled || undefined}
                aria-describedby={describedBy}
                aria-label="New chat"
                title="New chat"
              >
                <Icon name="plus" />
              </button>
            </li>
            {destinations.map((destination) => (
              <li key={destination.view}>
                <button
                  type="button"
                  className="icon-btn"
                  onClick={() => go(destination)}
                  aria-current={view === destination.view ? "page" : undefined}
                  aria-disabled={(disabled && destination.lockable) || undefined}
                  aria-describedby={destination.lockable ? describedBy : undefined}
                  aria-label={destination.view === "studio" ? "Studio: Agents & workflows" : destination.label}
                  title={destination.label}
                >
                  <Icon name={destination.icon} />
                </button>
              </li>
            ))}
          </ul>
          {decisionsAvailabilityError ? (
            <button type="button" className="icon-btn" onClick={onRetryDecisions}
              aria-label="Decision availability could not be checked. Retry Decision availability"
              title="Retry Decision availability">
              <Icon name="sliders" />
            </button>
          ) : null}
          <div className="sidebar-rail-spacer" />
          <ul className="sidebar-rail-group" aria-label="Utilities and account">
            <li>
              <WorkflowApprovalInboxEntry disabled={disabled} compact />
            </li>
            <li>
              <button
                type="button"
                className="icon-btn"
                onClick={() => {
                  setUtilitiesOpen(true);
                  onExpand?.();
                }}
                aria-label="Settings & more"
                title="Settings & more"
              >
                <Icon name="settings" />
              </button>
            </li>
          </ul>
        </div>
      </nav>
    );
  }

  return (
    <nav
      ref={drawerFocusRef}
      onKeyDown={onDrawerKeyDown}
      className="app-sidebar"
      data-mode={mode}
      role={drawer ? "dialog" : "navigation"}
      aria-modal={drawer ? true : undefined}
      aria-label="Chat sessions"
      style={{ overflow: "hidden" }}
    >
      <div className="sidebar-head">
        <span className="sidebar-brand">
          {/* eslint-disable-next-line @next/next/no-img-element -- small static brand mark */}
          <img src="/ai4ia-mark.png" alt="" aria-hidden="true" width={28} height={28} />
          AI4IA
        </span>
        {onCollapse ? (
          <button
            type="button"
            className="icon-btn"
            onClick={onCollapse}
            aria-label="Collapse sidebar"
            title={drawer ? "Close" : "Collapse sidebar"}
          >
            <Icon name={drawer ? "close" : "panel-left"} />
          </button>
        ) : null}
      </div>
      <div
        ref={scrollRef}
        className="sidebar-scroll"
        data-testid="sidebar-scroll"
        style={{ minHeight: 0, overflowY: "auto", overflowX: "hidden" }}
      >
        {disabled && disabledReason ? (
          <p id={lockHintId} role="status" className="sidebar-lock">
            {disabledReason}
          </p>
        ) : null}
        <button
          type="button"
          className="btn btn-primary sidebar-new-chat"
          onClick={newChat}
          aria-disabled={disabled || undefined}
          aria-describedby={describedBy}
        >
          <Icon name="plus" size={18} />
          New chat
        </button>
        <ul className="sidebar-nav" aria-label="Destinations">
          {destinations.map((destination) => (
            <li key={destination.view}>
              {destinationButton(destination)}
            </li>
          ))}
        </ul>
        {availabilityNotice}
        <section className="sidebar-conversations" aria-labelledby={headingId}>
          <h2 className="sidebar-section-title" id={headingId}>
            Conversations
          </h2>
          {sessions.length >= SEARCH_THRESHOLD || query ? (
            <div className="sidebar-search">
              <Icon name="search" size={16} />
              <input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search conversations"
                aria-label="Search conversations"
              />
            </div>
          ) : null}
          {sessions.length === 0 ? (
            <p className="conversation-empty">No conversations yet.</p>
          ) : groups.length === 0 ? (
            <p className="conversation-empty" role="status">
              No conversations match “{query.trim()}”.
            </p>
          ) : (
            groups.map((group) => (
              <div key={group.title ?? "all"}>
                {group.title ? (
                  <h3 className="conversation-group-title">{group.title}</h3>
                ) : null}
                <ul className="conversation-group">
                  {group.items.map((session) => {
                    const current = session.id === activeId && view === "chat";
                    const deleting = deletingIds?.has(session.id) ?? false;
                    const title = session.title || "Untitled";
                    const feedback = deletionFeedback?.get(session.id);
                    const blocked = feedback?.blocksRemoval === true;
                    const asking = confirming === session.id;
                    const rowIndex = visible.indexOf(session);
                    const rowKey = `${rowIdBase}-${rowIndex}`;
                    const feedbackId = `${rowKey}-outcome`;
                    return (
                      <li key={session.id} className="conversation-item">
                        <div
                          className="conversation-row"
                          data-current={current || undefined}
                          data-attention={asking || (feedback && !feedback.dismissed) || undefined}
                        >
                          <EditableSessionTitle
                            title={title}
                            onSave={(next) => onRename(session.id, next)}
                            onOpen={() => onSelect(session.id)}
                            current={session.id === activeId}
                            disabled={disabled}
                            disabledReasonId={lockHintId}
                            compact
                          />
                          <button
                            ref={(element) => {
                              if (element) deleteButtons.current.set(session.id, element);
                              else deleteButtons.current.delete(session.id);
                            }}
                            type="button"
                            className="conversation-delete"
                            onClick={() => startDeletion(session.id)}
                            disabled={deleting}
                            aria-disabled={disabled || deleting || blocked || undefined}
                            aria-busy={deleting || undefined}
                            aria-expanded={asking}
                            aria-label={`Delete ${session.title || "conversation"}`}
                            aria-describedby={describedBy ?? (blocked ? feedbackId : undefined)}
                            title={disabled || blocked ? undefined : "Delete"}
                          >
                            <Icon name="trash" size={16} />
                          </button>
                        </div>
                        {asking ? (
                          <div
                            className="conversation-confirm"
                            role="group"
                            aria-labelledby={`${rowKey}-ask`}
                            aria-describedby={`${rowKey}-scope`}
                            onKeyDown={(event) => onConfirmKeyDown(event, session.id)}
                          >
                            <p id={`${rowKey}-ask`} className="conversation-confirm-title">
                              Delete “{title}”?
                            </p>
                            <p id={`${rowKey}-scope`}>
                              Its messages and attachments are deleted. Your library documents,
                              memories and generated media are kept, and backups follow their own
                              retention.
                            </p>
                            <div className="conversation-confirm-actions">
                              <button
                                type="button"
                                className="btn btn-sm btn-danger"
                                onClick={() => confirmDeletion(session.id, rowIndex)}
                              >
                                Delete
                              </button>
                              <button
                                ref={cancelRef}
                                type="button"
                                className="btn btn-sm"
                                onClick={() => cancelDeletion(session.id)}
                              >
                                Cancel
                              </button>
                            </div>
                          </div>
                        ) : null}
                        {feedback && !feedback.dismissed ? (
                          <div className="conversation-feedback" data-kind={feedback.kind}>
                            <p id={feedbackId} role="alert">
                              {feedback.message}
                            </p>
                            <div className="conversation-confirm-actions">
                              {feedback.retryable ? (
                                <button
                                  type="button"
                                  className="btn btn-sm"
                                  disabled={deleting}
                                  aria-disabled={disabled || undefined}
                                  aria-describedby={describedBy}
                                  onClick={() => {
                                    if (disabled || deleting) return;
                                    removalFocus.current = { id: session.id, index: rowIndex };
                                    onDelete(session.id);
                                  }}
                                >
                                  Try again
                                </button>
                              ) : null}
                              <button
                                type="button"
                                className="btn btn-sm btn-ghost"
                                onClick={() => {
                                  onDismissDeletionFeedback?.(session.id);
                                  deleteButtons.current.get(session.id)?.focus();
                                }}
                                aria-label={`Dismiss the message about ${title}`}
                              >
                                Dismiss
                              </button>
                            </div>
                          </div>
                        ) : feedback?.dismissed && blocked ? (
                          <span id={feedbackId} className="visually-hidden">
                            {feedback.message}
                          </span>
                        ) : null}
                      </li>
                    );
                  })}
                </ul>
              </div>
            ))
          )}
        </section>
        <div className="sidebar-footer" aria-label="Utilities and account" role="group">
          <div className="sidebar-footer-row">
            <button
              ref={utilitiesButton}
              type="button"
              className="sidebar-link"
              aria-expanded={utilitiesOpen}
              aria-controls={utilitiesId}
              onClick={() => setUtilitiesOpen((open) => !open)}
            >
              <Icon name="settings" />
              <span>Settings & more</span>
              <Icon name="chevron-down" size={16} />
            </button>
            <WorkflowApprovalInboxEntry disabled={disabled} compact />
          </div>
          <div
            id={utilitiesId}
            hidden={!utilitiesOpen}
            className="sidebar-utilities"
            onKeyDown={(event) => {
              if (event.key !== "Escape") return;
              event.preventDefault();
              event.stopPropagation();
              setUtilitiesOpen(false);
              utilitiesButton.current?.focus();
            }}
          >
            {resources.length > 0 ? (
              <ul className="sidebar-nav" aria-label="Resources">
                {resources.map((destination) => <li key={destination.view}>{destinationButton(destination)}</li>)}
              </ul>
            ) : null}
            <button type="button" className="sidebar-link" onClick={() => onNavigate?.("settings")}
              aria-current={view === "settings" ? "page" : undefined}>
              <Icon name="settings" /><span>Settings</span>
            </button>
            <button type="button" className="sidebar-link" onClick={onOpenDeletionStatus}>
              <Icon name="trash" /><span>Deletion status</span>
            </button>
            <a
              className="sidebar-link"
              href={DOCS_INDEX_URL}
              target="_blank"
              rel="noopener noreferrer"
              aria-label="Documentation (opens in new tab)"
              title="Browse the AI4IA documentation hub"
            >
              <Icon name="docs" />
              <span>Docs</span>
            </a>
            <a
              className="sidebar-link"
              href={STATUS_URL}
              target="_blank"
              rel="noopener noreferrer"
              aria-label="Status (opens in new tab)"
              title="Live deployment health and service status"
            >
              <Icon name="status" />
              <span>Status</span>
            </a>
            <AdminLink disabled={disabled} disabledReasonId={describedBy} />
            <UserMenu onBeforeSignOut={onBeforeSignOut} />
          </div>
        </div>
      </div>
    </nav>
  );
}
