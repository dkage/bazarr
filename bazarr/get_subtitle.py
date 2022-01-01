# coding=utf-8
# fmt: off

import os
import sys
import ast
import logging
import subprocess
import time
import pickle
import codecs
import re
import subliminal
import copy
import operator
import time

from functools import reduce
from inspect import getfullargspec
from peewee import fn
from datetime import datetime, timedelta
from subzero.language import Language
from subzero.video import parse_video
from subliminal import region, score as subliminal_scores, \
    list_subtitles, Episode, Movie
from subliminal_patch.core import SZAsyncProviderPool, save_subtitles, get_subtitle_path

from subliminal_patch.core_persistent import download_best_subtitles, list_all_subtitles, download_subtitles
from subliminal_patch.score import compute_score
from subliminal_patch.subtitle import Subtitle
from get_languages import language_from_alpha3, alpha2_from_alpha3, alpha3_from_alpha2, language_from_alpha2, \
    alpha2_from_language, alpha3_from_language
from config import settings, get_array_from
from helper import path_mappings, pp_replace, get_target_folder, force_unicode
from list_subtitles import store_subtitles, list_missing_subtitles, store_subtitles_movie, list_missing_subtitles_movies
from utils import history_log, history_log_movie, get_binary, get_blacklist, notify_sonarr, notify_radarr
from notifier import send_notifications, send_notifications_movie
from get_providers import get_providers, get_providers_auth, provider_throttle, provider_pool
from knowit import api
from subsyncer import subsync
from guessit import guessit
from custom_lang import CustomLanguage
from database import dict_mapper, get_exclusion_clause, get_profiles_list, get_audio_profile_languages, \
    get_desired_languages, TableShows, TableEpisodes, TableMovies, TableHistory, TableHistoryMovie
from event_handler import event_stream, show_progress, hide_progress
from embedded_subs_reader import parse_video_metadata

from analytics import track_event
from locale import getpreferredencoding
from score import movie_score, series_score

def get_video(path, title, sceneName, providers=None, media_type="movie"):
    """
    Construct `Video` instance
    :param path: path to video
    :param title: series/movie title
    :param sceneName: sceneName
    :param providers: provider list for selective hashing
    :param media_type: movie/series
    :return: `Video` instance
    """
    hints = {"title": title, "type": "movie" if media_type == "movie" else "episode"}
    used_scene_name = False
    original_path = path
    original_name = os.path.basename(path)
    hash_from = None
    if sceneName != "None":
        # use the sceneName but keep the folder structure for better guessing
        path = os.path.join(os.path.dirname(path), sceneName + os.path.splitext(path)[1])
        used_scene_name = True
        hash_from = original_path

    try:
        video = parse_video(path, hints=hints, providers=providers, dry_run=used_scene_name,
                            hash_from=hash_from)
        video.used_scene_name = used_scene_name
        video.original_name = original_name
        video.original_path = original_path

        refine_from_db(original_path, video)
        refine_from_ffprobe(original_path, video)

        logging.debug('BAZARR is using these video object properties: %s', vars(copy.deepcopy(video)))
        return video

    except Exception as e:
        logging.exception("BAZARR Error trying to get video information for this file: " + original_path)


# fmt: on
def _init_pool(media_type, profile_id=None, providers=None):
    pool = provider_pool()
    return pool(
        providers=providers or get_providers(),
        provider_configs=get_providers_auth(),
        blacklist=get_blacklist(media_type),
        throttle_callback=provider_throttle,
        ban_list=get_ban_list(profile_id),
        language_hook=None,
    )


_pools = {}


def _get_pool(media_type, profile_id=None):
    try:
        return _pools[f'{media_type}_{profile_id or ""}']
    except KeyError:
        _update_pool(media_type, profile_id)

        return _pools[f'{media_type}_{profile_id or ""}']


def _update_pool(media_type, profile_id=None):
    pool_key = f'{media_type}_{profile_id or ""}'
    logging.debug("BAZARR updating pool: %s", pool_key)

    # Init a new pool if not present
    if pool_key not in _pools:
        logging.debug("BAZARR pool not initialized: %s. Initializing", pool_key)
        _pools[pool_key] = _init_pool(media_type, profile_id)

    pool = _pools[pool_key]
    if pool is None:
        return False

    return pool.update(
        get_providers(),
        get_providers_auth(),
        get_blacklist(media_type),
        get_ban_list(profile_id),
    )


def update_pools(f):
    """Decorator that ensures all pools are updated on each function run.
    It will detect any config changes in Bazarr"""

    def decorated(*args, **kwargs):
        logging.debug("BAZARR updating pools: %s", _pools)

        start = time.time()
        args_spec = getfullargspec(f).args

        try:
            profile_id = args[args_spec.index("profile_id")]
        except (IndexError, ValueError):
            profile_id = None

        updated = _update_pool(args[args_spec.index("media_type")], profile_id)

        if updated:
            logging.info("BAZARR pools update elapsed time: %s", time.time() - start)

        return f(*args, **kwargs)

    return decorated


# fmt: off

