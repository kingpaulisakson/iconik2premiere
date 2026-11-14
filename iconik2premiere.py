#!/usr/bin/env python3
"""
iconik comment CSV  ->  Premiere Pro sequence XML (FCP7 / XMEML v4) with markers.

Builds a sequence containing the reviewed media on V1 (+ A1/A2) and drops a
timeline marker at every iconik comment.

Usage:
    python iconik2premiere.py "downloaded-comments.csv"
    python iconik2premiere.py notes.csv --media "path/to/cut.mp4" --out notes.xml
    python iconik2premiere.py notes.csv --fps 23.976 --frames 9962
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import uuid
from fractions import Fraction
from urllib.parse import quote
from xml.sax.saxutils import escape

csv.field_size_limit(10000000)

# ---------------------------------------------------------------- media probe

# NTSC-family rates and how FCP7 XML spells them: fps -> (timebase, ntsc)
NTSC_RATES = {
    Fraction(24000, 1001): (24, True),
    Fraction(30000, 1001): (30, True),
    Fraction(48000, 1001): (48, True),
    Fraction(60000, 1001): (60, True),
}


def rate_tags(fps):
    """Return (timebase, ntsc_bool) for a given fps."""
    f = float(Fraction(fps).limit_denominator(1001))
    for ntsc_fps, tags in NTSC_RATES.items():
        if abs(f - float(ntsc_fps)) < 0.01:
            return tags
    return (int(round(f)), False)


def probe(path):
    """ffprobe the media. Returns an info dict, or None if that isn't possible."""
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_streams", "-show_format", path]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None

    data = json.loads(out)
    info = {"audio_channels": 0, "sample_rate": 48000}
    fmt_duration = float(data.get("format", {}).get("duration", 0) or 0)

    for st in data.get("streams", []):
        kind = st.get("codec_type")
        if kind == "video" and "fps" not in info:
            info["width"] = int(st["width"])
            info["height"] = int(st["height"])
            info["fps"] = Fraction(st.get("r_frame_rate", "24/1"))
            nb = str(st.get("nb_frames", ""))
            if nb.isdigit() and int(nb) > 0:
                info["frames"] = int(nb)
            else:
                dur = float(st.get("duration") or fmt_duration or 0)
                info["frames"] = int(round(dur * float(info["fps"])))
        elif kind == "audio" and not info["audio_channels"]:
            info["audio_channels"] = int(st.get("channels", 2))
            info["sample_rate"] = int(st.get("sample_rate", 48000))

    return info if "fps" in info else None


def find_media(csv_path):
    """Infer the reviewed media from the iconik CSV filename.

    iconik names its export '<original filename>-comment.csv', so strip that
    suffix and hunt for the file near the CSV.
    """
    m = re.match(r"^(.*)-comment\.csv$", os.path.basename(csv_path), re.I)
    if not m:
        return None
    target = m.group(1).lower()

    root = os.path.dirname(os.path.abspath(csv_path))
    for _ in range(3):
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f.lower() == target:
                    return os.path.join(dirpath, f)
        parent = os.path.dirname(root)
        if parent == root:
            break
        root = parent
    return None


# ------------------------------------------------------------------ csv parse

def author_of(row):
    first = (row.get("user_first_name") or "").strip()
    last = (row.get("user_last_name") or "").strip()
    if first or last:
        return " ".join(p for p in (first, last) if p)
    return (row.get("share_user_email") or "").strip()


