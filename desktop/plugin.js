/**
 * Hermes Worker Monitor: desktop half.
 *
 * One footer item (STATUSBAR_AREAS.right) showing the Kanban board groups. The
 * running count carries a state dot, colored by the worst running worker
 * (looping beats stalled beats active).
 *
 * Hovering a count opens a popover with the cards behind that number. Hovering
 * one of those rows opens a second panel beside it holding the whole card: the
 * description, the metadata, the newest run, the recent comments and the
 * attachments, which is what the board's own drawer shows. Both panels are made
 * of the app's own parts (PanelListRow, PanelPill, PanelSectionLabel, PanelMeta,
 * PanelBlock and the chat markdown renderer), so a card reads here the way it
 * reads on the Kanban page and the two cannot drift.
 *
 * Only one of each is up at a time, and both stay up while the pointer is on
 * them, so a card can be read and clicked without leaving the page. Clicking a
 * count, or a card, opens the board. The card panels are read-only: a plugin
 * cannot reach the Kanban plugin's own API, so the actions (complete, block,
 * comment, attach) still live in the board's drawer.
 *
 * A chip is an icon and a count, nothing else: the group's name lives in the
 * panel header, and the chips sit close together.
 *
 * This fork reads counts, card fields and the one card's body. There is no quota
 * item, so the desktop half never asks the backend for provider data and no
 * credential reaches it.
 *
 * Polling goes through `ctx.rest` (namespace-relative paths only, see the
 * plugin contract) with React Query `refetchInterval`, so the timer is torn
 * down automatically when the item unmounts and the plugin is disabled or
 * hot-reloaded. A failed request never leaves stale numbers on screen. The card
 * list is fetched only while its panel is open, and one card's detail only while
 * its row is hovered.
 *
 * Plain ESM, loaded uncompiled: the UI is `jsx()` calls, not JSX syntax. Only
 * `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime` resolve.
 */

