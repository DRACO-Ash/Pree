---
name: state-management
description: Runtime and client state. Use when handling view state, persistence, high-frequency redraws, or a shared synced document. Covers the static single view-state object with rAF coalescing and a failure-safe storage wrapper, and the server client-state object hydrated from local storage with monotonic-rev sync, 409 on stale writes, debounced trailing writes, echo suppression, and a prototype-pollution-safe sanitiser.
---

# State management

## Purpose and scope

How state is held, persisted, and (on the server archetype) synced between clients. The static artifact keeps one mutable view-state object, coalesces renders to one per animation frame, and persists only innocuous UI state through a failure-safe wrapper. The server app holds client state in one hydrated object and syncs a shared document with a monotonic-rev conflict guard, debounced trailing writes, echo suppression, and a boundary sanitiser. Scope is state held and synced. It does not cover the dataset store (`data-layer`), rendering (`frontend-and-rendering`), or routes (`api-and-integration`).

## When to use

- Handling camera or view interaction (drag, zoom, pan), or any high-frequency redraw.
- Persisting or reading a UI preference.
- Adding or changing client state or a field of the shared synced document.

## Prerequisites

- `code-architecture` read. Server sync also needs `data-layer` (atomic writes) and `api-and-integration` (the workspace endpoints).

## Procedure (static: view state and scheduling)

1. **Hold view state in one object** and mutate it in input handlers.
2. **Schedule renders; do not render synchronously per event.** Coalesce to one frame, debounced per target; run a heavier pass on settle. Key the coalescer by target: once more than one distinct render shares it (a grid, a tools list, a tips list), a single shared flag silently drops whichever render armed second, so give each target its own slot and flush them all on the frame.
   ```js
   function frameSoon(key,fn){ frameSoon._q=frameSoon._q||{}; frameSoon._q[key]=fn;
     if(frameSoon._on) return; frameSoon._on=true;
     (window.requestAnimationFrame||(f=>setTimeout(f,16)))(()=>{ frameSoon._on=false;
       var q=frameSoon._q; frameSoon._q={}; Object.keys(q).forEach(k=>q[k]()); }); }
   function settleSoon(){ clearTimeout(settleSoon._t);
     settleSoon._t=setTimeout(()=>{ if(!dragging) renderFrame(); },130); }
   ```
3. **Persist only through the wrapper.** Never call `localStorage` directly; wrap it with an in-memory fallback so private mode never throws.
   ```js
   const STORE={mem:{}};
   function stGet(k){try{return localStorage.getItem(k)??STORE.mem[k]??null;}catch(e){return STORE.mem[k]??null;}}
   function stSet(k,v){try{localStorage.setItem(k,v);}catch(e){} STORE.mem[k]=v;}
   ```

## Procedure (server: client state and shared-document sync)

1. **Hold client state in one object, hydrated from local storage over defaults** by a per-field guarded merge that never throws on a bad or old snapshot.
2. **Sync the shared document with a monotonic rev.** The document carries an integer `rev`; a write must send the rev it loaded or the server returns 409 with the current document. Serialise the server's read-modify-write through one promise chain so the guard is atomic.
   ```js
   if (typeof body.rev !== "number" || body.rev !== current.rev)
     return res.status(409).json({ current });
   ```
3. **Write on a debounced trailing timer**, gated to online only and suppressed during hydration so a pull does not echo back as a write; a network failure is a silent no-op.
4. **Sanitise the incoming document at the boundary**: rebuild from a field whitelist, deep-strip `__proto__`/`constructor`/`prototype` at every level, cap collections keeping the newest, and reject over a byte cap.

## Decision rules