def clean(text):
    """Normalise line endings and drop XML-illegal control characters."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(c for c in text if c in "\n\t" or ord(c) >= 32)
    return text.strip()


def read_comments(csv_path, include_resolved):
    """Return top-level comments, each with its replies attached."""
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    items, order = {}, []
    for row in rows:
        if (row.get("segment_type") or "").upper() != "COMMENT":
            continue
        status = (row.get("status") or "").upper()
        if not include_resolved and status and status != "ACTIVE":
            continue
        text = clean(row.get("segment_text"))
        if not text:
            continue

        cid = row.get("id") or str(uuid.uuid4())
        items[cid] = {
            "id": cid,
            "parent": (row.get("parent_id") or "").strip(),
            "ms": int(float(row.get("time_start_milliseconds") or 0)),
            "text": text,
            "author": author_of(row),
            "created": row.get("date_created") or "",
            "replies": [],
        }
        order.append(cid)

    roots = []
    for cid in order:
        it = items[cid]
        parent = items.get(it["parent"])
        if parent is not None and parent is not it:
            parent["replies"].append(it)
        else:
            roots.append(it)

    for it in items.values():
        it["replies"].sort(key=lambda r: r["created"])
    roots.sort(key=lambda r: (r["ms"], r["created"]))
    return roots


def render_note(item, show_author):
    """One comment thread rendered as marker body text."""
    head = item["text"]
    if show_author and item["author"]:
        head += "\n    -- " + item["author"]
    lines = [head]
    for rep in item["replies"]:
        body = "\n      ".join(rep["text"].split("\n"))
        rep_line = "  > " + body
        if show_author and rep["author"]:
            rep_line += "\n    -- " + rep["author"]
        lines.append(rep_line)
    return "\n".join(lines)


# ------------------------------------------------------------------- assemble

def build_markers(roots, fps, total_frames, show_author, number):
    """Collapse comment threads into one marker per frame.

    Premiere silently drops a second sequence marker landing on a frame that
    already has one, and iconik replies (plus the odd simultaneous note) share
    timestamps -- so everything on a given frame is merged into one marker.
    """
    buckets = {}
    for item in roots:
        frame = int(round(item["ms"] / 1000.0 * float(fps)))
        frame = max(0, min(frame, max(total_frames - 1, 0)))
        buckets.setdefault(frame, []).append(item)

    markers = []
    for idx, frame in enumerate(sorted(buckets), start=1):
        group = buckets[frame]
        comment = "\n\n".join(render_note(it, show_author) for it in group)

        first_line = group[0]["text"].split("\n")[0].strip()
        label = first_line if len(first_line) <= 46 else first_line[:45].rstrip() + "..."
        if len(group) > 1:
            label += "  (+%d)" % (len(group) - 1)
        name = ("%02d %s" % (idx, label)) if number else label

        markers.append({
            "frame": frame,
            "name": name,
            "comment": comment,
            "count": len(group) + sum(len(it["replies"]) for it in group),
        })
    return markers


def timecode(frame, timebase):
    """Frame number -> HH:MM:SS:FF, non-drop."""
    hh, rem = divmod(frame, timebase * 3600)
    mm, rem = divmod(rem, timebase * 60)
    ss, ff = divmod(rem, timebase)
    return "%02d:%02d:%02d:%02d" % (hh, mm, ss, ff)


def path_to_url(path):
    """Absolute path -> the file://localhost/ URL Premiere expects on Windows."""
    p = os.path.abspath(path).replace("\\", "/")
    return "file://localhost/" + quote(p.lstrip("/"), safe="/:")


def rate_xml(timebase, ntsc, indent):
    pad = " " * indent
    return ("%s<rate>\n%s  <timebase>%d</timebase>\n%s  <ntsc>%s</ntsc>\n%s</rate>"
            % (pad, pad, timebase, pad, "TRUE" if ntsc else "FALSE", pad))


