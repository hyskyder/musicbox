#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author: omi
# @Date:   2014-07-15 15:48:27
# @Last Modified by:   AlanAlbert
# @Last Modified time: 2018-11-21 14:00:00
'''
网易云音乐 Player
'''
# Let's make some noise
from __future__ import (
    print_function, unicode_literals, division, absolute_import
)

import subprocess
import threading
import time
import os,signal
import random

from future.builtins import str

from .ui import Ui
from .storage import Storage
from .api import NetEase, MusicStreamer
from .cache import Cache
from .config import Config
from .utils import notify
from .const import WALKAROUND

from . import logger

log = logger.getLogger(__name__)


class Player(object):
    MODE_ORDERED = 0
    MODE_ORDERED_LOOP = 1
    MODE_SINGLE_LOOP = 2
    MODE_RANDOM = 3
    MODE_RANDOM_LOOP = 4

    def __init__(self,ui_instance,api_instance):
        self.config = Config()
        self.ui = ui_instance
        self.popen_handler = None
        # flag stop, prevent thread start
        self.playing_flag = False
        self.process_length = 0
        self.process_location = 0
        self.storage = Storage()
        self.cache = Cache()
        self.end_callback = None
        self.playing_song_changed_callback = None
        self.api = api_instance
        self.download_percent=None

    @property
    def info(self):
        return self.storage.database['player_info']

    @property
    def songs(self):
        return self.storage.database['songs']

    @property
    def index(self):
        return self.info['idx']

    @property
    def list(self):
        return self.info['player_list']

    @property
    def order(self):
        return self.info['playing_order']

    @property
    def mode(self):
        return self.info['playing_mode']

    @property
    def is_ordered_mode(self):
        return self.mode == Player.MODE_ORDERED

    @property
    def is_ordered_loop_mode(self):
        return self.mode == Player.MODE_ORDERED_LOOP

    @property
    def is_single_loop_mode(self):
        return self.mode == Player.MODE_SINGLE_LOOP

    @property
    def is_random_mode(self):
        return self.mode == Player.MODE_RANDOM

    @property
    def is_random_loop_mode(self):
        return self.mode == Player.MODE_RANDOM_LOOP

    @property
    def config_notifier(self):
        return self.config.get('notifier')

    @property
    def config_mpg123(self):
        return self.config.get('mpg123_parameters')

    @property
    def current_song(self):
        if not self.songs:
            return {}

        if not self.is_index_valid:
            return {}
        song_id = self.list[self.index]
        return self.songs.get(song_id, {})

    @property
    def playing_id(self):
        return self.current_song['song_id']

    @property
    def playing_name(self):
        return self.current_song['song_name']

    @property
    def is_empty(self):
        return len(self.list) == 0

    @property
    def is_index_valid(self):
        return 0 <= self.index < len(self.list)

    def update_download_percent(self,id,percent):
        if id != self.playing_id :
            raise Exception("Song has changed.")
        self.download_percent=percent

    def notify_playing(self):
        if not self.current_song:
            return

        if not self.config_notifier:
            return

        song = self.current_song
        notify('正在播放: {}\n{}-{}'.format(song['song_name'], song['artist'], song['album_name']))

    def notify_copyright_issue(self):
        log.warning('Song {} is unavailable due to copyright issue.'.format(self.playing_id))
        notify('版权限制，无法播放此歌曲')

    def change_mode(self, step=1):
        self.info['playing_mode'] = (self.info['playing_mode'] + step) % 5

    def build_playinfo(self):
        if not self.current_song:
            return

        self.ui.build_playinfo(
            self.current_song['song_name'],
            self.current_song['artist'],
            self.current_song['album_name'],
            self.current_song['quality'],
            time.time(), pause=not self.playing_flag
        )

    def add_songs(self, songs):
        for song in songs:
            song_id = str(song['song_id'])
            self.info['player_list'].append(song_id)
            if song_id in self.songs:
                self.songs[song_id].update(song)
            else:
                self.songs[song_id] = song

    def refresh_urls(self):
        songs = self.api.dig_info(self.list, 'refresh_urls')
        if songs:
            for song in songs:
                song_id = str(song['song_id'])
                if song_id in self.songs:
                    self.songs[song_id]['mp3_url'] = song['mp3_url']
                    self.songs[song_id]['expires'] = song['expires']
                    self.songs[song_id]['get_time'] = song['get_time']
                else:
                    self.songs[song_id] = song

    def stop(self):
        if not self.popen_handler:
            return

        self.playing_flag = False
        if not WALKAROUND.mpg123_Stdin_Direct_Mode:
            self.popen_handler.stdin.write(b'Q\n')
            self.popen_handler.stdin.flush()
            self.popen_handler.stdin.close()
            time.sleep(0.05)
        
        if self.popen_handler is not None and self.popen_handler.poll() is None:
            self.popen_handler.kill()
            time.sleep(0.05)
        while self.popen_handler is not None and self.popen_handler.poll() is None:
            self.popen_handler.terminate()
            time.sleep(0.05)

        self.popen_handler = None
        

    def tune_volume(self, up=0):
        if not self.popen_handler or WALKAROUND.mpg123_Stdin_Direct_Mode:
            return

        new_volume = self.info['playing_volume'] + up
        if new_volume > 100:
            new_volume = 100
        elif new_volume < 0:
            new_volume = 0

        self.info['playing_volume'] = new_volume
        self.popen_handler.stdin.write(
            'V {}\n'.format(self.info['playing_volume']).encode()
        )
        self.popen_handler.stdin.flush()

    def switch(self):
        if not self.popen_handler:
            return

        self.playing_flag = not self.playing_flag
        if WALKAROUND.mpg123_Stdin_Direct_Mode:
            if self.playing_flag:
                self.popen_handler.send_signal(signal.SIGCONT)
            else:
                self.popen_handler.send_signal(signal.SIGSTOP)
        else:
            self.popen_handler.stdin.write(b'P\n')
            self.popen_handler.stdin.flush()
        
        self.build_playinfo()
    
    def run_mpg123(self, on_exit, url):
        def feed_mpg123_worker(buffer,pipe,source_alive=lambda : False,MAX_WAIT=64):
            wait_blocks=2
            while buffer.qsize()<4 and source_alive():
                time.sleep(0.25)
            for chunk in iter(buffer.get,b"#END#"):
                try:
                    pipe.buffer.write(chunk)
                    if buffer.empty():
                        wait_blocks=min(wait_blocks*2,MAX_WAIT)
                        while buffer.qsize()<wait_blocks and source_alive():
                            time.sleep(0.25)
                except Exception as e:
                    log.warning(e)
                    break
            try:
                pipe.close()
            except:
                pass
            log.debug("Feed: Exit.")

        if WALKAROUND.mpg123_Stdin_Direct_Mode:
            self.download_percent=None
            download_thread=MusicStreamer(url.encode('utf-8'),report_cb=self.update_download_percent,song_id=self.playing_id)
            download_thread.start()
            self.process_location = 0
            self.process_length = 1
        
            local_popen_handler = subprocess.Popen(
                ['mpg123', '-v','-'] + self.config_mpg123,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                universal_newlines=True,bufsize=0
            )
            self.popen_handler=local_popen_handler
            feed_thread = threading.Thread(target=feed_mpg123_worker,
                args=(download_thread.buffer,local_popen_handler.stdin,download_thread.is_alive))
            feed_thread.start()
            play_next = False
            try:
                for line in iter(local_popen_handler.stderr.readline, ""):
                    if line.startswith("Frame#"):
                        '''Frame#  1000 [ 9619], Time: 00:20.33 [04:17.05], RVA:   off, Vol: 100(100), [ 1020092]'''
                        playtime=line.split(',')[1]
                        current_pos=float(playtime[7:9])*60+float(playtime[10:15])
                        self.process_location = int(current_pos)
                        self.process_length = 60*int(playtime[17:19])+int(current_pos+float(playtime[20:25]))
                    elif "finished" in line:
                        play_next = True
                        break
                    elif "err" in line:
                        log.debug("mgp123: "+line.strip())
                    if download_thread.poll() is not None and download_thread.poll()>0:
                        if download_thread.poll() == 403:
                            play_next = True
                        break
                    if self.popen_handler is None or local_popen_handler.poll() is not None:
                        break
            except Exception as e:
                log.note(e)
                play_next = False
            self.playing_flag = play_next
            download_thread.shutdown_signal.set()

        else:
            para = ['mpg123', '-R'] + self.config_mpg123
            self.popen_handler = subprocess.Popen(
                para,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            self.tune_volume()
            self.popen_handler.stdin.write(b'L ' + url.encode('utf-8') + b'\n')
            self.popen_handler.stdin.flush()

            endless_loop_cnt = 0
            while True:
                if not self.popen_handler:
                    break

                if self.popen_handler.poll() is not None:
                    break

                strout = self.popen_handler.stdout.readline().decode('utf-8').strip()
                if strout[:2] == '@F':
                    # playing, update progress
                    out = strout.split(' ')
                    self.process_location = int(float(out[3]))
                    self.process_length = int(float(out[3]) + float(out[4]))
                elif strout[:2] == '@E':
                    # error, stop song and move to next
                    self.playing_flag = True
                    self.notify_copyright_issue()
                    break
                elif strout == '@P 0':
                    # end, moving to next
                        self.playing_flag = True
                        break
                elif strout == '':
                    endless_loop_cnt += 1
                    # 有播放后没有退出，mpg123一直在发送空消息的情况，此处直接终止处理
                    if endless_loop_cnt > 100:
                        log.warning('mpg123 error, halt, endless loop and high cpu use, then we kill it')
                        break

            if self.playing_flag:
                self.next()
            else:
                self.stop()

        if local_popen_handler.poll() is None:
            local_popen_handler.terminate()
        del local_popen_handler
        
        download_thread.join()
        feed_thread.join()

        if play_next:
            self.next()

    def download_lyric(self, is_transalted=False):
        key = 'lyric' if not is_transalted else 'tlyric'

        if key not in self.songs[str(self.playing_id)]:
            self.songs[str(self.playing_id)][key] = []

        if len(self.songs[str(self.playing_id)][key]) > 0:
            return

        if not is_transalted:
            lyric = self.api.song_lyric(self.playing_id)
        else:
            lyric = self.api.song_tlyric(self.playing_id)

        self.songs[str(self.playing_id)][key] = lyric

    def download_song(self, song_id, song_name, artist, url):
        def write_path(song_id, path):
            self.songs[str(song_id)]['cache'] = path

        self.cache.add(song_id, song_name, artist, url, write_path)
        self.cache.start_download()

    def start_playing(self, on_exit, args):
        '''
        Runs the given args in subprocess.Popen, and then calls the function
        on_exit when the subprocess completes.
        on_exit is a callable object, and args is a lists/tuple of args
        that would give to subprocess.Popen.
        '''
        log.debug("%s,%s,%s" % (args['song_id'], args['song_name'], args['mp3_url']))
        if 'cache' in args.keys() and os.path.isfile(args['cache']):
            thread = threading.Thread(target=self.run_mpg123,
                                      args=(on_exit, args['cache']))
        else:
            thread = threading.Thread(target=self.run_mpg123,
                                      args=(on_exit, args['mp3_url']))
            cache_thread = threading.Thread(
                target=self.download_song,
                args=(args['song_id'], args['song_name'], args['artist'], args['mp3_url'])
            )
            cache_thread.start()

        thread.start()
        lyric_download_thread = threading.Thread(target=self.download_lyric)
        lyric_download_thread.start()
        tlyric_download_thread = threading.Thread(target=self.download_lyric, args=(True,))
        tlyric_download_thread.start()
        # returns immediately after the thread starts
        return thread

    def replay(self):
        if not self.is_index_valid:
            self.stop()
            if self.end_callback:
                log.debug('Callback')
                self.end_callback()
            return

        if not self.current_song:
            return

        if self.current_song['expires'] >= 0 and self.current_song['get_time'] >= 0 and time.time() - self.current_song['expires'] - self.current_song['get_time'] >= 0:
            log.debug("URL过期，刷新URL.");
            self.refresh_urls()

        self.playing_flag = True
        self.build_playinfo()
        self.notify_playing()
        self.start_playing(lambda: 0, self.current_song)

    def shuffle_order(self):
        del self.order[:]
        self.order.extend(list(range(0, len(self.list))))
        random.shuffle(self.order)
        self.info['random_index'] = 0

    def new_player_list(self, type, title, datalist, offset):
        self.info['player_list_type'] = type
        self.info['player_list_title'] = title
        # self.info['idx'] = offset
        self.info['player_list'] = []
        self.info['playing_order'] = []
        self.info['random_index'] = 0
        self.add_songs(datalist)

    def append_songs(self, datalist):
        self.add_songs(datalist)

    # switch_flag为true表示：
    # 在播放列表中 || 当前所在列表类型不在"songs"、"djchannels"、"fmsongs"中
    def play_or_pause(self, idx, switch_flag):
        if self.is_empty:
            return

        # if same "list index" and "playing index" --> same song :: pause/resume it
        if self.index == idx and switch_flag:
            if not self.popen_handler:
                self.replay()
            else:
                self.switch()
        else:
            self.info['idx'] = idx
            self.stop()
            self.replay()

    def _swap_song(self):
        now_songs = self.order.index(self.index)
        self.order[0], self.order[now_songs] = self.order[now_songs], self.order[0]

    def _need_to_shuffle(self):
        playing_order = self.order
        random_index = self.info['random_index']
        if random_index >= len(playing_order) or playing_order[random_index] != self.index:
            return True
        else:
            return False

    def next_idx(self):
        if not self.is_index_valid:
            return self.stop()
        playlist_len = len(self.list)

        if self.mode == Player.MODE_ORDERED:
            # make sure self.index will not over
            if self.info['idx'] < playlist_len:
                self.info['idx'] += 1

        elif self.mode == Player.MODE_ORDERED_LOOP:
            self.info['idx'] = (self.index + 1) % playlist_len

        elif self.mode == Player.MODE_SINGLE_LOOP:
            self.info['idx'] = self.info['idx']

        else:
            playing_order_len = len(self.order)
            if self._need_to_shuffle():
                self.shuffle_order()
                # When you regenerate playing list
                # you should keep previous song same.
                self._swap_song()
                playing_order_len = len(self.order)

            self.info['random_index'] += 1

            # Out of border
            if self.mode == Player.MODE_RANDOM_LOOP:
                self.info['random_index'] %= playing_order_len

            # Random but not loop, out of border, stop playing.
            if self.info['random_index'] >= playing_order_len:
                self.info['idx'] = playlist_len
            else:
                self.info['idx'] = self.order[self.info['random_index']]

        if self.playing_song_changed_callback is not None:
            self.playing_song_changed_callback()

    def next(self):
        self.stop()
        self.next_idx()
        self.replay()

    def prev_idx(self):
        if not self.is_index_valid:
            self.stop()
            return
        playlist_len = len(self.list)

        if self.mode == Player.MODE_ORDERED:
            if self.info['idx'] > 0:
                self.info['idx'] -= 1

        elif self.mode == Player.MODE_ORDERED_LOOP:
            self.info['idx'] = (self.info['idx'] - 1) % playlist_len

        elif self.mode == Player.MODE_SINGLE_LOOP:
            self.info['idx'] = self.info['idx']

        else:
            playing_order_len = len(self.order)
            if self._need_to_shuffle():
                self.shuffle_order()
                playing_order_len = len(self.order)

            self.info['random_index'] -= 1
            if self.info['random_index'] < 0:
                if self.mode == Player.MODE_RANDOM:
                    self.info['random_index'] = 0
                else:
                    self.info['random_index'] %= playing_order_len
            self.info['idx'] = self.order[self.info['random_index']]

        if self.playing_song_changed_callback is not None:
            self.playing_song_changed_callback()

    def prev(self):
        self.stop()
        self.prev_idx()
        self.replay()

    def shuffle(self):
        self.stop()
        self.info['playing_mode'] = Player.MODE_RANDOM
        self.shuffle_order()
        self.info['idx'] = self.info['playing_order'][self.info['random_index']]
        self.replay()

    def volume_up(self):
        self.tune_volume(5)

    def volume_down(self):
        self.tune_volume(-5)

    def update_size(self):
        self.ui.update_size()
        self.build_playinfo()

    def cache_song(self, song_id, song_name, artist, song_url):
        def on_exit(song_id, path):
            self.songs[str(song_id)]['cache'] = path
            self.cache.enable = False

        self.cache.enable = True
        self.cache.add(song_id, song_name, artist, song_url, on_exit)
        self.cache.start_download()