@update_pools
def generate_subtitles(path, languages, audio_language, sceneName, title, media_type,
                       forced_minimum_score=None, is_upgrade=False, profile_id=None):
    if not languages:
        return None

    if settings.general.getboolean('utf8_encode'):
        os.environ["SZ_KEEP_ENCODING"] = ""
    else:
        os.environ["SZ_KEEP_ENCODING"] = "True"

    language_set = set()

    if not isinstance(languages, (set, list)):
        languages = [languages]

    pool = _get_pool(media_type, profile_id)
    providers = pool.providers

    for l in languages:
        l, hi_item, forced_item = l
        logging.debug('BAZARR Searching subtitles for this file: ' + path)
        if hi_item == "True":
            hi = "force HI"
        else:
            hi = "force non-HI"

        # Fixme: This block should be updated elsewhere
        if forced_item == "True":
            pool.provider_configs['podnapisi']['only_foreign'] = True
            pool.provider_configs['subscene']['only_foreign'] = True
            pool.provider_configs['opensubtitles']['only_foreign'] = True
        else:
            pool.provider_configs['podnapisi']['only_foreign'] = False
            pool.provider_configs['subscene']['only_foreign'] = False
            pool.provider_configs['opensubtitles']['only_foreign'] = False

        # Always use alpha2 in API Request
        l = alpha3_from_alpha2(l)

        lang_obj = _get_lang_obj(l)

        if forced_item == "True":
            lang_obj = Language.rebuild(lang_obj, forced=True)
        if hi == "force HI":
            lang_obj = Language.rebuild(lang_obj, hi=True)

        language_set.add(lang_obj)

    minimum_score = settings.general.minimum_score
    minimum_score_movie = settings.general.minimum_score_movie
    use_postprocessing = settings.general.getboolean('use_postprocessing')
    postprocessing_cmd = settings.general.postprocessing_cmd
    single = settings.general.getboolean('single_language')


    # todo:
    """
    AsyncProviderPool:
    implement:
        blacklist=None,
        pre_download_hook=None,
        post_download_hook=None,
        language_hook=None
    """
    video = get_video(force_unicode(path), title, sceneName, providers=providers,
                      media_type=media_type)

    if video:
        handler = series_score if media_type == "series" else movie_score
        min_score, max_score, scores = _get_scores(media_type, minimum_score_movie, minimum_score)

        if providers:
            if forced_minimum_score:
                min_score = int(forced_minimum_score) + 1
            downloaded_subtitles = download_best_subtitles({video}, language_set, pool,
                                                           int(min_score), hi,
                                                           compute_score=compute_score,
                                                           throttle_time=None,  # fixme
                                                           score_obj=handler)
        else:
            downloaded_subtitles = None
            logging.info("BAZARR All providers are throttled")
            return None

        subz_mods = get_array_from(settings.general.subzero_mods)
        saved_any = False
        if downloaded_subtitles:
            for video, subtitles in downloaded_subtitles.items():
                if not subtitles:
                    continue

                for s in subtitles:
                    s.mods = subz_mods

                try:
                    fld = get_target_folder(path)
                    chmod = int(settings.general.chmod, 8) if not sys.platform.startswith(
                        'win') and settings.general.getboolean('chmod_enabled') else None
                    saved_subtitles = save_subtitles(video.original_path, subtitles, single=single,
                                                     tags=None,  # fixme
                                                     directory=fld,
                                                     chmod=chmod,
                                                     # formats=("srt", "vtt")
                                                     path_decoder=force_unicode
                                                     )
                except Exception as e:
                    logging.exception(
                        'BAZARR Error saving Subtitles file to disk for this file:' + path + ': ' + repr(e))
                    pass
                else:
                    saved_any = True
                    for subtitle in saved_subtitles:
                        downloaded_provider = subtitle.provider_name
                        downloaded_language_code3 = _get_download_code3(subtitle)

                        downloaded_language = language_from_alpha3(downloaded_language_code3)
                        downloaded_language_code2 = alpha2_from_alpha3(downloaded_language_code3)
                        audio_language_code2 = alpha2_from_language(audio_language)
                        audio_language_code3 = alpha3_from_language(audio_language)
                        downloaded_path = subtitle.storage_path
                        subtitle_id = subtitle.id
                        if subtitle.language.hi:
                            modifier_string = " HI"
                        elif subtitle.language.forced:
                            modifier_string = " forced"
                        else:
                            modifier_string = ""
                        logging.debug('BAZARR Subtitles file saved to disk: ' + downloaded_path)
                        if is_upgrade:
                            action = "upgraded"
                        else:
                            action = "downloaded"

                        percent_score = round(subtitle.score * 100 / max_score, 2)
                        message = downloaded_language + modifier_string + " subtitles " + action + " from " + \
                            downloaded_provider + " with a score of " + str(percent_score) + "%."

                        if media_type == 'series':
                            episode_metadata = TableEpisodes.select(TableEpisodes.sonarrSeriesId,
                                                                    TableEpisodes.sonarrEpisodeId)\
                                .where(TableEpisodes.path == path_mappings.path_replace_reverse(path))\
                                .dicts()\
                                .get()
                            series_id = episode_metadata['sonarrSeriesId']
                            episode_id = episode_metadata['sonarrEpisodeId']
                            sync_subtitles(video_path=path, srt_path=downloaded_path,
                                           forced=subtitle.language.forced,
                                           srt_lang=downloaded_language_code2, media_type=media_type,
                                           percent_score=percent_score,
                                           sonarr_series_id=episode_metadata['sonarrSeriesId'],
                                           sonarr_episode_id=episode_metadata['sonarrEpisodeId'])
                        else:
                            movie_metadata = TableMovies.select(TableMovies.radarrId)\
                                .where(TableMovies.path == path_mappings.path_replace_reverse_movie(path))\
                                .dicts()\
                                .get()
                            series_id = ""
                            episode_id = movie_metadata['radarrId']
                            sync_subtitles(video_path=path, srt_path=downloaded_path,
                                           forced=subtitle.language.forced,
                                           srt_lang=downloaded_language_code2, media_type=media_type,
                                           percent_score=percent_score,
                                           radarr_id=movie_metadata['radarrId'])

                        if use_postprocessing is True:
                            command = pp_replace(postprocessing_cmd, path, downloaded_path, downloaded_language,
                                                 downloaded_language_code2, downloaded_language_code3, audio_language,
                                                 audio_language_code2, audio_language_code3, subtitle.language.forced,
                                                 percent_score, subtitle_id, downloaded_provider, series_id, episode_id,
                                                 subtitle.language.hi)

                            if media_type == 'series':
                                use_pp_threshold = settings.general.getboolean('use_postprocessing_threshold')
                                pp_threshold = int(settings.general.postprocessing_threshold)
                            else:
                                use_pp_threshold = settings.general.getboolean('use_postprocessing_threshold_movie')
                                pp_threshold = int(settings.general.postprocessing_threshold_movie)

                            if not use_pp_threshold or (use_pp_threshold and percent_score < pp_threshold):
                                logging.debug("BAZARR Using post-processing command: {}".format(command))
                                postprocessing(command, path)
                            else:
                                logging.debug("BAZARR post-processing skipped because subtitles score isn't below this "
                                              "threshold value: " + str(pp_threshold) + "%")

                        # fixme: support multiple languages at once
                        if media_type == 'series':
                            reversed_path = path_mappings.path_replace_reverse(path)
                            reversed_subtitles_path = path_mappings.path_replace_reverse(downloaded_path)
                            notify_sonarr(episode_metadata['sonarrSeriesId'])
                            event_stream(type='series', action='update', payload=episode_metadata['sonarrSeriesId'])
                            event_stream(type='episode-wanted', action='delete',
                                         payload=episode_metadata['sonarrEpisodeId'])

                        else:
                            reversed_path = path_mappings.path_replace_reverse_movie(path)
                            reversed_subtitles_path = path_mappings.path_replace_reverse_movie(downloaded_path)
                            notify_radarr(movie_metadata['radarrId'])
                            event_stream(type='movie-wanted', action='delete', payload=movie_metadata['radarrId'])

                        track_event(category=downloaded_provider, action=action, label=downloaded_language)

                        yield message, reversed_path, downloaded_language_code2, downloaded_provider, subtitle.score, \
                               subtitle.language.forced, subtitle.id, reversed_subtitles_path, subtitle.language.hi

        if not saved_any:
            logging.debug('BAZARR No Subtitles were found for this file: ' + path)
            return None

    subliminal.region.backend.sync()

    logging.debug('BAZARR Ended searching Subtitles for file: ' + path)


@update_pools
def manual_search(path, profile_id, providers, providers_auth, sceneName, title, media_type):
    logging.debug('BAZARR Manually searching subtitles for this file: ' + path)

    final_subtitles = []

    initial_language_set = set()
    language_set = set()

    # where [3] is items list of dict(id, lang, forced, hi)
    language_items = get_profiles_list(profile_id=int(profile_id))['items']
    pool = _get_pool(media_type, profile_id)

    for language in language_items:
        forced = language['forced']
        hi = language['hi']
        audio_exclude = language['audio_exclude']
        language = language['language']

        lang = alpha3_from_alpha2(language)

        lang_obj = _get_lang_obj(lang)

        if forced == "True":
            lang_obj = Language.rebuild(lang_obj, forced=True)

            pool.provider_configs['podnapisi']['also_foreign'] = True
            pool.provider_configs['opensubtitles']['also_foreign'] = True

        if hi == "True":
            lang_obj = Language.rebuild(lang_obj, hi=True)

        initial_language_set.add(lang_obj)

    language_set = initial_language_set.copy()
    for language in language_set.copy():
        lang_obj_for_hi = language
        if not language.forced and not language.hi:
            lang_obj_hi = Language.rebuild(lang_obj_for_hi, hi=True)
        elif not language.forced and language.hi:
            lang_obj_hi = Language.rebuild(lang_obj_for_hi, hi=False)
        else:
            continue
        language_set.add(lang_obj_hi)

    minimum_score = settings.general.minimum_score
    minimum_score_movie = settings.general.minimum_score_movie
    use_postprocessing = settings.general.getboolean('use_postprocessing')
    postprocessing_cmd = settings.general.postprocessing_cmd
    if providers:
        video = get_video(force_unicode(path), title, sceneName, providers=providers,
                          media_type=media_type)
    else:
        logging.info("BAZARR All providers are throttled")
        return None
    if video:
        handler = series_score if media_type == "series" else movie_score
        min_score, max_score, scores = _get_scores(media_type, minimum_score_movie, minimum_score)

        try:
            if providers:
                subtitles = list_all_subtitles([video], language_set, pool)

                if 'subscene' in providers:
                    s_pool = _init_pool("movie", profile_id, {"subscene"})

                    subscene_language_set = set()
                    for language in language_set:
                        if language.forced:
                            subscene_language_set.add(language)
                    if len(subscene_language_set):
                        s_pool.provider_configs['subscene'] = {}
                        s_pool.provider_configs['subscene']['only_foreign'] = True
                        subtitles_subscene = list_all_subtitles([video], subscene_language_set, s_pool)
                        s_pool.provider_configs['subscene']['only_foreign'] = False
                        subtitles[video] += subtitles_subscene[video]
            else:
                subtitles = []
                logging.info("BAZARR All providers are throttled")
                return None
        except Exception as e:
            logging.exception("BAZARR Error trying to get Subtitle list from provider for this file: " + path)
        else:
            subtitles_list = []

            for s in subtitles[video]:
                try:
                    matches = s.get_matches(video)
                except AttributeError:
                    continue

                # skip wrong season/episodes
                if media_type == "series":
                    can_verify_series = True
                    if not s.hash_verifiable and "hash" in matches:
                        can_verify_series = False

                    if can_verify_series and not {"series", "season", "episode"}.issubset(matches):
                        logging.debug(u"BAZARR Skipping %s, because it doesn't match our series/episode", s)
                        continue

                initial_hi = None
                initial_hi_match = False
                for language in initial_language_set:
                    if s.language.basename == language.basename and \
                            s.language.forced == language.forced and \
                            s.language.hi == language.hi:
                        initial_hi = language.hi
                        initial_hi_match = True
                        break
                if not initial_hi_match:
                    initial_hi = None

                score, score_without_hash = compute_score(matches, s, video, hearing_impaired=initial_hi, score_obj=handler)
                if 'hash' not in matches:
                    not_matched = scores - matches
                    s.score = score_without_hash
                else:
                    s.score = score
                    not_matched = set()

                if s.hearing_impaired == initial_hi:
                    matches.add('hearing_impaired')
                else:
                    not_matched.add('hearing_impaired')

                releases = []
                if hasattr(s, 'release_info'):
                    if s.release_info is not None:
                        for s_item in s.release_info.split(','):
                            if s_item.strip():
                                releases.append(s_item)

                if s.uploader and s.uploader.strip():
                    s_uploader = s.uploader.strip()
                else:
                    s_uploader = None

                subtitles_list.append(
                    dict(score=round((score / max_score * 100), 2),
                         orig_score=score,
                         score_without_hash=score_without_hash,
                         forced=str(s.language.forced),
                         language=str(s.language.basename),
                         hearing_impaired=str(s.hearing_impaired),
                         provider=s.provider_name,
                         subtitle=codecs.encode(pickle.dumps(s.make_picklable()), "base64").decode(),
                         url=s.page_link,
                         matches=list(matches),
                         dont_matches=list(not_matched),
                         release_info=releases,
                         uploader=s_uploader))

            final_subtitles = sorted(subtitles_list, key=lambda x: (x['orig_score'], x['score_without_hash']),
                                     reverse=True)
            logging.debug('BAZARR ' + str(len(final_subtitles)) + " Subtitles have been found for this file: " + path)
            logging.debug('BAZARR Ended searching Subtitles for this file: ' + path)

    subliminal.region.backend.sync()

    return final_subtitles