def build_xml(seq_name, media_path, info, markers, add_media):
    fps = info["fps"]
    timebase, ntsc = rate_tags(fps)
    frames = info["frames"]
    width, height = info["width"], info["height"]
    channels = min(info.get("audio_channels", 0), 2)
    srate = info.get("sample_rate", 48000)
    fname = os.path.basename(media_path) if media_path else "media"

    out = []
    a = out.append

    a('<?xml version="1.0" encoding="UTF-8"?>')
    a("<!DOCTYPE xmeml>")
    a('<xmeml version="4">')
    a('  <sequence id="iconik-notes-seq">')
    a("    <uuid>%s</uuid>" % uuid.uuid4())
    a("    <name>%s</name>" % escape(seq_name))
    a("    <duration>%d</duration>" % frames)
    a(rate_xml(timebase, ntsc, 4))
    a("    <timecode>")
    a(rate_xml(timebase, ntsc, 6))
    a("      <string>00:00:00:00</string>")
    a("      <frame>0</frame>")
    a("      <displayformat>NDF</displayformat>")
    a("    </timecode>")
    a("    <media>")

    # ---- video
    a("      <video>")
    a("        <format>")
    a("          <samplecharacteristics>")
    a(rate_xml(timebase, ntsc, 12))
    a("            <width>%d</width>" % width)
    a("            <height>%d</height>" % height)
    a("            <anamorphic>FALSE</anamorphic>")
    a("            <pixelaspectratio>square</pixelaspectratio>")
    a("            <fielddominance>none</fielddominance>")
    a("          </samplecharacteristics>")
    a("        </format>")
    a("        <track>")
    if add_media:
        a('          <clipitem id="clipitem-video-1">')
        a("            <name>%s</name>" % escape(fname))
        a("            <duration>%d</duration>" % frames)
        a(rate_xml(timebase, ntsc, 12))
        a("            <start>0</start>")
        a("            <end>%d</end>" % frames)
        a("            <in>0</in>")
        a("            <out>%d</out>" % frames)
        a('            <file id="iconik-notes-file-1">')
        a("              <name>%s</name>" % escape(fname))
        a("              <pathurl>%s</pathurl>" % escape(path_to_url(media_path)))
        a(rate_xml(timebase, ntsc, 14))
        a("              <duration>%d</duration>" % frames)
        a("              <media>")
        a("                <video>")
        a("                  <samplecharacteristics>")
        a(rate_xml(timebase, ntsc, 20))
        a("                    <width>%d</width>" % width)
        a("                    <height>%d</height>" % height)
        a("                  </samplecharacteristics>")
        a("                </video>")
        if channels:
            a("                <audio>")
            a("                  <samplecharacteristics>")
            a("                    <depth>16</depth>")
            a("                    <samplerate>%d</samplerate>" % srate)
            a("                  </samplecharacteristics>")
            a("                  <channelcount>%d</channelcount>" % channels)
            a("                </audio>")
        a("              </media>")
        a("            </file>")
        a("            <sourcetrack>")
        a("              <mediatype>video</mediatype>")
        a("              <trackindex>1</trackindex>")
        a("            </sourcetrack>")
        a("          </clipitem>")
    a("          <enabled>TRUE</enabled>")
    a("          <locked>FALSE</locked>")
    a("        </track>")
    a("      </video>")

    # ---- audio
    if add_media and channels:
        a("      <audio>")
        a("        <numOutputChannels>%d</numOutputChannels>" % channels)
        a("        <format>")
        a("          <samplecharacteristics>")
        a("            <depth>16</depth>")
        a("            <samplerate>%d</samplerate>" % srate)
        a("          </samplecharacteristics>")
        a("        </format>")
        a("        <outputs>")
        for ch in range(1, channels + 1):
            a("          <group>")
            a("            <index>%d</index>" % ch)
            a("            <numchannels>1</numchannels>")
            a("            <downmix>0</downmix>")
            a("            <channel>")
            a("              <index>%d</index>" % ch)
            a("            </channel>")
            a("          </group>")
        a("        </outputs>")
        for ch in range(1, channels + 1):
            a("        <track>")
            a('          <clipitem id="clipitem-audio-%d">' % ch)
            a("            <name>%s</name>" % escape(fname))
            a("            <duration>%d</duration>" % frames)
            a(rate_xml(timebase, ntsc, 12))
            a("            <start>0</start>")
            a("            <end>%d</end>" % frames)
            a("            <in>0</in>")
            a("            <out>%d</out>" % frames)
            a('            <file id="iconik-notes-file-1"/>')
            a("            <sourcetrack>")
            a("              <mediatype>audio</mediatype>")
            a("              <trackindex>%d</trackindex>" % ch)
            a("            </sourcetrack>")
            a("          </clipitem>")
            a("          <enabled>TRUE</enabled>")
            a("          <locked>FALSE</locked>")
            a("          <outputchannelindex>%d</outputchannelindex>" % ch)
            a("        </track>")
        a("      </audio>")
    a("    </media>")

    # ---- markers (must come last inside <sequence> per the FCP7 schema)
    for mk in markers:
        a("    <marker>")
        a("      <name>%s</name>" % escape(mk["name"]))
        a("      <comment>%s</comment>" % escape(mk["comment"]))
        a("      <in>%d</in>" % mk["frame"])
        a("      <out>-1</out>")
        a("    </marker>")

    a("  </sequence>")
    a("</xmeml>")
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------- main

