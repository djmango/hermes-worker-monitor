/**
 * Hermes Worker Monitor: desktop half.
 *
 * One footer item (STATUSBAR_AREAS.right) showing the Kanban board groups. The
 * running count carries a state dot, colored by the worst running worker
 * (looping beats stalled beats active). Hover any count for the breakdown.
 * Click any count to open the Kanban board page inside the app.
 *
 * This fork reads counts only. There is no quota item, so the desktop half
 * never asks the backend for provider data and no credential reaches it.
 *
 * Polling goes through `ctx.rest` (namespace-relative paths only, see the
 * plugin contract) with React Query `refetchInterval`, so the timer is torn
 * down automatically when the item unmounts and the plugin is disabled or
 * hot-reloaded. A failed request never leaves stale numbers on screen.
 *
 * Plain ESM, loaded uncompiled: the UI is `jsx()` calls, not JSX syntax. Only
 * `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime` resolve.
 */

import {
  STATUSBAR_AREAS,
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
  host,
  icons,
  useQuery
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'hermes-worker-monitor'

// The board page inside the app. `host.navigate` drives the app router.
const BOARD_PATH = '/kanban'

// Polling cadence (ms). Never below 10s.
const SUMMARY_INTERVAL_MS = 10_000

// Board groups that need a person before anything else moves.
const NEEDS_ACTION = ['blocked', 'waiting']

// Traffic-light colors for the running state. Mid-saturation hues that stay
// legible on both the light and dark themes.
const GREEN = '#30d158'
const AMBER = '#ff9f0a'
const RED = '#ff6b60'
const STATE_COLOR = { active: GREEN, stalled: AMBER, loop: RED }

const CLICK_HINT = 'Click for the board'

// Shared chrome styling for interactive statusbar items (matches core). Core
// already uses every class here, so the prebuilt Tailwind bundle carries them.
const ITEM_CLASS =
  'inline-flex items-center gap-1 whitespace-nowrap rounded-md px-1.5 text-[0.6875rem] transition-colors hover:bg-(--chrome-action-hover)'

// Row layout lives in inline styles. The desktop app ships a prebuilt Tailwind
// bundle: arbitrary utilities used only by plugins are never generated, so a
// layout that leans on them can silently collapse inside the statusbar. Inline
// styles always apply.
const ITEM_STYLE = { height: '1.25rem' }
const STRIP_STYLE = {
  display: 'inline-flex',
  flexDirection: 'row',
  alignItems: 'center',
  whiteSpace: 'nowrap',
  gap: '0.5rem'
}
const SEPARATOR_STYLE = {
  width: '1px',
  height: '0.6875rem',
  flex: '0 0 auto',
  backgroundColor: 'var(--ui-stroke-quaternary)'
}
const DOT_STYLE = { width: '0.375rem', height: '0.375rem', flex: '0 0 auto', borderRadius: '999px' }
const LABEL_COLOR = 'var(--ui-text-quaternary)'
const COUNT_COLOR = 'var(--ui-text-secondary)'
const QUIET_COLOR = 'var(--ui-text-tertiary)'

// -- pure helpers -------------------------------------------------------------

function toCount(value) {
  return Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0
}

/** The five groups the strip renders, as plain integers. */
function readGroups(data) {
  const source = data && typeof data.groups === 'object' && data.groups !== null ? data.groups : {}
  return {
    blocked: toCount(source.blocked),
    waiting: toCount(source.waiting),
    running: toCount(source.running),
    queued: toCount(source.queued),
    scheduled: toCount(source.scheduled),
    review: toCount(source.review),
    doneToday: toCount(source.done_today)
  }
}

function readWorkers(data) {
  const source = data && typeof data.workers === 'object' && data.workers !== null ? data.workers : {}
  const active = toCount(source.active)
  const stalled = toCount(source.stalled)
  const looping = toCount(source.looping)
  // The dot color follows the worst live state, and the state the backend
  // reports wins. A malformed state still reads as active rather than blank.
  const state = source.state === 'loop' || source.state === 'stalled' ? source.state : 'active'
  return { active, stalled, looping, state }
}

function plural(count, noun) {
  return `${count} ${noun}${count === 1 ? '' : 's'}`
}

/** "10 running: 8 active, 1 stalled, 1 looping." with only live numbers. */
function runningBreakdown(count, workers) {
  const parts = [`${workers.active} active`]
  if (workers.stalled > 0) parts.push(plural(workers.stalled, 'stalled'))
  if (workers.looping > 0) parts.push(plural(workers.looping, 'looping'))
  return `${count} running: ${parts.join(', ')}`
}

function stateColor(state) {
  return STATE_COLOR[state] ?? GREEN
}

// -- data hook ----------------------------------------------------------------

/**
 * Polls `path` at `intervalMs` through React Query on the cached backend path.
 * A failed poll turns the snapshot into `null` rather than leaving the last
 * successful numbers on screen, so the strip never shows stale counts. The
 * rejection stays contained here.
 */
function useSummary(ctx, path, intervalMs) {
  const query = useQuery({
    queryKey: [ID, path],
    queryFn: () => ctx.rest(path),
    refetchInterval: intervalMs,
    retry: false
  })

  // `isRefetchError` covers the background-refetch failure that keeps the
  // previous `data` around with `status: 'error'`.
  const failed = query.isError === true || query.isRefetchError === true
  return { data: failed ? null : query.data, error: failed }
}

// -- chrome helpers -----------------------------------------------------------

function withTooltip(trigger, label, key) {
  return jsxs(
    TooltipProvider,
    {
      delayDuration: 0,
      children: [
        jsxs(Tooltip, {
          children: [
            jsx(TooltipTrigger, { asChild: true, children: trigger }),
            jsx(TooltipContent, { children: label })
          ]
        })
      ]
    },
    key
  )
}

/** One clickable count: an optional dot or icon, the number, then the label. */
function countItem({ ctx, key, dot, icon, count, label, tooltip, tone, toneLabel }) {
  const children = []
  if (dot) children.push(jsx('span', { className: 'shrink-0', style: { ...DOT_STYLE, backgroundColor: dot } }))
  else if (icon) children.push(icon)
  children.push(jsx('span', { className: 'tabular-nums font-medium', style: { color: tone }, children: String(count) }))
  if (label) children.push(jsx('span', { style: { color: toneLabel ?? LABEL_COLOR }, children: label }))

  const trigger = jsx('button', {
    type: 'button',
    className: ITEM_CLASS,
    style: ITEM_STYLE,
    'aria-label': tooltip,
    onClick: () => {
      try {
        host.navigate(BOARD_PATH)
      } catch {
        // Bridge unavailable: never break the statusbar.
      }
    },
    children
  })

  return withTooltip(trigger, tooltip, key)
}

// -- the footer item ----------------------------------------------------------

function WorkerStrip({ ctx }) {
  const { data, error } = useSummary(ctx, '/summary', SUMMARY_INTERVAL_MS)

  const groups = readGroups(data)
  const workers = readWorkers(data)

  if (error) {
    return jsx('span', {
      style: STRIP_STYLE,
      children: countItem({
        ctx,
        key: 'unavailable',
        icon: jsx(icons.Users, { className: 'shrink-0 size-3.5', style: { color: LABEL_COLOR } }),
        count: 0,
        label: 'n/a',
        tooltip: 'Board counts unavailable',
        tone: LABEL_COLOR
      })
    })
  }

  const items = []

  if (groups.blocked > 0) {
    items.push(
      countItem({
        ctx,
        key: 'blocked',
        icon: jsx(icons.AlertTriangle, { className: 'shrink-0 size-3.5' }),
        count: groups.blocked,
        label: 'blocked',
        tooltip: `${plural(groups.blocked, 'card')} stopped and needs a person. ${CLICK_HINT}`,
        tone: RED,
        toneLabel: RED
      })
    )
  }

  if (groups.waiting > 0) {
    items.push(
      countItem({
        ctx,
        key: 'waiting',
        icon: jsx(icons.Clock, { className: 'shrink-0 size-3.5', style: { color: QUIET_COLOR } }),
        count: groups.waiting,
        label: 'waiting',
        tooltip: `${plural(groups.waiting, 'card')} blocked on another card, clears on its own. ${CLICK_HINT}`,
        tone: QUIET_COLOR
      })
    )
  }

  if (groups.running > 0) {
    items.push(
      countItem({
        ctx,
        key: 'running',
        dot: stateColor(workers.state),
        count: groups.running,
        label: 'running',
        tooltip: `${runningBreakdown(groups.running, workers)}. ${CLICK_HINT}`,
        tone: COUNT_COLOR
      })
    )
  }

  if (items.length > 0) items.push(jsx('span', { key: 'separator', style: SEPARATOR_STYLE }))

  if (groups.queued > 0) {
    items.push(
      countItem({
        ctx,
        key: 'queued',
        icon: jsx(icons.CircleIcon, { className: 'shrink-0 size-3', style: { color: LABEL_COLOR } }),
        count: groups.queued,
        label: 'queued',
        tooltip: `${plural(groups.queued, 'card')} waiting for a worker. ${CLICK_HINT}`,
        tone: QUIET_COLOR
      })
    )
  }

  if (groups.scheduled > 0) {
    items.push(
      countItem({
        ctx,
        key: 'scheduled',
        icon: jsx(icons.Clock, { className: 'shrink-0 size-3', style: { color: LABEL_COLOR } }),
        count: groups.scheduled,
        label: 'scheduled',
        tooltip: `${plural(groups.scheduled, 'card')} scheduled. ${CLICK_HINT}`,
        tone: QUIET_COLOR
      })
    )
  }

  if (groups.review > 0) {
    items.push(
      countItem({
        ctx,
        key: 'review',
        icon: jsx(icons.Eye, { className: 'shrink-0 size-3', style: { color: LABEL_COLOR } }),
        count: groups.review,
        label: 'review',
        tooltip: `${plural(groups.review, 'card')} waiting for review. ${CLICK_HINT}`,
        tone: QUIET_COLOR
      })
    )
  }

  if (groups.doneToday > 0) {
    items.push(
      countItem({
        ctx,
        key: 'done',
        icon: jsx(icons.CheckCircle2, { className: 'shrink-0 size-3', style: { color: LABEL_COLOR } }),
        count: groups.doneToday,
        label: 'done today',
        tooltip: `${plural(groups.doneToday, 'card')} finished today. ${CLICK_HINT}`,
        tone: QUIET_COLOR
      })
    )
  }

  // An idle board still says something: the backlog, or nothing at all.
  if (items.length === 0) {
    items.push(
      countItem({
        ctx,
        key: 'idle',
        icon: jsx(icons.Users, { className: 'shrink-0 size-3.5', style: { color: LABEL_COLOR } }),
        count: 0,
        label: 'idle',
        tooltip: `No cards running or blocked. ${CLICK_HINT}`,
        tone: LABEL_COLOR
      })
    )
  }

  return jsx('span', { style: STRIP_STYLE, children: items })
}

// -- plugin contract ----------------------------------------------------------

export default {
  id: ID, // must match the folder name / manifest name
  name: 'Hermes Worker Monitor',
  // On by default: the strip is the point of this plugin and it reads counts
  // only, so it should not need a switch flip to appear. The user can still
  // turn it off in Capabilities, Plugins.
  defaultEnabled: true,
  register(ctx) {
    ctx.register({
      id: 'summary',
      area: STATUSBAR_AREAS.right,
      order: 140,
      render: () => jsx(WorkerStrip, { ctx })
    })
  }
}
