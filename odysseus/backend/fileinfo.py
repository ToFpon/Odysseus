"""Pages « Propriétés de l'image » et « Propriétés audio / vidéo » des Propriétés, comme Nautilus 46.

Nautilus les tient de deux extensions livrées avec lui (extensions/image-properties : GdkPixbuf + gexiv2 ;
extensions/audio-video-properties : GStreamer Discoverer). Ici, sans dépendance de plus : GdkPixbuf pour
le type et la taille, un lecteur EXIF / XMP maison (JPEG, TIFF et RAW à base TIFF, PNG, WebP), ffprobe
(déjà utilisé pour les durées) pour l'audio et la vidéo. Mêmes champs, même ordre, mêmes libellés.
Tout se lit dans un fil (réseau, gros fichiers) ; rien ici ne touche à GTK.
"""

from __future__ import annotations

import math
import re
import struct
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional

from ..i18n import decimal, ntr, tr
from . import media

# ----------------------------------------------------------------------
# EXIF : toutes les étiquettes des répertoires IFD0, Exif et GPS

_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


def parse_tiff(data: bytes) -> dict:
    """{(répertoire, étiquette): valeur} d'un bloc TIFF / EXIF. Rationnels → (num, den) ; listes gardées."""
    out: dict = {}
    if len(data) < 8 or data[:2] not in (b"II", b"MM"):
        return out
    end = "<" if data[:2] == b"II" else ">"

    def value(typ, count, raw):
        if typ == 2:
            return raw.split(b"\0", 1)[0].decode("utf-8", "replace").strip()
        if typ in (1, 6, 7):
            return raw if typ == 7 else list(raw)
        fmt = {3: "H", 4: "I", 8: "h", 9: "i", 11: "f", 12: "d"}.get(typ)
        if fmt:
            n = len(raw) // struct.calcsize(fmt)
            return list(struct.unpack_from(end + fmt * n, raw))
        if typ in (5, 10):
            fmt = "I" if typ == 5 else "i"
            vals = struct.unpack_from(end + fmt * (2 * count), raw)
            return [(vals[2 * i], vals[2 * i + 1]) for i in range(count)]
        return None

    def ifd(offset, name, depth=0):
        if depth > 2 or offset + 2 > len(data):
            return
        (n,) = struct.unpack_from(end + "H", data, offset)
        for i in range(min(n, 512)):
            pos = offset + 2 + 12 * i
            if pos + 12 > len(data):
                break
            tag, typ, count = struct.unpack_from(end + "HHI", data, pos)
            size = _SIZES.get(typ, 1) * count
            if size > 1 << 20:
                continue
            if size <= 4:
                raw = data[pos + 8:pos + 8 + size]
            else:
                (off,) = struct.unpack_from(end + "I", data, pos + 8)
                raw = data[off:off + size]
            if len(raw) < size:
                continue
            try:
                v = value(typ, count, raw)
            except struct.error:
                continue
            if v is None:
                continue
            if isinstance(v, list) and len(v) == 1:
                v = v[0]
            out[(name, tag)] = v
            if name == "0" and tag == 0x8769 and isinstance(v, int):
                ifd(v, "exif", depth + 1)
            elif name == "0" and tag == 0x8825 and isinstance(v, int):
                ifd(v, "gps", depth + 1)

    try:
        (first,) = struct.unpack_from(end + "I", data, 4)
        ifd(first, "0")
    except struct.error:
        pass
    return out


