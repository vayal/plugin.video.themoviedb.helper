import threading
from tmdbhelper.lib.addon.plugin import get_setting
from tmdbhelper.lib.addon.logger import kodi_log


AUTOADDED_FILENAME = 'library_autoadded.json'
PENDING_WATCHED_FILENAME = 'library_pending_watched.json'


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


def add_to_library_on_watched(tmdb_type, tmdb_id, season=0, episode=0, imdb_id=None, tvdb_id=None):
    if not get_setting('library_autoadd_on_play'):
        return
    if not tmdb_type or not tmdb_id:
        return

    key = f'{tmdb_type}.{tmdb_id}'
    autoadded = _load_json(AUTOADDED_FILENAME)
    if key in autoadded:
        return

    kodi_log(f'LIBRARY AUTO-ADD: {key}', 2)
    autoadded[key] = True
    _save_json(AUTOADDED_FILENAME, autoadded)

    _queue_pending_watched(tmdb_type, tmdb_id, season, episode, imdb_id, tvdb_id)

    thread = threading.Thread(target=_do_add, args=(tmdb_type, tmdb_id), daemon=True)
    thread.start()


def _queue_pending_watched(tmdb_type, tmdb_id, season=0, episode=0, imdb_id=None, tvdb_id=None):
    pending = _load_json(PENDING_WATCHED_FILENAME)
    content_id = f'{tmdb_type}.{tmdb_id}.{season}.{episode}'
    pending[content_id] = {
        'tmdb_type': tmdb_type,
        'tmdb_id': tmdb_id,
        'season': season,
        'episode': episode,
        'imdb_id': imdb_id,
        'tvdb_id': tvdb_id,
    }
    _save_json(PENDING_WATCHED_FILENAME, pending)


def process_pending_watched():
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

        dbid = _find_dbid(rpc, tmdb_type, tmdb_id, season, episode, imdb_id, tvdb_id)
        if not dbid:
            continue

        dbtype = 'episode' if tmdb_type == 'tv' else 'movie'
        rpc.set_watched(dbid=dbid, dbtype=dbtype)
        kodi_log(f'LIBRARY AUTO-ADD: [Watched] {content_id}', 2)
        processed.append(content_id)

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


def _do_add(tmdb_type, tmdb_id):
    try:
        from tmdbhelper.lib.script.method.library import add_movie_to_library, add_tvshow_to_library
        if tmdb_type == 'movie':
            add_movie_to_library(tmdb_id)
        elif tmdb_type == 'tv':
            add_tvshow_to_library(tmdb_id)
    except Exception as exc:
        kodi_log(f'LIBRARY AUTO-ADD: [Error] {tmdb_type}.{tmdb_id}\n{exc}', 1)
