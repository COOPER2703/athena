import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useRef } from "react";

import { fetchStatus } from "../../api/client";
import { describeCode } from "../../api/errors";
import { queryKeys } from "../../api/queryClient";
import {
  fetchThread,
  flattenThread,
  THREAD_PAGE_SIZE,
  type ThreadPage,
} from "../../api/thread";
import { useDraft } from "../../shared/useDraft";
import { RefreshIcon } from "../../shared/icons";
import { Button } from "../../shared/ui/Button";
import { Notice } from "../../shared/ui/Notice";
import { Composer } from "./Composer";
import { LiveTurn } from "./LiveTurn";
import { Thread } from "./Thread";
import type { TurnIssue } from "./useTurn";
import { useAudioPlayer } from "./useAudioPlayer";
import { useMuted } from "./useMuted";
import { useTurn } from "./useTurn";
import { voiceSessionActive } from "./voiceSession";

/**
 * Conversation : un fil unique et une barre de saisie.
 *
 * Le fil vient du Journal (`GET /api/thread`, paginé vers le passé) ; le Tour
 * en cours vient du flux SSE de `POST /api/chat`. Dès qu'un Tour se termine, le
 * fil est relu et le Tour local disparaît : le message n'est jamais affiché
 * deux fois. L'audio est coupé par défaut et se joue après un geste.
 */
export function ConversationZone() {
  const thread = useInfiniteQuery({
    queryKey: queryKeys.thread,
    queryFn: ({ pageParam }) =>
      fetchThread({ before: pageParam, limit: THREAD_PAGE_SIZE }),
    initialPageParam: null as number | null,
    getNextPageParam: (lastPage: ThreadPage) => lastPage.nextBefore,
  });

  const occupancy = useQuery({
    queryKey: queryKeys.voiceSession,
    queryFn: async () => voiceSessionActive(await fetchStatus()),
    refetchInterval: 15_000,
    staleTime: 5_000,
  });

  const { muted, toggle: toggleMuted } = useMuted();
  const player = useAudioPlayer();

  const mutedRef = useRef(muted);
  mutedRef.current = muted;
  const handleAudio = useCallback(
    (pcm: Uint8Array) => {
      if (!mutedRef.current) {
        player.push(pcm);
      }
    },
    [player],
  );

  const refreshThread = useCallback(async () => {
    try {
      const result = await thread.refetch();
      return !result.isError;
    } catch {
      return false;
    }
  }, [thread.refetch]);

  const turn = useTurn({ onFinished: refreshThread, onAudio: handleAudio });

  const [draft, updateDraft] = useDraft("conversation");
  const restoredIssue = useRef<TurnIssue | null>(null);

  // Un refus ou une panne ne doit jamais faire perdre la saisie : le message
  // revient dans la barre, sans écraser un brouillon plus récent.
  useEffect(() => {
    const issue = turn.issue;
    if (issue === null || issue === restoredIssue.current) {
      return;
    }
    restoredIssue.current = issue;
    if (draft === "") {
      updateDraft(issue.message);
    }
  }, [turn.issue, draft, updateDraft]);

  // Couper le son arrête la lecture tout de suite, pas au morceau suivant.
  useEffect(() => {
    if (muted) {
      player.stop();
    }
  }, [muted, player]);

  const liveRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (turn.turn === null) {
      return;
    }
    const reduced =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    liveRef.current?.scrollIntoView({
      block: "end",
      behavior: reduced ? "auto" : "smooth",
    });
  }, [turn.turn?.message]);

  const handleSend = useCallback(() => {
    const text = draft.trim();
    if (text === "" || turn.streaming) {
      return;
    }
    if (!muted) {
      player.unlock();
    }
    player.stop();
    updateDraft("");
    turn.send(text);
  }, [draft, muted, player, turn, updateDraft]);

  const handleRetry = useCallback(() => {
    if (!muted) {
      player.unlock();
    }
    player.stop();
    updateDraft("");
    turn.retry();
  }, [muted, player, turn, updateDraft]);

  const handleCancel = useCallback(() => {
    turn.cancel();
    player.stop();
  }, [player, turn]);

  const handleToggleMute = useCallback(() => {
    if (muted) {
      // Le déblocage de l'audio doit venir d'un geste : c'est celui-ci.
      player.unlock();
    }
    toggleMuted();
  }, [muted, player, toggleMuted]);

  const entries = flattenThread(thread.data?.pages);
  const retentionDays = thread.data?.pages[0]?.retentionDays ?? 0;
  const voiceSession = occupancy.data === true;

  return (
    <div className="flex flex-col gap-5 px-5 py-6 sm:px-8 lg:px-10 lg:py-8">
      {voiceSession ? (
        <Notice tone="warning" title="Session vocale en cours">
          Athena tient une session vocale avec un Client : elle ne peut pas
          recevoir de message écrit en même temps. Votre envoi risque d'être
          refusé tant qu'elle dure.
        </Notice>
      ) : null}

      <Thread
        entries={entries}
        loading={thread.isPending}
        error={thread.error}
        hasMore={thread.hasNextPage}
        loadingMore={thread.isFetchingNextPage}
        onLoadMore={() => {
          void thread.fetchNextPage();
        }}
        onRetry={() => {
          void thread.refetch();
        }}
        retentionDays={retentionDays}
      />

      <div ref={liveRef}>
        {turn.turn !== null ? <LiveTurn turn={turn.turn} /> : null}
      </div>

      {turn.issue !== null ? (
        <Notice
          tone={turn.issue.kind === "busy" ? "warning" : "danger"}
          title={
            turn.issue.kind === "busy"
              ? "Athena est occupée"
              : "La réponse a échoué"
          }
        >
          <p>{describeCode(turn.issue.code)}</p>
          <div className="mt-3 flex flex-wrap gap-2">
            <Button
              variant="secondary"
              size="sm"
              onClick={handleRetry}
              icon={<RefreshIcon />}
            >
              Réessayer
            </Button>
            <Button variant="ghost" size="sm" onClick={turn.dismissIssue}>
              Fermer
            </Button>
          </div>
        </Notice>
      ) : null}

      <Composer
        value={draft}
        onChange={updateDraft}
        onSend={handleSend}
        onCancel={handleCancel}
        streaming={turn.streaming}
        muted={muted}
        onToggleMute={handleToggleMute}
      />
    </div>
  );
}
