import os
import threading
import xbmc
import xbmcvfs
from tmdbhelper.lib.addon.plugin import get_setting
from tmdbhelper.lib.addon.logger import kodi_log


AUTOADDED_FILENAME = 'library_autoadded.json'
PENDING_WATCHED_FILENAME = 'library_pending_watched.json'
DEFAULT_POLL_TIMEOUT_SECONDS = 45
DEFAULT_POLL_INTERVAL_MS = 1500
FAST_POLL_TIMEOUT_SECONDS = 8
FAST_POLL_INTERVAL_MS = 500


def _load_json(filename):
    from json import loads
    from tmdbhelper.lib.files.futils import read_file, get_file_path
    try:
        path = get_file_path('library', filename, join_addon_data=True, make_dir=False)
        content = read_file(path)
        if content:
            return loads(content)
    except (ValueError, TypeError, OSError):
        pass
    return {}


def _save_json(filename, data):
    from json import dumps
    from tmdbhelper.lib.files.futils import write_to_file
    write_to_file(dumps(data, indent=2), 'library', filename, join_addon_data=True)


def add_to_library_on_watched(
    tmdb_type,
    tmdb_id,
    season=0,
    episode=0,
    imdb_id=None,
    tvdb_id=None,
    resume_position=0,
    resume_total=0,
    mark_watched=False,
    skip_resume=False,
    strm_path=None,
):
    if not get_setting('library_autoadd_on_play'):
        return
    if not tmdb_type or not tmdb_id:
        return

    key = f'{tmdb_type}.{tmdb_id}'
    autoadded = _load_json(AUTOADDED_FILENAME)
    is_already_added = key in autoadded
    if is_already_added:
        kodi_log(f'LIBRARY AUTO-ADD: [Dedup] {key}', 2)

    if not is_already_added:
        kodi_log(f'LIBRARY AUTO-ADD: {key}', 2)
        autoadded[key] = True
        _save_json(AUTOADDED_FILENAME, autoadded)

    content_id = _queue_pending_watched(
        tmdb_type,
        tmdb_id,
        season,
        episode,
        imdb_id,
        tvdb_id,
        resume_position=resume_position,
        resume_total=resume_total,
        mark_watched=mark_watched,
        skip_resume=skip_resume,
        strm_path=strm_path,
    )

    # Resume/watched syncing should still run for already-added items without waiting for a scan event.
    if is_already_added:
        _run_pending_sync_worker(
            timeout_seconds=FAST_POLL_TIMEOUT_SECONDS,
            interval_ms=FAST_POLL_INTERVAL_MS)
        return

    thread = threading.Thread(
        target=_do_add,
        args=(content_id, tmdb_type, tmdb_id, season, episode),
        daemon=True)
    thread.start()


def _queue_pending_watched(
    tmdb_type,
    tmdb_id,
    season=0,
    episode=0,
    imdb_id=None,
    tvdb_id=None,
    resume_position=0,
    resume_total=0,
    mark_watched=False,
    skip_resume=False,
    strm_path=None,
):
    pending = _load_json(PENDING_WATCHED_FILENAME)
    content_id = f'{tmdb_type}.{tmdb_id}.{season}.{episode}'
    pending[content_id] = {
        'tmdb_type': tmdb_type,
        'tmdb_id': tmdb_id,
        'season': season,
        'episode': episode,
        'imdb_id': imdb_id,
        'tvdb_id': tvdb_id,
        'resume_position': resume_position or 0,
        'resume_total': resume_total or 0,
        'mark_watched': bool(mark_watched),
        'skip_resume': bool(skip_resume),
        'strm_path': strm_path or '',
    }
    _save_json(PENDING_WATCHED_FILENAME, pending)
    return content_id


