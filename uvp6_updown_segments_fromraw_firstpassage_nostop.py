#!/usr/bin/env python3
"""
uvp6_updown_segments_fromraw_firstpassage_nostop.py

"First-passage no-stop" version of uvp6_updown_segments_fromraw_firstpassage.py. Reads the UVP6 RAW files
directly (the "raw" folder of a UVP6 project), not UVPapp's ecodata export.

Find the stretches of a UVP6 CTD cast where the instrument was moving
steadily down (downcast) or steadily up (upcast), skipping any images
taken while the rosette reversed direction or stopped (ship heave, soak,
bottle stops, yo-yos). Writes the first and last image number of each
clean stretch to CSV.

The UVP6 updates pressure once per block of images (10 in practice), so
the cast is handled as "pressure blocks": runs of consecutive images that
share the same pressure value. A block counts as "undisturbed" when:
  downcast: its pressure is strictly deeper than every earlier block of
            the downcast (water not passed through yet), and the next
            block is deeper still (still descending);
  upcast:   the same, shallower instead of deeper;
  and in both cases it is no longer than one block (--block, default:
  the most common run length in the file). A longer run means pressure
  did not change between two updates: the rosette was stopped.

Aim: compare particles in undisturbed water (downcast, first passage of
the rosette) with particles in water stirred by the rosette (upcast).

Changes from the first-passage version:
  - Pressure equal to an earlier maximum (or minimum) is no longer
    accepted, and stopped blocks are rejected (see above): while stopped,
    the camera sees water the rosette has just stirred.
  - As in the first-passage version, the downcast is counted from the very start of the file, so
    any depth already passed (e.g. by a false start before going back to
    the surface) is never kept again. V2 instead started the downcast at
    the last minimum before the bottom; that kept water already stirred.
    The 0-8 dbar before the UVP auto-start (and down to the surface soak)
    are passed through before recording, so they are never kept.

The downcast ends at the first block within --buffer dbar (default 5) of
the cast's maximum pressure, unless the descent is still unbroken there,
in which case it carries on to the bottom. The upcast starts once the
rosette has risen above that same level. Images from the bottom stop,
where the rosette heaves and drifts near maximum pressure, belong to
neither.

USAGE
    python uvp6_updown_segments_fromraw_firstpassage_nostop.py /path/to/raw
    python uvp6_updown_segments_fromraw_firstpassage_nostop.py /path/to/raw -o /path/to/output --plot

The raw folder holds one folder per cast, named yyyymmdd-HHMMSS, each
containing <cast>_data.txt (and images.zip).

Casts whose maximum pressure is below --buffer (deck tests at 0 dbar) are
skipped.

OUTPUT (default: a "updown_segments" folder next to the raw folder)
    <cast>_down_segments.csv   one row per clean downcast stretch
    <cast>_up_segments.csv     one row per clean upcast stretch
    all_casts_segments.csv     every stretch from every cast in one file
    <cast>_segments.png        quick-look plot (only with --plot)

Columns: cast, direction, segment, first_image, last_image, n_images,
start_time, end_time, start_pressure_dbar, end_pressure_dbar.
Times are in the UVP's own yyyymmdd-HHMMSS-mmm format, which matches the
.vig file names in the cast's images.zip.

Image numbers count the data lines of <cast>_data.txt, starting at 1.

Needs only Python 3.8+ (matplotlib only if you use --plot).
"""

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

FIELDS = ["cast", "direction", "segment", "first_image", "last_image",
          "n_images", "start_time", "end_time",
          "start_pressure_dbar", "end_pressure_dbar"]


def read_images(data_path):
    """Return lists of (timestamp, pressure) for each image line.
    Header lines (HW_CONF, ACQ_CONF, blanks) are skipped; image lines start
    with a yyyymmdd-HHMMSS-mmm timestamp."""
    times, pressures = [], []
    with open(data_path, errors="replace") as f:
        for line in f:
            if not line[:8].isdigit():
                continue
            fields = line.split(":", 1)[0].split(",")
            times.append(fields[0])
            pressures.append(float(fields[1]))
    return times, pressures


def pressure_runs(pressures):
    """Split the cast into runs of consecutive images with the same
    pressure. Return a list of (first index, last index + 1, pressure)."""
    runs, start = [], 0
    for i in range(1, len(pressures) + 1):
        if i == len(pressures) or pressures[i] != pressures[start]:
            runs.append((start, i, pressures[start]))
            start = i
    return runs


