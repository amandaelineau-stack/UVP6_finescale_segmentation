#!/usr/bin/env python3
"""
uvp6_updown_segments.py

Find the stretches of a UVP6 CTD cast where the instrument was moving
steadily down (downcast) or steadily up (upcast), skipping any images
taken while the rosette reversed direction (ship heave, stops, yo-yos).
Writes the first and last image number of each clean stretch to CSV.

An image counts as "undisturbed" when:
  downcast: its pressure is >= every earlier pressure in the downcast
            (a new maximum, so the water has not been passed through yet)
  upcast:   its pressure is <= every earlier pressure in the upcast
            (a new minimum)
The downcast ends at the first image within --buffer dbar (default 5) of
the cast's maximum pressure, unless the descent is still unbroken there,
in which case it carries on to the bottom. The upcast starts once the
rosette has risen above that same level. Images from the bottom stop,
where the rosette heaves and drifts near maximum pressure, belong to
neither.

USAGE
    python uvp6_updown_segments.py /path/to/ecodata
    python uvp6_updown_segments.py /path/to/ecodata -o /path/to/output --plot

The ecodata folder should hold one folder per cast (as exported by UVPapp),
each containing a "<cast>_Particule" folder (or "<cast>_Particule.zip")
with particules.csv and metadata.ini inside.

OUTPUT (default: a "updown_segments" folder next to ecodata)
    <cast>_down_segments.csv   one row per clean downcast stretch
    <cast>_up_segments.csv     one row per clean upcast stretch
    all_casts_segments.csv     every stretch from every cast in one file
    <cast>_segments.png        quick-look plot (only with --plot)

Columns: cast, direction, segment, first_image, last_image, n_images,
start_time, end_time, start_pressure_dbar, end_pressure_dbar.
Times are in the UVP's own yyyymmdd-HHMMSS-mmm format, which matches the
image file names in the <cast>_Images.zip export.

Image numbers start at "firstimage" from metadata.ini and increase by one
for each data line in particules.csv.

Needs only Python 3.8+ (matplotlib only if you use --plot).
"""

import argparse
import csv
import io
import re
import sys
import zipfile
from pathlib import Path

FIELDS = ["cast", "direction", "segment", "first_image", "last_image",
          "n_images", "start_time", "end_time",
          "start_pressure_dbar", "end_pressure_dbar"]


def find_particule_files(cast_dir):
    """Return (particules.csv text, metadata.ini text) for one cast folder,
    or None if the cast has no particle data."""
    folders = sorted(p for p in cast_dir.glob("*_Particule") if p.is_dir())
    for folder in folders:
        csv_path = folder / "particules.csv"
        ini_path = folder / "metadata.ini"
        if csv_path.exists() and ini_path.exists():
            return (csv_path.read_text(errors="replace"),
                    ini_path.read_text(errors="replace"))
    for zpath in sorted(cast_dir.glob("*_Particule.zip")):
        with zipfile.ZipFile(zpath) as z:
            names = {Path(n).name: n for n in z.namelist()}
            if "particules.csv" in names and "metadata.ini" in names:
                read = lambda n: z.read(names[n]).decode(errors="replace")
                return read("particules.csv"), read("metadata.ini")
    return None


def read_first_image(ini_text):
    match = re.search(r"^\s*firstimage\s*=\s*(\d+)", ini_text, re.MULTILINE)
    if not match:
        raise ValueError("'firstimage' not found in metadata.ini")
    return int(match.group(1))


def read_images(csv_text):
    """Return lists of (timestamp, pressure) for each image line.
    Header lines (HW_CONF, ACQ_CONF, blanks) are skipped; image lines start
    with a yyyymmdd-HHMMSS-mmm timestamp."""
    times, pressures = [], []
    for line in io.StringIO(csv_text):
        if not line[:8].isdigit():
            continue
        fields = line.split(":", 1)[0].split(",")
        times.append(fields[0])
        pressures.append(float(fields[1]))
    return times, pressures


def flag_images(pressures, buffer_dbar):
    """Return (is_down, undisturbed) lists, one entry per image.
    Images during the bottom stop (heave near maximum pressure) end up
    with undisturbed = False."""
    n = len(pressures)
    pmax = max(pressures)
    turn = next(i for i, p in enumerate(pressures) if p >= pmax - buffer_dbar)

    undisturbed = [False] * n
    deepest = float("-inf")
    for i in range(turn):
        deepest = max(deepest, pressures[i])
        undisturbed[i] = pressures[i] == deepest
    # carry an unbroken descent on past the buffer to the bottom
    end_down = turn
    if turn == 0 or undisturbed[turn - 1]:
        while end_down < n and pressures[end_down] >= deepest:
            deepest = pressures[end_down]
            undisturbed[end_down] = True
            end_down += 1
    is_down = [i < end_down for i in range(n)]

    # upcast starts after the deepest image, once above the buffer
    up_start = max(end_down, pressures.index(pmax) + 1)
    shallowest = pmax - buffer_dbar
    for i in range(up_start, n):
        if pressures[i] <= shallowest:
            shallowest = pressures[i]
            undisturbed[i] = True
    return is_down, undisturbed


