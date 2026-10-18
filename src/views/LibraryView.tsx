import { useCallback, useEffect, useRef, useState } from 'react';

import {
  ApiError,
  BackendApi,
  type DownloadedMaterial,
  type SavedProject,
  type SavedProjectSummary,
  type TaskStatus,
} from '../lib/api';
import { Toast, Toasts } from '../components/Toast';
import { DownloadedClips } from '../components/DownloadedClips';
import { TaskCard } from '../components/TaskCard';

/** Four columns of five rows. The Library opens with one screenful per
 *  section rather than everything on disk: each tile costs a thumbnail
 *  request, and a cache of hundreds would spend the first seconds of every
 *  visit fetching images nobody scrolled to. */
const ROWS_PER_PAGE = 5;
const COLUMNS = 4;
const PAGE_SIZE = ROWS_PER_PAGE * COLUMNS;

interface Props {
  api: BackendApi;
  dataDir: string | null;
  /** Whether the Library is the tab on screen. The view stays mounted when it
   *  is not, so this is what tells it to catch up on what happened elsewhere. */
  active: boolean;
  /** Hands a full project to the Generate tab for re-editing. The parent owns
   *  the tab switch, because it owns which tab is showing. */
  onReEdit: (project: SavedProject) => void;
}

function savedOn(timestamp: string | undefined): string {
  if (!timestamp) return '';
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
}