@update_pools
def manual_download_subtitle(path, language, audio_language, hi, forced, subtitle, provider, providers_auth, sceneName,
                             title, media_type, profile_id):
    logging.debug('BAZARR Manually downloading Subtitles for this file: ' + path)

    if settings.general.getboolean('utf8_encode'):
        os.environ["SZ_KEEP_ENCODING"] = ""
    else:
        os.environ["SZ_KEEP_ENCODING"] = "True"

    subtitle = pickle.loads(codecs.decode(subtitle.encode(), "base64"))
    if hi == 'True':
        subtitle.language.hi = True
    else:
        subtitle.language.hi = False
    if forced == 'True':
        subtitle.language.forced = True
    else:
        subtitle.language.forced = False
    subtitle.mods = get_array_from(settings.general.subzero_mods)
    use_postprocessing = settings.general.getboolean('use_postprocessing')
    postprocessing_cmd = settings.general.postprocessing_cmd
    single = settings.general.getboolean('single_language')
    video = get_video(force_unicode(path), title, sceneName, providers={provider},
                      media_type=media_type)
    if video:
        min_score, max_score, scores = _get_scores(media_type)
        try:
            if provider:
                download_subtitles([subtitle], _get_pool(media_type, profile_id))
                logging.debug('BAZARR Subtitles file downloaded for this file:' + path)
            else:
                logging.info("BAZARR All providers are throttled")
                return None
        except Exception as e:
            logging.exception('BAZARR Error downloading Subtitles for this file ' + path)
            return None
        else:
            if not subtitle.is_valid():
                logging.exception('BAZARR No valid Subtitles file found for this file: ' + path)
                return
            try:
                score = round(subtitle.score / max_score * 100, 2)
                fld = get_target_folder(path)
                chmod = int(settings.general.chmod, 8) if not sys.platform.startswith(
                    'win') and settings.general.getboolean('chmod_enabled') else None
                saved_subtitles = save_subtitles(video.original_path, [subtitle], single=single,
                                                 tags=None,  # fixme
                                                 directory=fld,
                                                 chmod=chmod,
                                                 # formats=("srt", "vtt")
                                                 path_decoder=force_unicode)

            except Exception as e:
                logging.exception('BAZARR Error saving Subtitles file to disk for this file:' + path)
                return
            else:
                if saved_subtitles:
                    for saved_subtitle in saved_subtitles:
                        downloaded_provider = saved_subtitle.provider_name
                        downloaded_language_code3 = _get_download_code3(subtitle)

                        downloaded_language = language_from_alpha3(downloaded_language_code3)
                        downloaded_language_code2 = alpha2_from_alpha3(downloaded_language_code3)
                        audio_language_code2 = alpha2_from_language(audio_language)
                        audio_language_code3 = alpha3_from_language(audio_language)
                        downloaded_path = saved_subtitle.storage_path
                        subtitle_id = subtitle.id
                        logging.debug('BAZARR Subtitles file saved to disk: ' + downloaded_path)
                        if subtitle.language.hi:
                            modifier_string = " HI"
                        elif subtitle.language.forced:
                            modifier_string = " forced"
                        else:
                            modifier_string = ""
                        message = downloaded_language + modifier_string + " subtitles downloaded from " + \
                                  downloaded_provider + " with a score of " + str(score) + "% using manual search."

                        if media_type == 'series':
                            episode_metadata = TableEpisodes.select(TableEpisodes.sonarrSeriesId,
                                                                    TableEpisodes.sonarrEpisodeId)\
                                .where(TableEpisodes.path == path_mappings.path_replace_reverse(path))\
                                .dicts()\
                                .get()
                            series_id = episode_metadata['sonarrSeriesId']
                            episode_id = episode_metadata['sonarrEpisodeId']
                            sync_subtitles(video_path=path, srt_path=downloaded_path,
                                           forced=subtitle.language.forced,
                                           srt_lang=downloaded_language_code2, media_type=media_type,
                                           percent_score=score,
                                           sonarr_series_id=episode_metadata['sonarrSeriesId'],
                                           sonarr_episode_id=episode_metadata['sonarrEpisodeId'])
                        else:
                            movie_metadata = TableMovies.select(TableMovies.radarrId)\
                                .where(TableMovies.path == path_mappings.path_replace_reverse_movie(path))\
                                .dicts()\
                                .get()
                            series_id = ""
                            episode_id = movie_metadata['radarrId']
                            sync_subtitles(video_path=path, srt_path=downloaded_path,
                                           forced=subtitle.language.forced,
                                           srt_lang=downloaded_language_code2, media_type=media_type,
                                           percent_score=score, radarr_id=movie_metadata['radarrId'])

                        if use_postprocessing:
                            percent_score = round(subtitle.score * 100 / max_score, 2)
                            command = pp_replace(postprocessing_cmd, path, downloaded_path, downloaded_language,
                                                 downloaded_language_code2, downloaded_language_code3, audio_language,
                                                 audio_language_code2, audio_language_code3, subtitle.language.forced,
                                                 percent_score, subtitle_id, downloaded_provider, series_id, episode_id,
                                                 subtitle.language.hi)

                            if media_type == 'series':
                                use_pp_threshold = settings.general.getboolean('use_postprocessing_threshold')
                                pp_threshold = settings.general.postprocessing_threshold
                            else:
                                use_pp_threshold = settings.general.getboolean('use_postprocessing_threshold_movie')
                                pp_threshold = settings.general.postprocessing_threshold_movie

                            if not use_pp_threshold or (use_pp_threshold and score < float(pp_threshold)):
                                logging.debug("BAZARR Using post-processing command: {}".format(command))
                                postprocessing(command, path)
                            else:
                                logging.debug("BAZARR post-processing skipped because subtitles score isn't below this "
                                              "threshold value: " + pp_threshold + "%")

                        if media_type == 'series':
                            reversed_path = path_mappings.path_replace_reverse(path)
                            reversed_subtitles_path = path_mappings.path_replace_reverse(downloaded_path)
                            notify_sonarr(episode_metadata['sonarrSeriesId'])
                        else:
                            reversed_path = path_mappings.path_replace_reverse_movie(path)
                            reversed_subtitles_path = path_mappings.path_replace_reverse_movie(downloaded_path)
                            notify_radarr(movie_metadata['radarrId'])

                        track_event(category=downloaded_provider, action="manually_downloaded",
                                    label=downloaded_language)

                        return message, reversed_path, downloaded_language_code2, downloaded_provider, subtitle.score, \
                               subtitle.language.forced, subtitle.id, reversed_subtitles_path, subtitle.language.hi
                else:
                    logging.error(
                        "BAZARR Tried to manually download a Subtitles for file: " + path + " but we weren't able to do (probably throttled by " + str(
                            subtitle.provider_name) + ". Please retry later or select a Subtitles from another provider.")
                    return None

    subliminal.region.backend.sync()

    logging.debug('BAZARR Ended manually downloading Subtitles for file: ' + path)


