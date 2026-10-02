// @vitest-environment jsdom
import { useRef, useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DeletionFeedback } from "@/lib/conversationDeletion";
import { Sidebar } from "./Sidebar";
import { makeChatSession } from "./chatTestFixtures";

vi.mock("./AdminLink", () => ({ AdminLink: () => null }));
vi.mock("./UserMenu", () => ({ UserMenu: () => null }));

beforeEach(() => {
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("responsive sidebar", () => {
  it("makes the background inert and restores explicit opener focus on every close path", async () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      const openerRef = useRef<HTMLElement | null>(null);
      return (
        <div style={{ width: 320, fontSize: "200%" }}>
          {!open ? (
            <button
              ref={(element) => {
                if (element) openerRef.current = element;
              }}
              type="button"
              onClick={() => setOpen(true)}
            >
              Open conversations
            </button>
          ) : (
            <>
              <button
                type="button"
                aria-label="Close conversations backdrop"
                onClick={() => setOpen(false)}
              />
              <Sidebar
                mode="drawer"
                sessions={[
                  {
                    id: "s1",
                    userId: "u1",
                    title: "A very long conversation title that must not overlap controls",
                    titleSource: "manual",
                    model: null,
                    systemPrompt: null,
                    agentName: null,
                    toolOverrides: { added: [], removed: [] },
                    libraryDocumentIds: [],
                    createdAt: "",
                    updatedAt: "",
                  },
                ]}
                activeId="s1"
                onSelect={vi.fn()}
                onNewChat={vi.fn()}
                onDelete={vi.fn()}
                onRename={vi.fn()}
                onOpenDeletionStatus={vi.fn()}
                onCollapse={() => setOpen(false)}
                openerRef={openerRef}
              />
            </>
          )}
          <main inert={open ? true : undefined} aria-hidden={open ? true : undefined}>
            Conversation
          </main>
        </div>
      );
    }
    const user = userEvent.setup();
    render(<Harness />);
    const open = async () => {
      await user.click(
        screen.getByRole("button", { name: "Open conversations" }),
      );
      expect(document.querySelector("main")).toHaveAttribute("inert");
      return screen.getByRole("dialog", { name: "Chat sessions" });
    };

    let dialog = await open();
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(
      await screen.findByRole("button", { name: "Open conversations" }),
    ).toHaveFocus();

    await open();
    await user.click(
      screen.getByRole("button", { name: "Close conversations backdrop" }),
    );
    expect(
      await screen.findByRole("button", { name: "Open conversations" }),
    ).toHaveFocus();

    dialog = await open();
    await user.click(screen.getByRole("button", { name: "Rename" }));
    const titleInput = screen.getByRole("textbox", { name: "Conversation title" });
    await user.keyboard("{Escape}");
    expect(titleInput).not.toBeInTheDocument();
    expect(dialog).toBeInTheDocument();
    expect(screen.getByTestId("sidebar-scroll")).toHaveStyle({
      minHeight: "0",
      overflowY: "auto",
      overflowX: "hidden",
    });
    // Only `overflow` is asserted here. jsdom 30's CSSOM rejects viewport units
    // on max-width/max-height, so `maxWidth: 100vw` / `maxHeight: 100dvh` --
    // which Sidebar.tsx does set, and which real browsers honour -- are dropped
    // rather than stored, leaving no DOM-observable trace to assert on. Those
    // two were incidental to this test anyway: it covers background inertness
    // and focus restoration, and jsdom has no layout engine, so asserting them
    // only ever proved React passed a literal string through.
    expect(dialog).toHaveStyle({
      overflow: "hidden",
    });
    expect(
      screen.getByRole("button", {
        name: "A very long conversation title that must not overlap controls",
      }),
    ).toHaveClass("editable-session-title-text");
    expect(
      screen.getByRole("button", {
        name: "A very long conversation title that must not overlap controls",
      }),
    ).toHaveAttribute("aria-current", "true");
    const status = screen.getByRole("link", {
      name: "Status (opens in new tab)",
    });
    status.focus();
    expect(status).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    expect(
      await screen.findByRole("button", { name: "Open conversations" }),
    ).toHaveFocus();
    expect(dialog).not.toBeInTheDocument();
  });

  it.each([false, true])("keeps deletion status reachable without bypassing navigation locks (locked=%s)", async (disabled) => {
    const props = {
      sessions: [makeChatSession("A"), makeChatSession("B")],
      activeId: "A", onSelect: vi.fn(), onNewChat: vi.fn(), onDelete: vi.fn(),
      onRename: vi.fn(), onOpenDeletionStatus: vi.fn(), disabled,
      disabledReason: "Wait for the current reply to finish generating.",
    };
    const user = userEvent.setup();
    render(<Sidebar {...props} />);
    await user.click(screen.getByRole("button", { name: "Deletion status" }));
    expect(props.onOpenDeletionStatus).toHaveBeenCalledTimes(1);
    expect(props.onSelect).not.toHaveBeenCalled();
    expect(props.onDelete).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Session B" }));
    await user.click(screen.getByRole("button", { name: "New chat" }));
    await user.click(screen.getByRole("button", { name: "Delete Session A" }));
    // A locked sidebar doesn't even ask.
    const question = screen.queryByRole("group", { name: "Delete “Session A”?" });
    expect(question === null).toBe(disabled);
    if (question) await user.click(within(question).getByRole("button", { name: "Delete" }));
    expect(props.onSelect).toHaveBeenCalledTimes(disabled ? 0 : 1);
    expect(props.onNewChat).toHaveBeenCalledTimes(disabled ? 0 : 1);
    expect(props.onDelete).toHaveBeenCalledTimes(disabled ? 0 : 1);
    if (disabled) {
      expect(screen.getByRole("button", { name: "Delete Session A" })).toHaveAccessibleDescription(props.disabledReason);
    }
  });

  it("disables only the in-flight deletion and leaves another conversation usable", async () => {
    const props = {
      sessions: [makeChatSession("A"), makeChatSession("B")],
      activeId: "B", onSelect: vi.fn(), onNewChat: vi.fn(), onDelete: vi.fn(),
      onRename: vi.fn(), onOpenDeletionStatus: vi.fn(),
    };
    const user = userEvent.setup();
    const view = render(<Sidebar {...props} deletingIds={new Set(["A"])} />);
    const deleting = screen.getByRole("button", { name: "Delete Session A" });
    expect(deleting).toBeDisabled();
    expect(deleting).toHaveAttribute("aria-busy", "true");
    await user.click(deleting);
    expect(props.onDelete).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Session B" }));
    expect(props.onSelect).toHaveBeenCalledExactlyOnceWith("B");
    expect(screen.getByRole("button", { name: "Delete Session B" })).toBeEnabled();
    expect(screen.queryByRole("group", { name: /Delete “Session A”/ })).toBeNull();
    view.rerender(<Sidebar {...props} deletingIds={new Set()} />);
    await user.click(screen.getByRole("button", { name: "Delete Session A" }));
    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(props.onDelete).toHaveBeenCalledExactlyOnceWith("A");
  });

  it("navigates to destinations, holding only the lockable ones while navigation is locked", async () => {
    const user = userEvent.setup();
    const onNavigate = vi.fn();
    const base = {
      sessions: [makeChatSession("A")],
      activeId: "A", onSelect: vi.fn(), onNewChat: vi.fn(), onDelete: vi.fn(),
      onRename: vi.fn(), onOpenDeletionStatus: vi.fn(), onNavigate,
      libraryAvailable: true, photoAvatarsAvailable: true,
    };
    const view = render(<Sidebar {...base} />);
    await user.click(screen.getByRole("button", { name: "Document library" }));
    await user.click(screen.getByRole("button", { name: "Photo avatars" }));
    await user.click(screen.getByRole("button", { name: "Agents & workflows" }));
    await user.click(screen.getByRole("button", { name: "Settings" }));
    expect(onNavigate.mock.calls.map(([target]) => target)).toEqual([
      "library",
      "avatars",
      "studio",
      "settings",
    ]);

    onNavigate.mockClear();
    const reason = "Wait for the current reply to finish generating.";
    view.rerender(<Sidebar {...base} view="avatars" disabled disabledReason={reason} />);
    expect(screen.getByRole("button", { name: "Photo avatars" })).toHaveAttribute("aria-current", "page");
    for (const name of ["Document library", "Agents & workflows"]) {
      const button = screen.getByRole("button", { name });
      expect(button).toHaveAttribute("aria-disabled", "true");
      expect(button).toHaveAccessibleDescription(reason);
      await user.click(button);
    }
    // The gallery never changes the conversation, so it stays reachable.
    await user.click(screen.getByRole("button", { name: "Photo avatars" }));
    expect(onNavigate.mock.calls.map(([target]) => target)).toEqual(["avatars"]);
  });

  it("hides destinations the deployment does not offer", () => {
    render(
      <Sidebar
        sessions={[]}
        activeId={null}
        onSelect={vi.fn()}
        onNewChat={vi.fn()}
        onDelete={vi.fn()}
        onRename={vi.fn()}
        onOpenDeletionStatus={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Document library" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Photo avatars" })).toBeNull();
    expect(screen.getByRole("button", { name: "Agents & workflows" })).toBeInTheDocument();
    expect(screen.getByText("No conversations yet.")).toBeInTheDocument();
  });

  it("groups conversations by recency and offers search once the list is long", async () => {
    const user = userEvent.setup();
    const now = Date.now();
    const daysAgo = (days: number) => new Date(now - days * 24 * 60 * 60 * 1000).toISOString();
    const conversation = (id: string, title: string, days: number) => ({
      ...makeChatSession(id),
      title,
      updatedAt: daysAgo(days),
    });
    const sessions = [
      conversation("b1", "Budget review", 0),
      conversation("b2", "Bicep modules", 3),
      conversation("b3", "Avatar script", 3),
      conversation("b4", "Quarterly plan", 12),
      conversation("b5", "Old notes", 90),
    ];
    const props = {
      activeId: null, onSelect: vi.fn(), onNewChat: vi.fn(), onDelete: vi.fn(),
      onRename: vi.fn(), onOpenDeletionStatus: vi.fn(),
    };
    const view = render(<Sidebar {...props} sessions={sessions} />);
    expect(
      screen.getAllByRole("heading", { level: 3 }).map((heading) => heading.textContent),
    ).toEqual(["Today", "Previous 7 days", "Previous 30 days", "Older"]);
    // Five conversations: no search field yet.
    expect(screen.queryByRole("searchbox", { name: "Search conversations" })).toBeNull();

    const longer = [...sessions, conversation("b6", "Budget follow-up", 1)];
    view.rerender(<Sidebar {...props} sessions={longer} />);
    expect(
      screen.getAllByRole("heading", { level: 3 }).map((heading) => heading.textContent),
    ).toEqual(["Today", "Yesterday", "Previous 7 days", "Previous 30 days", "Older"]);
    const search = screen.getByRole("searchbox", { name: "Search conversations" });
    await user.type(search, "budget");
    expect(screen.getByRole("button", { name: "Budget review" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Budget follow-up" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Bicep modules" })).toBeNull();

    await user.clear(search);
    await user.type(search, "nothing like this");
    expect(screen.getByRole("status")).toHaveTextContent("No conversations match");
    expect(screen.queryByRole("button", { name: "Budget review" })).toBeNull();
  });

  it("collapses to an icon rail that keeps every destination named", async () => {
    const user = userEvent.setup();
    const onExpand = vi.fn();
    const onNavigate = vi.fn();
    const onNewChat = vi.fn();
    render(
      <Sidebar
        mode="collapsed"
        sessions={[makeChatSession("A")]}
        activeId="A"
        onSelect={vi.fn()}
        onNewChat={onNewChat}
        onDelete={vi.fn()}
        onRename={vi.fn()}
        onOpenDeletionStatus={vi.fn()}
        onNavigate={onNavigate}
        onExpand={onExpand}
        libraryAvailable
        photoAvatarsAvailable
      />,
    );
    // The rail lists destinations, not conversations.
    expect(screen.queryByRole("button", { name: "Session A" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Expand sidebar" }));
    await user.click(screen.getByRole("button", { name: "New chat" }));
    await user.click(screen.getByRole("button", { name: "Conversation" }));
    await user.click(screen.getByRole("button", { name: "Photo avatars" }));
    expect(onExpand).toHaveBeenCalledTimes(1);
    expect(onNewChat).toHaveBeenCalledTimes(1);
    expect(onNavigate.mock.calls.map(([target]) => target)).toEqual(["chat", "avatars"]);
    expect(screen.getByRole("link", { name: "Documentation (opens in new tab)" })).toHaveAttribute("target", "_blank");
    expect(screen.getByRole("link", { name: "Status (opens in new tab)" })).toHaveAttribute("target", "_blank");
  });

  describe("deleting a conversation", () => {
    function setup(overrides: Partial<Parameters<typeof Sidebar>[0]> = {}) {
      const props = {
        sessions: [makeChatSession("A"), makeChatSession("B")],
        activeId: "B",
        onSelect: vi.fn(),
        onNewChat: vi.fn(),
        onDelete: vi.fn(),
        onRename: vi.fn(),
        onOpenDeletionStatus: vi.fn(),
        onDismissDeletionFeedback: vi.fn(),
        ...overrides,
      };
      const view = render(<Sidebar {...props} />);
      return { props, view, user: userEvent.setup() };
    }

    it("asks on the row, says what deleting does, and deletes only once confirmed", async () => {
      const { props, user } = setup();
      const trash = screen.getByRole("button", { name: "Delete Session A" });
      expect(trash).toHaveAttribute("aria-expanded", "false");
      await user.click(trash);

      const question = screen.getByRole("group", { name: "Delete “Session A”?" });
      expect(trash).toHaveAttribute("aria-expanded", "true");
      expect(question).toHaveAccessibleDescription(
        /leaves your chats.*queued for cleanup.*may stay pending.*aren't erased/,
      );
      // The safe choice has focus; nothing has been requested yet.
      expect(within(question).getByRole("button", { name: "Cancel" })).toHaveFocus();
      expect(props.onDelete).not.toHaveBeenCalled();

      await user.click(within(question).getByRole("button", { name: "Delete" }));
      expect(props.onDelete).toHaveBeenCalledExactlyOnceWith("A");
      expect(screen.queryByRole("group", { name: /Delete “Session A”/ })).toBeNull();
      expect(trash).toHaveFocus();
    });

    it("cancels with the button or Escape and returns focus to the row's action", async () => {
      const { props, user } = setup();
      const trash = screen.getByRole("button", { name: "Delete Session A" });
      await user.click(trash);
      await user.click(screen.getByRole("button", { name: "Cancel" }));
      expect(screen.queryByRole("group", { name: /Delete “Session A”/ })).toBeNull();
      expect(trash).toHaveFocus();

      await user.click(trash);
      await user.keyboard("{Escape}");
      expect(screen.queryByRole("group", { name: /Delete “Session A”/ })).toBeNull();
      expect(trash).toHaveFocus();

      // Pressing the row's action again closes the question too.
      await user.click(trash);
      await user.click(trash);
      expect(screen.queryByRole("group", { name: /Delete “Session A”/ })).toBeNull();
      expect(props.onDelete).not.toHaveBeenCalled();
    });

    it("closes only the question when Escape is pressed inside the drawer", async () => {
      const onCollapse = vi.fn();
      const { props, user } = setup({ mode: "drawer", onCollapse });
      // The drawer settles its own initial focus first, as it does for a person.
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Collapse sidebar" })).toHaveFocus(),
      );
      await user.click(screen.getByRole("button", { name: "Delete Session A" }));
      expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
      await user.keyboard("{Escape}");
      expect(screen.queryByRole("group", { name: /Delete “Session A”/ })).toBeNull();
      expect(onCollapse).not.toHaveBeenCalled();
      // Control: with no question open, Escape closes the drawer.
      await user.keyboard("{Escape}");
      expect(onCollapse).toHaveBeenCalledTimes(1);
      expect(props.onDelete).not.toHaveBeenCalled();
    });

    it("keeps one question open at a time", async () => {
      const { user } = setup();
      await user.click(screen.getByRole("button", { name: "Delete Session A" }));
      await user.click(screen.getByRole("button", { name: "Delete Session B" }));
      expect(screen.queryByRole("group", { name: /Delete “Session A”/ })).toBeNull();
      expect(screen.getByRole("group", { name: "Delete “Session B”?" })).toBeInTheDocument();
    });

    it("explains an outcome on its row and resumes the same deletion on Try again", async () => {
      const unknown: DeletionFeedback = {
        kind: "unknown",
        message: "We couldn't confirm whether this conversation was deleted.",
        retryable: true,
        blocksRemoval: false,
      };
      const { props, user } = setup({ deletionFeedback: new Map([["A", unknown]]) });
      expect(screen.getByRole("alert")).toHaveTextContent(unknown.message);
      // Try again doesn't ask a second time: the user already confirmed.
      await user.click(screen.getByRole("button", { name: "Try again" }));
      expect(props.onDelete).toHaveBeenCalledExactlyOnceWith("A");
      expect(screen.queryByRole("group", { name: /Delete “Session A”/ })).toBeNull();
      await user.click(screen.getByRole("button", { name: "Dismiss the message about Session A" }));
      expect(props.onDismissDeletionFeedback).toHaveBeenCalledExactlyOnceWith("A");
    });

    it("holds a row whose deletion can't succeed, with the reason as its description", async () => {
      const refusal: DeletionFeedback = {
        kind: "migration_required",
        message: "This conversation is older than resumable deletion. Nothing was removed.",
        retryable: false,
        blocksRemoval: true,
      };
      const { props, user, view } = setup({ deletionFeedback: new Map([["A", refusal]]) });
      const trash = screen.getByRole("button", { name: "Delete Session A" });
      expect(trash).toHaveAttribute("aria-disabled", "true");
      expect(trash).toHaveAccessibleDescription(refusal.message);
      expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
      await user.click(trash);
      expect(screen.queryByRole("group", { name: /Delete “Session A”/ })).toBeNull();

      // Dismissed, the words leave the screen but still explain the action.
      view.rerender(
        <Sidebar {...props} deletionFeedback={new Map([["A", { ...refusal, dismissed: true }]])} />,
      );
      expect(screen.queryByRole("alert")).toBeNull();
      expect(trash).toHaveAccessibleDescription(refusal.message);
      await user.click(trash);
      expect(screen.queryByRole("group", { name: /Delete “Session A”/ })).toBeNull();

      // Control: the other conversation still asks and deletes.
      await user.click(screen.getByRole("button", { name: "Delete Session B" }));
      await user.click(screen.getByRole("button", { name: "Delete" }));
      expect(props.onDelete).toHaveBeenCalledExactlyOnceWith("B");
    });

    it("moves focus to the next conversation once a deleted row leaves the list", async () => {
      const sessions = [makeChatSession("A"), makeChatSession("B"), makeChatSession("C")];
      const { props, user, view } = setup({ sessions, activeId: null });
      await user.click(screen.getByRole("button", { name: "Delete Session B" }));
      await user.click(screen.getByRole("button", { name: "Delete" }));
      expect(screen.getByRole("button", { name: "Delete Session B" })).toHaveFocus();
      view.rerender(<Sidebar {...props} sessions={[sessions[0], sessions[2]]} />);
      expect(screen.getByRole("button", { name: "Session C" })).toHaveFocus();
    });
  });
});