export function LibraryView({ api, dataDir, active, onReEdit }: Props) {
  const [tasks, setTasks] = useState<TaskStatus[]>([]);
  const [total, setTotal] = useState(0);
  // How many pages are currently on screen. Kept so a refresh or a delete can
  // rebuild exactly what the user had revealed instead of collapsing it.
  const [loadedPages, setLoadedPages] = useState(1);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [downloaded, setDownloaded] = useState<DownloadedMaterial[]>([]);
  const [cacheSize, setCacheSize] = useState(0);
  const [downloadedShown, setDownloadedShown] = useState(PAGE_SIZE);
  const [projects, setProjects] = useState<SavedProjectSummary[]>([]);
  // Which project has a request in flight, so only that row's buttons lock.
  const [projectBusyId, setProjectBusyId] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  // Which project is one click from being deleted. Deleting one destroys
  // settings that were tuned by hand and cannot be recovered, so it asks
  // twice - the same two steps the download cache already uses for a file
  // that costs nothing to fetch again.
  const [confirmingId, setConfirmingId] = useState<string | null>(null);

  const load = useCallback(
    async (pageCount: number) => {
      setLoading(true);
      try {
        // One await for both lists: a partial Library - renders present,
        // downloads missing - reads as data loss rather than a slow fetch.
        // Pages already revealed are re-fetched together so that refreshing,
        // or deleting an entry, does not silently fold the list back up.
        const pages = Array.from({ length: pageCount }, (_, index) => index + 1);
        const [results, cache, saved] = await Promise.all([
          Promise.all(pages.map((page) => api.listTasks(page, PAGE_SIZE))),
          api.listDownloadedMaterials(),
          api.listProjects(),
        ]);
        setTasks(results.flatMap((result) => result.tasks));
        setTotal(results[results.length - 1]?.total ?? 0);
        setLoadedPages(pageCount);
        setDownloaded(cache.files);
        setCacheSize(cache.total_size);
        setProjects(saved.projects);
        setError(null);
      } catch (cause) {
        setError(cause instanceof ApiError ? cause.message : String(cause));
      } finally {
        setLoading(false);
      }
    },
    [api],
  );

  async function loadMoreTasks() {
    setLoadingMore(true);
    try {
      const next = loadedPages + 1;
      const result = await api.listTasks(next, PAGE_SIZE);
      setTasks((current) => [...current, ...result.tasks]);
      setTotal(result.total);
      setLoadedPages(next);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setLoadingMore(false);
    }
  }

  useEffect(() => {
    // Fetching the task list is exactly the "synchronise with an external
    // system" case effects are for. `load` flips `loading`, which the rule
    // flags, but that is a no-op bail-out on mount because the state already
    // starts true, and the same function backs the Refresh button where the
    // flag genuinely has to change.
    // oxlint-disable-next-line react/set-state-in-effect
    void load(1);
  }, [load]);

  // The view no longer remounts on a tab change, so it no longer refetches by
  // accident. Reload when it comes back into view instead: a render finished
  // on the Generate tab, or a clip deleted from it, has to show up here.
  const wasActive = useRef(active);
  useEffect(() => {
    const returned = active && !wasActive.current;
    wasActive.current = active;
    // oxlint-disable-next-line react/set-state-in-effect
    if (returned) void load(loadedPages);
  }, [active, load, loadedPages]);

  async function handleDelete(taskId: string) {
    try {
      await api.deleteTask(taskId);
      // Reload rather than splicing locally: the pages behind this one shift
      // up, so a local splice would leave a gap the next Load more skips over.
      await load(loadedPages);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    }
  }

  async function handleDeleteDownloaded(name: string) {
    try {
      await api.deleteDownloadedMaterial(name);
      const cache = await api.listDownloadedMaterials();
      setDownloaded(cache.files);
      setCacheSize(cache.total_size);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    }
  }

  async function handleReEdit(id: string) {
    setProjectBusyId(id);
    try {
      // The listing carries metadata only; the settings come with the full
      // record, so the Generate form is handed a project it can apply as is.
      const result = await api.getProject(id);
      onReEdit(result.project);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setProjectBusyId(null);
    }
  }

  async function handleRename(id: string) {
    const title = renameValue.trim();
    if (!title) return;
    setProjectBusyId(id);
    try {
      const result = await api.updateProject(id, { title });
      setProjects((current) =>
        current.map((item) =>
          item.id === id ? { ...item, title: result.project.title } : item,
        ),
      );
      setRenamingId(null);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setProjectBusyId(null);
    }
  }

  async function handleDeleteProject(id: string) {
    setProjectBusyId(id);
    try {
      await api.deleteProject(id);
      setProjects((current) => current.filter((item) => item.id !== id));
      setConfirmingId(null);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setProjectBusyId(null);
    }
  }

  const hasMoreTasks = tasks.length < total;
  const visibleDownloads = downloaded.slice(0, downloadedShown);
  const hasMoreDownloads = downloadedShown < downloaded.length;

  return (
    <>
      <h1 className="page-title">Library</h1>
      <p className="page-subtitle">
        Saved projects, the videos already rendered, and the stock footage they
        were cut from.
      </p>

      <Toasts>
        {error && <Toast tone="error" message={error} onDismiss={() => setError(null)} />}
      </Toasts>

      <div className="button-row" style={{ marginBottom: 16 }}>
        <button
          className="button"
          onClick={() => void load(loadedPages)}
          disabled={loading}
        >
          {loading ? 'Loading…' : 'Refresh'}
        </button>
        <span className="field__hint">
          {total} {total === 1 ? 'task' : 'tasks'}
        </span>
      </div>

      <h2 className="library__heading">Saved projects</h2>

      {projects.length === 0 ? (
        <div className="empty">
          Nothing saved yet. After a render, use “Save project” on the Generate
          tab to keep its settings.
        </div>
      ) : (
        <ul className="project-list">
          {projects.map((item) => {
            const busy = projectBusyId === item.id;
            return (
              <li key={item.id} className="project">
                <div className="project__body">
                  {renamingId === item.id ? (
                    <input
                      type="text"
                      aria-label="Project name"
                      value={renameValue}
                      onChange={(event) => setRenameValue(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter') void handleRename(item.id);
                        if (event.key === 'Escape') setRenamingId(null);
                      }}
                      // eslint-disable-next-line jsx-a11y/no-autofocus
                      autoFocus
                      disabled={busy}
                    />
                  ) : (
                    <p className="project__title" title={item.title}>
                      {item.title}
                    </p>
                  )}
                  <p className="project__meta">
                    {item.video_subject || 'No subject'}
                    {item.clip_count > 0 &&
                      ` · ${item.clip_count} clip${item.clip_count === 1 ? '' : 's'}`}
                    {savedOn(item.updated_at) && ` · saved ${savedOn(item.updated_at)}`}
                  </p>
                </div>

                <div className="button-row project__actions">
                  {confirmingId === item.id ? (
                    // A project is hand-tuned settings plus the clips a render
                    // was cut from. Nothing brings it back, so the second step
                    // says which one is about to go rather than "Are you sure".
                    <>
                      <button
                        className="button button--danger"
                        onClick={() => void handleDeleteProject(item.id)}
                        disabled={busy}
                      >
                        {busy ? 'Deleting…' : `Delete "${item.title}"`}
                      </button>
                      <button
                        className="button"
                        onClick={() => setConfirmingId(null)}
                        disabled={busy}
                      >
                        Cancel
                      </button>
                    </>
                  ) : renamingId === item.id ? (
                    <>
                      <button
                        className="button button--primary"
                        onClick={() => void handleRename(item.id)}
                        disabled={busy || !renameValue.trim()}
                      >
                        Save name
                      </button>
                      <button
                        className="button"
                        onClick={() => setRenamingId(null)}
                        disabled={busy}
                      >
                        Cancel
                      </button>
                    </>
                  ) : (
                    <>
                      <button
                        className="button button--primary"
                        onClick={() => void handleReEdit(item.id)}
                        disabled={busy}
                      >
                        {busy ? 'Loading…' : 'Re-edit'}
                      </button>
                      <button
                        className="button"
                        onClick={() => {
                          setRenamingId(item.id);
                          setRenameValue(item.title);
                        }}
                        disabled={busy}
                      >
                        Rename
                      </button>
                      <button
                        className="button button--danger"
                        onClick={() => setConfirmingId(item.id)}
                        disabled={busy}
                      >
                        Delete
                      </button>
                    </>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      <h2 className="library__heading library__heading--section">
        Generated videos
      </h2>

      {!loading && tasks.length === 0 && (
        <div className="empty">Nothing rendered yet. Start from the Generate tab.</div>
      )}

      <div className="library__grid">
        {tasks.map((task) => (
          <TaskCard
            key={task.task_id}
            task={task}
            api={api}
            dataDir={dataDir}
            onDelete={handleDelete}
          />
        ))}
      </div>

      {hasMoreTasks && (
        <div className="library__more">
          <button
            className="button"
            onClick={() => void loadMoreTasks()}
            disabled={loading || loadingMore}
          >
            {loadingMore ? 'Loading…' : 'Load more'}
          </button>
          <span className="field__hint">
            {tasks.length} of {total}
          </span>
        </div>
      )}

      <h2 className="library__heading library__heading--section">
        Downloaded clips
      </h2>
      <DownloadedClips
        api={api}
        clips={visibleDownloads}
        shownOf={downloaded.length}
        totalSize={cacheSize}
        busy={loading}
        onDelete={handleDeleteDownloaded}
      />

      {hasMoreDownloads && (
        <div className="library__more">
          <button
            className="button"
            onClick={() => setDownloadedShown((shown) => shown + PAGE_SIZE)}
            disabled={loading}
          >
            Load more
          </button>
          <span className="field__hint">
            {visibleDownloads.length} of {downloaded.length}
          </span>
        </div>
      )}
    </>
  );
}
