import { useCallback, useState } from 'react';

import './App.css';
import type { SavedProject } from './lib/api';
import { useBackend } from './lib/useBackend';
import { GenerateView, type RenderActivity } from './views/GenerateView';
import { LibraryView } from './views/LibraryView';
import { SettingsView } from './views/SettingsView';
import { SeoArchitectSidebarLink } from './components/SeoArchitectPromo';

type Tab = 'generate' | 'library' | 'settings';

const TABS: Array<{ id: Tab; label: string; icon: string }> = [
  { id: 'generate', label: 'Generate', icon: '✦' },
  { id: 'library', label: 'Library', icon: '▦' },
  { id: 'settings', label: 'Settings', icon: '⚙' },
];

export default function App() {
  const [tab, setTab] = useState<Tab>('generate');
  // A project the Library asked to re-edit. It lives here rather than in
  // GenerateView because the Library is what starts the handover, and the
  // Generate tab has to pick it up on the far side of the tab switch.
  const [projectToEdit, setProjectToEdit] = useState<SavedProject | null>(null);
  // A summary of the render the Generate tab is watching. Lifted only this
  // far, so the shell can show it from any tab: the render outlives the tab it
  // was started from, and a job with no presence outside that tab reads as a
  // job that was lost.
  const [render, setRender] = useState<RenderActivity | null>(null);
  const { status, api, dataDir, reachable } = useBackend();

  const handleReEdit = useCallback((project: SavedProject) => {
    setProjectToEdit(project);
    setTab('generate');
  }, []);
  const clearProjectToEdit = useCallback(() => setProjectToEdit(null), []);

  if (status.state !== 'ready' || !api) {
    return <StartupScreen status={status} />;
  }

  return (
    <div className="app">
      <nav className="sidebar">
        <div className="sidebar__brand">
          <span className="sidebar__brand-name">Clip Architect</span>
          <span className="sidebar__brand-version">0.2.2</span>
        </div>

        {TABS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            className="nav-item"
            aria-current={tab === entry.id ? 'page' : undefined}
            onClick={() => setTab(entry.id)}
          >
            <span className="nav-item__icon" aria-hidden="true">
              {entry.icon}
            </span>
            {entry.label}
          </button>
        ))}

        {render && <RenderStatus render={render} onOpen={() => setTab('generate')} />}

        {/* `margin-top: auto` on this block claims the gap between the tabs
            and the footer, so both sit at the bottom of the column and the
            empty middle of the sidebar carries something. The footer keeps its
            own rule, which is what separates the two. */}
        <SeoArchitectSidebarLink />

        {/* The footer used to say "running" unconditionally, from a status
            published once at startup. It now reflects the last request that
            was actually attempted, so it cannot contradict the view next to
            it. */}
        <div className="sidebar__footer">
          {reachable
            ? `Backend running on ${status.base_url}`
            : `No answer from ${status.base_url}. Retrying with the next request.`}
        </div>
      </nav>

      <main className="main">
        <div className="main__inner">
          {/* All three views stay mounted. Unmounting them on a tab change
              would refetch on every visit, but it also throws away a
              half-filled form, an unsaved set of API keys and the poll loop
              watching a running render - and the most likely reason to leave
              the Generate tab is to go and set the key it just asked for.
              `hidden` takes the inactive ones out of the layout and out of the
              tab order; the Library refetches on becoming visible instead. */}
          <div className="view" hidden={tab !== 'generate'}>
            <GenerateView
              api={api}
              dataDir={dataDir}
              reachable={reachable}
              project={projectToEdit}
              onProjectApplied={clearProjectToEdit}
              onRenderActivity={setRender}
            />
          </div>
          <div className="view view--reading" hidden={tab !== 'library'}>
            <LibraryView
              api={api}
              dataDir={dataDir}
              active={tab === 'library'}
              onReEdit={handleReEdit}
            />
          </div>
          <div className="view view--reading view--settings" hidden={tab !== 'settings'}>
            <SettingsView api={api} dataDir={dataDir} />
          </div>
        </div>
      </main>
    </div>
  );
}

/** The running render, in the shell. Shown from every tab so switching away
 *  from Generate no longer hides the fact that a job is in flight. */
function RenderStatus({ render, onOpen }: { render: RenderActivity; onOpen: () => void }) {
  return (
    <button
      type="button"
      className={`render-status${render.failed ? ' render-status--failed' : ''}`}
      onClick={onOpen}
    >
      <span className="render-status__head">
        <span className="render-status__label">
          {render.running ? 'Rendering' : render.failed ? 'Render failed' : 'Render complete'}
        </span>
        {render.running && <span className="render-status__percent">{render.progress}%</span>}
      </span>
      {render.running && (
        <div className="progress">
          <div className="progress__bar" style={{ width: `${render.progress}%` }} />
        </div>
      )}
      {render.running && render.stage && (
        <span className="render-status__stage">{render.stage}</span>
      )}
    </button>
  );
}

function StartupScreen({ status }: { status: ReturnType<typeof useBackend>['status'] }) {
  // Never started and died mid-session are different failures, and the advice
  // for one is useless for the other.
  if (status.state === 'failed' || status.state === 'stopped') {
    return (
      <div className="splash">
        <div className="splash__title">
          {status.state === 'stopped'
            ? 'The rendering engine stopped'
            : 'The rendering engine failed to start'}
        </div>
        <p className="splash__detail">{status.message}</p>
        <p className="splash__detail">
          Restart the app. If this keeps happening, <code>logs/backend.log</code> in the app
          data folder holds the engine's own output, and <code>logs/Clip Architect.log</code>{' '}
          holds the shell's.
        </p>
      </div>
    );
  }

  return (
    <div className="splash">
      <div className="spinner" role="status" aria-label="Starting" />
      <div className="splash__title">Starting the rendering engine…</div>
      <p className="splash__detail">This takes a few seconds on first launch.</p>
    </div>
  );
}