def read_blocks(path: str) -> tuple[bytes, bytes]:
    """(bloc EXIF au format TIFF, paquet XMP) d'une image ; vides si absents."""
    exif, xmp = b"", b""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if head[:2] == b"\xff\xd8":                                   # JPEG : segments APP1
                f.seek(2)
                while True:
                    marker = f.read(4)
                    if len(marker) < 4 or marker[0] != 0xFF or marker[1] in (0xD9, 0xDA):
                        break
                    size = struct.unpack(">H", marker[2:])[0] - 2
                    if marker[1] == 0xE1:
                        block = f.read(size)
                        if block[:6] == b"Exif\0\0" and not exif:
                            exif = block[6:]
                        elif block.startswith(b"http://ns.adobe.com/xap/1.0/\0") and not xmp:
                            xmp = block[29:]
                    else:
                        f.seek(size, 1)
            elif head[:2] in (b"II", b"MM"):                              # TIFF, DNG, NEF, CR2…
                f.seek(0)
                exif = f.read(8 * 1024 * 1024)
            elif head[:8] == b"\x89PNG\r\n\x1a\n":                       # PNG : eXIf, iTXt XMP
                f.seek(8)
                while True:
                    hdr = f.read(8)
                    if len(hdr) < 8:
                        break
                    size, kind = struct.unpack(">I4s", hdr)
                    if kind == b"IEND" or size > 16 << 20:
                        break
                    if kind == b"eXIf":
                        exif = f.read(size)
                        f.seek(4, 1)
                    elif kind == b"iTXt":
                        chunk = f.read(size)
                        f.seek(4, 1)
                        if chunk.startswith(b"XML:com.adobe.xmp\0"):
                            xmp = chunk[chunk.find(b"<"):]
                    else:
                        f.seek(size + 4, 1)
            elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":           # WebP : EXIF, XMP
                f.seek(12)
                while True:
                    hdr = f.read(8)
                    if len(hdr) < 8:
                        break
                    kind, size = struct.unpack("<4sI", hdr)
                    if size > 16 << 20:
                        break
                    if kind == b"EXIF":
                        exif = f.read(size)
                        if exif.startswith(b"Exif\0\0"):
                            exif = exif[6:]
                    elif kind == b"XMP ":
                        xmp = f.read(size)
                    else:
                        f.seek(size, 1)
                    if size & 1:
                        f.seek(1, 1)
    except (OSError, struct.error):
        pass
    return exif, xmp


# ----------------------------------------------------------------------
# XMP : quelques champs de Dublin Core et xmp:

def parse_xmp(packet: bytes) -> dict:
    """{nom local: valeur} pour title, description, subject, creator, rights, Rating, CreateDate."""
    out: dict = {}
    if not packet:
        return out
    text = packet.decode("utf-8", "replace")
    start, stop = text.find("<x:xmpmeta"), text.rfind("</x:xmpmeta>")
    if start < 0 or stop < 0:
        start, stop = text.find("<rdf:RDF"), text.rfind("</rdf:RDF>")
        if start < 0 or stop < 0:
            return out
        stop += len("</rdf:RDF>")
    else:
        stop += len("</x:xmpmeta>")
    try:
        root = ET.fromstring(text[start:stop])
    except ET.ParseError:
        return out
    wanted = {"title", "description", "subject", "creator", "rights", "Rating", "CreateDate"}
    for el in root.iter():
        for attr, val in el.attrib.items():            # xmp:Rating="3" posé en attribut
            local = attr.rsplit("}", 1)[-1]
            if local in wanted and local not in out and val.strip():
                out[local] = val.strip()
        local = el.tag.rsplit("}", 1)[-1]
        if local not in wanted or local in out:
            continue
        items = [li.text.strip() for li in el.iter() if li.tag.endswith("}li") and li.text and li.text.strip()]
        if items:
            out[local] = ", ".join(items)              # rdf:Alt (langues) → toutes ; Bag / Seq → liste
        elif el.text and el.text.strip():
            out[local] = el.text.strip()
    return out


# ----------------------------------------------------------------------
# Mise en forme des valeurs (ce qu'exiv2 « interprète » pour Nautilus)

def _num(v) -> Optional[float]:
    if isinstance(v, tuple):
        return v[0] / v[1] if v[1] else None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _short(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".")


def exposure_time(v) -> str:
    if isinstance(v, tuple) and v[0] and v[1]:
        t = v[0] / v[1]
        if t < 1:
            return f"1/{round(v[1] / v[0])} s"
        return f"{_short(t)} s"
    return ""


def exposure_mode(v) -> str:
    return {0: tr("Automatique"), 1: tr("Manuel"), 2: tr("Bracketing automatique")}.get(v, "")


def aperture(v) -> str:
    apex = _num(v)
    return f"F{_short(math.sqrt(2) ** apex)}" if apex is not None else ""


def flash(v) -> str:
    return (tr("Oui") if v & 1 else tr("Non")) if isinstance(v, int) else ""


