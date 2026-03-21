"""
Local-library Next Up / in-progress episodes via JSON-RPC only (no web APIs).
"""
from jurialmunkey.parser import try_int
from tmdbhelper.lib.addon.logger import TimerList
from tmdbhelper.lib.addon.plugin import get_localized
from tmdbhelper.lib.api.kodi.mapping import ItemMapper
from tmdbhelper.lib.api.kodi.rpc import get_jsonrpc
from tmdbhelper.lib.items.container import ContainerDirectoryCommon
from tmdbhelper.lib.items.listitem import ListItem

_EPISODE_PROPS = [
    'tvshowid', 'season', 'episode', 'playcount', 'resume', 'lastplayed',
    'art', 'title', 'showtitle', 'file', 'uniqueid', 'plot', 'runtime',
    'firstaired', 'cast', 'streamdetails',
]


def _resume_position(resume):
    if not isinstance(resume, dict):
        return 0.0
    try:
        return float(resume.get('position') or 0)
    except (TypeError, ValueError):
        return 0.0


def _resume_total(resume):
    if not isinstance(resume, dict):
        return 0.0
    try:
        return float(resume.get('total') or 0)
    except (TypeError, ValueError):
        return 0.0


def _has_activity(ep):
    if not isinstance(ep, dict):
        return False
    pc = try_int(ep.get('playcount')) or 0
    if pc >= 1:
        return True
    if _resume_position(ep.get('resume')) > 0:
        return True
    lastplayed = ep.get('lastplayed')
    return bool(lastplayed and str(lastplayed).strip())


def _lastplayed_sort_key(ep):
    """Kodi lastplayed is YYYY-MM-DD HH:MM:SS — lexicographic sort works."""
    lp = ep.get('lastplayed') if isinstance(ep, dict) else None
    if not lp:
        return ''
    return str(lp)


def _pick_latest_per_show(episodes_by_show):
    """Per tvshowid, episode with max lastplayed; tie-break on higher episodeid."""
    latest = {}
    for tvid, eps in episodes_by_show.items():
        best = None
        best_key = None
        for ep in eps:
            if not isinstance(ep, dict):
                continue
            eid = ep.get('episodeid')
            key = (_lastplayed_sort_key(ep), try_int(eid) or 0)
            if best is None or key > best_key:
                best = ep
                best_key = key
        if best is not None:
            latest[tvid] = best
    return latest


def _build_next_index(episodes):
    """(season, episode) -> ep dict for season >= 1 only."""
    idx = {}
    for ep in episodes:
        if not isinstance(ep, dict):
            continue
        tid = try_int(ep.get('tvshowid'))
        if tid is None or tid < 0:
            continue
        s = try_int(ep.get('season'))
        e = try_int(ep.get('episode'))
        if s is None or e is None or s < 1 or e < 1:
            continue
        idx[(s, e)] = ep
    return idx


def _find_next_episode(ep_by_show, tvshowid, season, episode):
    s = try_int(season)
    e = try_int(episode)
    if s is None or e is None or s < 0 or e < 0:
        return None
    index = ep_by_show.get(tvshowid) or {}
    if s == 0:
        return index.get((1, 1))
    cand = index.get((s, e + 1))
    if cand:
        return cand
    return index.get((s + 1, 1))


def _fetch_all_episodes():
    params = {'properties': _EPISODE_PROPS}
    episodes = []
    try:
        response = get_jsonrpc('VideoLibrary.GetEpisodes', params)
        episodes = (response.get('result') or {}).get('episodes') or []
    except (TypeError, AttributeError, KeyError):
        episodes = []
    if episodes:
        return [e for e in episodes if isinstance(e, dict)]
    # Fallback: iterate shows (some Kodi builds require tvshowid)
    try:
        rshows = get_jsonrpc('VideoLibrary.GetTVShows', {'properties': ['title']})
        shows = (rshows.get('result') or {}).get('tvshows') or []
    except (TypeError, AttributeError, KeyError):
        shows = []
    out = []
    for show in shows:
        if not isinstance(show, dict):
            continue
        tid = show.get('tvshowid')
        if tid is None:
            continue
        try:
            r2 = get_jsonrpc('VideoLibrary.GetEpisodes', {
                'tvshowid': tid,
                'properties': _EPISODE_PROPS,
            })
            out.extend((r2.get('result') or {}).get('episodes') or [])
        except (TypeError, AttributeError, KeyError):
            continue
    return [e for e in out if isinstance(e, dict)]


def _apply_resume_infoproperties(infoproperties, resume):
    total = _resume_total(resume)
    pos = _resume_position(resume)
    if total > 0:
        infoproperties['TotalTime'] = int(total)
        if pos > 0:
            infoproperties['ResumeTime'] = int(pos)


