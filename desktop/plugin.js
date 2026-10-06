/**
 * Hermes Worker Monitor: desktop half.
 *
 * One footer item (STATUSBAR_AREAS.right) showing the Kanban board groups. The
 * running count carries a state dot, colored by the worst running worker
 * (looping beats stalled beats active).
 *
 * Hovering a count opens a popover with the cards behind that number. Only one
 * panel is ever open: every chip shares one open slot, so moving across the
 * strip closes the panel behind you. The rows are the app's own panel list row
 * (PanelListRow) with PanelPill and PanelSectionLabel, so a card reads here the
 * way it reads in the app's Kanban list and the two cannot drift. Clicking a
 * count, or a card, opens the board.
 *
 * This fork reads counts and card titles only. There is no quota item, so the
 * desktop half never asks the backend for provider data and no credential
 * reaches it.
 *
 * Polling goes through `ctx.rest` (namespace-relative paths only, see the
 * plugin contract) with React Query `refetchInterval`, so the timer is torn
 * down automatically when the item unmounts and the plugin is disabled or
 * hot-reloaded. A failed request never leaves stale numbers on screen. The card
 * list is fetched only while one of its popovers is open.
 *
 * Plain ESM, loaded uncompiled: the UI is `jsx()` calls, not JSX syntax. Only
 * `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime` resolve.
 */