import {
  Button,
  MessageTextContent,
  PanelBlock,
  PanelListRow,
  PanelMeta,
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
import { Component, useEffect } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'hermes-worker-monitor'

// The board page inside the app. `host.navigate` drives the app router.
const BOARD_PATH = '/kanban'

// Polling cadence (ms). Never below 10s.
const SUMMARY_INTERVAL_MS = 10_000

// Cards requested per panel. The header always shows the group's real total
// from the same payload the strip counts, so a capped list never reads as the
// whole board.
const CARD_LIMIT = 25

// How long a panel waits for the pointer to reach it. The timer is a heartbeat,
// not a countdown: while pointer events keep arriving on a chip, the strip, a
// panel or the strip's own panel, the hover stays, however slow the path is.
// The panel sits one small gap away, and a pointer that crosses that gap is
// mid-journey, not gone.
const HOVER_CLOSE_MS = 700

// ONE hover at a time: the open chip and, inside it, the row whose card panel is
// up. Every part reports into this one atom, so moving anywhere else closes what
// it left behind instead of leaving a row of panels across the screen.
const $hover = atom(null)
let closeTimer = null
let lastInsideAt = 0

function clearCloseTimer() {
  if (closeTimer !== null) {
    clearTimeout(closeTimer)
    closeTimer = null
  }
}

/** The pointer is on our chrome: hold the hover. */
function markInside() {
  lastInsideAt = Date.now()
  clearCloseTimer()
}

/** Take the chip's slot, closing whatever was open. */
function hoverSlot(slot) {
  markInside()
  const hover = $hover.get()
  if (hover === null || hover.slot !== slot || hover.card !== null) {
    $hover.set({ slot, card: null })
  }
}

/** Take the card, keeping its chip open behind it. */
function hoverCard(slot, card) {
  markInside()
  const hover = $hover.get()
  if (hover === null || hover.slot !== slot || hover.card !== card) {
    $hover.set({ slot, card })
  }
}

/** Drop the card panel but keep the list it came from. */
function clearCard() {
  clearCloseTimer()
  const hover = $hover.get()
  if (hover !== null && hover.card !== null) {
    $hover.set({ slot: hover.slot, card: null })
  }
}

/**
 * Ask for the hover to be released. The timer re-checks the heartbeat first, so
 * a timer left over from another chip can never close a panel the pointer has
 * since moved onto, and a slow journey onto a panel never loses the race.
 */
function releaseSlot() {
  clearCloseTimer()
  closeTimer = setTimeout(() => {
    closeTimer = null
    if (Date.now() - lastInsideAt < HOVER_CLOSE_MS) {
      releaseSlot()
      return
    }
    $hover.set(null)
  }, HOVER_CLOSE_MS)
}

/** Release the hover now: escape, an outside click, or a board navigation. */
function closeHover() {
  clearCloseTimer()
  if ($hover.get() !== null) $hover.set(null)
}

/**
 * What the pointer is over, from the pointer event itself. The strip owns this
 * decision, so a panel under the pointer cannot win it by being under the
 * pointer.
 */
function slotUnder(event) {
  const chip = event.target?.closest?.('[data-hwm-slot]')
  return chip ? chip.getAttribute('data-hwm-slot') : null
}

function cardUnder(event) {
  const row = event.target?.closest?.('[data-hwm-card]')
  return row ? row.getAttribute('data-hwm-card') : null
}

// Traffic-light colors for the running state. Mid-saturation hues that stay
// legible on both the light and dark themes.
const GREEN = '#30d158'
const AMBER = '#ff9f0a'
const RED = '#ff6b60'
const STATE_COLOR = { active: GREEN, stalled: AMBER, loop: RED }

// Shared chrome styling for interactive statusbar items (matches core). Core
// already uses both classes here, so the prebuilt Tailwind bundle carries them.
const ITEM_CLASS = 'whitespace-nowrap rounded-md transition-colors hover:bg-(--chrome-action-hover)'

// Row layout lives in inline styles. The desktop app ships a prebuilt Tailwind
// bundle: arbitrary utilities used only by plugins are never generated, so a
// layout that leans on them can silently collapse inside the statusbar. Inline
// styles always apply.
//
// The strip is icons and counts only, so the chips sit as close as they can
// while still reading as separate targets.
const ITEM_STYLE = {
  alignItems: 'center',
  display: 'inline-flex',
  fontSize: '0.6875rem',
  gap: '0.1875rem',
  height: '1.25rem',
  padding: '0 0.25rem'
}
const STRIP_STYLE = {
  display: 'inline-flex',
  flexDirection: 'row',
  alignItems: 'center',
  whiteSpace: 'nowrap',
  gap: '0.125rem'
}
const SEPARATOR_STYLE = {
  width: '1px',
  height: '0.6875rem',
  flex: '0 0 auto',
  backgroundColor: 'var(--ui-stroke-quaternary)'
}
const DOT_STYLE = { width: '0.375rem', height: '0.375rem', flex: '0 0 auto', borderRadius: '999px' }
const LABEL_COLOR = 'var(--ui-text-quaternary)'
const QUIET_COLOR = 'var(--ui-text-tertiary)'

const POPOVER_STYLE = { width: '22rem', maxWidth: '90vw' }
const POPOVER_BODY_STYLE = {
  display: 'flex',
  flexDirection: 'column',
  maxHeight: '19rem',
  overflowY: 'auto'
}
const POPOVER_NOTE_STYLE = { color: QUIET_COLOR, fontSize: '0.6875rem', lineHeight: 1.4, padding: '0.375rem' }
const COLUMN_STYLE = { display: 'flex', flexDirection: 'column' }
const STACK_STYLE = { display: 'flex', flexDirection: 'column', gap: '0.375rem' }
const SECTION_STYLE = { display: 'flex', flexDirection: 'column', gap: '0.25rem' }

const PREVIEW_STYLE = { width: '26rem', maxWidth: '92vw' }
const PREVIEW_BODY_STYLE = {
  display: 'flex',
  flexDirection: 'column',
  gap: '0.625rem',
  maxHeight: '26rem',
  overflowY: 'auto'
}
const PREVIEW_TITLE_STYLE = { fontSize: '0.8125rem', fontWeight: 600, lineHeight: 1.35 }
const PILL_ROW_STYLE = { alignItems: 'center', display: 'flex', flexWrap: 'wrap', gap: '0.25rem' }

// One entry per group, keyed by the backend's own group name: the chip's color
// and the panels' dot color, so a red count opens a red-dotted list. The word
// lives in the panel header, never on the chip.
const GROUPS = {
  blocked: { dot: RED, label: 'blocked', tone: 'bad' },
  waiting: { dot: AMBER, label: 'waiting', tone: 'warn' },
  running: { dot: GREEN, label: 'running', tone: 'good' },
  queued: { dot: LABEL_COLOR, label: 'queued', tone: 'muted' },
  scheduled: { dot: LABEL_COLOR, label: 'scheduled', tone: 'muted' },
  review: { dot: AMBER, label: 'review', tone: 'warn' },
  done_today: { dot: GREEN, label: 'done today', tone: 'good' }
}

const STATUS_TONE = { blocked: 'bad', cancelled: 'bad', done: 'good', running: 'good', review: 'warn' }

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

function statusTone(status) {
  return STATUS_TONE[status] ?? 'muted'
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

/** The same stamp with the wall clock in front of it, for a detail panel. */
function stampLabel(stamp, now) {
  const value = Number(stamp)
  if (!Number.isFinite(value) || value <= 0) return ''
  const age = ageLabel(value, now)
  const wall = new Date(value * 1000).toLocaleString()
  return age ? `${wall}  (${age})` : wall
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
 * The cards behind one count. Fetched only while its panel is open, so an idle
 * statusbar stays silent. A connection whose plugin backend predates the route
 * answers 404, which lands here as `error` and the panel says so in one line
 * instead of showing a wrong or empty list.
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

/** One card in full, fetched only while its row is hovered. */
function useCardDetail(ctx, id, enabled) {
  const query = useQuery({
    queryKey: [ID, 'card', id],
    queryFn: () => ctx.rest(`/card?id=${encodeURIComponent(id)}`),
    enabled,
    retry: false,
    staleTime: 15_000
  })

  const failed = query.isError === true || query.isRefetchError === true
  const data = failed ? null : query.data
  return {
    attachments: data?.attachments ?? { count: 0, names: [] },
    card: data?.card ?? null,
    comments: data?.comments ?? { count: 0, recent: [] },
    error: failed,
    loading: query.isPending === true,
    run: data?.run ?? null
  }
}

// -- the card panel -----------------------------------------------------------

/**
 * The card's own text goes through the app's chat markdown renderer. A renderer
 * that cannot mount anywhere else must not take the whole footer item down with
 * it, so the fallback is the raw text in the app's code block.
 */
class PreviewBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { failed: false }
  }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children
  }
}