def process_pending_watched(timeout_seconds=DEFAULT_POLL_TIMEOUT_SECONDS, interval_ms=DEFAULT_POLL_INTERVAL_MS):
    pending = _load_json(PENDING_WATCHED_FILENAME)
    if not pending:
        return

    import tmdbhelper.lib.api.kodi.rpc as rpc

    processed = []
    for content_id, item in pending.items():
        tmdb_type = item['tmdb_type']
        tmdb_id = item['tmdb_id']
        season = item.get('season', 0)
        episode = item.get('episode', 0)
        imdb_id = item.get('imdb_id')
        tvdb_id = item.get('tvdb_id')
        strm_path = item.get('strm_path')
        mark_watched = item.get('mark_watched')
        skip_resume = item.get('skip_resume')
        resume_position = item.get('resume_position') or 0
        resume_total = item.get('resume_total') or 0

        dbid = _poll_for_dbid(
            rpc,
            tmdb_type,
            tmdb_id,
            season=season,
            episode=episode,
            imdb_id=imdb_id,
            tvdb_id=tvdb_id,
            strm_path=strm_path,
            timeout_seconds=timeout_seconds,
            interval_ms=interval_ms,
        )
        if not dbid:
            continue

        if mark_watched:
            dbtype = 'episode' if tmdb_type == 'tv' else 'movie'
            rpc.set_watched(dbid=dbid, dbtype=dbtype)
            kodi_log(f'LIBRARY AUTO-ADD: [Watched] {content_id}', 2)
            processed.append(content_id)
            continue

        if skip_resume:
            processed.append(content_id)
            continue

        if not _has_valid_resume(resume_position, resume_total):
            kodi_log(f'LIBRARY AUTO-ADD: [Resume] Invalid payload {content_id}', 2)
            processed.append(content_id)
            continue

        if _set_resume_progress(rpc, tmdb_type, dbid, resume_position, resume_total):
            kodi_log(
                f'LIBRARY AUTO-ADD: [Resume] {content_id} ({resume_position:.2f}/{resume_total:.2f})',
                2)
            processed.append(content_id)
        else:
            kodi_log(f'LIBRARY AUTO-ADD: [Resume] Failed {content_id}', 2)
            continue

    for cid in processed:
        del pending[cid]
    _save_json(PENDING_WATCHED_FILENAME, pending)


def _find_dbid(rpc, tmdb_type, tmdb_id, season=0, episode=0, imdb_id=None, tvdb_id=None):
    if tmdb_type == 'movie':
        return rpc.KodiLibrary('movie', cache_refresh=True).get_info(
            info='dbid', imdb_id=imdb_id, tmdb_id=tmdb_id, tvdb_id=tvdb_id)

    if tmdb_type == 'tv':
        tvshowid = rpc.KodiLibrary('tvshow', cache_refresh=True).get_info(
            info='dbid', imdb_id=imdb_id, tmdb_id=tmdb_id, tvdb_id=tvdb_id)
        if not tvshowid:
            return None
        return rpc.KodiLibrary('episode', tvshowid, cache_refresh=True).get_info(
            info='dbid', season=season, episode=episode)

    return None


def _poll_for_dbid(
    rpc,
    tmdb_type,
    tmdb_id,
    season=0,
    episode=0,
    imdb_id=None,
    tvdb_id=None,
    strm_path=None,
    timeout_seconds=DEFAULT_POLL_TIMEOUT_SECONDS,
    interval_ms=DEFAULT_POLL_INTERVAL_MS,
):
    attempts = max(1, int((timeout_seconds * 1000) / interval_ms))
    for attempt in range(attempts):
        dbid = _find_dbid_by_path(rpc, tmdb_type, strm_path)
        if dbid:
            kodi_log(f'LIBRARY AUTO-ADD: [Resolve] matched by path on attempt {attempt + 1}', 2)
            return dbid

        dbid = _find_dbid(rpc, tmdb_type, tmdb_id, season, episode, imdb_id, tvdb_id)
        if dbid:
            kodi_log(f'LIBRARY AUTO-ADD: [Resolve] matched by ids on attempt {attempt + 1}', 2)
            return dbid

        xbmc.sleep(interval_ms)

    kodi_log(
        f'LIBRARY AUTO-ADD: [Resolve] timeout {tmdb_type}.{tmdb_id}.{season}.{episode}',
        2)
    return None


def _find_dbid_by_path(rpc, tmdb_type, strm_path):
    path = _normalize_path(strm_path)
    if not path:
        return None
    try:
        if tmdb_type == 'movie':
            response = rpc.get_jsonrpc('VideoLibrary.GetMovies', {'properties': ['file']})
            for item in (response.get('result', {}).get('movies') or []):
                if _normalize_path(item.get('file')) == path:
                    return item.get('movieid')

        if tmdb_type == 'tv':
            response = rpc.get_jsonrpc('VideoLibrary.GetEpisodes', {'properties': ['file']})
            for item in (response.get('result', {}).get('episodes') or []):
                if _normalize_path(item.get('file')) == path:
                    return item.get('episodeid')
    except Exception as exc:
        kodi_log(f'LIBRARY AUTO-ADD: [Resolve] Path lookup error\n{exc}', 2)
    return None


def _normalize_path(path):
    if not path:
        return ''
    try:
        path = xbmcvfs.translatePath(path)
    except Exception:
        pass
    path = path.strip().rstrip('/\\')
    if not path:
        return ''
    return os.path.normcase(os.path.normpath(path))


