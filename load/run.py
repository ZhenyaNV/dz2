#!/usr/bin/env python3
"""One measured stage. Run from any directory, Python 3 standard library only."""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess as sp
import sys
import threading
import time
from results import summarize

ROOT = Path(__file__).resolve().parents[1]
K6 = 'grafana/k6:1.4.2'
PROFILER = 'evgenii_nevokshenov-profiler:0.4.1'
OUT = ROOT / 'results' / 'load'


def run(args, **kwargs):
    return sp.run(args, cwd=ROOT, check=True, **kwargs)


def capture(args):
    return run(args, stdout=sp.PIPE, text=True).stdout.strip()


def compose(*args):
    return ['docker', 'compose', *args]


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def git_value(*args):
    try:
        result = sp.run(['git', *args], cwd=ROOT, stdout=sp.PIPE, stderr=sp.DEVNULL, text=True)
    except FileNotFoundError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def identity():
    # No .env contents, credentials or Docker environment in the metadata.
    files = sorted(p for folder in ('app', 'scripts', 'load') for p in (ROOT / folder).rglob('*')
                   if p.is_file() and '__pycache__' not in p.parts)
    files += [ROOT / n for n in ('Dockerfile', 'compose.yaml', 'requirements.lock.txt')]
    manifest = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    return {'git_commit': git_value('rev-parse', 'HEAD'),
            'git_status': git_value('status', '--short'), 'sha256': manifest,
            'machine': capture(['uname', '-m']),
            'cpu': capture(['sysctl', '-n', 'machdep.cpu.brand_string']),
            'cores': capture(['sysctl', '-n', 'hw.logicalcpu']),
            'memory_bytes': capture(['sysctl', '-n', 'hw.memsize']),
            'docker_resources': capture(['docker', 'info', '--format', '{{.NCPU}} CPUs; {{.MemTotal}} bytes']),
            'k6': capture(['docker', 'run', '--rm', K6, 'version']),
            'http_path': 'k6 container -> app:8083 via Docker bridge; no browser',
            'account': 'demo', 'prefix': 'evgenii_nevokshenov'}


def idle(ids):
    good = 0
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        lines = capture(['docker', 'stats', '--no-stream', '--format', '{{.CPUPerc}}', *ids]).splitlines()
        good = good + 1 if len(lines) == 2 and all(float(x.rstrip('%')) < 5 for x in lines) else 0
        if good >= 3: return
        time.sleep(2)
    raise RuntimeError('Простой не наступил за 180 с; остановите фоновые задачи и повторите.')


def collect_connections(folder, stop):
    query = "SELECT count(*),count(*) FILTER (WHERE state='active' AND pid<>pg_backend_pid()) FROM pg_stat_activity WHERE datname=current_database() AND backend_type='client backend' AND pid<>pg_backend_pid();"
    with (folder / 'connections.jsonl').open('w') as f:
        while not stop.is_set():
            start = time.monotonic()
            try:
                value = capture(compose('exec', '-T', 'db', 'psql', '-U', 'app', '-d', 'evgenii_nevokshenov', '-At', '-c', query))
                total, active = map(int, value.split('|'))
                row = {'at': stamp(), 'total': total, 'active': active}
            except Exception as e:
                row = {'at': stamp(), 'collector_error': str(e)}
            f.write(json.dumps(row) + '\n'); f.flush()
            stop.wait(max(0, 2 - (time.monotonic() - start)))