function Section({ children, label }) {
  return jsxs('div', { style: SECTION_STYLE, children: [jsx(PanelSectionLabel, { children: label }), children] })
}

/** The card's own fields, in the order the drawer shows them. */
function metaRows(card, now) {
  const rows = []
  const push = (label, value) => {
    if (value === null || value === undefined || value === '' || value === 0) return
    rows.push({ label, value: String(value) })
  }

  push('id', card.id)
  push('assignee', card.assignee)
  push('created by', card.created_by)
  push('created', stampLabel(card.created_at, now))
  push('started', stampLabel(card.started_at, now))
  push('completed', stampLabel(card.completed_at, now))
  push('blocked', card.block_kind)
  push('re-blocked', card.block_recurrences ? `${card.block_recurrences} times` : '')
  push('failures', card.consecutive_failures ? `${card.consecutive_failures} in a row` : '')
  push('retries', card.max_retries)
  push('project', card.project_id)
  push('workspace', [card.workspace_kind, card.workspace_path].filter(Boolean).join('  '))
  push('branch', card.branch_name)
  push('model', [card.provider_override, card.model_override].filter(Boolean).join('  '))
  push('goal loop', card.goal_mode ? 'on' : '')
  push('contract', card.completion_contract)
  return rows
}

function runBody(run, now) {
  const rows = []
  if (run.status || run.outcome) {
    rows.push({ label: 'outcome', value: [run.status, run.outcome].filter(Boolean).join('  ') })
  }
  if (run.profile) rows.push({ label: 'profile', value: String(run.profile) })
  if (run.started_at) rows.push({ label: 'ran', value: stampLabel(run.started_at, now) })
  if (run.ended_at) rows.push({ label: 'ended', value: stampLabel(run.ended_at, now) })

  return jsxs('div', {
    style: STACK_STYLE,
    children: [
      rows.length > 0 ? jsx(PanelMeta, { rows }) : null,
      run.summary ? jsx(PanelBlock, { children: run.summary }) : null,
      run.error ? jsx(PanelBlock, { children: run.error }) : null
    ].filter(Boolean)
  })
}

