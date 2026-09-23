import logging
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from django.core.files.base import ContentFile
from django.utils.dateparse import parse_date, parse_datetime

from core.addressing.models import City, Neighborhood, Region
from core.blog.infra.models import Post
from core.flood_camera_monitoring.infra.models import Camera
from core.flood_point_registering.infra.models import Flood_Point_Register
from core.forecast.infra.models import Forecast
from core.occurrences.infra.models import Occurrence
from core.users.infra.models import User
from core.uploader.models import Document, Image
from core.weather.infra.models import Weather
from core.sync.client import fetch_file

logger = logging.getLogger(__name__)

SITUATION_MAP: dict[str, int] = {
    "alerta": 1,
    "atencao": 2,
    "mobilizacao": 3,
    "normalidade": 4,
}

CAMERA_STATUS_MAP: dict[str, int] = {
    "ACTIVE": 1,
    "INACTIVE": 2,
    "OFFLINE": 3,
}


def _safe_uuid(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except (ValueError, AttributeError):
        return None


def _maybe_date(value: str | None):
    if not value:
        return None
    parsed = parse_date(str(value))
    if parsed:
        return parsed
    parsed_dt = parse_datetime(str(value))
    return parsed_dt.date() if parsed_dt else None


def _resolve_status(status_value: str | int | None, mapping: dict[str, int]) -> int | None:
    if status_value is None:
        return None
    if isinstance(status_value, int):
        return status_value
    return mapping.get(str(status_value).strip())


def sync_images(data: list, token: str) -> tuple[int, int]:
    """Import media before entities that reference ``Image.attachment_key``.

    Existing files are deliberately not downloaded again: an attachment key is
    immutable in the application and re-uploading an image creates a new key.
    This makes repeated syncs cheap and avoids leaving obsolete files in the
    configured Django storage.
    """
    created = 0
    updated = 0

    for item in data:
        attachment_key = _safe_uuid(item.get("attachment_key"))
        file_url = item.get("url")
        if not attachment_key or not isinstance(file_url, str) or not file_url:
            logger.warning("Imagem ignorada: attachment_key ou url inválido")
            continue

        image = Image.objects.filter(attachment_key=attachment_key).first()
        if image and image.file.name:
            description = item.get("description") or ""
            if image.description != description:
                image.description = description
                image.save(update_fields=["description"])
                updated += 1
            continue

        payload = fetch_file(file_url, token)
        filename = Path(urlsplit(file_url).path).name or f"{attachment_key}.img"
        description = item.get("description") or ""

        if image is None:
            image = Image(attachment_key=attachment_key, description=description)
            image.file = ContentFile(payload, name=filename)
            image.save()
            created += 1
        else:
            image.description = description
            image.file.save(filename, ContentFile(payload), save=False)
            image.save(update_fields=["description", "file"])
            updated += 1

    return created, updated


def sync_documents(data: list, token: str) -> tuple[int, int]:
    """Import documents idempotently using their attachment key as identity."""
    created = 0
    updated = 0

    for item in data:
        attachment_key = _safe_uuid(item.get("attachment_key"))
        file_url = item.get("url")
        if not attachment_key or not isinstance(file_url, str) or not file_url:
            logger.warning("Documento ignorado: attachment_key ou url inválido")
            continue

        document = Document.objects.filter(attachment_key=attachment_key).first()
        description = item.get("description") or ""
        if document and document.file.name:
            if document.description != description:
                document.description = description
                document.save(update_fields=["description"])
                updated += 1
            continue

        payload = fetch_file(file_url, token)
        filename = Path(urlsplit(file_url).path).name or f"{attachment_key}.bin"

        if document is None:
            document = Document(
                attachment_key=attachment_key,
                description=description,
            )
            document.file = ContentFile(payload, name=filename)
            document.save()
            created += 1
        else:
            document.description = description
            document.file.save(filename, ContentFile(payload), save=False)
            document.save(update_fields=["description", "file"])
            updated += 1

    return created, updated


def sync_addressing(data: list | dict, token: str) -> tuple[int, int]:
    if isinstance(data, list):
        body = data[0] if data else {}
    else:
        body = data

    regions_data = body.get("regions") or []
    created = 0
    updated = 0
    city_cache: dict[str, City] = {}

    def _get_city(name: str) -> City | None:
        if not name:
            return None
        if name not in city_cache:
            city, _ = City.objects.update_or_create(
                name=name.strip(),
                defaults={},
            )
            city_cache[name] = city
        return city_cache[name]

    for reg in regions_data:
        city_name = (reg.get("city") or "").strip()
        city_obj = _get_city(city_name) if city_name else None

        reg_id = _safe_uuid(reg.get("id"))
        if not reg_id:
            continue

        region_defaults = {
            "name": (reg.get("name") or "").strip(),
            "city": city_name,
        }
        if city_obj:
            region_defaults["city_ref"] = city_obj

        _, reg_created = Region.objects.update_or_create(
            id=reg_id,
            defaults=region_defaults,
        )
        if reg_created:
            created += 1
        else:
            updated += 1

        for nb in reg.get("neighborhoods") or []:
            nb_id = _safe_uuid(nb.get("id"))
            if not nb_id:
                continue

            nb_defaults = {
                "name": (nb.get("name") or "").strip(),
                "city": nb.get("city") or city_name,
            }
            if city_obj:
                nb_defaults["city_ref"] = city_obj
            nb_defaults["region"] = Region.objects.filter(id=reg_id).first()

            _, nb_created = Neighborhood.objects.update_or_create(
                id=nb_id,
                defaults=nb_defaults,
            )
            if nb_created:
                created += 1
            else:
                updated += 1

    return created, updated


def sync_users(data: list, token: str) -> tuple[int, int]:
    created = 0
    updated = 0
    for item in data:
        user_id = _safe_uuid(item.get("id"))
        if not user_id:
            continue

        defaults = {
            "name": (item.get("name") or "").strip(),
            "email": (item.get("email") or "").strip(),
        }

        profile_pic_url = item.get("profile_picture")
        if profile_pic_url and isinstance(profile_pic_url, str):
            defaults["profile_picture_url"] = profile_pic_url

        profile_picture_id = _safe_uuid(item.get("profile_picture_id"))
        if profile_picture_id:
            profile_picture = Image.objects.filter(
                attachment_key=profile_picture_id
            ).first()
            if profile_picture:
                defaults["profile_picture"] = profile_picture

        password = item.get("password")
        if password:
            defaults["password"] = password

        user_type = item.get("type")
        if user_type:
            defaults["type"] = user_type

        dob = item.get("date_of_birth")
        if dob:
            defaults["date_of_birth"] = _maybe_date(dob)

        google_sub = item.get("google_sub")
        if google_sub:
            defaults["google_sub"] = google_sub

        _, was_created = User.objects.update_or_create(
            id=user_id,
            defaults=defaults,
        )
        if was_created:
            created += 1
        else:
            updated += 1
    return created, updated


def sync_cameras(data: list, token: str) -> tuple[int, int]:
    created = 0
    updated = 0
    for item in data:
        cam_id = _safe_uuid(item.get("id"))
        if not cam_id:
            continue

        status_val = _resolve_status(item.get("status"), CAMERA_STATUS_MAP)

        defaults = {
            "description": (item.get("description") or "").strip(),
            "video_hls": (item.get("video_hls") or "") or "",
            "video_embed": (item.get("video_embed") or "") or "",
            "latitude": item.get("latitude"),
            "longitude": item.get("longitude"),
        }
        if status_val is not None:
            defaults["status"] = status_val

        nb_data = item.get("neighborhood")
        if nb_data and isinstance(nb_data, dict):
            nb_id = _safe_uuid(nb_data.get("id"))
            if nb_id:
                nb = Neighborhood.objects.filter(id=nb_id).first()
                if nb:
                    defaults["neighborhood"] = nb

        _, was_created = Camera.objects.update_or_create(
            id=cam_id,
            defaults=defaults,
        )
        if was_created:
            created += 1
        else:
            updated += 1
    return created, updated


def sync_occurrences(data: list, token: str) -> tuple[int, int]:
    created = 0
    updated = 0
    for item in data:
        date_val = _maybe_date(item.get("date"))
        if not date_val:
            continue

        situation_raw = item.get("situation")
        if isinstance(situation_raw, int):
            situation_int = situation_raw
        elif isinstance(situation_raw, str):
            situation_int = SITUATION_MAP.get(situation_raw.strip().lower(), 4)
        else:
            situation_int = 4

        obj, was_created = Occurrence.objects.get_or_create(
            date=date_val,
            type=(item.get("type") or "").strip(),
            neighborhood=(item.get("neighborhood") or "").strip(),
            defaults={"situation": situation_int},
        )
        if not was_created and obj.situation != situation_int:
            obj.situation = situation_int
            obj.save(update_fields=["situation"])
            updated += 1
        elif was_created:
            created += 1
    return created, updated


def sync_posts(data: list, token: str) -> tuple[int, int]:
    created = 0
    updated = 0
    for item in data:
        post_id = _safe_uuid(item.get("id"))
        if not post_id:
            continue

        defaults = {
            "title": (item.get("title") or "").strip(),
            "subject": (item.get("subject") or "").strip(),
            "content": item.get("content") or "",
        }
        author = item.get("author")
        if author:
            defaults["author"] = author

        for fk_field in ("banner_image", "content_image"):
            fk_data = item.get(fk_field)
            if fk_data and isinstance(fk_data, dict):
                img_id = _safe_uuid(fk_data.get("attachment_key"))
                if img_id:
                    img = Image.objects.filter(attachment_key=img_id).first()
                    if img:
                        defaults[fk_field] = img

        _, was_created = Post.objects.update_or_create(
            id=post_id,
            defaults=defaults,
        )
        if was_created:
            created += 1
        else:
            updated += 1
    return created, updated


def sync_weather(data: list, token: str) -> tuple[int, int]:
    created = 0
    updated = 0
    for item in data:
        date_val = _maybe_date(item.get("date"))
        lat = item.get("latitude")
        lon = item.get("longitude")
        neighborhood = (item.get("neighborhood") or "").strip()
        if not date_val or lat is None or lon is None or not neighborhood:
            continue

        defaults: dict = {
            "rain": item.get("rain"),
            "temperature": item.get("temperature"),
            "humidity": item.get("humidity"),
            "elevation": item.get("elevation"),
            "pressure": item.get("pressure"),
            "river_discharge": item.get("river_discharge"),
        }

        occ_id = item.get("occurrence")
        if occ_id is not None:
            try:
                occ = Occurrence.objects.filter(id=occ_id).first()
                if occ:
                    defaults["occurrence"] = occ
            except (ValueError, TypeError):
                pass

        _, was_created = Weather.objects.update_or_create(
            date=date_val,
            latitude=lat,
            longitude=lon,
            neighborhood=neighborhood,
            defaults=defaults,
        )
        if was_created:
            created += 1
        else:
            updated += 1
    return created, updated


def sync_forecasts(data: list, token: str) -> tuple[int, int]:
    created = 0
    updated = 0
    for item in data:
        date_val = _maybe_date(item.get("date"))
        lat = item.get("latitude")
        lon = item.get("longitude")
        if not date_val or lat is None or lon is None:
            continue

        defaults = {
            "flood": item.get("flood", 0.0),
            "probability": item.get("probability", 0.0),
        }

        _, was_created = Forecast.objects.update_or_create(
            latitude=lat,
            longitude=lon,
            date=date_val,
            defaults=defaults,
        )
        if was_created:
            created += 1
        else:
            updated += 1
    return created, updated


def sync_flood_points(data: list, token: str) -> tuple[int, int]:
    created = 0
    updated = 0
    for item in data:
        fp_id = item.get("id")
        if fp_id is None:
            continue

        city_id = item.get("city")
        neighborhood_id = item.get("neighborhood")

        city = None
        if city_id:
            try:
                city = City.objects.filter(id=_safe_uuid(str(city_id))).first()
            except (ValueError, TypeError):
                pass

        neighborhood = None
        if neighborhood_id:
            try:
                neighborhood = Neighborhood.objects.filter(
                    id=_safe_uuid(str(neighborhood_id))
                ).first()
            except (ValueError, TypeError):
                pass

        if not city or not neighborhood:
            logger.warning(
                "FloodPoint %s ignorado: cidade ou bairro não encontrado", fp_id
            )
            continue

        defaults = {
            "city": city,
            "neighborhood": neighborhood,
            "possibility": float(item.get("possibility", 0.0)),
            "props": item.get("props", {}),
        }

        finished_at = item.get("finished_at")
        if finished_at:
            defaults["finished_at"] = parse_datetime(str(finished_at))

        _, was_created = Flood_Point_Register.objects.update_or_create(
            id=fp_id,
            defaults=defaults,
        )
        if was_created:
            created += 1
        else:
            updated += 1
    return created, updated