def find_segments(cast, times, pressures, first_image, is_down, undisturbed):
    """Group consecutive undisturbed images of the same direction."""
    segments = []
    i, n = 0, len(pressures)
    while i < n:
        if not undisturbed[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and undisturbed[j + 1] and is_down[j + 1] == is_down[i]:
            j += 1
        direction = "down" if is_down[i] else "up"
        segments.append({
            "cast": cast,
            "direction": direction,
            "segment": sum(s["direction"] == direction for s in segments) + 1,
            "first_image": first_image + i,
            "last_image": first_image + j,
            "n_images": j - i + 1,
            "start_time": times[i],
            "end_time": times[j],
            "start_pressure_dbar": pressures[i],
            "end_pressure_dbar": pressures[j],
        })
        i = j + 1
    return segments


def write_csv(path, rows):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def plot_cast(path, cast, pressures, first_image, is_down, undisturbed):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    images = [first_image + i for i in range(len(pressures))]
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(images, pressures, color="0.8", lw=1, label="all images")
    for want_down, colour, label in [(True, "tab:blue", "downcast kept"),
                                     (False, "tab:red", "upcast kept")]:
        keep = [k for k in range(len(images))
                if undisturbed[k] and is_down[k] == want_down]
        ax.plot([images[k] for k in keep], [pressures[k] for k in keep],
                ".", ms=1.5, color=colour, label=label)
    ax.invert_yaxis()
    ax.set_xlabel("image number")
    ax.set_ylabel("pressure (dbar)")
    ax.set_title(cast)
    ax.legend(loc="best", markerscale=6)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(
        description="Split UVP6 casts into clean downcast and upcast "
                    "stretches, removing direction reversals.")
    parser.add_argument("ecodata", type=Path,
                        help="folder containing one sub-folder per cast")
    parser.add_argument("-o", "--output", type=Path,
                        help="output folder (default: updown_segments "
                             "next to the ecodata folder)")
    parser.add_argument("--plot", action="store_true",
                        help="also save a quick-look PNG for each cast "
                             "(needs matplotlib)")
    parser.add_argument("--buffer", type=float, default=5.0,
                        help="dbar above maximum pressure treated as the "
                             "bottom stop (default 5)")
    args = parser.parse_args()

    ecodata = args.ecodata.expanduser().resolve()
    if not ecodata.is_dir():
        sys.exit(f"Not a folder: {ecodata}")
    out_dir = (args.output.expanduser() if args.output
               else ecodata.parent / "updown_segments")
    out_dir.mkdir(parents=True, exist_ok=True)

    all_segments = []
    for cast_dir in sorted(p for p in ecodata.iterdir() if p.is_dir()):
        cast = cast_dir.name
        files = find_particule_files(cast_dir)
        if files is None:
            print(f"{cast}: no *_Particule folder or zip, skipped")
            continue
        csv_text, ini_text = files
        try:
            first_image = read_first_image(ini_text)
            times, pressures = read_images(csv_text)
            if not pressures:
                raise ValueError("no image lines in particules.csv")
        except ValueError as err:
            print(f"{cast}: {err}, skipped")
            continue

        is_down, undisturbed = flag_images(pressures, args.buffer)
        segments = find_segments(cast, times, pressures, first_image,
                                 is_down, undisturbed)
        down = [s for s in segments if s["direction"] == "down"]
        up = [s for s in segments if s["direction"] == "up"]
        write_csv(out_dir / f"{cast}_down_segments.csv", down)
        write_csv(out_dir / f"{cast}_up_segments.csv", up)
        all_segments += segments
        if args.plot:
            plot_cast(out_dir / f"{cast}_segments.png", cast, pressures,
                      first_image, is_down, undisturbed)

        n_down = sum(s["n_images"] for s in down)
        n_up = sum(s["n_images"] for s in up)
        print(f"{cast}: {len(pressures)} images, max {max(pressures):.0f} "
              f"dbar; kept {n_down} down ({len(down)} segments), "
              f"{n_up} up ({len(up)} segments)")

    if not all_segments:
        sys.exit(f"No casts with particle data found in {ecodata}")
    write_csv(out_dir / "all_casts_segments.csv", all_segments)
    print(f"Saved to {out_dir}")


if __name__ == "__main__":
    main()