import {
  PanelListRow,
  PanelPill,
  PanelSectionLabel,
  Popover,
  PopoverContent,
  PopoverTrigger,
  STATUSBAR_AREAS,
  atom,
  host,
  icons,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'hermes-worker-monitor'

// The board page inside the app. `host.navigate` drives the app router.
const BOARD_PATH = '/kanban'

// Polling cadence (ms). Never below 10s.
const SUMMARY_INTERVAL_MS = 10_000

// Cards requested per popover. The header always shows the group's real total
// from the same payload the strip counts, so a capped list never reads as the
// whole board.
const CARD_LIMIT = 25

// How long the panel survives the pointer leaving the chip. Without a grace
// period, the gap between the chip and the panel closes it on the way in.
const HOVER_CLOSE_MS = 140

// ONE popover at a time. Every chip reports into this shared slot, so taking it
// closes the one before it instead of leaving a row of panels open across the
// footer. A panel only ever HOLDS its own slot, it never takes one back, so
// moving out of an open panel onto another chip always switches the panel.
const $openSlot = atom(null)
let closeTimer = null
let closeToken = 0

function cancelClose() {
  if (closeTimer !== null) {
    clearTimeout(closeTimer)
    closeTimer = null
  }
  // Invalidates any timer still in flight, so a stale one cannot close a panel
  // that another chip just opened.
  closeToken += 1
}

/** Take the slot for `slot`, closing whatever was open. */
function openSlot(slot) {
  cancelClose()
  if ($openSlot.get() !== slot) $openSlot.set(slot)
}

/** Release the slot after the grace period, so the pointer can reach the panel. */
function closeSlotSoon() {
  cancelClose()
  const token = closeToken
  closeTimer = setTimeout(() => {
    closeTimer = null
    if (token === closeToken) $openSlot.set(null)
  }, HOVER_CLOSE_MS)
}

/** Release `slot` now: escape, an outside click, or a board navigation. */
function closeSlot(slot) {
  cancelClose()
  if ($openSlot.get() === slot) $openSlot.set(null)
}

function closeAnySlot() {
  cancelClose()
  if ($openSlot.get() !== null) $openSlot.set(null)
}

/**
 * The chip the pointer is over, from the pointer event itself. The strip owns
 * this decision, so the panel under the pointer cannot win it by being under
 * the pointer.
 */
function slotUnder(event) {
  const chip = event.target?.closest?.('[data-hwm-slot]')
  return chip ? chip.getAttribute('data-hwm-slot') : null
}

// Traffic-light colors for the running state. Mid-saturation hues that stay
// legible on both the light and dark themes.
const GREEN = '#30d158'
const AMBER = '#ff9f0a'
const RED = '#ff6b60'
const STATE_COLOR = { active: GREEN, stalled: AMBER, loop: RED }

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

const POPOVER_STYLE = { width: '22rem', maxWidth: '90vw' }
const POPOVER_BODY_STYLE = {
  display: 'flex',
  flexDirection: 'column',
  maxHeight: '19rem',
  overflowY: 'auto'
}
const POPOVER_NOTE_STYLE = { padding: '0.375rem', color: QUIET_COLOR, fontSize: '0.6875rem' }

// One entry per group: the chip's color and the popover's dot color, so a red
// count opens a red-dotted list.
const GROUPS = {
  blocked: { dot: RED, label: 'blocked', tone: 'bad' },
  waiting: { dot: AMBER, label: 'waiting', tone: 'warn' },
  running: { dot: GREEN, label: 'running', tone: 'good' },
  queued: { dot: LABEL_COLOR, label: 'queued', tone: 'muted' },
  scheduled: { dot: LABEL_COLOR, label: 'scheduled', tone: 'muted' },
  review: { dot: AMBER, label: 'review', tone: 'warn' },
  done: { dot: GREEN, label: 'done today', tone: 'good' }
}

// -- pure helpers -------------------------------------------------------------

function toCount(value) {
  return Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0
}

/** The groups the strip renders, as plain integers. */
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

function stateColor(state) {
  return STATE_COLOR[state] ?? GREEN
}

/** The clock a card's age is measured from: start, finish, or filing. */
function cardStamp(card) {
  if (card.status === 'running') return card.started_at || card.created_at
  if (card.status === 'done' || card.status === 'archived') return card.completed_at || card.created_at
  return card.created_at
}

/** Compact age for a row's trailing meta. Empty when the board has no clock. */
function ageLabel(stamp, now) {
  const seconds = Math.floor(Number(now) - Number(stamp))
  if (!Number.isFinite(seconds) || seconds < 0) return ''
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h`
  return `${Math.floor(seconds / 86_400)}d`
}

function cardTitle(card) {
  const title = typeof card.title === 'string' ? card.title.trim() : ''
  return title || String(card.id ?? 'untitled')
}

/** Row meta: the card id, then its age. Either half can be missing. */
function cardMeta(card, now) {
  return [String(card.id ?? ''), ageLabel(cardStamp(card), now)].filter(Boolean).join('  ')
}

// -- data hooks ---------------------------------------------------------------

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

/**
 * The cards behind one count. Fetched only while its popover is open, so an
 * idle statusbar stays silent. A connection whose plugin backend predates the
 * route answers 404, which lands here as `error` and the panel says so in one
 * line instead of showing a wrong or empty list.
 */
function useCards(ctx, group, enabled) {
  const query = useQuery({
    queryKey: [ID, 'cards', group],
    queryFn: () => ctx.rest(`/cards?group=${encodeURIComponent(group)}&limit=${CARD_LIMIT}`),
    enabled,
    retry: false,
    staleTime: 10_000
  })

  const failed = query.isError === true || query.isRefetchError === true
  return {
    cards: Array.isArray(query.data?.cards) ? query.data.cards : [],
    error: failed,
    loading: query.isPending === true,
    total: toCount(query.data?.total)
  }
}

// -- the popover --------------------------------------------------------------

/** One card, in the app's own list-row shape. */
function CardRow({ card, group, now, onOpen }) {
  const spec = GROUPS[group] ?? GROUPS.queued
  const dot = group === 'running' ? STATE_COLOR[card.worker_state] ?? spec.dot : spec.dot

  return jsx(PanelListRow, {
    active: false,
    lead: jsx('span', { style: { ...DOT_STYLE, backgroundColor: dot } }),
    meta: cardMeta(card, now),
    onSelect: onOpen,
    rowKey: String(card.id ?? cardTitle(card)),
    title: cardTitle(card)
  })
}

function PopoverBody({ cards, error, group, loading, now, onOpen, total }) {
  if (error) {
    return jsx('div', {
      style: POPOVER_NOTE_STYLE,
      children: 'This connection has no card list yet.'
    })
  }

  if (cards.length === 0) {
    return jsx('div', {
      style: POPOVER_NOTE_STYLE,
      children: loading ? 'Loading cards' : 'No cards in this group.'
    })
  }

  // Every component here is RENDERED (`jsx(Type, props)`), never CALLED as a
  // plain function: a component that owns hooks must keep a stable hook order,
  // and the strip's group set changes from render to render.
  const rows = cards.map(card =>
    jsx(CardRow, { card, group, key: String(card.id ?? cardTitle(card)), now, onOpen })
  )

  if (total > cards.length) {
    rows.push(
      jsx('div', {
        key: 'more',
        style: POPOVER_NOTE_STYLE,
        children: `${total - cards.length} more on the board`
      })
    )
  }

  return jsx('div', { style: POPOVER_BODY_STYLE, children: rows })
}

/**
 * A count chip that opens its cards on hover. The open slot is shared, so only
 * one panel is ever up. Radix opens the panel from the trigger, and the
 * enter/leave pair on BOTH halves keeps it open across the gap between them.
 * Focus opens it too, so the list is reachable from the keyboard.
 */
function CountWithCards({ ctx, count, group, icon, label, slot, tone, toneLabel, showDot }) {
  const spec = GROUPS[group] ?? GROUPS.queued
  const open = useValue($openSlot) === slot
  const { cards, error, loading, total } = useCards(ctx, group, open)

  const show = () => openSlot(slot)
  const hide = closeSlotSoon

  const openBoard = () => {
    closeSlot(slot)
    try {
      host.navigate(BOARD_PATH)
    } catch {
      // Bridge unavailable: never break the statusbar.
    }
  }

  const children = []
  if (showDot) children.push(jsx('span', { className: 'shrink-0', style: { ...DOT_STYLE, backgroundColor: tone ?? GREEN } }))
  else if (icon) children.push(icon)
  children.push(jsx('span', { className: 'tabular-nums font-medium', style: { color: tone }, children: String(count) }))
  if (label) children.push(jsx('span', { style: { color: toneLabel ?? LABEL_COLOR }, children: label }))

  const trigger = jsx('button', {
    'aria-label': `${label ?? group}: ${plural(count, 'card')}`,
    className: ITEM_CLASS,
    'data-hwm-slot': slot,
    onBlur: hide,
    onClick: openBoard,
    onFocus: show,
    onPointerEnter: show,
    onPointerLeave: hide,
    style: ITEM_STYLE,
    type: 'button',
    children
  })

  const panel = jsx(PopoverContent, {
    align: 'end',
    // A hover panel must never take focus away from the composer.
    onOpenAutoFocus: event => event.preventDefault(),
    // HOLD, never take: the panel keeps itself open while the pointer is on it,
    // but it cannot pull the slot back from the chip the pointer just moved to.
    onPointerEnter: cancelClose,
    onPointerLeave: hide,
    side: 'top',
    sideOffset: 8,
    style: POPOVER_STYLE,
    children: jsxs('div', {
      style: { display: 'flex', flexDirection: 'column' },
      children: [
        jsxs('div', {
          className: 'mb-1 flex items-center justify-between gap-2 px-1',
          children: [
            jsx(PanelSectionLabel, { children: spec.label }),
            jsx(PanelPill, { tone: spec.tone, children: String(total || count) })
          ]
        }),
        jsx(PopoverBody, {
          cards,
          error,
          group,
          loading,
          now: Math.floor(Date.now() / 1000),
          onOpen: openBoard,
          total: total || count
        })
      ]
    })
  })

  return jsxs(Popover, {
    onOpenChange: next => {
      if (!next) closeSlot(slot)
    },
    open,
    children: [jsx(PopoverTrigger, { asChild: true, children: trigger }), open ? panel : null]
  })
}

// -- the footer item ----------------------------------------------------------

function WorkerStrip({ ctx }) {
  // A slot left behind by a hot reload or a disable would open a panel on the
  // next mount with no pointer over it.
  useEffect(() => () => closeAnySlot(), [])

  const { data, error } = useSummary(ctx, '/summary', SUMMARY_INTERVAL_MS)

  const groups = readGroups(data)
  const workers = readWorkers(data)

  if (error) {
    return jsx('span', {
      style: STRIP_STYLE,
      children: jsx('button', {
        'aria-label': 'Board counts unavailable',
        className: ITEM_CLASS,
        onClick: () => {
          try {
            host.navigate(BOARD_PATH)
          } catch {
            // Bridge unavailable: never break the statusbar.
          }
        },
        style: ITEM_STYLE,
        type: 'button',
        children: [
          jsx(icons.Users, { className: 'shrink-0 size-3.5', style: { color: LABEL_COLOR } }),
          jsx('span', { style: { color: LABEL_COLOR }, children: 'n/a' })
        ]
      })
    })
  }

  const items = []

  if (groups.blocked > 0) {
    items.push(
      jsx(CountWithCards, {
        key: 'blocked',
        slot: 'blocked',
        ctx,
        count: groups.blocked,
        group: 'blocked',
        icon: jsx(icons.AlertTriangle, { className: 'shrink-0 size-3.5' }),
        label: 'blocked',
        tone: RED,
        toneLabel: RED
      })
    )
  }

  if (groups.waiting > 0) {
    items.push(
      jsx(CountWithCards, {
        key: 'waiting',
        slot: 'waiting',
        ctx,
        count: groups.waiting,
        group: 'waiting',
        icon: jsx(icons.Clock, { className: 'shrink-0 size-3.5', style: { color: QUIET_COLOR } }),
        label: 'waiting',
        tone: QUIET_COLOR
      })
    )
  }

  if (groups.running > 0) {
    items.push(
      jsx(CountWithCards, {
        key: 'running',
        slot: 'running',
        ctx,
        count: groups.running,
        group: 'running',
        label: 'running',
        showDot: true,
        tone: stateColor(workers.state),
        toneLabel: COUNT_COLOR
      })
    )
  }

  if (items.length > 0) items.push(jsx('span', { key: 'separator', style: SEPARATOR_STYLE }))

  if (groups.queued > 0) {
    items.push(
      jsx(CountWithCards, {
        key: 'queued',
        slot: 'queued',
        ctx,
        count: groups.queued,
        group: 'queued',
        icon: jsx(icons.CircleIcon, { className: 'shrink-0 size-3', style: { color: LABEL_COLOR } }),
        label: 'queued',
        tone: QUIET_COLOR
      })
    )
  }

  if (groups.scheduled > 0) {
    items.push(
      jsx(CountWithCards, {
        key: 'scheduled',
        slot: 'scheduled',
        ctx,
        count: groups.scheduled,
        group: 'scheduled',
        icon: jsx(icons.Clock, { className: 'shrink-0 size-3', style: { color: LABEL_COLOR } }),
        label: 'scheduled',
        tone: QUIET_COLOR
      })
    )
  }

  if (groups.review > 0) {
    items.push(
      jsx(CountWithCards, {
        key: 'review',
        slot: 'review',
        ctx,
        count: groups.review,
        group: 'review',
        icon: jsx(icons.Eye, { className: 'shrink-0 size-3', style: { color: LABEL_COLOR } }),
        label: 'review',
        tone: QUIET_COLOR
      })
    )
  }

  if (groups.doneToday > 0) {
    items.push(
      jsx(CountWithCards, {
        key: 'done',
        slot: 'done',
        ctx,
        count: groups.doneToday,
        group: 'done',
        icon: jsx(icons.CheckCircle2, { className: 'shrink-0 size-3', style: { color: LABEL_COLOR } }),
        label: 'done today',
        tone: QUIET_COLOR
      })
    )
  }

  // An idle board still says something: the backlog, or nothing at all.
  if (items.length === 0) {
    items.push(
      jsx(CountWithCards, {
        key: 'idle',
        slot: 'idle',
        ctx,
        count: 0,
        group: 'queued',
        icon: jsx(icons.Users, { className: 'shrink-0 size-3.5', style: { color: LABEL_COLOR } }),
        label: 'idle',
        tone: LABEL_COLOR
      })
    )
  }

  // The strip decides which chip is hovered, from the pointer event. A chip
  // handler alone loses the race whenever a panel sits over a neighbouring
  // chip: the pointer never reaches that chip, so the old panel stays up.
  return jsx('span', {
    onPointerLeave: closeSlotSoon,
    onPointerMove: event => {
      const slot = slotUnder(event)
      if (slot) openSlot(slot)
    },
    style: STRIP_STYLE,
    children: items
  })
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
