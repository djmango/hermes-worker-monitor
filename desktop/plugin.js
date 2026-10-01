/**
 * Hermes Worker Monitor — desktop half.
 *
 * One footer item (STATUSBAR_AREAS.right): an animated spinner and the count of
 * running Kanban cards, colored by the worst worker state (loop > stalled >
 * active). Opening the menu lists each running card, its title, assignee,
 * runtime, last activity, and a link to Kanban.
 *
 * This fork is worker health only. There is no quota item, so the desktop half
 * never asks the backend for provider data and no credential ever reaches it.
 *
 * Polling goes through `ctx.rest` (namespace-relative paths only — see the
 * plugin contract) with React Query `refetchInterval`, so the timer is torn
 * down automatically when the item unmounts and the plugin is disabled or
 * hot-reloaded. Periodic polling never bypasses the backend cache. Opening the
 * menu re-fetches with `?fresh=1`.
 *
 * Plain ESM, loaded uncompiled — UI is `jsx()` calls, not JSX syntax. Only
 * `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime` resolve.
 */

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  GlyphSpinner,
  STATUSBAR_AREAS,
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
  icons,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useCallback, useEffect, useRef, useState } from 'react'

const ID = 'hermes-worker-monitor'

// Polling cadence (ms). Never below 10s.
const WORKER_INTERVAL_MS = 10_000

// Traffic-light colors for the worker states. Mid-saturation hues that stay
// legible on both the light and dark themes (spinner + status dots).
const GREEN = '#22c55e'
const ORANGE = '#f59e0b'
const RED = '#ef4444'

const STATE_COLOR = { active: GREEN, stalled: ORANGE, loop: RED }

// Shared chrome styling for interactive statusbar items (matches core).
const CHIP_CLASS =
  'inline-flex h-full items-center gap-1 whitespace-nowrap rounded-none px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground'

// Footer items lay their content out in a single row through inline styles,
// not through Tailwind arbitrary utilities. The desktop app ships a prebuilt
// Tailwind bundle: arbitrary utilities used only by plugins (e.g.
// `grid-cols-[auto_1.75rem_2.25rem_auto]`) are never generated, so an element
// left with `inline-grid` and no grid template collapses to a SINGLE implicit
// column — label, bar and value stack vertically and the statusbar (a few tens
// of px tall) clips the trailing rows. Inline styles always apply, so the row
// layout cannot silently regress.
const FOOTER_ROW = {
  display: 'inline-flex',
  flexDirection: 'row',
  alignItems: 'center',
  whiteSpace: 'nowrap'
}

// -- pure helpers -------------------------------------------------------------

