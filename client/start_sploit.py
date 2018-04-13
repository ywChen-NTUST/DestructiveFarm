#!/usr/bin/env python3

import argparse
import binascii
import itertools
import json
import logging
import os
import random
import re
import stat
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from math import ceil
from urllib.parse import urljoin
from urllib.request import Request, urlopen


if sys.version_info < (3, 4):
    logging.critical('Support of Python < 3.4 is not implemented yet')
    sys.exit(1)


HEADER = '''
 ____            _                   _   _             _____
|  _ \  ___  ___| |_ _ __ _   _  ___| |_(_)_   _____  |  ___|_ _ _ __ _ __ ___
| | | |/ _ \/ __| __| '__| | | |/ __| __| \ \ / / _ \ | |_ / _` | '__| '_ ` _ `
| |_| |  __/\__ \ |_| |  | |_| | (__| |_| |\ V /  __/ |  _| (_| | |  | | | | |
|____/ \___||___/\__|_|   \__,_|\___|\__|_| \_/ \___| |_|  \__,_|_|  |_| |_| |_

Note that this software is highly destructive. Keep it away from children.
'''[1:]


HIGHLIGHT_COLORS = [31, 32, 34, 35, 36]


def highlight(text, bold=True, color=None):
    if os.name == 'nt':
        return text

    if color is None:
        color = random.choice(HIGHLIGHT_COLORS)
    return '\033[{}{}m'.format('1;' if bold else '', color) + text + '\033[0m'


log_format = '%(asctime)s {} %(message)s'.format(highlight('%(levelname)s', bold=False, color=33))
logging.basicConfig(format=log_format, datefmt='%H:%M:%S', level=logging.DEBUG)