- **High-frequency event?** Mutate state, call `frameSoon(key, ...)` with the target's key; never build the document directly in the handler.
- **More than one render target through the coalescer?** Key it per target. A single shared flag coalesces unrelated renders into one and drops the rest, which shows up as a surface that is empty on load until the user interacts with it.
- **Persist client-side or server-side?** Ephemeral view state (filters, theme) goes to local storage; shared team state goes to the server document and syncs.
- **Conflict resolution?** Last-write-wins guarded by rev; a write without the current rev is stale and rejected (409), then the client adopts the returned current document and reapplies its change.
- **Snapshot cap direction?** Keep the most recently added, never the oldest, so a fresh entry is never silently lost.
- **Persist this value (static)?** Only innocuous UI state; never anything sensitive.

## Standards (checkable assertions)

- Static: no direct `localStorage` call outside the wrapper; renders go through `frameSoon`/`settleSoon`, keyed per target so no render is dropped; storage access never throws.
- Server: client state is one object hydrated by a guarded merge that cannot throw on a bad snapshot.
- Server: a shared-document write without the loaded rev is rejected with 409 and the current document; the read-modify-write is serialised.
- Server: the incoming document is whitelisted, prototype-pollution-stripped at every level, collection-capped (newest kept), and rejected over the byte cap.
- Server: client writes are debounced, online-gated, and suppressed during hydration; a failed write is a no-op.

## Failure modes and remedies

- **Page white-screens in private mode (static).** Fix: route storage through `stGet`/`stSet`.
- **Janky drag (static).** Fix: `frameSoon` coalescing plus level-of-detail.
- **A surface is empty on load until the user interacts (static).** Cause: two renders share one coalescer flag, so the second is dropped. Fix: key `frameSoon` per target and flush all keyed callbacks on the frame.
- **Two clients clobber each other (server).** Fix: restore the 409-on-stale check and the single promise chain.
- **A crafted `__proto__` body pollutes objects (server).** Fix: restore the deep strip and re-run the pollution test.
- **A sync pull triggers a write loop (server).** Fix: suppress the trailing write during hydration.
- **New snapshots vanish at the cap (server).** Fix: keep the newest, not the oldest.

## Verification

Static: drag and zoom are smooth (one render per frame), the page works with storage disabled, and a grep confirms storage goes only through the wrapper. Server: `npm test` covers 401 without token, 409 on stale rev, prototype-pollution strip, size cap, and the newest-kept snapshot cap.

## Worked example

Server: two planners edit a shared plan. A loads rev 4, B loads rev 4. A saves (rev 5). B saves with rev 4; the server returns 409 with current rev 5; B's client adopts rev 5 and reapplies its change as rev 6. A malicious body `{"__proto__":{"x":1},"status":"going"}` is sanitised: the dangerous key is dropped, `status` is kept, `Object.prototype` is untouched. `npm test` proves all of this.

## Glossary

- **rAF coalescing:** one render per animation frame.
- **Storage wrapper:** `stGet`/`stSet` with an in-memory fallback.
- **Monotonic rev:** an integer version that only increases; a write must carry the rev it read.
- **Optimistic concurrency:** detect conflicting writes by version rather than locking.
- **Echo suppression:** suppress the client's own write while hydrating from a pull.
- **Prototype pollution:** injecting `__proto__`/`constructor`/`prototype` to corrupt objects; defended by deep-stripping.
- Other terms: `glossary`.

## Provenance

Merged from the static artifact's view-state object, `frameSoon`/`settleSoon` scheduling, and `stGet`/`stSet` wrapper, and the server bundle's state-and-sync skill (hydrated client-state object, monotonic-rev guard, debounced sync, echo suppression, prototype-pollution-safe sanitiser).

## Field lesson: one setter, one source of truth

The most robust part of the Launchpad build was a single guidance setter that wrote the level once and repainted every dependent surface: the splash, the hero control, the slider, the page density, the analyser and the active panel. Bugs clustered exactly where this was bypassed, in a dead comparison left over from a wider range and a shared flag in a frame coalescer that dropped concurrent repaints. Route shared state through one setter that clamps the value and repaints all dependents, rather than letting several places mutate it.