def collect_stats(process, output):
    with output.open('w') as f:
        for line in process.stdout:
            clean = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', line).strip()
            if not clean: continue
            try:
                row = json.loads(clean)
                row['at'] = stamp()
            except ValueError:
                row = {'at': stamp(), 'collector_error': clean}
            f.write(json.dumps(row) + '\n'); f.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'stage', 'profile', 'smoke'])
    parser.add_argument('mode', nargs='?', choices=['mixed', 'heavy'], default='mixed')
    parser.add_argument('rate', nargs='?', type=int, default=2)
    parser.add_argument('--seconds', type=int, default=30)
    args = parser.parse_args()
    if args.rate < 1 or args.seconds < 1: parser.error('Частота и длительность должны быть положительными')
    if args.command == 'prepare':
        run(['docker', 'pull', K6])
        run(['docker', 'build', '-t', PROFILER, '-f', 'load/Dockerfile.profiler', '.'])
        return
    OUT.mkdir(parents=True, exist_ok=True)
    mode = 'heavy' if args.command == 'profile' else args.mode
    seconds = 5 if args.command == 'smoke' else args.seconds
    if args.command == 'profile' and seconds < 20:
        parser.error('Для профиля нужна ступень не короче 20 с')
    folder = OUT / (dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f') + f'-{args.command}-{mode}-{args.rate}')
    folder.mkdir()
    app_id = capture(compose('ps', '-q', 'app'))
    db_id = capture(compose('ps', '-q', 'db'))
    if not app_id or not db_id: raise RuntimeError('Сначала запустите docker compose up -d --build --wait')
    network = json.loads(capture(['docker', 'inspect', '--format', '{{json .NetworkSettings.Networks}}', app_id]))
    network = next(iter(network))
    # Pull/version calls before reset and measurement.
    metadata = identity()
    metadata['application_command'] = json.loads(capture(['docker', 'inspect', '--format', '{{json .Config.Cmd}}', app_id]))
    (folder / 'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    if args.command != 'smoke':
        with (folder / 'seed.json').open('w') as f:
            run(compose('exec', '-T', 'app', 'python', '-m', 'scripts.seed', '--size', 'work', '--reset'), stdout=f)
        idle([app_id, db_id])
    else:
        mode = 'heavy'
    # Read demo password from the running service without printing/storing it.
    password = capture(compose('exec', '-T', 'app', 'python', '-c', 'from app.config import DEMO_PASSWORD; print(DEMO_PASSWORD)'))
    env = os.environ.copy(); env['DEMO_PASSWORD'] = password
    container_name = 'evgenii-nevokshenov-k6-' + str(os.getpid())
    command = ['docker', 'run', '--rm', '--name', container_name, '--network', network,
               '--user', '0:0', '-v', f'{ROOT / "load"}:/scripts:ro', '-v', f'{folder}:/output',
               '-e', 'DEMO_PASSWORD', '-e', f'MODE={mode}', '-e', f'RATE={args.rate}',
               '-e', f'SECONDS={seconds}', K6, 'run', '--out', 'json=/output/raw.jsonl', '/scripts/test.js']
    stop = threading.Event()
    thread = threading.Thread(target=collect_connections, args=(folder, stop), daemon=True)
    started = stamp()
    # Streaming samples also reveal a saturated load generator.
    stats = sp.Popen(['docker', 'stats', '--format', '{{json .}}'], cwd=ROOT, stdout=sp.PIPE, stderr=sp.DEVNULL, text=True)
    stats_thread = threading.Thread(target=collect_stats, args=(stats, folder / 'docker-stats.jsonl'), daemon=True)
    stats_thread.start()
    thread.start()
    profiler = None
    load = None
    try:
        load = sp.Popen(command, cwd=ROOT, env=env, stdout=sp.PIPE, stderr=sp.STDOUT, text=True)
        if args.command == 'profile':
            # Wait for an actual measured request, not image startup or login.
            deadline = time.monotonic() + 15
            ready = False
            while time.monotonic() < deadline and load.poll() is None:
                raw = folder / 'raw.jsonl'
                if raw.exists() and '"metric":"requests_done"' in raw.read_text():
                    ready = True; break
                time.sleep(.25)
            if not ready: raise RuntimeError('Нагрузка не началась; профиль не снят')
            profile_log = (folder / 'profiler.log').open('w')
            profiler = sp.Popen(['docker', 'run', '--rm', '--name', container_name + '-profiler',
                '--pid', f'container:{app_id}', '--cap-add', 'SYS_PTRACE', '--security-opt', 'seccomp=unconfined',
                '-v', f'{folder}:/output', PROFILER, 'record', '--pid', '1', '--duration', '10',
                '--rate', '100', '--format', 'speedscope', '--output', '/output/profile.speedscope.json'],
                cwd=ROOT, stdout=profile_log, stderr=sp.STDOUT)
        with (folder / 'k6.txt').open('w') as f:
            for line in load.stdout:
                sys.stdout.write(line); sys.stdout.flush(); f.write(line)
        code = load.wait()
        if profiler is not None and profiler.wait() != 0:
            raise RuntimeError('py-spy завершился с ошибкой: см. profiler.log')
    finally:
        if load is not None and load.poll() is None:
            sp.run(['docker', 'stop', container_name], stdout=sp.DEVNULL, stderr=sp.DEVNULL)
            load.wait()
        if profiler is not None and profiler.poll() is None:
            sp.run(['docker', 'stop', container_name + '-profiler'], stdout=sp.DEVNULL, stderr=sp.DEVNULL)
            profiler.wait()
        stop.set(); thread.join(timeout=10)
        stats.terminate(); stats.wait(); stats_thread.join(timeout=5)
        with (folder / 'app.log').open('w') as f:
            run(compose('logs', '--no-color', '--since', started, 'app'), stdout=f)
    (folder / 'timing.json').write_text(json.dumps({'started_at': started, 'finished_at': stamp(),
        'k6_exit_code': code, 'profiled': args.command == 'profile'}, indent=2))
    if code not in (0, 99): raise RuntimeError(f'k6 не выполнил испытание: код {code}')
    summary = json.loads((folder / 'summary.json').read_text())
    report = summarize(summary, args.rate, seconds)
    (folder / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    try: main()
    except KeyboardInterrupt: sys.exit(130)
    except Exception as error:
        print(f'ОШИБКА: {error}', file=sys.stderr); sys.exit(1)