def manual_upload_subtitle(path, language, forced, hi, title, scene_name, media_type, subtitle, audio_language):
    logging.debug('BAZARR Manually uploading subtitles for this file: ' + path)

    single = settings.general.getboolean('single_language')

    use_postprocessing = settings.general.getboolean('use_postprocessing')
    postprocessing_cmd = settings.general.postprocessing_cmd

    chmod = int(settings.general.chmod, 8) if not sys.platform.startswith(
        'win') and settings.general.getboolean('chmod_enabled') else None

    language = alpha3_from_alpha2(language)
    
    custom = CustomLanguage.from_value(language, "alpha3")
    if custom is None:
        lang_obj = Language(language)
    else:
        lang_obj = custom.subzero_language()

    if forced:
        lang_obj = Language.rebuild(lang_obj, forced=True)

    sub = Subtitle(
        lang_obj,
        mods = get_array_from(settings.general.subzero_mods)
    )

    sub.content = subtitle.read()
    if not sub.is_valid():
        logging.exception('BAZARR Invalid subtitle file: ' + subtitle.filename)
        sub.mods = None

    if settings.general.getboolean('utf8_encode'):
        sub.set_encoding("utf-8")

    saved_subtitles = []
    try:
        saved_subtitles = save_subtitles(path,
                                         [sub],
                                         single=single,
                                         tags=None,  # fixme
                                         directory=get_target_folder(path),
                                         chmod=chmod,
                                         # formats=("srt", "vtt")
                                         path_decoder=force_unicode)
    except:
        pass

    if len(saved_subtitles) < 1:
        logging.exception('BAZARR Error saving Subtitles file to disk for this file:' + path)
        return

    subtitle_path = saved_subtitles[0].storage_path

    if hi:
        modifier_string = " HI"
    elif forced:
        modifier_string = " forced"
    else:
        modifier_string = ""
    message = language_from_alpha3(language) + modifier_string + " Subtitles manually uploaded."

    if hi:
        modifier_code = ":hi"
    elif forced:
        modifier_code = ":forced"
    else:
        modifier_code = ""
    uploaded_language_code3 = language + modifier_code
    uploaded_language = language_from_alpha3(language) + modifier_string
    uploaded_language_code2 = alpha2_from_alpha3(language) + modifier_code
    audio_language_code2 = alpha2_from_language(audio_language)
    audio_language_code3 = alpha3_from_language(audio_language)

    if media_type == 'series':
        episode_metadata = TableEpisodes.select(TableEpisodes.sonarrSeriesId, TableEpisodes.sonarrEpisodeId)\
            .where(TableEpisodes.path == path_mappings.path_replace_reverse(path))\
            .dicts()\
            .get()
        series_id = episode_metadata['sonarrSeriesId']
        episode_id = episode_metadata['sonarrEpisodeId']
        sync_subtitles(video_path=path, srt_path=subtitle_path, srt_lang=uploaded_language_code2, media_type=media_type,
                       percent_score=100, sonarr_series_id=episode_metadata['sonarrSeriesId'], forced=forced,
                       sonarr_episode_id=episode_metadata['sonarrEpisodeId'])
    else:
        movie_metadata = TableMovies.select(TableMovies.radarrId)\
            .where(TableMovies.path == path_mappings.path_replace_reverse_movie(path))\
            .dicts()\
            .get()
        series_id = ""
        episode_id = movie_metadata['radarrId']
        sync_subtitles(video_path=path, srt_path=subtitle_path, srt_lang=uploaded_language_code2, media_type=media_type,
                       percent_score=100, radarr_id=movie_metadata['radarrId'], forced=forced)

    if use_postprocessing :
        command = pp_replace(postprocessing_cmd, path, subtitle_path, uploaded_language,
                             uploaded_language_code2, uploaded_language_code3, audio_language,
                             audio_language_code2, audio_language_code3, forced, 100, "1", "manual", series_id,
                             episode_id, hi=hi)
        postprocessing(command, path)

    if media_type == 'series':
        reversed_path = path_mappings.path_replace_reverse(path)
        reversed_subtitles_path = path_mappings.path_replace_reverse(subtitle_path)
        notify_sonarr(episode_metadata['sonarrSeriesId'])
        event_stream(type='series', action='update', payload=episode_metadata['sonarrSeriesId'])
        event_stream(type='episode-wanted', action='delete', payload=episode_metadata['sonarrEpisodeId'])
    else:
        reversed_path = path_mappings.path_replace_reverse_movie(path)
        reversed_subtitles_path = path_mappings.path_replace_reverse_movie(subtitle_path)
        notify_radarr(movie_metadata['radarrId'])
        event_stream(type='movie', action='update', payload=movie_metadata['radarrId'])
        event_stream(type='movie-wanted', action='delete', payload=movie_metadata['radarrId'])

    return message, reversed_path, reversed_subtitles_path


def series_download_subtitles(no):
    conditions = [(TableEpisodes.sonarrSeriesId == no),
                  (TableEpisodes.missing_subtitles != '[]')]
    conditions += get_exclusion_clause('series')
    episodes_details = TableEpisodes.select(TableEpisodes.path,
                                            TableEpisodes.missing_subtitles,
                                            TableEpisodes.monitored,
                                            TableEpisodes.sonarrEpisodeId,
                                            TableEpisodes.scene_name,
                                            TableShows.tags,
                                            TableShows.seriesType,
                                            TableEpisodes.audio_language,
                                            TableShows.title,
                                            TableEpisodes.season,
                                            TableEpisodes.episode,
                                            TableEpisodes.title.alias('episodeTitle'))\
        .join(TableShows, on=(TableEpisodes.sonarrSeriesId == TableShows.sonarrSeriesId))\
        .where(reduce(operator.and_, conditions))\
        .dicts()
    if not episodes_details:
        logging.debug("BAZARR no episode for that sonarrSeriesId have been found in database or they have all been "
                      "ignored because of monitored status, series type or series tags: {}".format(no))
        return

    count_episodes_details = len(episodes_details)

    for i, episode in enumerate(episodes_details):
        providers_list = get_providers()

        if providers_list:
            show_progress(id='series_search_progress_{}'.format(no),
                          header='Searching missing subtitles...',
                          name='{0} - S{1:02d}E{2:02d} - {3}'.format(episode['title'],
                                                                     episode['season'],
                                                                     episode['episode'],
                                                                     episode['episodeTitle']),
                          value=i,
                          count=count_episodes_details)

            audio_language_list = get_audio_profile_languages(episode_id=episode['sonarrEpisodeId'])
            if len(audio_language_list) > 0:
                audio_language = audio_language_list[0]['name']
            else:
                audio_language = 'None'
            
            languages = []
            for language in ast.literal_eval(episode['missing_subtitles']):
                # confirm if language is still missing or if cutoff have been reached
                confirmed_missing_subs = TableEpisodes.select(TableEpisodes.missing_subtitles) \
                    .where(TableEpisodes.sonarrEpisodeId == episode['sonarrEpisodeId']) \
                    .dicts() \
                    .get()
                if language not in ast.literal_eval(confirmed_missing_subs['missing_subtitles']):
                    continue

                if language is not None:
                    hi_ = "True" if language.endswith(':hi') else "False"
                    forced_ ="True" if language.endswith(':forced') else "False"
                    languages.append((language.split(":")[0], hi_, forced_))

            if not languages:
                continue

            for result in generate_subtitles(path_mappings.path_replace(episode['path']),
                                           languages,
                                           audio_language,
                                           str(episode['scene_name']),
                                           episode['title'], 'series'):
                if result:
                    message = result[0]
                    path = result[1]
                    forced = result[5]
                    if result[8]:
                        language_code = result[2] + ":hi"
                    elif forced:
                        language_code = result[2] + ":forced"
                    else:
                        language_code = result[2]
                    provider = result[3]
                    score = result[4]
                    subs_id = result[6]
                    subs_path = result[7]
                    store_subtitles(episode['path'], path_mappings.path_replace(episode['path']))
                    history_log(1, no, episode['sonarrEpisodeId'], message, path, language_code, provider, score,
                                subs_id, subs_path)
                    send_notifications(no, episode['sonarrEpisodeId'], message)
        else:
            logging.info("BAZARR All providers are throttled")
            break

    hide_progress(id='series_search_progress_{}'.format(no))


def episode_download_subtitles(no, send_progress=False):
    conditions = [(TableEpisodes.sonarrEpisodeId == no)]
    conditions += get_exclusion_clause('series')
    episodes_details = TableEpisodes.select(TableEpisodes.path,
                                            TableEpisodes.missing_subtitles,
                                            TableEpisodes.monitored,
                                            TableEpisodes.sonarrEpisodeId,
                                            TableEpisodes.scene_name,
                                            TableShows.tags,
                                            TableShows.title,
                                            TableShows.sonarrSeriesId,
                                            TableEpisodes.audio_language,
                                            TableShows.seriesType,
                                            TableEpisodes.title.alias('episodeTitle'),
                                            TableEpisodes.season,
                                            TableEpisodes.episode)\
        .join(TableShows, on=(TableEpisodes.sonarrSeriesId == TableShows.sonarrSeriesId))\
        .where(reduce(operator.and_, conditions))\
        .dicts()
    if not episodes_details:
        logging.debug("BAZARR no episode with that sonarrEpisodeId can be found in database:", str(no))
        return

    providers_auth = get_providers_auth()

    for episode in episodes_details:
        providers_list = get_providers()

        if providers_list:
            if send_progress:
                show_progress(id='episode_search_progress_{}'.format(no),
                              header='Searching missing subtitles...',
                              name='{0} - S{1:02d}E{2:02d} - {3}'.format(episode['title'],
                                                                         episode['season'],
                                                                         episode['episode'],
                                                                         episode['episodeTitle']),
                              value=0,
                              count=1)

            audio_language_list = get_audio_profile_languages(episode_id=episode['sonarrEpisodeId'])
            if len(audio_language_list) > 0:
                audio_language = audio_language_list[0]['name']
            else:
                audio_language = 'None'

            languages = []
            for language in ast.literal_eval(episode['missing_subtitles']):
                # confirm if language is still missing or if cutoff have been reached
                confirmed_missing_subs = TableEpisodes.select(TableEpisodes.missing_subtitles) \
                    .where(TableEpisodes.sonarrEpisodeId == episode['sonarrEpisodeId']) \
                    .dicts() \
                    .get()
                if language not in ast.literal_eval(confirmed_missing_subs['missing_subtitles']):
                    continue

                if language is not None:
                    hi_ = "True" if language.endswith(':hi') else "False"
                    forced_ ="True" if language.endswith(':forced') else "False"
                    languages.append((language.split(":")[0], hi_, forced_))

            if not languages:
                continue

            for result in generate_subtitles(path_mappings.path_replace(episode['path']),
                                       languages,
                                       audio_language,
                                       str(episode['scene_name']),
                                       episode['title'],
                                       'series'):
                if result:
                    message = result[0]
                    path = result[1]
                    forced = result[5]
                    if result[8]:
                        language_code = result[2] + ":hi"
                    elif forced:
                        language_code = result[2] + ":forced"
                    else:
                        language_code = result[2]
                    provider = result[3]
                    score = result[4]
                    subs_id = result[6]
                    subs_path = result[7]
                    store_subtitles(episode['path'], path_mappings.path_replace(episode['path']))
                    history_log(1, episode['sonarrSeriesId'], episode['sonarrEpisodeId'], message, path,
                                language_code, provider, score, subs_id, subs_path)
                    send_notifications(episode['sonarrSeriesId'], episode['sonarrEpisodeId'], message)

            if send_progress:
                hide_progress(id='episode_search_progress_{}'.format(no))
        else:
            logging.info("BAZARR All providers are throttled")
            break