function commentsBody(comments) {
  return jsx('div', {
    style: STACK_STYLE,
    children: comments.recent.map((item, index) =>
      jsxs(
        'div',
        {
          key: `${item.author}-${String(item.created_at ?? index)}`,
          style: { display: 'flex', flexDirection: 'column', gap: '0.125rem' }
        },
        [
          jsx('span', { style: { color: LABEL_COLOR, fontSize: '0.625rem' }, children: item.author || 'someone' }),
          jsx('span', { style: { color: QUIET_COLOR, fontSize: '0.6875rem', lineHeight: 1.4 }, children: item.body })
        ]
      )
    )
  })
}

/**
 * One card, whole, the way the board's drawer shows it: the title and its pills,
 * the fields, the description, the newest run, the recent comments and the
 * attachments. Read-only, and it says where the actions are instead of faking
 * them: a plugin cannot reach the Kanban plugin's own API.
 */
function CardPreview({ detail, onOpen }) {
  if (!detail.card) {
    return jsx('div', {
      style: POPOVER_NOTE_STYLE,
      children: detail.error ? 'No card detail from this connection.' : 'Loading the card'
    })
  }

  const card = detail.card
  const now = Math.floor(Date.now() / 1000)

  const pills = [
    jsx(PanelPill, { key: 'status', tone: statusTone(card.status), children: String(card.status ?? '') }),
    card.priority ? jsx(PanelPill, { key: 'priority', tone: 'muted', children: `p${card.priority}` }) : null,
    card.goal_mode ? jsx(PanelPill, { key: 'goal', tone: 'muted', children: 'goal loop' }) : null
  ].filter(Boolean)

  const body = typeof card.body === 'string' ? card.body.trim() : ''

  const sections = [
    jsxs(
      'div',
      {
        key: 'head',
        style: STACK_STYLE,
        children: [
          jsx('div', { style: PREVIEW_TITLE_STYLE, children: cardTitle(card) }),
          jsx('div', { style: PILL_ROW_STYLE, children: pills })
        ]
      }
    ),
    jsx(PanelMeta, { key: 'meta', rows: metaRows(card, now) })
  ]

  if (body) {
    sections.push(
      jsx(
        Section,
        {
          key: 'body',
          label: 'description',
          children: jsx(PreviewBoundary, {
            fallback: jsx(PanelBlock, { children: body }),
            children: jsx(MessageTextContent, { media: false, text: body })
          })
        }
      )
    )
  }

  if (card.result) {
    sections.push(jsx(Section, { key: 'result', label: 'result', children: jsx(PanelBlock, { children: card.result }) }))
  }

  if (card.last_failure_error) {
    sections.push(
      jsx(Section, {
        key: 'failure',
        label: 'last failure',
        children: jsx(PanelBlock, { children: card.last_failure_error })
      })
    )
  }

  if (detail.run) {
    sections.push(jsx(Section, { key: 'run', label: 'latest run', children: runBody(detail.run, now) }))
  }

  if (detail.comments.count > 0) {
    sections.push(
      jsx(Section, {
        key: 'comments',
        label: `comments (${detail.comments.count})`,
        children: commentsBody(detail.comments)
      })
    )
  }

  if (detail.attachments.count > 0) {
    sections.push(
      jsx(Section, {
        key: 'attachments',
        label: `attachments (${detail.attachments.count})`,
        children: jsx('div', { style: POPOVER_NOTE_STYLE, children: detail.attachments.names.join('\n') })
      })
    )
  }

  sections.push(
    jsx('div', {
      key: 'open',
      children: jsx(Button, { onClick: onOpen, size: 'sm', variant: 'outline', children: 'Open on the board' })
    })
  )

  return jsx('div', { style: PREVIEW_BODY_STYLE, children: sections })
}

/**
 * One card row. Hovering it opens the card beside the row, which is what makes
 * the pair read like a side panel instead of a modal: the pointer travels from
 * the row onto the card panel and back without either closing.
 */