def _has_valid_resume(position, total):
    return bool(total and position and total > 0 and position > 0 and position < total)


def _set_resume_progress(rpc, tmdb_type, dbid, position, total):
    try:
        if tmdb_type == 'tv':
            method = 'VideoLibrary.SetEpisodeDetails'
            id_key = 'episodeid'
        else:
            method = 'VideoLibrary.SetMovieDetails'
            id_key = 'movieid'

        params = {
            id_key: int(dbid),
            'resume': {
                'position': float(position),
                'total': float(total),
            }
        }
        response = rpc.get_jsonrpc(method, params)
        return bool(response and response.get('result') == 'OK')
    except Exception as exc:
        kodi_log(f'LIBRARY AUTO-ADD: [Resume] JSONRPC error\n{exc}', 2)
        return False


def _run_pending_sync_worker(timeout_seconds=DEFAULT_POLL_TIMEOUT_SECONDS, interval_ms=DEFAULT_POLL_INTERVAL_MS):
    thread = threading.Thread(
        target=process_pending_watched,
        kwargs={'timeout_seconds': timeout_seconds, 'interval_ms': interval_ms},
        daemon=True)
    thread.start()


def _do_add(content_id, tmdb_type, tmdb_id, season=0, episode=0):
    try:
        from tmdbhelper.lib.script.method.library import add_movie_to_library, add_tvshow_to_library
        if tmdb_type == 'movie':
            add_movie_to_library(tmdb_id)
        elif tmdb_type == 'tv':
            add_tvshow_to_library(tmdb_id)
        strm_path = _find_existing_strm_path(tmdb_type, tmdb_id, season=season, episode=episode)
        if strm_path:
            _set_pending_strm_path(content_id, strm_path)
            kodi_log(f'LIBRARY AUTO-ADD: [Path] {strm_path}', 2)
    except Exception as exc:
        kodi_log(f'LIBRARY AUTO-ADD: [Error] {tmdb_type}.{tmdb_id}\n{exc}', 1)


def _set_pending_strm_path(content_id, strm_path):
    pending = _load_json(PENDING_WATCHED_FILENAME)
    if not pending.get(content_id):
        return
    pending[content_id]['strm_path'] = strm_path
    _save_json(PENDING_WATCHED_FILENAME, pending)


def _find_existing_strm_path(tmdb_type, tmdb_id, season=0, episode=0):
    from tmdbhelper.lib.update.common import LibraryCommon
    from tmdbhelper.lib.files.futils import get_tmdb_id_nfo, validate_join
    tmdb_id = f'{tmdb_id}'

    if tmdb_type == 'movie':
        basedir = LibraryCommon.get_basedir('movies')
        return _find_movie_strm_path(basedir, tmdb_id, get_tmdb_id_nfo, validate_join)

    if tmdb_type == 'tv':
        basedir = LibraryCommon.get_basedir('tvshows')
        return _find_episode_strm_path(
            basedir, tmdb_id, season, episode, get_tmdb_id_nfo, validate_join)
    return ''


def _find_movie_strm_path(basedir, tmdb_id, get_tmdb_id_nfo, validate_join):
    folders, _ = _listdir_safe(basedir)
    for folder in folders:
        if f'{get_tmdb_id_nfo(basedir, folder, tmdb_type="movie")}' != tmdb_id:
            continue
        folder_path = validate_join(basedir, folder)
        _, filenames = _listdir_safe(folder_path)
        for filename in filenames:
            if filename.lower().endswith('.strm'):
                return validate_join(folder_path, filename)
    return ''


def _find_episode_strm_path(basedir, tmdb_id, season, episode, get_tmdb_id_nfo, validate_join):
    episode_prefix = f'S{int(season):02d}E{int(episode):02d}'.lower()
    folders, _ = _listdir_safe(basedir)
    for folder in folders:
        if f'{get_tmdb_id_nfo(basedir, folder, tmdb_type="tv")}' != tmdb_id:
            continue
        show_path = validate_join(basedir, folder)
        season_folders, _ = _listdir_safe(show_path)
        for season_folder in season_folders:
            season_path = validate_join(show_path, season_folder)
            _, filenames = _listdir_safe(season_path)
            for filename in filenames:
                name = filename.lower()
                if name.endswith('.strm') and name.startswith(episode_prefix):
                    return validate_join(season_path, filename)
    return ''


def _listdir_safe(path):
    try:
        return xbmcvfs.listdir(path)
    except Exception:
        return [], []