def movies_download_subtitles(no):
    conditions = [(TableMovies.radarrId == no)]
    conditions += get_exclusion_clause('movie')
    movies = TableMovies.select(TableMovies.path,
                                TableMovies.missing_subtitles,
                                TableMovies.audio_language,
                                TableMovies.radarrId,
                                TableMovies.sceneName,
                                TableMovies.title,
                                TableMovies.tags,
                                TableMovies.monitored)\
        .where(reduce(operator.and_, conditions))\
        .dicts()
    if not len(movies):
        logging.debug("BAZARR no movie with that radarrId can be found in database:", str(no))
        return
    else:
        movie = movies[0]

    if ast.literal_eval(movie['missing_subtitles']):
        count_movie = len(ast.literal_eval(movie['missing_subtitles']))
    else:
        count_movie = 0

    audio_language_list = get_audio_profile_languages(movie_id=movie['radarrId'])
    if len(audio_language_list) > 0:
        audio_language = audio_language_list[0]['name']
    else:
        audio_language = 'None'

    languages = []
    providers_list = None

    for i, language in enumerate(ast.literal_eval(movie['missing_subtitles'])):
        providers_list = get_providers()

        if language is not None:
            hi_ = "True" if language.endswith(':hi') else "False"
            forced_ ="True" if language.endswith(':forced') else "False"
            languages.append((language.split(":")[0], hi_, forced_))

        if providers_list:
            # confirm if language is still missing or if cutoff have been reached
            confirmed_missing_subs = TableMovies.select(TableMovies.missing_subtitles) \
                .where(TableMovies.radarrId == movie['radarrId']) \
                .dicts() \
                .get()
            if language not in ast.literal_eval(confirmed_missing_subs['missing_subtitles']):
                continue

            show_progress(id='movie_search_progress_{}'.format(no),
                          header='Searching missing subtitles...',
                          name=movie['title'],
                          value=i,
                          count=count_movie)

    if providers_list:
        for result in generate_subtitles(path_mappings.path_replace_movie(movie['path']),
                                       languages,
                                       audio_language,
                                       str(movie['sceneName']),
                                       movie['title'],
                                       'movie'):

            if result:
                message = result[0]
                path = result[1]
                forced = result[5]
                if result[8]:
                    language_code = result[2] + ":hi"
                elif forced:
                    language_code = result[2] + ":forced"
                else:
                    language_code = result[2]
                provider = result[3]
                score = result[4]
                subs_id = result[6]
                subs_path = result[7]
                store_subtitles_movie(movie['path'], path_mappings.path_replace_movie(movie['path']))
                history_log_movie(1, no, message, path, language_code, provider, score, subs_id, subs_path)
                send_notifications_movie(no, message)
    else:
        logging.info("BAZARR All providers are throttled")

    hide_progress(id='movie_search_progress_{}'.format(no))


def _wanted_episode(episode):
    audio_language_list = get_audio_profile_languages(episode_id=episode['sonarrEpisodeId'])
    if len(audio_language_list) > 0:
        audio_language = audio_language_list[0]['name']
    else:
        audio_language = 'None'

    languages = []
    for language in ast.literal_eval(episode['missing_subtitles']):

        # confirm if language is still missing or if cutoff have been reached
        confirmed_missing_subs = TableEpisodes.select(TableEpisodes.missing_subtitles) \
            .where(TableEpisodes.sonarrEpisodeId == episode['sonarrEpisodeId']) \
            .dicts() \
            .get()
        if language not in ast.literal_eval(confirmed_missing_subs['missing_subtitles']):
            continue

        if is_search_active(desired_language=language, attempt_string=episode['failedAttempts']):
            TableEpisodes.update({TableEpisodes.failedAttempts:
                                  updateFailedAttempts(desired_language=language,
                                                       attempt_string=episode['failedAttempts'])}) \
                .where(TableEpisodes.sonarrEpisodeId == episode['sonarrEpisodeId']) \
                .execute()


            hi_ = "True" if language.endswith(':hi') else "False"
            forced_ ="True" if language.endswith(':forced') else "False"
            languages.append((language.split(":")[0], hi_, forced_))

        else:
            logging.debug(
                f"BAZARR Search is throttled by adaptive search for this episode {episode['path']} and "
                f"language: {language}")

    for result in generate_subtitles(path_mappings.path_replace(episode['path']),
                                   languages,
                                   audio_language,
                                   str(episode['scene_name']),
                                   episode['title'],
                                   'series'):
        if result:
            message = result[0]
            path = result[1]
            forced = result[5]
            if result[8]:
                language_code = result[2] + ":hi"
            elif forced:
                language_code = result[2] + ":forced"
            else:
                language_code = result[2]
            provider = result[3]
            score = result[4]
            subs_id = result[6]
            subs_path = result[7]
            store_subtitles(episode['path'], path_mappings.path_replace(episode['path']))
            history_log(1, episode['sonarrSeriesId'], episode['sonarrEpisodeId'], message, path,
                        language_code, provider, score, subs_id, subs_path)
            event_stream(type='series', action='update', payload=episode['sonarrSeriesId'])
            event_stream(type='episode-wanted', action='delete', payload=episode['sonarrEpisodeId'])
            send_notifications(episode['sonarrSeriesId'], episode['sonarrEpisodeId'], message)


def wanted_download_subtitles(sonarr_episode_id):
    episodes_details = TableEpisodes.select(TableEpisodes.path,
                                            TableEpisodes.missing_subtitles,
                                            TableEpisodes.sonarrEpisodeId,
                                            TableEpisodes.sonarrSeriesId,
                                            TableEpisodes.audio_language,
                                            TableEpisodes.scene_name,
                                            TableEpisodes.failedAttempts,
                                            TableShows.title)\
        .join(TableShows, on=(TableEpisodes.sonarrSeriesId == TableShows.sonarrSeriesId))\
        .where((TableEpisodes.sonarrEpisodeId == sonarr_episode_id))\
        .dicts()
    episodes_details = list(episodes_details)

    for episode in episodes_details:
        providers_list = get_providers()

        if providers_list:
            _wanted_episode(episode)
        else:
            logging.info("BAZARR All providers are throttled")
            break


def _wanted_movie(movie):
    audio_language_list = get_audio_profile_languages(movie_id=movie['radarrId'])
    if len(audio_language_list) > 0:
        audio_language = audio_language_list[0]['name']
    else:
        audio_language = 'None'

    languages = []

    for language in ast.literal_eval(movie['missing_subtitles']):
        # confirm if language is still missing or if cutoff have been reached
        confirmed_missing_subs = TableMovies.select(TableMovies.missing_subtitles) \
            .where(TableMovies.radarrId == movie['radarrId']) \
            .dicts() \
            .get()
        if language not in ast.literal_eval(confirmed_missing_subs['missing_subtitles']):
            continue

        if is_search_active(desired_language=language, attempt_string=movie['failedAttempts']):
            TableMovies.update({TableMovies.failedAttempts:
                                updateFailedAttempts(desired_language=language,
                                                     attempt_string=movie['failedAttempts'])}) \
                .where(TableMovies.radarrId == movie['radarrId']) \
                .execute()

            hi_ = "True" if language.endswith(':hi') else "False"
            forced_ ="True" if language.endswith(':forced') else "False"
            languages.append((language.split(":")[0], hi_, forced_))

        else:
            logging.info(f"BAZARR Search is throttled by adaptive search for this movie {movie['path']} and "
                         f"language: {language}")

    for result in generate_subtitles(path_mappings.path_replace_movie(movie['path']),
                                languages,
                                audio_language,
                                str(movie['sceneName']),
                                movie['title'], 'movie'):

        if result:
            message = result[0]
            path = result[1]
            forced = result[5]
            if result[8]:
                language_code = result[2] + ":hi"
            elif forced:
                language_code = result[2] + ":forced"
            else:
                language_code = result[2]
            provider = result[3]
            score = result[4]
            subs_id = result[6]
            subs_path = result[7]
            store_subtitles_movie(movie['path'], path_mappings.path_replace_movie(movie['path']))
            history_log_movie(1, movie['radarrId'], message, path, language_code, provider, score,
                              subs_id, subs_path)
            event_stream(type='movie-wanted', action='delete', payload=movie['radarrId'])
            send_notifications_movie(movie['radarrId'], message)


