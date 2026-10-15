"""Independent watchdog: no Celery tasks, Redis state or synchronous request email."""
import json
import logging
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from datetime import timedelta
from django.utils import timezone

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.db.models import Q
from core.users.infra.models import User

logger = logging.getLogger('aqua.operations')


def atomic_json(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False))
    temporary.chmod(0o600)
    os.replace(temporary, path)


def probe(url, health=False):
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Aqua-Operations/1.0", "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=8) as response:
            if response.status != 200:
                return False
            if health:
                return json.load(response).get('status') == 'ok'
            return True
    except (OSError, ValueError, urllib.error.URLError):
        return False


class Command(BaseCommand):
    help = 'Monitor availability and email every active administrative account.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        root = Path(os.getenv('OPS_STATE_DIR', '/var/lib/aqua-monitor'))
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        state_path = root / 'state.json'
        try:
            state = json.loads(state_path.read_text())
        except FileNotFoundError:
            state = {'recipients': [], 'incidents': {}}
        except ValueError as exc:
            raise CommandError('Monitor state is invalid; preserve it and recover before restarting.') from exc
        interval = max(15, int(os.getenv('OPS_INTERVAL_SECONDS', '60')))
        while True:
            now = time.time()
            checks = {}
            try:
                close_old_connections()
                recipients = list(User.objects.filter(is_active=True).filter(
                    Q(type=User.UserType.ADMIN) | Q(is_staff=True) | Q(is_superuser=True)
                ).exclude(email='').values_list('email', flat=True))
                state['recipients'] = sorted(set(recipients))
                checks['administrators_database'] = True
            except Exception:
                # Last successful snapshot remains available while PostgreSQL is down.
                checks['administrators_database'] = False
            try:
                from core.flood_camera_monitoring.infra.models import Camera, CameraOperationalSnapshot
                active = Camera.objects.filter(status=Camera.CameraStatus.ACTIVE)
                total_active = active.count()
                snapshots = CameraOperationalSnapshot.objects.filter(camera__in=active)
                cutoff = timezone.now() - timedelta(minutes=int(os.getenv('OPS_CAMERA_STALE_MINUTES', '30')))
                recent = snapshots.filter(analyzed_at__gte=cutoff).count()
                unavailable = snapshots.filter(stream_status='UNAVAILABLE').count()
                # Alert on substantial loss of coverage, including absent snapshots.
                checks['camera_analysis_freshness'] = total_active == 0 or recent / total_active >= 0.5
                checks['camera_streams'] = total_active == 0 or unavailable / total_active < 0.5
            except Exception:
                checks['camera_analysis_freshness'] = False
            base = os.getenv('OPS_API_URL', 'http://web:8090').rstrip('/')
            checks['backend_database_redis'] = probe(base + '/health/', health=True)
            checks['camera_catalog'] = probe(base + '/api/flood_monitoring/cameras/?page_size=1')
            frontend = os.getenv('OPS_FRONTEND_URL', 'https://aqua.michalski.app/health')
            checks['frontend'] = probe(frontend)
            total, used, free = shutil.disk_usage(os.getenv('OPS_DISK_PATH', '/host-srv'))
            checks['disk_space'] = free / total > 0.10 and free > 2 * 1024**3
            # Each process writes its latest sanitized 5xx event; no request payloads.
            event_root = Path(os.getenv('OPS_EVENTS_DIR', '/var/lib/aqua-events'))
            checks.update(api_errors=True, camera_errors=True)
            for event_file in event_root.glob('*.json'):
                try:
                    event = json.loads(event_file.read_text())
                    if now - float(event['time']) < 300:
                        key = 'camera_errors' if event['component'] == 'cameras' else 'api_errors'
                        checks[key] = False
                except (OSError, ValueError, KeyError, TypeError):
                    checks['event_collection'] = False
            delivery_ok = bool(settings.EMAIL_HOST and settings.DEFAULT_FROM_EMAIL and state['recipients'])
            for name, healthy in checks.items():
                incident = state['incidents'].setdefault(name, {'bad': 0, 'good': 0, 'active': False, 'sent': {}})
                incident['bad'] = 0 if healthy else incident['bad'] + 1
                incident['good'] = incident['good'] + 1 if healthy else 0
                if not healthy and not incident['active'] and incident['bad'] >= 2:
                    incident.update(active=True, started=now, sent={}, recovered=False)
                if healthy and incident['active'] and incident['good'] >= 2:
                    incident.update(active=False, recovered=True, sent={})
                if not incident['active'] and not incident.get('recovered'):
                    continue
                # Failed SMTP delivery is retried next cycle. Successful sends are throttled.
                if options['dry_run']:
                    continue
                smtp_ready = bool(settings.EMAIL_HOST and settings.DEFAULT_FROM_EMAIL)
                if not smtp_ready:
                    continue
                kind = 'FALHA' if incident['active'] else 'RECUPERADO'
                descriptions = {
                    'disk_space': 'Pouco espaço no disco. Banco, fila e gravação de imagens podem parar.',
                    'backend_database_redis': 'A API, o PostgreSQL ou o Redis não passaram na verificação de saúde.',
                    'administrators_database': 'Não foi possível consultar o banco. Destinatários do último registro válido foram preservados.',
                    'camera_catalog': 'O catálogo de câmeras não está respondendo corretamente.',
                    'frontend': 'O frontend não respondeu à verificação de disponibilidade.',
                    'api_errors': 'A API registrou erros internos nos últimos cinco minutos.',
                    'camera_errors': 'A API de câmeras registrou erros internos nos últimos cinco minutos.',
                    'camera_analysis_freshness': 'Mais da metade das câmeras ativas está sem análise recente. Verifique workers, agendador e modelos.',
                    'camera_streams': 'Pelo menos metade das câmeras ativas está com transmissão indisponível.',
                    'event_collection': 'Não foi possível ler um registro de erro da aplicação.',
                }
                for recipient in state['recipients']:
                    last_sent = incident['sent'].get(recipient, 0)
                    if last_sent and (not incident['active'] or now - last_sent < 3600):
                        continue
                    body = (f'Aqua — {kind}\nComponente: {name}\n'
                            f'{descriptions.get(name, name)}\n'
                            f'Estado atual: {"indisponível" if incident["active"] else "normalizado"}.\n'
                            f'Verificado em UTC: {time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now))}\n'
                            'Ação: conferir /health/, logs dos serviços e espaço em /srv. '
                            'Não reinicialize volumes nem apague dados do banco.\n')
                    try:
                        if send_mail(f'[Aqua][{kind}] {name}', body, settings.DEFAULT_FROM_EMAIL,
                                     [recipient], fail_silently=False) != 1:
                            raise RuntimeError('Mail backend did not accept message')
                        incident['sent'][recipient] = now
                        atomic_json(state_path, state)
                    except Exception as exc:
                        delivery_ok = False
                        # Do not log SMTP exception text, addresses or credentials.
                        logger.error('Operational email delivery failed component=%s type=%s', name, type(exc).__name__)
                if incident.get('recovered') and state['recipients'] and all(
                    recipient in incident['sent'] for recipient in state['recipients']
                ):
                    incident['recovered'] = False
            status = {'checked_at': now, 'checks': checks,
                      'recipient_count': len(state['recipients']),
                      'email_configured': bool(settings.EMAIL_HOST and settings.DEFAULT_FROM_EMAIL),
                      'email_delivery_ok': delivery_ok,
                      'dry_run': options['dry_run']}
            atomic_json(root / 'status.json', status)
            atomic_json(state_path, state)
            self.stdout.write(json.dumps(status))
            if options['once']:
                return
            time.sleep(interval)