def parse_args():
    parser = argparse.ArgumentParser(description='Run a sploit on all teams in a loop',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument('sploit', help='Sploit executable')
    parser.add_argument('--server-url', default='http://farm.kolambda.com:5000', help='Server URL')

    parser.add_argument('--pool-size', type=int, default=50,
                        help='Maximal number of concurrent sploit instances '
                             '(too little will make time limits for sploits smaller, '
                             'too big will eat all RAM on your computer)')
    parser.add_argument('--attack-period', type=float, default=120,
                        help='Rerun the sploit on all teams each N seconds '
                             '(too little will make time limits for sploits smaller, '
                             'too big will miss flags from some rounds)')

    parser.add_argument('-v', '--verbose-attacks', type=int, default=1,
                        help="Sploits' outputs and found flags will be shown for the N first attacks")

    group = parser.add_mutually_exclusive_group()
    group.add_argument('--not-per-team', action='store_true',
                       help='Run a single instance of the sploit instead of an instance per team')
    group.add_argument('--distribute',
                       help='Divide the team list to N parts (by address hash modulo N) '
                            'and run the sploits only on Kth part of it (K >= 1). '
                            'Syntax: --distribute K/N')

    return parser.parse_args()


def parse_distribute_argument(value):
    if value is not None:
        match = re.fullmatch(r'(\d+)/(\d+)', value)
        if match is not None:
            k, n = (int(match.group(1)), int(match.group(2)))
            if n >= 2 and 1 <= k <= n:
                return k, n
        raise ValueError('Wrong syntax for --distribute, use --distribute=K/N (N >= 2, 1 <= K <= N)')
    return None


SCRIPT_EXTENSIONS = ['.pl', '.py', '.rb']


def check_script_source(source):
    errors = []
    if source[:2] != '#!':
        errors.append(
            'Please use shebang (e.g. {}) as the first line of your script'.format(
                highlight('#!/usr/bin/env python3', bold=False, color=32)))
    if re.search(r'flush[(=]', source) is None:
        errors.append(
            'Please print the newline and call {} each time after your sploit outputs flags. '
            'In Python 3, you can use {}. '
            'Otherwise, the flags may be lost (if the sploit process is killed) or '
            'sent with a delay.'.format(
                highlight('flush()', bold=False, color=31),
                highlight('print(..., flush=True)', bold=False, color=32)))
    return errors


class InvalidSploitError(Exception):
    pass


def check_sploit(path):
    if not os.path.isfile(path):
        raise ValueError('No such file: {}'.format(path))

    is_script = os.path.splitext(path)[1].lower() in SCRIPT_EXTENSIONS
    if is_script:
        with open(path, 'r', errors='ignore') as f:
            source = f.read()
        errors = check_script_source(source)

        if errors:
            for message in errors:
                logging.error(message)
            raise InvalidSploitError('Sploit won\'t be run because of validation errors')

    if os.name != 'nt':
        file_mode = os.stat(path).st_mode
        if not file_mode & stat.S_IXUSR:
            if is_script:
                logging.info('Setting the executable bit on `{}`'.format(path))
                os.chmod(path, file_mode | stat.S_IXUSR)
            else:
                raise InvalidSploitError("The provided file doesn't appear to be executable")


class APIException(Exception):
    pass


SERVER_TIMEOUT = 5


def get_config(args):
    with urlopen(urljoin(args.server_url, '/api/get_config'), timeout=SERVER_TIMEOUT) as conn:
        if conn.status != 200:
            raise APIException(conn.read())

        return json.loads(conn.read().decode())


def post_flags(args, flags):
    sploit_name = os.path.basename(args.sploit)
    data = [{'flag': item['flag'], 'sploit': sploit_name, 'team': item['team']}
            for item in flags]

    req = Request(urljoin(args.server_url, '/api/post_flags'))
    req.add_header('Content-Type', 'application/json')
    with urlopen(req, data=json.dumps(data).encode(), timeout=SERVER_TIMEOUT) as conn:
        if conn.status != 200:
            raise APIException(conn.read())


exit_event = threading.Event()


def once_in_a_period(period):
    for iter_no in itertools.count(1):
        start_time = time.time()
        yield iter_no

        time_spent = time.time() - start_time
        if period > time_spent:
            exit_event.wait(period - time_spent)
        if exit_event.is_set():
            break


class FlagStorage:
    """
    Thread-safe storage comprised of a set and a post queue.

    Any number of threads can call add(), but only one "consumer thread"
    can call get_flags() and mark_as_sent().
    """

    def __init__(self):
        self._flags_seen = set()
        self._queue = []
        self._lock = threading.RLock()

    def add(self, flags, team_name):
        with self._lock:
            for item in flags:
                if item not in self._flags_seen:
                    self._flags_seen.add(item)
                    self._queue.append({'flag': item, 'team': team_name})

    def pick_flags(self, count):
        with self._lock:
            return self._queue[:count]

    def mark_as_sent(self, count):
        with self._lock:
            self._queue = self._queue[count:]

    @property
    def queue_size(self):
        with self._lock:
            return len(self._queue)


flag_storage = FlagStorage()


POST_PERIOD = 5
POST_FLAG_LIMIT = 10000


def run_post_loop(args):
    try:
        for _ in once_in_a_period(POST_PERIOD):
            flags_to_post = flag_storage.pick_flags(POST_FLAG_LIMIT)

            if flags_to_post:
                try:
                    post_flags(args, flags_to_post)

                    flag_storage.mark_as_sent(len(flags_to_post))
                    logging.info('{} flags posted to the server ({} in the queue)'.format(
                        len(flags_to_post), flag_storage.queue_size))
                except Exception as e:
                    logging.error("Can't post flags to the server: {}".format(repr(e)))
                    logging.info("The flags will be posted next time")
    except Exception as e:
        logging.critical('Posting loop died: {}'.format(repr(e)))
        shutdown()


display_output_lock = threading.RLock()


def display_sploit_output(team_name, output_lines):
    if not output_lines:
        logging.info('{}: No output from the sploit'.format(team_name))
        return

    prefix = highlight(team_name + ': ')
    with display_output_lock:
        print('\n' + '\n'.join(prefix + line.rstrip() for line in output_lines) + '\n')


def process_sploit_output(stream, args, team_name, flag_format, attack_no):
    try:
        output_lines = []
        instance_flags = set()

        while True:
            line = stream.readline()
            if not line:
                break
            line = line.decode(errors='replace')
            output_lines.append(line)

            line_flags = set(flag_format.findall(line))
            if line_flags:
                flag_storage.add(line_flags, team_name)
                instance_flags |= line_flags

        if attack_no <= args.verbose_attacks and not exit_event.is_set():
            # We don't want to spam the terminal on KeyboardInterrupt

            display_sploit_output(team_name, output_lines)
            if instance_flags:
                logging.info('Got {} flags from "{}": {}'.format(
                    len(instance_flags), team_name, instance_flags))
    except Exception as e:
        logging.error('Failed to process sploit output: {}'.format(repr(e)))


class InstanceManager:
    """
    Storage comprised of a dictionary of all running sploit instances and some statistics.

    The lock inside this class should be acquired by a class user. Any methods and fields
    must be used only when the lock is taken by the current thread.
    """

    def __init__(self):
        self._counter = 0
        self.instances = {}
        self.lock = threading.RLock()

        self.n_completed = 0
        self.n_killed = 0

    def register_start(self, process):
        instance_id = self._counter
        self.instances[instance_id] = process
        self._counter += 1
        return instance_id

    def register_stop(self, instance_id, was_killed):
        del self.instances[instance_id]

        self.n_completed += 1
        self.n_killed += was_killed


instance_manager = InstanceManager()


def run_sploit(args, team_name, team_addr, attack_no, max_runtime, flag_format):
    try:
        with instance_manager.lock:
            if exit_event.is_set():
                return

            # For sploits written in Python, this env variable forces the interpreter to flush
            # stdout and stderr after each newline. Note that this is not default behavior
            # if the sploit's output is redirected to a pipe.
            env = os.environ.copy()
            env['PYTHONUNBUFFERED'] = '1'

            need_close_fds = (os.name != 'nt')
            proc = subprocess.Popen([os.path.abspath(args.sploit), team_addr],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    bufsize=1, close_fds=need_close_fds, env=env)
            threading.Thread(target=lambda: process_sploit_output(
                proc.stdout, args, team_name, flag_format, attack_no)).start()

            instance_id = instance_manager.register_start(proc)
    except Exception as e:
        logging.error('Failed to run sploit: {}'.format(repr(e)))
        if isinstance(e, FileNotFoundError):
            logging.error('Check your shebang (use {} for compatibility) and presence of the sploit file'.format(
                highlight('#!/usr/bin/env ...', bold=False, color=32)))

        if attack_no == 1:
            shutdown()
        return

    try:
        try:
            proc.wait(timeout=max_runtime)
            need_kill = False
        except subprocess.TimeoutExpired:
            need_kill = True
            if attack_no <= args.verbose_attacks:
                logging.warning('Sploit for "{}" ({}) ran out of time'.format(team_name, team_addr))

        with instance_manager.lock:
            if need_kill:
                proc.kill()

            instance_manager.register_stop(instance_id, need_kill)
    except Exception as e:
        logging.error('Failed to finish sploit: {}'.format(repr(e)))


def show_time_limit_info(args, config, max_runtime, attack_no):
    if attack_no == 1:
        min_attack_period = config['FLAG_LIFETIME'] - config['SUBMIT_PERIOD'] - POST_PERIOD
        if args.attack_period >= min_attack_period:
            logging.warning("--attack-period should be < {:.1f} sec, "
                            "otherwise the sploit will not have time "
                            "to catch flags for each round before their expiration".format(min_attack_period))

    logging.info('Time limit for a sploit instance: {:.1f} sec'.format(max_runtime))
    if instance_manager.n_completed > 0:
        logging.info('Total {:.1f}% of instances ran out of time'.format(
            float(instance_manager.n_killed) / instance_manager.n_completed * 100))


PRINTED_TEAM_NAMES = 5


def get_target_teams(args, teams, distribute, attack_no):
    if args.not_per_team:
        return {'*': '0.0.0.0'}

    if distribute is not None:
        k, n = distribute
        teams = {name: addr for name, addr in teams.items()
                 if binascii.crc32(addr.encode()) % n == k - 1}

    if teams:
        if attack_no <= args.verbose_attacks:
            names = sorted(teams.keys())
            if len(names) > PRINTED_TEAM_NAMES:
                names = names[:PRINTED_TEAM_NAMES] + ['...']
            logging.info('Sploit will be run on {} teams: {}'.format(len(teams), ', '.join(names)))
    else:
        logging.error('There is no teams to attack for this farm client, fix "TEAMS" value '
                      'in your server config or the usage of --distribute')

    return teams


def main(args):
    try:
        distribute = parse_distribute_argument(args.distribute)

        check_sploit(args.sploit)
    except (ValueError, InvalidSploitError) as e:
        logging.critical(str(e))
        return

    print(highlight(HEADER))
    logging.info('Connecting to the farm server at {}'.format(args.server_url))

    threading.Thread(target=lambda: run_post_loop(args)).start()

    config = flag_format = None
    pool = ThreadPoolExecutor(max_workers=args.pool_size)
    for attack_no in once_in_a_period(args.attack_period):
        try:
            config = get_config(args)
            flag_format = re.compile(config['FLAG_FORMAT'])
        except Exception as e:
            logging.error("Can't get config from the server: {}".format(repr(e)))
            if attack_no == 1:
                return
            logging.info('Using the old config')
        teams = get_target_teams(args, config['TEAMS'], distribute, attack_no)
        if not teams:
            if attack_no == 1:
                return
            continue

        print()
        logging.info('Launching an attack #{}'.format(attack_no))

        max_runtime = args.attack_period / ceil(len(teams) / args.pool_size)
        show_time_limit_info(args, config, max_runtime, attack_no)

        for team_name, team_addr in teams.items():
            pool.submit(run_sploit, args, team_name, team_addr, attack_no, max_runtime, flag_format)


def shutdown():
    # Stop run_post_loop thread
    exit_event.set()
    # Kill all child processes (so consume_sploit_ouput and run_sploit also will stop)
    with instance_manager.lock:
        for proc in instance_manager.instances.values():
            proc.kill()


if __name__ == '__main__':
    try:
        main(parse_args())
    except KeyboardInterrupt:
        logging.info('Got Ctrl+C, shutting down')
    finally:
        shutdown()