def wanted_download_subtitles_movie(radarr_id):
    movies_details = TableMovies.select(TableMovies.path,
                                        TableMovies.missing_subtitles,
                                        TableMovies.radarrId,
                                        TableMovies.audio_language,
                                        TableMovies.sceneName,
                                        TableMovies.failedAttempts,
                                        TableMovies.title)\
        .where((TableMovies.radarrId == radarr_id))\
        .dicts()
    movies_details = list(movies_details)

    for movie in movies_details:
        providers_list = get_providers()

        if providers_list:
            _wanted_movie(movie)
        else:
            logging.info("BAZARR All providers are throttled")
            break


def wanted_search_missing_subtitles_series():
    conditions = [(TableEpisodes.missing_subtitles != '[]')]
    conditions += get_exclusion_clause('series')
    episodes = TableEpisodes.select(TableEpisodes.sonarrSeriesId,
                                    TableEpisodes.sonarrEpisodeId,
                                    TableShows.tags,
                                    TableEpisodes.monitored,
                                    TableShows.title,
                                    TableEpisodes.season,
                                    TableEpisodes.episode,
                                    TableEpisodes.title.alias('episodeTitle'),
                                    TableShows.seriesType)\
        .join(TableShows, on=(TableEpisodes.sonarrSeriesId == TableShows.sonarrSeriesId))\
        .where(reduce(operator.and_, conditions))\
        .dicts()
    episodes = list(episodes)

    count_episodes = len(episodes)
    for i, episode in enumerate(episodes):
        show_progress(id='wanted_episodes_progress',
                      header='Searching subtitles...',
                      name='{0} - S{1:02d}E{2:02d} - {3}'.format(episode['title'],
                                                                 episode['season'],
                                                                 episode['episode'],
                                                                 episode['episodeTitle']),
                      value=i,
                      count=count_episodes)

        providers = get_providers()
        if providers:
            wanted_download_subtitles(episode['sonarrEpisodeId'])
        else:
            logging.info("BAZARR All providers are throttled")
            return

    hide_progress(id='wanted_episodes_progress')

    logging.info('BAZARR Finished searching for missing Series Subtitles. Check History for more information.')


def wanted_search_missing_subtitles_movies():
    conditions = [(TableMovies.missing_subtitles != '[]')]
    conditions += get_exclusion_clause('movie')
    movies = TableMovies.select(TableMovies.radarrId,
                                TableMovies.tags,
                                TableMovies.monitored,
                                TableMovies.title)\
        .where(reduce(operator.and_, conditions))\
        .dicts()
    movies = list(movies)

    count_movies = len(movies)
    for i, movie in enumerate(movies):
        show_progress(id='wanted_movies_progress',
                      header='Searching subtitles...',
                      name=movie['title'],
                      value=i,
                      count=count_movies)

        providers = get_providers()
        if providers:
            wanted_download_subtitles_movie(movie['radarrId'])
        else:
            logging.info("BAZARR All providers are throttled")
            return

    hide_progress(id='wanted_movies_progress')
    
    logging.info('BAZARR Finished searching for missing Movies Subtitles. Check History for more information.')


def convert_to_guessit(guessit_key, attr_from_db):
    try:
        return guessit(attr_from_db)[guessit_key]
    except KeyError:
        return attr_from_db


def refine_from_db(path, video):
    if isinstance(video, Episode):
        data = TableEpisodes.select(TableShows.title.alias('seriesTitle'),
                                    TableEpisodes.season,
                                    TableEpisodes.episode,
                                    TableEpisodes.title.alias('episodeTitle'),
                                    TableShows.year,
                                    TableShows.tvdbId,
                                    TableShows.alternateTitles,
                                    TableEpisodes.format,
                                    TableEpisodes.resolution,
                                    TableEpisodes.video_codec,
                                    TableEpisodes.audio_codec,
                                    TableEpisodes.path,
                                    TableShows.imdbId)\
            .join(TableShows, on=(TableEpisodes.sonarrSeriesId == TableShows.sonarrSeriesId))\
            .where((TableEpisodes.path == path_mappings.path_replace_reverse(path)))\
            .dicts()

        if len(data):
            data = data[0]
            video.series = re.sub(r'\s(\(\d\d\d\d\))', '', data['seriesTitle'])
            video.season = int(data['season'])
            video.episode = int(data['episode'])
            video.title = data['episodeTitle']
            # Commented out because Sonarr provided so much bad year
            # if data['year']:
            #     if int(data['year']) > 0: video.year = int(data['year'])
            video.series_tvdb_id = int(data['tvdbId'])
            video.alternative_series = ast.literal_eval(data['alternateTitles'])
            if data['imdbId'] and not video.series_imdb_id:
                video.series_imdb_id = data['imdbId']
            if not video.source:
                video.source = convert_to_guessit('source', str(data['format']))
            if not video.resolution:
                video.resolution = str(data['resolution'])
            if not video.video_codec:
                if data['video_codec']: video.video_codec = convert_to_guessit('video_codec', data['video_codec'])
            if not video.audio_codec:
                if data['audio_codec']: video.audio_codec = convert_to_guessit('audio_codec', data['audio_codec'])
    elif isinstance(video, Movie):
        data = TableMovies.select(TableMovies.title,
                                  TableMovies.year,
                                  TableMovies.alternativeTitles,
                                  TableMovies.format,
                                  TableMovies.resolution,
                                  TableMovies.video_codec,
                                  TableMovies.audio_codec,
                                  TableMovies.imdbId)\
            .where(TableMovies.path == path_mappings.path_replace_reverse_movie(path))\
            .dicts()

        if len(data):
            data = data[0]
            video.title = re.sub(r'\s(\(\d\d\d\d\))', '', data['title'])
            # Commented out because Radarr provided so much bad year
            # if data['year']:
            #     if int(data['year']) > 0: video.year = int(data['year'])
            if data['imdbId'] and not video.imdb_id:
                video.imdb_id = data['imdbId']
            video.alternative_titles = ast.literal_eval(data['alternativeTitles'])
            if not video.source:
                if data['format']: video.source = convert_to_guessit('source', data['format'])
            if not video.resolution:
                if data['resolution']: video.resolution = data['resolution']
            if not video.video_codec:
                if data['video_codec']: video.video_codec = convert_to_guessit('video_codec', data['video_codec'])
            if not video.audio_codec:
                if data['audio_codec']: video.audio_codec = convert_to_guessit('audio_codec', data['audio_codec'])

    return video


def refine_from_ffprobe(path, video):
    if isinstance(video, Movie):
        file_id = TableMovies.select(TableMovies.movie_file_id, TableMovies.file_size)\
            .where(TableMovies.path == path_mappings.path_replace_reverse_movie(path))\
            .dicts()\
            .get()
    else:
        file_id = TableEpisodes.select(TableEpisodes.episode_file_id, TableEpisodes.file_size)\
            .where(TableEpisodes.path == path_mappings.path_replace_reverse(path))\
            .dicts()\
            .get()

    if not isinstance(file_id, dict):
        return video

    if isinstance(video, Movie):
        data = parse_video_metadata(file=path, file_size=file_id['file_size'],
                                    movie_file_id=file_id['movie_file_id'])
    else:
        data = parse_video_metadata(file=path, file_size=file_id['file_size'],
                                    episode_file_id=file_id['episode_file_id'])

    if not data['ffprobe']:
        logging.debug("No FFprobe available in cache for this file: {}".format(path))
        return video

    logging.debug('FFprobe found: %s', data['ffprobe'])

    if 'video' not in data['ffprobe']:
        logging.debug('BAZARR FFprobe was unable to find video tracks in the file!')
    else:
        if 'resolution' in data['ffprobe']['video'][0]:
            if not video.resolution:
                video.resolution = data['ffprobe']['video'][0]['resolution']
        if 'codec' in data['ffprobe']['video'][0]:
            if not video.video_codec:
                video.video_codec = data['ffprobe']['video'][0]['codec']
        if 'frame_rate' in data['ffprobe']['video'][0]:
            if not video.fps:
                if isinstance(data['ffprobe']['video'][0]['frame_rate'], float):
                    video.fps = data['ffprobe']['video'][0]['frame_rate']
                else:
                    video.fps = data['ffprobe']['video'][0]['frame_rate'].magnitude

    if 'audio' not in data['ffprobe']:
        logging.debug('BAZARR FFprobe was unable to find audio tracks in the file!')
    else:
        if 'codec' in data['ffprobe']['audio'][0]:
            if not video.audio_codec:
                video.audio_codec = data['ffprobe']['audio'][0]['codec']
        for track in data['ffprobe']['audio']:
            if 'language' in track:
                video.audio_languages.add(track['language'].alpha3)

    return video