function CardRow({ ctx, card, group, now, onOpen, slot }) {
  const spec = GROUPS[group] ?? GROUPS.queued
  const hover = useValue($hover)
  const open = hover !== null && hover.slot === slot && hover.card === card.id
  const detail = useCardDetail(ctx, card.id, open)
  const dot = group === 'running' ? STATE_COLOR[card.worker_state] ?? spec.dot : spec.dot

  const row = jsx(PanelListRow, {
    active: open,
    lead: jsx('span', { style: { ...DOT_STYLE, backgroundColor: dot } }),
    meta: cardMeta(card, now),
    onSelect: onOpen,
    rowKey: String(card.id ?? cardTitle(card)),
    title: cardTitle(card)
  })

  const trigger = jsx('div', {
    'data-hwm-card': String(card.id ?? ''),
    onPointerEnter: () => hoverCard(slot, card.id),
    children: row
  })

  const panel = jsx(PopoverContent, {
    align: 'start',
    // A hover panel must never take focus away from the composer.
    onOpenAutoFocus: event => event.preventDefault(),
    onPointerEnter: markInside,
    onPointerLeave: releaseSlot,
    onPointerMove: markInside,
    side: 'right',
    sideOffset: 6,
    style: PREVIEW_STYLE,
    children: jsx(CardPreview, { detail, onOpen })
  })

  return jsxs(Popover, {
    onOpenChange: next => {
      if (!next) clearCard()
    },
    open,
    children: [jsx(PopoverTrigger, { asChild: true, children: trigger }), open ? panel : null]
  })
}

function PopoverBody({ cards, ctx, error, group, loading, now, onOpen, slot, total }) {
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
    jsx(CardRow, { card, ctx, group, key: String(card.id ?? cardTitle(card)), now, onOpen, slot })
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
 * A count chip that opens its cards on hover. The hover is shared, so only one
 * panel of each kind is ever up, and both stay up while the pointer is on them.
 * Focus opens the list too, so it is reachable from the keyboard.
 */
function CountWithCards({ ctx, count, group, icon, label, slot, tone, showDot }) {
  const spec = GROUPS[group] ?? GROUPS.queued
  const hover = useValue($hover)
  const open = hover !== null && hover.slot === slot
  const { cards, error, loading, total } = useCards(ctx, group, open)

  const show = () => hoverSlot(slot)

  const openBoard = () => {
    closeHover()
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

  const trigger = jsx('button', {
    'aria-label': `${label ?? group}: ${plural(count, 'card')}`,
    className: ITEM_CLASS,
    'data-hwm-slot': slot,
    onBlur: releaseSlot,
    onClick: openBoard,
    onFocus: show,
    onPointerEnter: show,
    onPointerLeave: releaseSlot,
    onPointerMove: markInside,
    style: ITEM_STYLE,
    type: 'button',
    children
  })

  const panel = jsx(PopoverContent, {
    align: 'end',
    // A hover panel must never take focus away from the composer.
    onOpenAutoFocus: event => event.preventDefault(),
    // HOLD, never take: the panel keeps itself open while the pointer is on it,
    // but it cannot pull the hover back from the chip the pointer moved to.
    onPointerEnter: markInside,
    onPointerLeave: releaseSlot,
    side: 'top',
    sideOffset: 6,
    style: POPOVER_STYLE,
    children: jsxs('div', {
      // The list owns which row is hovered, from the pointer event: leaving a
      // row for the header closes the card panel without closing the list.
      onPointerMove: event => {
        const card = cardUnder(event)
        if (card) hoverCard(slot, card)
        else {
          markInside()
          clearCard()
        }
      },
      style: COLUMN_STYLE,
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
          ctx,
          error,
          group,
          loading,
          now: Math.floor(Date.now() / 1000),
          onOpen: openBoard,
          slot,
          total: total || count
        })
      ]
    })
  })

  return jsxs(Popover, {
    onOpenChange: next => {
      if (!next) closeHover()
    },
    open,
    children: [jsx(PopoverTrigger, { asChild: true, children: trigger }), open ? panel : null]
  })
}

// -- the footer item ----------------------------------------------------------

function WorkerStrip({ ctx }) {
  // A hover left behind by a hot reload or a disable would open a panel on the
  // next mount with no pointer over it.
  useEffect(() => () => closeHover(), [])

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
        tone: RED
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
        tone: stateColor(workers.state)
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
        key: 'done_today',
        slot: 'done_today',
        ctx,
        count: groups.doneToday,
        group: 'done_today',
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
    onPointerLeave: releaseSlot,
    onPointerMove: event => {
      const slot = slotUnder(event)
      if (slot) hoverSlot(slot)
      else markInside()
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