def metering(v) -> str:
    return {0: tr("Inconnu"), 1: tr("Moyenne globale"), 2: tr("Moyenne pondérée au centre"), 3: tr("Spot"),
            4: tr("Multi-spot"), 5: tr("Matricielle"), 6: tr("Partielle"), 255: tr("Autre")}.get(v, "")


def focal_length(v) -> str:
    f = _num(v)
    return f"{_short(f)} mm" if f else ""


def exif_date(text: str) -> str:
    """« 2015:04:17 12:20:19 » → « 2015-04-17 12:20:19 », comme Nautilus (format_exif_datetime)."""
    try:
        return datetime.strptime(text[:19], "%Y:%m:%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return text or ""


def user_comment(v) -> str:
    """UserComment : 8 octets de jeu de caractères, puis le texte."""
    if not isinstance(v, (bytes, bytearray)) or len(v) <= 8:
        return ""
    head, body = bytes(v[:8]), bytes(v[8:])
    if head.startswith(b"UNICODE"):
        for enc in ("utf-16-be", "utf-16-le"):
            try:
                return body.decode(enc).strip("\0 ")
            except UnicodeDecodeError:
                pass
    return body.decode("utf-8", "replace").strip("\0 ")


def gps(tags: dict) -> str:
    def degrees(v):
        if isinstance(v, list) and len(v) == 3:
            d, m, s = (_num(x) or 0.0 for x in v)
            return d + m / 60 + s / 3600
        return None
    lat, lon = degrees(tags.get(("gps", 2))), degrees(tags.get(("gps", 4)))
    if lat is None or lon is None:
        return ""
    if tags.get(("gps", 1)) == "S":
        lat = -lat
    if tags.get(("gps", 3)) == "W":
        lon = -lon
    alt = _num(tags.get(("gps", 6))) or 0.0
    if tags.get(("gps", 5)) == 1:
        alt = -alt
    return "%f° %s %f° %s (%.0f m)" % (abs(lat), tr("N") if lat >= 0 else tr("S"),
                                       abs(lon), tr("E") if lon >= 0 else tr("O"), alt)


def image_properties(path: str) -> list[tuple[str, str]]:
    """Lignes de « Propriétés de l'image », dans l'ordre de Nautilus 46. Image illisible : la ligne
    d'erreur de Nautilus."""
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
    fmt, width, height = GdkPixbuf.Pixbuf.get_file_info(path)
    if fmt is None:
        return [(tr("Oups ! Quelque chose s'est mal passé."), tr("Impossible de charger les informations de l'image"))]
    exif, xmp_packet = read_blocks(path)
    tags = parse_tiff(exif) if exif else {}
    xmp = parse_xmp(xmp_packet)
    if tags.get(("0", 0x0112)) in (5, 6, 7, 8):       # photo tournée d'un quart de tour (Nautilus aussi)
        width, height = height, width
    rows = [(tr("Type d'image"), "%s (%s)" % (fmt.get_name(), fmt.get_description())),
            (tr("Largeur"), ntr("{0} pixel", "{0} pixels", width).format(width)),
            (tr("Hauteur"), ntr("{0} pixel", "{0} pixels", height).format(height))]

    def text(key):
        v = tags.get(key)
        return v.strip() if isinstance(v, str) else ""

    def exif_value(key, fmt_fn):
        v = tags.get(key)
        return fmt_fn(v) if v is not None else ""

    created = exif_date(text(("exif", 0x9003))) or xmp.get("CreateDate", "") or exif_date(text(("0", 0x0132)))
    fields = [
        (tr("Marque de l'appareil photo"), text(("0", 0x010F))),
        (tr("Modèle de l'appareil photo"), text(("0", 0x0110)) or text(("0", 0xC614))),
        (tr("Temps de pose"), exif_value(("exif", 0x829A), exposure_time)),
        (tr("Mode d'exposition"), exif_value(("exif", 0xA402), exposure_mode)),
        (tr("Ouverture focale"), exif_value(("exif", 0x9202), aperture)),
        (tr("Vitesse ISO"), str(tags[("exif", 0x8827)]) if isinstance(tags.get(("exif", 0x8827)), int) else ""),
        (tr("Flash déclenché"), exif_value(("exif", 0x9209), flash)),
        (tr("Mode de mesure"), exif_value(("exif", 0x9207), metering)),
        (tr("Distance focale"), exif_value(("exif", 0x920A), focal_length)),
        (tr("Logiciel"), text(("0", 0x0131))),
        (tr("Titre"), xmp.get("title", "")),
        (tr("Description"), xmp.get("description", "") or exif_value(("exif", 0x9286), user_comment)),
        (tr("Mots-clés"), xmp.get("subject", "")),
        (tr("Auteur"), xmp.get("creator", "") or text(("0", 0x013B))),
        (tr("Créé le"), created),
        (tr("Copyright"), xmp.get("rights", "")),
        (tr("Classement"), xmp.get("Rating", "")),
        (tr("Coordonnées"), gps(tags)),
    ]
    return rows + [(k, v) for k, v in fields if v]


# ----------------------------------------------------------------------
# Audio et vidéo (ffprobe)

# Noms de codecs comme les donne GStreamer à Nautilus (gst_pb_utils_get_codec_description) ; sinon le nom
# long de ffprobe
CODECS = {"mp3": "MPEG-1 Layer 3 (MP3)", "mp2": "MPEG-1 Layer 2 (MP2)", "aac": "MPEG-4 AAC", "flac": "Free Lossless Audio Codec (FLAC)",
          "vorbis": "Vorbis", "opus": "Opus", "ac3": "AC-3 (ATSC A/52)", "eac3": "E-AC-3 (ATSC A/52B)", "dts": "DTS",
          "alac": "Apple Lossless Audio (ALAC)", "wmav2": "Windows Media Audio 8", "truehd": "Dolby TrueHD",
          "pcm_s16le": "Uncompressed 16-bit PCM audio", "pcm_s24le": "Uncompressed 24-bit PCM audio",
          "h264": "H.264", "hevc": "H.265", "av1": "AV1", "vp9": "VP9", "vp8": "VP8", "mpeg4": "MPEG-4 Video",
          "mpeg2video": "MPEG-2 Video", "mpeg1video": "MPEG-1 Video", "theora": "Theora", "wmv3": "Windows Media Video 9",
          "mjpeg": "Motion JPEG", "prores": "Apple ProRes"}
CONTAINERS = {"matroska,webm": "Matroska", "mov,mp4,m4a,3gp,3g2,mj2": "Quicktime", "ogg": "Ogg", "avi": "AVI",
              "asf": "Advanced Streaming Format (ASF)", "mpegts": "MPEG-2 Transport Stream",
              "mpeg": "MPEG-1 System Stream", "flv": "Flash", "wav": "WAV", "aiff": "AIFF"}
RAW_AUDIO = {"mp3", "flac", "aac", "ac3", "eac3", "dts", "truehd"}      # pas de conteneur pour GStreamer


def duration_text(seconds: float) -> str:
    """« 2 minutes 37 secondes », comme Nautilus (time_to_string_text)."""
    total = int(seconds)
    hour, rest = divmod(total, 3600)
    minute, sec = divmod(rest, 60)
    hours = ntr("{0} heure", "{0} heures", hour).format(hour)
    mins = ntr("{0} minute", "{0} minutes", minute).format(minute)
    secs = ntr("{0} seconde", "{0} secondes", sec).format(sec)
    if hour:
        return tr("{0} {1} {2}").format(hours, mins, secs)
    if minute:
        return tr("{0} {1}").format(mins, secs)
    return secs if sec else tr("0 seconde")


def _rate(text: str) -> float:
    try:
        n, d = (float(p) for p in str(text).split("/"))
        return n / d if d else 0.0
    except ValueError:
        return 0.0


def media_properties(path: str) -> tuple[str, list[tuple[str, str]]]:
    """(titre de la page, lignes) pour un fichier audio / vidéo, dans l'ordre de Nautilus 46.
    Titre : « audio », « video » ou « both » ; sans ffprobe ou illisible : ligne d'erreur."""
    info = media.probe(path)
    if not info or "format" not in info:
        return "both", [(tr("Oups ! Quelque chose s'est mal passé."), tr("Impossible de lire ce fichier"))]
    streams = info.get("streams", [])
    # pochette d'un MP3 / FLAC (image fixe « attached_pic ») : ni vidéo ni dimensions, comme GStreamer
    video = [s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    kind = "both" if bool(video) == bool(audio) else ("audio" if audio else "video")
    fmt = info["format"]
    rows: list[tuple[str, str]] = []
    try:
        rows.append((tr("Durée"), duration_text(float(fmt.get("duration", 0)))))
    except (TypeError, ValueError):
        rows.append((tr("Durée"), tr("0 seconde")))
    name = fmt.get("format_name", "")
    if name not in RAW_AUDIO:
        container = CONTAINERS.get(name) or fmt.get("format_long_name") or name
        if name == "matroska,webm" and path.lower().endswith(".webm"):
            container = "WebM"
        elif name.startswith("mov,mp4") and not path.lower().endswith(".mov"):
            container = "ISO MP4/M4A"
        if container:
            rows.append((tr("Conteneur"), container))
    tags: dict = {}
    for src in [fmt] + streams:
        for k, v in (src.get("tags") or {}).items():
            tags.setdefault(k.lower(), str(v).strip())
    for key, label in (("title", tr("Titre")), ("artist", tr("Artiste")), ("album", tr("Album"))):
        if tags.get(key):
            rows.append((label, tags[key]))
    comment = tags.get("comment") or tags.get("description")
    if comment:
        rows.append((tr("Commentaire"), comment))
    year = re.match(r"\d{4}", tags.get("date") or tags.get("year") or tags.get("creation_time") or "")
    if year:
        rows.append((tr("Année"), year.group(0)))

    def codec(s):
        name = CODECS.get(s.get("codec_name", "")) or s.get("codec_long_name") or s.get("codec_name", "")
        if s.get("codec_name") in ("h264", "hevc") and s.get("profile"):
            name = "%s (%s Profile)" % (name, s["profile"])        # « H.264 (High Profile) », comme GStreamer
        return name

    def bitrate(s):
        try:
            b = int(s.get("bit_rate") or 0)
        except ValueError:
            b = 0
        return tr("{0} kbit/s").format(b // 1000) if b else ""

    if video:
        v = video[0]
        if v.get("width") and v.get("height"):
            rows.append((tr("Dimensions"), "%d × %d" % (v["width"], v["height"])))
        if codec(v):
            rows.append((tr("Codec vidéo"), codec(v)))
        if bitrate(v):
            rows.append((tr("Débit vidéo"), bitrate(v)))
        fps = _rate(v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/0")
        if fps > 1.0:
            rows.append((tr("Cadence"), ntr("{0} image par seconde", "{0} images par seconde",
                                            math.ceil(fps)).format(decimal(fps, 2))))
    if audio:
        a = audio[0]
        if codec(a):
            rows.append((tr("Codec audio"), codec(a)))
        if bitrate(a) or (not video and len(audio) == 1 and fmt.get("bit_rate")):
            rows.append((tr("Débit audio"), bitrate(a) or bitrate(fmt)))
        try:
            rate = int(a.get("sample_rate") or 0)
        except ValueError:
            rate = 0
        if rate:
            rows.append((tr("Échantillonnage"), f"{decimal(rate / 1000, 2)} kHz" if rate > 999 else f"{rate} Hz"))
        ch = int(a.get("channels") or 0)
        if ch:
            rows.append((tr("Canaux"), tr("Mono") if ch == 1 else tr("Stéréo") if ch == 2
                         else "%s %d.1" % (tr("Ambiophonique"), ch - 1)))
    return kind, rows


def page_title(kind: str) -> str:
    return {"image": tr("Propriétés de l'image"), "audio": tr("Propriétés audio"),
            "video": tr("Propriétés vidéo")}.get(kind, tr("Propriétés audio et vidéo"))


def kind_of(content_type: Optional[str], name: str) -> Optional[str]:
    """« image », « media » ou None : quelle page proposer pour ce fichier (d'après son type)."""
    ct = content_type or ""
    if ct.startswith("image/"):
        return "image"
    if ct.startswith(("audio/", "video/")) or media.is_media(name):
        return "media"
    return None