def upgrade_subtitles():
    days_to_upgrade_subs = settings.general.days_to_upgrade_subs
    minimum_timestamp = ((datetime.now() - timedelta(days=int(days_to_upgrade_subs))) -
                         datetime(1970, 1, 1)).total_seconds()

    if settings.general.getboolean('upgrade_manual'):
        query_actions = [1, 2, 3, 4, 6]
    else:
        query_actions = [1, 3]

    if settings.general.getboolean('use_sonarr'):
        upgradable_episodes_conditions = [(TableHistory.action << query_actions),
                                          (TableHistory.timestamp > minimum_timestamp),
                                          (TableHistory.score is not None)]
        upgradable_episodes_conditions += get_exclusion_clause('series')
        upgradable_episodes = TableHistory.select(TableHistory.video_path,
                                                  TableHistory.language,
                                                  TableHistory.score,
                                                  TableShows.tags,
                                                  TableShows.profileId,
                                                  TableEpisodes.audio_language,
                                                  TableEpisodes.scene_name,
                                                  TableEpisodes.title,
                                                  TableEpisodes.sonarrSeriesId,
                                                  TableHistory.action,
                                                  TableHistory.subtitles_path,
                                                  TableEpisodes.sonarrEpisodeId,
                                                  fn.MAX(TableHistory.timestamp).alias('timestamp'),
                                                  TableEpisodes.monitored,
                                                  TableEpisodes.season,
                                                  TableEpisodes.episode,
                                                  TableShows.title.alias('seriesTitle'),
                                                  TableShows.seriesType)\
            .join(TableShows, on=(TableHistory.sonarrSeriesId == TableShows.sonarrSeriesId))\
            .join(TableEpisodes, on=(TableHistory.sonarrEpisodeId == TableEpisodes.sonarrEpisodeId))\
            .where(reduce(operator.and_, upgradable_episodes_conditions))\
            .group_by(TableHistory.video_path, TableHistory.language)\
            .dicts()
        upgradable_episodes_not_perfect = []
        for upgradable_episode in upgradable_episodes:
            if upgradable_episode['timestamp'] > minimum_timestamp:
                try:
                    int(upgradable_episode['score'])
                except ValueError:
                    pass
                else:
                    if int(upgradable_episode['score']) < 360 or (settings.general.getboolean('upgrade_manual') and
                                                                  upgradable_episode['action'] in [2, 4, 6]):
                        upgradable_episodes_not_perfect.append(upgradable_episode)

        episodes_to_upgrade = []
        for episode in upgradable_episodes_not_perfect:
            if os.path.exists(path_mappings.path_replace(episode['subtitles_path'])) and int(episode['score']) < 357:
                episodes_to_upgrade.append(episode)

        count_episode_to_upgrade = len(episodes_to_upgrade)

    if settings.general.getboolean('use_radarr'):
        upgradable_movies_conditions = [(TableHistoryMovie.action << query_actions),
                                        (TableHistoryMovie.timestamp > minimum_timestamp),
                                        (TableHistoryMovie.score is not None)]
        upgradable_movies_conditions += get_exclusion_clause('movie')
        upgradable_movies = TableHistoryMovie.select(TableHistoryMovie.video_path,
                                                     TableHistoryMovie.language,
                                                     TableHistoryMovie.score,
                                                     TableMovies.profileId,
                                                     TableHistoryMovie.action,
                                                     TableHistoryMovie.subtitles_path,
                                                     TableMovies.audio_language,
                                                     TableMovies.sceneName,
                                                     fn.MAX(TableHistoryMovie.timestamp).alias('timestamp'),
                                                     TableMovies.monitored,
                                                     TableMovies.tags,
                                                     TableMovies.radarrId,
                                                     TableMovies.title)\
            .join(TableMovies, on=(TableHistoryMovie.radarrId == TableMovies.radarrId))\
            .where(reduce(operator.and_, upgradable_movies_conditions))\
            .group_by(TableHistoryMovie.video_path, TableHistoryMovie.language)\
            .dicts()
        upgradable_movies_not_perfect = []
        for upgradable_movie in upgradable_movies:
            if upgradable_movie['timestamp'] > minimum_timestamp:
                try:
                    int(upgradable_movie['score'])
                except ValueError:
                    pass
                else:
                    if int(upgradable_movie['score']) < 120 or (settings.general.getboolean('upgrade_manual') and
                                                                upgradable_movie['action'] in [2, 4, 6]):
                        upgradable_movies_not_perfect.append(upgradable_movie)

        movies_to_upgrade = []
        for movie in upgradable_movies_not_perfect:
            if os.path.exists(path_mappings.path_replace_movie(movie['subtitles_path'])) and int(movie['score']) < 117:
                movies_to_upgrade.append(movie)

        count_movie_to_upgrade = len(movies_to_upgrade)

    if settings.general.getboolean('use_sonarr'):
        for i, episode in enumerate(episodes_to_upgrade):
            providers_list = get_providers()

            show_progress(id='upgrade_episodes_progress',
                          header='Upgrading episodes subtitles...',
                          name='{0} - S{1:02d}E{2:02d} - {3}'.format(episode['seriesTitle'],
                                                                     episode['season'],
                                                                     episode['episode'],
                                                                     episode['title']),
                          value=i,
                          count=count_episode_to_upgrade)

            if not providers_list:
                logging.info("BAZARR All providers are throttled")
                return
            if episode['language'].endswith('forced'):
                language = episode['language'].split(':')[0]
                is_forced = "True"
                is_hi = "False"
            elif episode['language'].endswith('hi'):
                language = episode['language'].split(':')[0]
                is_forced = "False"
                is_hi = "True"
            else:
                language = episode['language'].split(':')[0]
                is_forced = "False"
                is_hi = "False"

            audio_language_list = get_audio_profile_languages(episode_id=episode['sonarrEpisodeId'])
            if len(audio_language_list) > 0:
                audio_language = audio_language_list[0]['name']
            else:
                audio_language = 'None'

            result = list(generate_subtitles(path_mappings.path_replace(episode['video_path']),
                                       [(language, is_hi, is_forced)],
                                       audio_language,
                                       str(episode['scene_name']),
                                       episode['title'],
                                       'series',
                                       forced_minimum_score=int(episode['score']),
                                       is_upgrade=True))

            if result:
                result = result[0]
                message = result[0]
                path = result[1]
                forced = result[5]
                if result[8]:
                    language_code = result[2] + ":hi"
                elif forced:
                    language_code = result[2] + ":forced"
                else:
                    language_code = result[2]
                provider = result[3]
                score = result[4]
                subs_id = result[6]
                subs_path = result[7]
                store_subtitles(episode['video_path'], path_mappings.path_replace(episode['video_path']))
                history_log(3, episode['sonarrSeriesId'], episode['sonarrEpisodeId'], message, path,
                            language_code, provider, score, subs_id, subs_path)
                send_notifications(episode['sonarrSeriesId'], episode['sonarrEpisodeId'], message)

        hide_progress(id='upgrade_episodes_progress')

    if settings.general.getboolean('use_radarr'):
        for i, movie in enumerate(movies_to_upgrade):
            providers_list = get_providers()

            show_progress(id='upgrade_movies_progress',
                          header='Upgrading movies subtitles...',
                          name=movie['title'],
                          value=i,
                          count=count_movie_to_upgrade)

            if not providers_list:
                logging.info("BAZARR All providers are throttled")
                return
            if movie['language'].endswith('forced'):
                language = movie['language'].split(':')[0]
                is_forced = "True"
                is_hi = "False"
            elif movie['language'].endswith('hi'):
                language = movie['language'].split(':')[0]
                is_forced = "False"
                is_hi = "True"
            else:
                language = movie['language'].split(':')[0]
                is_forced = "False"
                is_hi = "False"

            audio_language_list = get_audio_profile_languages(movie_id=movie['radarrId'])
            if len(audio_language_list) > 0:
                audio_language = audio_language_list[0]['name']
            else:
                audio_language = 'None'

            result = list(generate_subtitles(path_mappings.path_replace_movie(movie['video_path']),
                                       [(language, is_hi, is_forced)],
                                       audio_language,
                                       str(movie['sceneName']),
                                       movie['title'],
                                       'movie',
                                       forced_minimum_score=int(movie['score']),
                                       is_upgrade=True))
            if result:
                result = result[0]
                message = result[0]
                path = result[1]
                forced = result[5]
                if result[8]:
                    language_code = result[2] + ":hi"
                elif forced:
                    language_code = result[2] + ":forced"
                else:
                    language_code = result[2]
                provider = result[3]
                score = result[4]
                subs_id = result[6]
                subs_path = result[7]
                store_subtitles_movie(movie['video_path'],
                                      path_mappings.path_replace_movie(movie['video_path']))
                history_log_movie(3, movie['radarrId'], message, path, language_code, provider, score, subs_id, subs_path)
                send_notifications_movie(movie['radarrId'], message)

        hide_progress(id='upgrade_movies_progress')

    logging.info('BAZARR Finished searching for Subtitles to upgrade. Check History for more information.')