def _episode_to_listitem(ep_raw):
    path = ep_raw.get('file') if isinstance(ep_raw, dict) else None
    if not path:
        return None
    eid = ep_raw.get('episodeid')
    if eid is None:
        return None
    mapped = ItemMapper('episode').get_info(ep_raw, tmdb_type='episode')
    infolabels = mapped.get('infolabels') or {}
    infolabels['mediatype'] = 'episode'
    infolabels['dbid'] = eid
    if ep_raw.get('file') and not infolabels.get('file'):
        infolabels['file'] = ep_raw['file']
    fa = ep_raw.get('firstaired')
    if fa and not infolabels.get('premiered'):
        infolabels['premiered'] = str(fa)
    infoproperties = mapped.get('infoproperties') or {}
    _apply_resume_infoproperties(infoproperties, ep_raw.get('resume'))
    label = mapped.get('label') or ''
    if not label.strip():
        st = infolabels.get('tvshowtitle') or ep_raw.get('showtitle') or ''
        et = infolabels.get('title') or ep_raw.get('title') or ''
        label = f'{st} - {et}'.strip(' -') or et or st or str(eid)
    return ListItem(
        label=label,
        path=path,
        is_folder=False,
        infolabels=infolabels,
        infoproperties=infoproperties,
        art=mapped.get('art') or {},
        cast=mapped.get('cast') or [],
        stream_details=mapped.get('stream_details') or {},
        unique_ids=mapped.get('unique_ids') or {},
        params=mapped.get('params') or {},
    )


def _compute_rows():
    all_eps = _fetch_all_episodes()
    active = [e for e in all_eps if _has_activity(e)]
    by_show = {}
    for ep in active:
        tid = ep.get('tvshowid')
        if tid is None:
            continue
        by_show.setdefault(tid, []).append(ep)
    latest_per_show = _pick_latest_per_show(by_show)
    by_show_all = {}
    for ep in all_eps:
        tid = ep.get('tvshowid')
        if tid is None:
            continue
        by_show_all.setdefault(tid, []).append(ep)
    ep_by_show = {tid: _build_next_index(eps) for tid, eps in by_show_all.items()}
    seen = set()
    sort_meta = []
    for tvid, latest in latest_per_show.items():
        sort_key = _lastplayed_sort_key(latest)
        eid_latest = latest.get('episodeid')
        pc = try_int(latest.get('playcount')) or 0
        pos = _resume_position(latest.get('resume'))
        if pc == 0 and pos > 0:
            target = latest
        elif pc >= 1:
            target = _find_next_episode(
                ep_by_show, tvid,
                latest.get('season'), latest.get('episode'),
            )
        else:
            continue
        if not target or not isinstance(target, dict):
            continue
        tid_out = target.get('episodeid')
        if tid_out is None or tid_out in seen:
            continue
        seen.add(tid_out)
        sort_meta.append((sort_key, try_int(eid_latest) or 0, target))
    sort_meta.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [t[2] for t in sort_meta]


class ListLocalNextUp(ContainerDirectoryCommon):
    container_content = 'episodes'
    localize_id = 32539

    def get_kodi_database(self, tmdb_type):
        return None

    def get_items(self, **kwargs):
        self.container_content = 'episodes'
        return [li for li in (_episode_to_listitem(e) for e in _compute_rows()) if li]

    def get_directory(self, items_only=False, build_items=True):
        from tmdbhelper.lib.addon.plugin import executebuiltin

        with TimerList(self.timer_lists, 'total', logging=self.log_timers):
            self.trakt_playdata.pre_sync_start(**self.params)
            items = self.get_directory_items()
            if items is None:
                items = []

            if not build_items:
                return items

            self.property_params.update(self.set_params_to_container())
            pc = self.params.get('plugin_category')
            self.plugin_category = pc if pc else get_localized(self.localize_id)

            with TimerList(self.timer_lists, '--sync', log_threshold=0.001, logging=self.log_timers):
                self.trakt_playdata.pre_sync_join()

            if items:
                with TimerList(self.timer_lists, 'add_items', logging=self.log_timers):
                    items = self.build_items(items)
                    if items_only:
                        return items
                    self.add_items(items)

            self.finish_container()

        if self.log_timers:
            from tmdbhelper.lib.files.futils import write_to_file
            from tmdbhelper.lib.addon.logger import log_timer_report
            from tmdbhelper.lib.addon.tmdate import get_todays_date
            report_data = log_timer_report(self.timer_lists, self.paramstring, logging=False)
            write_to_file(
                ''.join(report_data), 'timer_report', f'{get_todays_date()}.txt',
                join_addon_data=True, append_to_file=True,
            )

        if self.container_update:
            executebuiltin(f'Container.Update({self.container_update})')

        if self.container_refresh:
            executebuiltin('Container.Refresh')
