"""Run with Django shell; preserves incident media without changing classifications.

Executes bounded ffmpeg captures for active cameras and recent indications.
Files are private, uniquely named, and accompanied by a checksum manifest.
"""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

from django.utils import timezone
from core.flood_camera_monitoring.infra.models import Camera, FloodDetectionRecord


root = Path('/app/private_incidents') / (timezone.now().strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
root.mkdir(parents=True, mode=0o700)
cameras = list(Camera.objects.filter(status=1).select_related('operational_snapshot'))


def capture(camera):
    folder = root / str(camera.id)
    folder.mkdir(mode=0o700)
    snapshot = getattr(camera, 'operational_snapshot', None)
    entry = {'camera_id': str(camera.id), 'description': camera.description,
             'region_id': str(camera.region_id or ''), 'captured_at': timezone.now().isoformat(),
             'classification': getattr(snapshot, 'classification', None),
             'prob_flooded': getattr(snapshot, 'prob_flooded', None),
             'model_version': getattr(snapshot, 'model_version', None), 'files': [], 'errors': []}
    if not camera.video_hls:
        entry['errors'].append('STREAM_NOT_CONFIGURED')
    else:
        for label, output, options in [
            ('snapshot', 'snapshot.jpg', ['-frames:v', '1']),
            ('clip', 'clip.mp4', ['-t', '12', '-an', '-c:v', 'libx264', '-preset', 'ultrafast', '-movflags', '+faststart']),
        ]:
            try:
                result = subprocess.run(['ffmpeg', '-nostdin', '-n', '-loglevel', 'error',
                    '-rw_timeout', '10000000', '-i', camera.video_hls, *options, str(folder / output)],
                    capture_output=True, timeout=35)
                if result.returncode:
                    entry['errors'].append(label + ':CAPTURE_FAILED')
            except subprocess.TimeoutExpired:
                entry['errors'].append(label + ':TIMEOUT')
        for path in folder.iterdir():
            if path.is_file():
                os.chmod(path, 0o600)
                entry['files'].append({'name': path.name, 'size': path.stat().st_size,
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        if (folder / 'clip.mp4').is_file():
            try:
                result = subprocess.run(['ffmpeg', '-nostdin', '-n', '-loglevel', 'error',
                    '-i', str(folder/'clip.mp4'), '-vf', 'fps=1/2', '-frames:v', '6',
                    str(folder/'sequence-%02d.jpg')], capture_output=True, timeout=20)
                if result.returncode:
                    entry['errors'].append('SEQUENCE_EXTRACTION_FAILED')
                for path in sorted(folder.glob('sequence-*.jpg')):
                    os.chmod(path, 0o600)
                    entry['files'].append({'name': path.name, 'size': path.stat().st_size,
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
            except subprocess.TimeoutExpired:
                entry['errors'].append('SEQUENCE_EXTRACTION_TIMEOUT')
    return entry


with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    entries = list(pool.map(capture, cameras))
recent = list(FloodDetectionRecord.objects.filter(created_at__gte=timezone.now()-__import__('datetime').timedelta(hours=6))
    .values('camera__description', 'camera_id', 'created_at', 'is_flooded', 'medium', 'confidence', 'image').order_by('-created_at')[:300])
manifest = root / 'manifest.json'
manifest.write_text(json.dumps({'captures': entries, 'recent_detections': recent}, default=str, indent=2))
os.chmod(manifest, 0o600)
print(json.dumps({'directory': str(root), 'captures': entries}, default=str))