def postprocessing(command, path):
    try:
        encoding = getpreferredencoding()
        if os.name == 'nt':
            codepage = subprocess.Popen("chcp", shell=True, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, encoding=getpreferredencoding())
            # wait for the process to terminate
            out_codepage, err_codepage = codepage.communicate()
            encoding = out_codepage.split(':')[-1].strip()

        process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, encoding=encoding)
        # wait for the process to terminate
        out, err = process.communicate()

        out = out.replace('\n', ' ').replace('\r', ' ')

    except Exception as e:
        logging.error('BAZARR Post-processing failed for file ' + path + ' : ' + repr(e))
    else:
        if out == "":
            logging.info(
                'BAZARR Post-processing result for file ' + path + ' : Nothing returned from command execution')
        elif err:
            logging.error(
                'BAZARR Post-processing result for file ' + path + ' : ' + err.replace('\n', ' ').replace('\r', ' '))
        else:
            logging.info('BAZARR Post-processing result for file ' + path + ' : ' + out)


def sync_subtitles(video_path, srt_path, srt_lang, forced, media_type, percent_score, sonarr_series_id=None,
                   sonarr_episode_id=None, radarr_id=None):
    if forced:
        logging.debug('BAZARR cannot sync forced subtitles. Skipping sync routine.')
    elif not settings.subsync.getboolean('use_subsync'):
        logging.debug('BAZARR automatic syncing is disabled in settings. Skipping sync routine.')
    else:
        logging.debug(f'BAZARR automatic syncing is enabled in settings. We\'ll try to sync this '
                      f'subtitles: {srt_path}.')
        if media_type == 'series':
            use_subsync_threshold = settings.subsync.getboolean('use_subsync_threshold')
            subsync_threshold = settings.subsync.subsync_threshold
        else:
            use_subsync_threshold = settings.subsync.getboolean('use_subsync_movie_threshold')
            subsync_threshold = settings.subsync.subsync_movie_threshold

        if not use_subsync_threshold or (use_subsync_threshold and percent_score < float(subsync_threshold)):
            subsync.sync(video_path=video_path, srt_path=srt_path, srt_lang=srt_lang, media_type=media_type,
                         sonarr_series_id=sonarr_series_id, sonarr_episode_id=sonarr_episode_id, radarr_id=radarr_id)
            return True
        else:
            logging.debug("BAZARR subsync skipped because subtitles score isn't below this "
                          "threshold value: " + subsync_threshold + "%")
    return False


def _get_download_code3(subtitle):
    custom = CustomLanguage.from_value(subtitle.language, "language")
    if custom is None:
        return subtitle.language.alpha3
    return custom.alpha3


def _get_lang_obj(alpha3):
    sub = CustomLanguage.from_value(alpha3, "alpha3")
    if sub is None:
        return Language(alpha3)

    return sub.subzero_language()


def _get_scores(media_type, min_movie=None, min_ep=None):
    series = "series" == media_type
    handler = series_score if series else movie_score
    min_movie = min_movie or (60 * 100 / handler.max_score)
    min_ep = min_ep or (240 * 100 / handler.max_score)
    min_score_ = int(min_ep if series else min_movie)
    return handler.get_scores(min_score_)


def get_ban_list(profile_id):
    if profile_id:
        profile = get_profiles_list(profile_id)
        if profile:
            return {'must_contain': profile['mustContain'] or [],
                    'must_not_contain': profile['mustNotContain'] or []}
    return None


def is_search_active(desired_language, attempt_string):
    """
    Function to test if it's time to search again after a previous attempt matching the desired language. For 3 weeks,
    we search on a scheduled basis but after 3 weeks we start searching only once a week.

    @param desired_language: 2 letters language to search for in attempts
    @type desired_language: str
    @param attempt_string: string representation of a list of lists from database column failedAttempts
    @type attempt_string: str

    @return: return True if it's time to search again and False if not
    @rtype: bool
    """

    if settings.general.getboolean('adaptive_searching'):
        logging.debug("Adaptive searching is enable, we'll see if it's time to search again...")
        try:
            # let's try to get a list of lists from the string representation in database
            attempts = ast.literal_eval(attempt_string)
            if type(attempts) is not list:
                # attempts should be a list if not, it's malformed or None
                raise ValueError
        except ValueError:
            logging.debug("Adaptive searching: attempts is malformed. As a failsafe, search will run.")
            return True

        if not len(attempts):
            logging.debug("Adaptive searching: attempts list is empty, search will run.")
            return True

        # get attempts matching the desired language and sort them by timestamp ascending
        matching_attempts = sorted([x for x in attempts if x[0] == desired_language], key=lambda x: x[1])

        if not len(matching_attempts):
            logging.debug("Adaptive searching: there's no attempts matching desired language, search will run.")
            return True
        else:
            logging.debug(f"Adaptive searching: attempts matching language {desired_language}: {matching_attempts}")

        # try to get the initial and latest search timestamp from matching attempts
        initial_search_attempt = matching_attempts[0]
        latest_search_attempt = matching_attempts[-1]

        # try to parse the timestamps for those attempts
        try:
            initial_search_timestamp = datetime.fromtimestamp(initial_search_attempt[1])
            latest_search_timestamp = datetime.fromtimestamp(latest_search_attempt[1])
        except (OverflowError, ValueError, OSError):
            logging.debug("Adaptive searching: unable to parse initial and latest search timestamps, search will run.")
            return True
        else:
            logging.debug(f"Adaptive searching: initial search date for {desired_language} is "
                          f"{initial_search_timestamp}")
            logging.debug(f"Adaptive searching: latest search date for {desired_language} is {latest_search_timestamp}")

        # defining basic calculation variables
        now = datetime.now()
        if settings.general.adaptive_searching_delay.endswith('d'):
            extended_search_delay = timedelta(days=int(settings.general.adaptive_searching_delay[:1]))
        elif settings.general.adaptive_searching_delay.endswith('w'):
            extended_search_delay = timedelta(weeks=int(settings.general.adaptive_searching_delay[:1]))
        else:
            logging.debug(f"Adaptive searching: cannot parse adaptive_searching_delay from config file: "
                          f"{settings.general.adaptive_searching_delay}")
            return True
        logging.debug(f"Adaptive searching: delay after initial search value: {extended_search_delay}")

        if settings.general.adaptive_searching_delta.endswith('d'):
            extended_search_delta = timedelta(days=int(settings.general.adaptive_searching_delta[:1]))
        elif settings.general.adaptive_searching_delta.endswith('w'):
            extended_search_delta = timedelta(weeks=int(settings.general.adaptive_searching_delta[:1]))
        else:
            logging.debug(f"Adaptive searching: cannot parse adaptive_searching_delta from config file: "
                          f"{settings.general.adaptive_searching_delta}")
            return True
        logging.debug(f"Adaptive searching: delta between latest search and now value: {extended_search_delta}")

        if initial_search_timestamp + extended_search_delay > now:
            logging.debug(f"Adaptive searching: it's been less than {settings.general.adaptive_searching_delay} since "
                          f"initial search, search will run.")
            return True
        else:
            logging.debug(f"Adaptive searching: it's been more than {settings.general.adaptive_searching_delay} since "
                          f"initial search, let's check if it's time to search again.")
            if latest_search_timestamp + extended_search_delta <= now:
                logging.debug(
                    f"Adaptive searching: it's been more than {settings.general.adaptive_searching_delta} since "
                    f"latest search, search will run.")
                return True
            else:
                logging.debug(
                    f"Adaptive searching: it's been less than {settings.general.adaptive_searching_delta} since "
                    f"latest search, we're not ready to search yet.")
                return False

    logging.debug("adaptive searching is disabled, search will run.")
    return True


def updateFailedAttempts(desired_language, attempt_string):
    """
    Function to parse attempts and make sure we only keep initial and latest search timestamp for each language.

    @param desired_language: 2 letters language to search for in attempts
    @type desired_language: str
    @param attempt_string: string representation of a list of lists from database column failedAttempts
    @type attempt_string: str

    @return: return a string representation of a list of lists like [str(language_code), str(attempts)]
    @rtype: str
    """

    try:
        # let's try to get a list of lists from the string representation in database
        attempts = ast.literal_eval(attempt_string)
        logging.debug(f"Adaptive searching: current attempts value is {attempts}")
        if type(attempts) is not list:
            # attempts should be a list if not, it's malformed or None
            raise ValueError
    except ValueError:
        logging.debug("Adaptive searching: failed to parse attempts value, we'll use an empty list.")
        attempts = []

    matching_attempts = sorted([x for x in attempts if x[0] == desired_language], key=lambda x: x[1])
    logging.debug(f"Adaptive searching: attempts matching language {desired_language}: {matching_attempts}")

    filtered_attempts = sorted([x for x in attempts if x[0] != desired_language], key=lambda x: x[1])
    logging.debug(f"Adaptive searching: attempts not matching language {desired_language}: {filtered_attempts}")

    # get the initial search from attempts if there's one
    if len(matching_attempts):
        filtered_attempts.append(matching_attempts[0])

    # append current attempt with language and timestamp to attempts
    filtered_attempts.append([desired_language, datetime.timestamp(datetime.now())])

    updated_attempts = sorted(filtered_attempts, key=lambda x: x[0])
    logging.debug(f"Adaptive searching: updated attempts that will be saved to database is {updated_attempts}")

    return str(updated_attempts)