def main(argv=None):
    ap =argparse.ArgumentParser(
        description="Convert an iconik comment CSV into a Premiere Pro XML sequence with markers.")
    ap.add_argument("csv", help="iconik '<media>-comment.csv' export")
    ap.add_argument("--media", help="reviewed media file (auto-detected from the CSV name if omitted)")
    ap.add_argument("--out", help="output .xml path (default: alongside the CSV)")
    ap.add_argument("--name", help="sequence name (default: media basename + ' NOTES')")
    ap.add_argument("--fps", type=float, help="override frame rate, e.g. 23.976")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--frames", type=int, help="override sequence length in frames")
    ap.add_argument("--no-media", action="store_true",
                    help="markers only -- do not lay the clip on the timeline")
    ap.add_argument("--no-author", action="store_true", help="omit comment authors")
    ap.add_argument("--no-number", action="store_true", help="omit the NN prefix on marker names")
    ap.add_argument("--include-resolved", action="store_true",
                    help="include comments whose status is not ACTIVE")
    ap.add_argument("--txt", action="store_true", help="also write a plain-text notes list")
    args = ap.parse_args(argv)

    csv_path = os.path.abspath(args.csv)
    if not os.path.isfile(csv_path):
        sys.exit("CSV not found: " + csv_path)

    media = os.path.abspath(args.media) if args.media else find_media(csv_path)
    if media and not os.path.isfile(media):
        if args.media:
            sys.exit("Media not found: " + media)
        media = None

    info = probe(media) if media else None
    if info is None:
        info = {"width": args.width, "height": args.height,
                "fps": Fraction(24000, 1001), "frames": 0,
                "audio_channels": 2, "sample_rate": 48000}
        if not args.fps:
            print("! Could not probe the media -- assuming 23.976 fps. "
                  "Use --fps/--frames to override.", file=sys.stderr)

    if args.fps:
        info["fps"] = Fraction(args.fps).limit_denominator(1001)
    if args.frames:
        info["frames"] = args.frames

    roots = read_comments(csv_path, args.include_resolved)
    if not roots:
        sys.exit("No comments found in that CSV.")

    if not info["frames"]:
        last_ms = max(r["ms"] for r in roots)
        info["frames"] = int(round(last_ms / 1000.0 * float(info["fps"]))) + 240

    markers = build_markers(roots, info["fps"], info["frames"],
                            show_author=not args.no_author, number=not args.no_number)

    stem = os.path.splitext(os.path.basename(media))[0] if media \
        else re.sub(r"-comment\.csv$", "", os.path.basename(csv_path), flags=re.I)
    seq_name = args.name or (stem + " NOTES")
    out_path = os.path.abspath(args.out) if args.out else \
        os.path.join(os.path.dirname(csv_path), stem + "_NOTES.xml")

    xml = build_xml(seq_name, media, info, markers,
                    add_media=bool(media) and not args.no_media)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(xml)

    timebase, ntsc = rate_tags(info["fps"])
    total = sum(m["count"] for m in markers)
    print("Media     : " + (media or "(none -- markers only)"))
    print("Rate      : %.3f fps  (timebase %d, ntsc %s)   %dx%d   %d frames"
          % (float(info["fps"]), timebase, "TRUE" if ntsc else "FALSE",
             info["width"], info["height"], info["frames"]))
    print("Comments  : %d  (%d markers after same-frame merges)" % (total, len(markers)))
    print("Sequence  : " + seq_name)
    print("Written   : " + out_path + "\n")
    for mk in markers:
        print("  %s  f%-6d %s" % (timecode(mk["frame"], timebase), mk["frame"], mk["name"]))

    if args.txt:
        txt_path = os.path.splitext(out_path)[0] + ".txt"
        with open(txt_path, "w", encoding="utf-8") as fh:
            fh.write(seq_name + "\n" + "=" * len(seq_name) + "\n\n")
            for mk in markers:
                fh.write("[%s] %s\n" % (timecode(mk["frame"], timebase), mk["name"]))
                fh.write(mk["comment"] + "\n\n")
        print("\nText notes: " + txt_path)


if __name__ == "__main__":
    main()