function formatDuration(totalSeconds) {
  const s = Math.max(0, Math.floor(Number(totalSeconds) || 0))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${sec}s`
  return `${sec}s`
}

/** seconds-since-last-activity -> "Ns ago" / "Nm ago" / "Nh ago". */
function formatLastActivity(sec) {
  if (!Number.isFinite(sec)) return 'n/a'
  const s = Math.max(0, Math.floor(sec))
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  return `${Math.floor(m / 60)}h ago`
}

function worstWorkerState(workers) {
  if (workers.some(w => w.state === 'loop')) return 'loop'
  if (workers.some(w => w.state === 'stalled')) return 'stalled'
  return 'active'
}

// -- data hook ----------------------------------------------------------------

/**
 * Polls `path` at `intervalMs` through React Query (cached backend path), and
 * exposes `refreshFresh()` to re-fetch with `?fresh=1` when the menu opens.
 * A successful click refresh replaces the same query cache entry consumed by
 * the footer and menu, keeping both views on one shared snapshot.
 *
 * A failed request — poll or fresh — never leaves stale numbers on screen:
 * React Query keeps the last successful `data` across a failed refetch, so the
 * failure is turned into a neutral `null` snapshot plus `error: true` and the
 * chip renders its inactive state. Rejections stay contained here.
 */
function useMonitor(ctx, path, intervalMs) {
  const queryClient = useQueryClient()
  const queryKey = [ID, path]
  const query = useQuery({
    queryKey,
    queryFn: () => ctx.rest(path),
    refetchInterval: intervalMs,
    retry: false
  })

  const [freshFailed, setFreshFailed] = useState(false)
  const openRef = useRef(false)

  useEffect(() => () => {
    openRef.current = false
  }, [])

  // A successful poll supersedes a previous fresh-fetch failure, so the chip
  // returns to live data on its own.
  useEffect(() => {
    if (!query.isError) setFreshFailed(false)
  }, [query.isError, query.dataUpdatedAt])

  const refreshFresh = useCallback(() => {
    openRef.current = true
    let request
    try {
      request = ctx.rest(`${path}?fresh=1`)
    } catch {
      // Bridge threw synchronously: same neutral outcome as a rejection.
      setFreshFailed(true)
      return
    }
    Promise.resolve(request)
      .then(data => {
        // Replace the polling snapshot in the shared cache so the footer and
        // open menu always render the same response.
        queryClient.setQueryData([ID, path], data)
        if (openRef.current) setFreshFailed(false)
      })
      .catch(() => {
        if (!openRef.current) return
        setFreshFailed(true)
      })
  }, [ctx, path, queryClient])

  const close = useCallback(() => {
    openRef.current = false
    setFreshFailed(false)
  }, [])

  // `isRefetchError` covers the v5 background-refetch failure that keeps the
  // previous `data` around with `status: 'error'`.
  const failed = query.isError === true || query.isRefetchError === true || freshFailed
  const data = failed ? null : query.data

  return { data, error: failed, refreshFresh, close }
}

// -- shared chrome helpers ----------------------------------------------------

function withTooltip(trigger, label) {
  return jsxs(TooltipProvider, {
    delayDuration: 0,
    children: [
      jsxs(Tooltip, {
        children: [
          jsx(TooltipTrigger, { asChild: true, children: trigger }),
          jsx(TooltipContent, { children: label })
        ]
      })
    ]
  })
}

// -- Worker item --------------------------------------------------------------

function renderWorkerRow(w) {
  return jsx(DropdownMenuItem, {
    onSelect: event => event.preventDefault(),
    children: jsxs('div', {
      className: 'flex w-full min-w-0 flex-col gap-1',
      children: [
        jsxs('div', {
          className: 'grid w-full grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-2',
          children: [
            jsx('span', {
              className: 'size-1.5 shrink-0 rounded-full',
              style: { backgroundColor: STATE_COLOR[w.state] ?? GREEN }
            }),
            jsx('span', {
              className: 'min-w-0 flex-1 truncate font-medium',
              children: w.title || w.card_id || '(untitled)'
            }),
            jsx('span', {
              className: 'shrink-0 tabular-nums text-(--ui-text-quaternary)',
              children: formatDuration(w.duration_s)
            })
          ]
        }),
        jsxs('div', {
          className: 'grid w-full grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-2 pl-3.5 text-[0.625rem] text-(--ui-text-quaternary)',
          children: [
            jsx('span', { className: 'shrink-0 font-mono', children: w.card_id || '—' }),
            jsx('span', { className: 'min-w-0 flex-1 truncate', children: w.assignee || '—' }),
            jsx('span', { className: 'shrink-0', children: `last activity ${formatLastActivity(w.last_activity_s)}` })
          ]
        })
      ]
    })
  }, w.card_id)
}

function WorkerChip({ ctx }) {
  const { data, error, refreshFresh, close } = useMonitor(ctx, '/workers', WORKER_INTERVAL_MS)

  const workers = (Array.isArray(data?.workers) ? data.workers : []).filter(w => w && typeof w === 'object')
  const count = Number.isFinite(data?.count) ? data.count : workers.length
  const color = count > 0 ? STATE_COLOR[worstWorkerState(workers)] : null
  // The Kanban link comes from the first worker only; if it has no URL we omit
  // the entry entirely rather than borrowing another card's URL.
  const firstWorker = workers[0]
  const kanbanUrl =
    firstWorker && typeof firstWorker.kanban_url === 'string' && firstWorker.kanban_url
      ? firstWorker.kanban_url
      : null

  const statusLabel = error
    ? 'Workers: unavailable'
    : count > 0
      ? `Workers: ${count} active`
      : 'No active workers'

  const handleOpenChange = open => (open ? refreshFresh() : close())

  const trigger = jsx(DropdownMenuTrigger, {
    asChild: true,
    children: jsxs('button', {
      type: 'button',
      className: CHIP_CLASS,
      style: { ...FOOTER_ROW, gap: '0.25rem' },
      'aria-label': statusLabel,
      children:
        count > 0
          ? [
              jsx('span', {
                className: 'inline-flex items-center',
                style: { color },
                children: jsx(GlyphSpinner, { className: 'text-[0.8125rem]', ariaLabel: 'Active workers' })
              }),
              jsx('span', { className: 'tabular-nums text-(--ui-text-secondary)', children: String(count) })
            ]
          : [
              jsx(icons.Users, { className: 'size-3.5 text-(--ui-text-quaternary)' }),
              jsx('span', { className: 'tabular-nums text-(--ui-text-quaternary)', children: '0' })
            ]
    })
  })

  const menuChildren = [
    jsx('div', {
      className: 'px-2 pt-1 pb-0.5 text-[0.625rem] font-medium uppercase tracking-wide text-(--ui-text-quaternary)',
      children: error
        ? 'Workers n/a'
        : count > 0
          ? `${count} active worker${count === 1 ? '' : 's'}`
          : 'No active workers'
    }, 'header')
  ]

  if (count > 0) {
    for (const w of workers) menuChildren.push(renderWorkerRow(w))
  }

  if (kanbanUrl) {
    menuChildren.push(jsx(DropdownMenuSeparator, {}, 'sep'))
    menuChildren.push(
      jsxs(DropdownMenuItem, {
        onSelect: () => {
          try {
            Promise.resolve(ctx.os.openExternal(kanbanUrl)).catch(() => {})
          } catch {
            // Bridge unavailable: nothing to open, never break the menu.
          }
        },
        children: [jsx(icons.ExternalLink, { className: 'size-3.5' }), jsx('span', { children: 'Open Kanban' })]
      }, 'kanban')
    )
  }

  return jsxs(DropdownMenu, {
    onOpenChange: handleOpenChange,
    children: [
      withTooltip(trigger, statusLabel),
      jsx(DropdownMenuContent, {
        align: 'end',
        side: 'top',
        sideOffset: 8,
        style: { minWidth: '18rem' },
        children: menuChildren
      })
    ]
  })
}

// -- plugin contract ----------------------------------------------------------

export default {
  id: ID, // must match the folder name / manifest name
  name: 'Hermes Worker Monitor',
  // Unified-package desktop halves ship opt-in: the plugin inventories in
  // Capabilities → Plugins and stays off until the user flips the switch.
  defaultEnabled: false,
  register(ctx) {
    ctx.register({
      id: 'worker',
      area: STATUSBAR_AREAS.right,
      order: 140,
      render: () => jsx(WorkerChip, { ctx })
    })
  }
}