def flag_images(pressures, buffer_dbar, block=None):
    """Return (is_down, undisturbed) lists, one entry per image.
    Work is done on pressure runs (see module docstring). Images at depths
    already passed through (surface soak, false starts), during stops and during the
    bottom stop end up with undisturbed = False."""
    runs = pressure_runs(pressures)
    P = [r[2] for r in runs]
    length = [r[1] - r[0] for r in runs]
    if block is None:
        block = Counter(length).most_common(1)[0][0]
    R = len(runs)

    def moving(k, deeper):
        """Run k is one block long and the next run continues the move."""
        if length[k] > block or k + 1 >= R:
            return False
        return P[k + 1] > P[k] if deeper else P[k + 1] < P[k]

    pmax = max(P)
    turn = next(k for k, p in enumerate(P) if p >= pmax - buffer_dbar)
    # the descent is counted from the very start of the file: water passed
    # through by a false start is not undisturbed any more
    ok = [False] * R
    deepest = float("-inf")
    for k in range(turn):
        if P[k] > deepest:
            deepest = P[k]
            ok[k] = moving(k, deeper=True)
    # carry an unbroken descent on past the buffer to the bottom
    end_down = turn
    if turn == 0 or ok[turn - 1]:
        while end_down < R and P[end_down] > deepest:
            deepest = P[end_down]
            ok[end_down] = moving(end_down, deeper=True)
            end_down += 1

    # upcast starts after the deepest block, once above the buffer
    up_start = max(end_down, P.index(pmax) + 1)
    shallowest = pmax - buffer_dbar
    for k in range(up_start, R):
        if P[k] < shallowest:
            shallowest = P[k]
            ok[k] = moving(k, deeper=False)

    is_down, undisturbed = [], []
    for k, (a, b, _) in enumerate(runs):
        is_down += [k < end_down] * (b - a)
        undisturbed += [ok[k]] * (b - a)
    return is_down, undisturbed


def find_segments(cast, times, pressures, is_down, undisturbed):
    """Group consecutive undisturbed images of the same direction.
    Image numbers start at 1."""
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
            "first_image": i + 1,
            "last_image": j + 1,
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


def plot_cast(path, cast, pressures, is_down, undisturbed):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    images = list(range(1, len(pressures) + 1))
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
        description="Split UVP6 RAW casts into clean downcast and upcast "
                    "stretches, removing direction reversals.")
    parser.add_argument("raw", type=Path,
                        help="UVP6 raw folder (one sub-folder per cast)")
    parser.add_argument("-o", "--output", type=Path,
                        help="output folder (default: updown_segments "
                             "next to the raw folder)")
    parser.add_argument("--plot", action="store_true",
                        help="also save a quick-look PNG for each cast "
                             "(needs matplotlib)")
    parser.add_argument("--buffer", type=float, default=5.0,
                        help="dbar above maximum pressure treated as the "
                             "bottom stop (default 5)")
    parser.add_argument("--block", type=int,
                        help="images per pressure update; longer runs of "
                             "equal pressure count as stopped (default: "
                             "most common run length in each file)")
    args = parser.parse_args()

    raw = args.raw.expanduser().resolve()
    if not raw.is_dir():
        sys.exit(f"Not a folder: {raw}")
    out_dir = (args.output.expanduser() if args.output
               else raw.parent / "updown_segments")
    out_dir.mkdir(parents=True, exist_ok=True)

    all_segments = []
    for cast_dir in sorted(p for p in raw.iterdir() if p.is_dir()):
        cast = cast_dir.name
        data_path = cast_dir / f"{cast}_data.txt"
        if not data_path.exists():
            print(f"{cast}: no {data_path.name}, skipped")
            continue
        times, pressures = read_images(data_path)
        if not pressures:
            print(f"{cast}: no image lines in {data_path.name}, skipped")
            continue
        if max(pressures) < args.buffer:
            print(f"{cast}: max {max(pressures):.0f} dbar, not a cast "
                  f"(deck test?), skipped")
            continue

        is_down, undisturbed = flag_images(pressures, args.buffer,
                                           args.block)
        segments = find_segments(cast, times, pressures, is_down, undisturbed)
        down = [s for s in segments if s["direction"] == "down"]
        up = [s for s in segments if s["direction"] == "up"]
        write_csv(out_dir / f"{cast}_down_segments.csv", down)
        write_csv(out_dir / f"{cast}_up_segments.csv", up)
        all_segments += segments
        if args.plot:
            plot_cast(out_dir / f"{cast}_segments.png", cast, pressures,
                      is_down, undisturbed)

        n_down = sum(s["n_images"] for s in down)
        n_up = sum(s["n_images"] for s in up)
        print(f"{cast}: {len(pressures)} images, max {max(pressures):.0f} "
              f"dbar; kept {n_down} down ({len(down)} segments), "
              f"{n_up} up ({len(up)} segments)")

    if not all_segments:
        sys.exit(f"No casts with data found in {raw}")
    write_csv(out_dir / "all_casts_segments.csv", all_segments)
    print(f"Saved to {out_dir}")


if __name__ == "__main__":
    main()
