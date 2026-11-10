#!/usr/bin/env python3
"""
UVP6_finescale_segmentation_tool.py

Small window app to go from UVP6 data to UVPapp's sample_header file:

  1. Project: choose the UVP6 project folder and work from raw or ecodata;
     the deployments are listed (one deployment = one rosette dive = one
     RAW file, yyyymmdd-HHMMSS).
  2. Metadata: A. cruise metadata (cruise ID, ship, camera settings);
     B. one row per deployment (ctdref, station, GPS, comment...). Can be
     pre-filled from an existing UVPapp header or a CSV.
  3. Segmentation (first-passage no-stop) and creation of the sample_header:
     profiles are named automatically <cruise>_<ctdref>_d01, ..._u01.
     The header is written as meta/uvp6_header_<project>.txt; an existing
     one is first moved to meta/archives/.
  4. Figures: view the segmentation of each deployment, download as PNG.

Image numbers: UVPapp counts from 0. In RAW, the first image line of
<cast>_data.txt is image 0. In ecodata, numbering comes from firstimage in
metadata.ini (already UVPapp's).

USAGE
    python UVP6_finescale_segmentation_tool.py   (or double-click UVP6_finescale_segmentation.pyw)

Needs: Python 3.8+, matplotlib, and in the same folder
uvp6_updown_segments_fromraw_firstpassage_nostop.py (segmentation method) and
uvp6_updown_segments.py (reads ecodata).
"""

import csv
import importlib.util
import queue
import re
import threading
from datetime import datetime
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.backends.backend_tkagg import (FigureCanvasTkAgg,
                                               NavigationToolbar2Tk)
from matplotlib.figure import Figure

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"),
                                                  HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nostop = load("uvp6_updown_segments_fromraw_firstpassage_nostop")
ecodata_reader = load("uvp6_updown_segments")

HEADER_COLS = ["cruise", "ship", "filename", "profileid", "bottomdepth",
               "ctdrosettefilename", "latitude", "longitude", "firstimage",
               "volimage", "aa", "exp", "dn", "winddir", "windspeed",
               "seastate", "nebuloussness", "comment", "endimg", "yoyo",
               "stationid", "sampletype", "integrationtime", "argoid",
               "pixelsize", "sampledatetime", "constantdepth"]

# fields entered per deployment (table columns)
DEPLOY_FIELDS = ["ctdref", "stationid", "latitude", "longitude",
                 "bottomdepth", "comment", "constantdepth"]
FIELD_WIDTH = {"ctdref": 16, "stationid": 14, "latitude": 9, "longitude": 9,
               "bottomdepth": 8, "comment": 28, "constantdepth": 8}

# fields common to the whole file
GLOBAL_FIELDS = ["cruise", "ship", "volimage", "aa", "exp", "pixelsize",
                 "sampletype"]


# --------------------------------------------------------------------------
# Segmentation (no UI)
# --------------------------------------------------------------------------

def read_ini(text):
    """key=value lines of a metadata.ini -> dict (last value wins)."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s*([^=\[\]]+?)\s*=\s*(.*?)\s*$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def hw_from_raw(data_path):
    """Camera settings from the HW_CONF line of a RAW _data.txt."""
    with open(data_path, errors="replace") as f:
        first = f.readline()
    fields = first.strip().rstrip(";").split(",")
    if not fields or fields[0] != "HW_CONF" or len(fields) < 23:
        return {}
    return {"aa": f"{float(fields[19]) / 1e6:g}", "exp": fields[20],
            "pixelsize": fields[21], "volimage": fields[22]}


def number_segments(deploy):
    """Sort a deployment's segments and number them per direction."""
    deploy["segments"].sort(key=lambda s: s["firstimage"])
    count = {"down": 0, "up": 0}
    for s in deploy["segments"]:
        count[s["direction"]] += 1
        s["number"] = count[s["direction"]]


def segment_raw(raw_dir, buffer_dbar, log):
    deployments, hw = {}, {}
    for cast_dir in sorted(d for d in raw_dir.iterdir() if d.is_dir()):
        name = cast_dir.name
        data = cast_dir / f"{name}_data.txt"
        if not data.exists():
            continue
        times, p = nostop.read_images(data)
        if not p or max(p) < buffer_dbar:
            log(f"{name}: max pressure < {buffer_dbar:g} dbar, skipped "
                f"(deck test?)")
            continue
        hw = hw or hw_from_raw(data)
        is_down, undisturbed = nostop.flag_images(p, buffer_dbar)
        segs = nostop.find_segments(name, times, p, is_down, undisturbed)
        deployments[name] = {
            "filename": name, "meta": {},
            "segments": [{"direction": s["direction"],
                          "firstimage": s["first_image"] - 1,   # UVPapp: 0
                          "endimg": s["last_image"] - 1,
                          "sampledatetime": s["start_time"][:15],
                          "start_pressure": s["start_pressure_dbar"],
                          "end_pressure": s["end_pressure_dbar"]}
                         for s in segs],
            "pieces": [(0, p, is_down, undisturbed)],
        }
        number_segments(deployments[name])
        nd = sum(s["direction"] == "down" for s in segs)
        log(f"{name}: {len(p)} images, max {max(p):.0f} dbar, "
            f"{nd} down segments, {len(segs) - nd} up")
    return deployments, hw


def segment_ecodata(eco_dir, buffer_dbar, log):
    """Each ecodata folder is one UVPapp profile; profiles are grouped into
    deployments by their RAW file name (filename in metadata.ini)."""
    deployments, hw = {}, {}
    for prof_dir in sorted(d for d in eco_dir.iterdir() if d.is_dir()):
        files = ecodata_reader.find_particule_files(prof_dir)
        if files is None:
            continue
        csv_text, ini_text = files
        ini = read_ini(ini_text)
        first = ecodata_reader.read_first_image(ini_text)
        times, p = ecodata_reader.read_images(csv_text)
        if not p:
            continue
        name = ini.get("filename") or prof_dir.name
        hw = hw or {k: ini[v] for k, v in [("exp", "Exp"),
                                           ("pixelsize", "Pixel_Size"),
                                           ("volimage", "Image_volume")]
                    if v in ini}
        if "Aa" in ini and "aa" not in hw:
            hw["aa"] = f"{float(ini['Aa']) / 1e6:g}"
        is_down, undisturbed = nostop.flag_images(p, buffer_dbar)
        segs = nostop.find_segments(name, times, p, is_down, undisturbed)
        d = deployments.setdefault(name, {"filename": name, "meta": {},
                                          "segments": [], "pieces": []})
        # pre-fill what metadata.ini already knows
        for key in ["stationid", "latitude", "longitude", "bottomdepth",
                    "comment", "constantdepth", "cruise", "ship"]:
            if ini.get(key) and key not in d["meta"]:
                d["meta"][key] = ini[key]
        if ini.get("profileid") and "ctdref" not in d["meta"]:
            d["meta"]["ctdref"] = re.sub(r"(_yo\d+)?_[du]\d+$", "",
                                         ini["profileid"])
        d["segments"] += [{"direction": s["direction"],
                           "firstimage": first + s["first_image"] - 1,
                           "endimg": first + s["last_image"] - 1,
                           "sampledatetime": s["start_time"][:15],
                           "start_pressure": s["start_pressure_dbar"],
                           "end_pressure": s["end_pressure_dbar"]}
                          for s in segs]
        d["pieces"].append((first, p, is_down, undisturbed))
        log(f"{prof_dir.name} ({name}): {len(p)} images, "
            f"{len(segs)} segments")
    for d in deployments.values():
        number_segments(d)
    return deployments, hw


def draw_deployment(fig, deploy):
    """Pressure vs image number, kept images coloured by direction."""
    fig.clear()
    ax = fig.add_subplot(111)
    first_label = True
    for start, p, is_down, undisturbed in deploy["pieces"]:
        x = range(start, start + len(p))
        ax.plot(list(x), p, color="0.8", lw=1,
                label="all images" if first_label else None)
        for want, colour, label in [(True, "tab:blue", "downcast kept"),
                                    (False, "tab:red", "upcast kept")]:
            k = [i for i in range(len(p)) if undisturbed[i] and is_down[i] == want]
            ax.plot([start + i for i in k], [p[i] for i in k], ".", ms=1.5,
                    color=colour, label=label if first_label else None)
        first_label = False
    nd = sum(s["direction"] == "down" for s in deploy["segments"])
    nu = len(deploy["segments"]) - nd
    ref = deploy["meta"].get("ctdref", "")
    ax.set_title(f"{deploy['filename']}  {ref}   ({nd} down segments, "
                 f"{nu} up)", loc="left")
    ax.invert_yaxis()
    ax.set_xlabel("image number (UVPapp numbering)")
    ax.set_ylabel("pressure (dbar)")
    ax.legend(loc="best", markerscale=6, fontsize=8)
    fig.tight_layout()


def save_png(deploy, path):
    fig = Figure(figsize=(10, 5))
    FigureCanvasAgg(fig)
    draw_deployment(fig, deploy)
    fig.savefig(path, dpi=120)


def read_uvpapp_header(path):
    """Existing UVPapp header (meta/uvp6_header_*.txt, one line per profile)
    -> ({filename: per-deployment fields}, {cruise, ship}, conflicts).
    Fields that differ between profiles of one deployment take the most
    common value; each such case is listed in conflicts."""
    return parse_uvpapp_header(Path(path).read_text(errors="replace"))


def parse_uvpapp_header(text):
    """read_uvpapp_header on the file's text (e.g. an uploaded file)."""
    rows = [r for r in csv.DictReader(text.splitlines(), delimiter=";")
            if r.get("filename")]
    if not rows or "profileid" not in rows[0]:
        raise ValueError("not a UVPapp header (no filename / profileid columns)")
    by_file = {}
    for r in rows:
        by_file.setdefault(r["filename"], []).append(r)
    metas, conflicts = {}, []
    for name, rs in by_file.items():
        m = {}
        for k in DEPLOY_FIELDS:
            if k == "ctdref":
                # profile id minus its _d01 / _yo01_u02 suffix, and minus the
                # cruise ID if it starts with it (names made by this app)
                values = []
                for r in rs:
                    ref = re.sub(r"(_yo\d+)?_[du]\d+$", "", r["profileid"])
                    cruise = (r.get("cruise") or "").strip()
                    if cruise and ref.startswith(cruise + "_"):
                        ref = ref[len(cruise) + 1:]
                    values.append(ref)
            else:
                values = [r.get(k, "") for r in rs]
            counts = {}
            for v in values:
                counts[v] = counts.get(v, 0) + 1
            m[k] = max(counts, key=counts.get)
            if len(counts) > 1:
                conflicts.append(f"{name} {k}: " + " / ".join(counts) +
                                 f" -> kept {m[k]}")
        metas[name] = m
    common = {k: rows[0].get(k, "") for k in ("cruise", "ship")}
    return metas, common, conflicts


def header_path(project_dir):
    """The header UVPapp reads: meta/uvp6_header_<project name without the
    'uvp6_' prefix>.txt (e.g. project uvp6_sn000328hf_..._ctd ->
    meta/uvp6_header_sn000328hf_..._ctd.txt)."""
    project = Path(project_dir)
    name = project.name[len("uvp6_"):] if project.name.startswith("uvp6_") else project.name
    return project / "meta" / f"uvp6_header_{name}.txt"


def find_uvpapp_header(project_dir):
    """Existing UVPapp header of the project, if any (the expected name
    first, otherwise any meta/uvp6_header_*.txt)."""
    expected = header_path(project_dir)
    if expected.exists():
        return expected
    hits = sorted((Path(project_dir) / "meta").glob("uvp6_header_*.txt"))
    return hits[0] if hits else None


def install_header(new_file, project_dir, stamp):
    """Put new_file in place as the project's UVPapp header. An existing
    header is first moved to meta/archives/<name>.backup_<stamp>.
    Returns (header path, archived path or None)."""
    target = header_path(project_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    archived = None
    if target.exists():
        archives = target.parent / "archives"
        archives.mkdir(exist_ok=True)
        archived = archives / f"{target.name}.backup_{stamp}"
        target.replace(archived)
    Path(new_file).replace(target)
    return target, archived


def write_segments_csv(deployments, path):
    cols = ["filename", "direction", "number", "firstimage", "endimg",
            "n_images", "sampledatetime", "start_pressure", "end_pressure"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(cols)
        for d in deployments.values():
            for s in d["segments"]:
                w.writerow([d["filename"], s["direction"], s["number"],
                            s["firstimage"], s["endimg"],
                            s["endimg"] - s["firstimage"] + 1,
                            s["sampledatetime"], s["start_pressure"],
                            s["end_pressure"]])


def profile_name(cruise, ctdref, direction, number):
    """Automatic segment name: <cruise>_<ctdref>_<d|u><nn>, e.g.
    jc282_Rebels2_CTD001_d01. Nothing is repeated: a ctdref that already
    starts with the cruise ID is used as it is, and a ctdref that starts
    with the cruise ID's first part (jc282_CTD001 with cruise jc282_Rebels2)
    loses that part."""
    letter = "d" if direction == "down" else "u"
    if cruise and ctdref.startswith(cruise):
        base = ctdref
    else:
        head = cruise.split("_")[0]
        if head and ctdref.lower().startswith(head.lower() + "_"):
            ctdref = ctdref[len(head) + 1:]
        base = "_".join(x for x in (cruise, ctdref) if x)
    return f"{base}_{letter}{number:02d}"


def write_header(path, deployments, g, metas):
    """Write the UVPapp sample_header: one line per segment.
    g = cruise metadata, metas = {filename: deployment metadata} for the
    deployments to include. Returns the number of profiles written."""
    lines = 0
    with open(path, "w", newline="") as f:
        w = csv.writer(f, delimiter=";", lineterminator="\n")
        w.writerow(HEADER_COLS)
        for name, m in metas.items():
            for s in deployments[name]["segments"]:
                values = {
                    "cruise": g["cruise"], "ship": g["ship"], "filename": name,
                    "profileid": profile_name(g["cruise"], m["ctdref"],
                                              s["direction"], s["number"]),
                    "bottomdepth": m["bottomdepth"] or "nan", "ctdrosettefilename": "",
                    "latitude": m["latitude"] or "nan", "longitude": m["longitude"] or "nan",
                    "firstimage": s["firstimage"], "volimage": g["volimage"],
                    "aa": g["aa"], "exp": g["exp"], "dn": "", "winddir": "nan",
                    "windspeed": "nan", "seastate": "nan", "nebuloussness": "nan",
                    "comment": m["comment"], "endimg": s["endimg"], "yoyo": "",
                    "stationid": m["stationid"], "sampletype": g["sampletype"],
                    "integrationtime": "nan", "argoid": "", "pixelsize": g["pixelsize"],
                    "sampledatetime": s["sampledatetime"],
                    "constantdepth": m["constantdepth"] or "nan",
                }
                w.writerow([values[c] for c in HEADER_COLS])
                lines += 1
    return lines


def ecodata_ini(prof_dir):
    """metadata.ini text of one ecodata profile folder (folder or zip)."""
    for folder in sorted(p for p in prof_dir.glob("*_Particule") if p.is_dir()):
        ini = folder / "metadata.ini"
        if ini.exists():
            return ini.read_text(errors="replace")
    for zpath in sorted(prof_dir.glob("*_Particule.zip")):
        import zipfile
        with zipfile.ZipFile(zpath) as z:
            names = [n for n in z.namelist() if n.endswith("metadata.ini")]
            if names:
                return z.read(names[0]).decode(errors="replace")
    return None


def read_cruise_info(project_dir):
    """Cruise metadata from the project's config/cruise_info.txt, if any:
    {"cruise": ..., "ship": ...} (only the keys found). Reads the UVPapp
    format (acron=jc282_rebels2) and the simple one (name: jc282_rebels2)."""
    path = Path(project_dir) / "config" / "cruise_info.txt"
    if not path.is_file():
        return {}
    out = {}
    for line in path.read_text(encoding="latin-1", errors="replace").splitlines():
        m = re.match(r"\s*(acron|name|ship|ship_name)\s*[=:]\s*(.*?)\s*$", line, re.I)
        if m and m.group(2):
            key = "ship" if m.group(1).lower().startswith("ship") else "cruise"
            out.setdefault(key, m.group(2))
    return out


def list_deployments(project_dir, source):
    """Deployments of a project without segmenting: (names, pre-filled
    metadata per name, camera settings, cruise metadata). The cruise
    metadata comes from config/cruise_info.txt first, then ecodata."""
    src = Path(project_dir) / source
    names, metas, hw = [], {}, {}
    common = read_cruise_info(project_dir)
    if source == "raw":
        for d in sorted(x for x in src.iterdir() if x.is_dir()):
            data = d / f"{d.name}_data.txt"
            if data.exists():
                names.append(d.name)
                hw = hw or hw_from_raw(data)
        return names, metas, hw, common
    for prof_dir in sorted(x for x in src.iterdir() if x.is_dir()):
        text = ecodata_ini(prof_dir)
        if text is None:
            continue
        ini = read_ini(text)
        name = ini.get("filename") or prof_dir.name
        if name not in metas:
            names.append(name)
            m = {k: ini.get(k, "") for k in DEPLOY_FIELDS if k != "ctdref"}
            m["ctdref"] = re.sub(r"(_yo\d+)?_[du]\d+$", "", ini.get("profileid", ""))
            metas[name] = m
        for k, v in (("exp", "Exp"), ("pixelsize", "Pixel_Size"), ("volimage", "Image_volume")):
            if v in ini and k not in hw:
                hw[k] = ini[v]
        if "Aa" in ini and "aa" not in hw:
            hw["aa"] = f"{float(ini['Aa']) / 1e6:g}"
        for k in ("cruise", "ship"):
            if ini.get(k) and k not in common:
                common[k] = ini[k]
    return sorted(names), metas, hw, common


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------

class App(tk.Tk):
    """Four steps, one tab each: project -> metadata -> segmentation and
    header -> figures."""

    def __init__(self):
        super().__init__()
        self.title("UVP6_finescale_segmentation")
        self.geometry("1300x780")
        self.deployments = {}   # segmentation results, filename -> dict
        self.rows = {}          # table rows, filename -> {"include", fields..., "status"}
        self.globals = {k: tk.StringVar() for k in GLOBAL_FIELDS}
        self.globals["sampletype"].set("P")
        self.queue = queue.Queue()

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        self.tabs = {}
        for key, label in [("project", "1. Project"),
                           ("meta", "2. Metadata"),
                           ("run", "3. Segmentation and header"),
                           ("fig", "4. Figures")]:
            self.tabs[key] = ttk.Frame(self.nb, padding=10)
            self.nb.add(self.tabs[key], text=label)
        self.build_project_tab()
        self.build_meta_tab()
        self.build_run_tab()
        self.build_fig_tab()
        self.globals["cruise"].trace_add("write", lambda *a: self.update_names())
        self.after(100, self.poll_queue)

    # ---- 1. project --------------------------------------------------------

    def build_project_tab(self):
        t = self.tabs["project"]
        self.project = tk.StringVar()
        self.source = tk.StringVar(value="raw")
        self.out_dir = tk.StringVar()

        ttk.Label(t, text="UVP6 project folder (the one holding raw, ecodata, meta):").grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Entry(t, textvariable=self.project, width=110).grid(row=1, column=0, columnspan=2, sticky="we")
        ttk.Button(t, text="Browse…", command=self.pick_project).grid(row=1, column=2, padx=5)

        ttk.Label(t, text="Work from:").grid(row=2, column=0, sticky="w", pady=(12, 0))
        ttk.Radiobutton(t, text="raw  (RAW files, one sub-folder per deployment)",
                        variable=self.source, value="raw").grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(t, text="ecodata  (profiles exported by UVPapp)",
                        variable=self.source, value="ecodata").grid(row=4, column=0, columnspan=2, sticky="w")

        ttk.Label(t, text="Segments and figures are saved in:").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Entry(t, textvariable=self.out_dir, width=110).grid(row=6, column=0, columnspan=2, sticky="we")
        ttk.Button(t, text="Browse…", command=self.pick_out).grid(row=6, column=2, padx=5)

        ttk.Button(t, text="Next: list the deployments  →",
                   command=self.list_deployments).grid(row=7, column=0, sticky="w", pady=16)
        self.project_info = ttk.Label(t, text="", foreground="navy")
        self.project_info.grid(row=8, column=0, columnspan=3, sticky="w")
        t.columnconfigure(1, weight=1)

    def pick_project(self):
        d = filedialog.askdirectory(title="UVP6 project folder (holds raw, ecodata, meta)")
        if not d:
            return
        self.project.set(d)
        self.out_dir.set(str(Path(d) / "casts_segmented"))
        has = [s for s in ("raw", "ecodata")
               if (Path(d) / s).is_dir() and any((Path(d) / s).iterdir())]
        if has == ["ecodata"]:
            self.source.set("ecodata")
        elif "raw" in has:
            self.source.set("raw")
        self.project_info.config(text="Found: " + (", ".join(has) or "no raw or ecodata data"))

    def pick_out(self):
        d = filedialog.askdirectory(title="Folder for segments and figures")
        if d:
            self.out_dir.set(d)

    def list_deployments(self):
        project = Path(self.project.get())
        if not self.project.get() or not project.is_dir():
            messagebox.showerror("Project folder", "Choose a UVP6 project folder that exists.")
            return
        src = project / self.source.get()
        if not src.is_dir():
            messagebox.showerror("Project folder", f"No {self.source.get()} folder in\n{project}")
            return
        if not self.out_dir.get():
            self.out_dir.set(str(project / "casts_segmented"))
        try:
            names, metas, hw, common = list_deployments(project, self.source.get())
        except OSError as err:
            messagebox.showerror("Project folder", str(err))
            return
        if not names:
            messagebox.showerror("Project folder", f"No deployment found in\n{src}")
            return
        self.deployments = {}
        for k, v in list(hw.items()) + list(common.items()):
            self.globals[k].set(v)
        self.fill_table(names, metas)
        self.create_btn.state(["disabled"])
        self.project_info.config(text=f"{len(names)} deployments found in {src}")
        self.nb.select(self.tabs["meta"])
        found = find_uvpapp_header(project)
        if found and messagebox.askyesno(
                "Existing UVPapp header",
                f"This project already has a UVPapp header:\n{found}\n\n"
                "Use it to pre-fill the metadata (ctdref, station, GPS, comment…)?\n"
                "Only the metadata is taken, not its segmentation."):
            self.apply_uvpapp_header(found)

    # ---- 2. metadata -------------------------------------------------------

    def build_meta_tab(self):
        t = self.tabs["meta"]
        a = ttk.LabelFrame(t, text="A. Cruise metadata (same for every deployment)", padding=8)
        a.pack(fill="x")
        labels = {"cruise": "Cruise ID", "ship": "Ship", "sampletype": "Sample type",
                  "volimage": "Image volume (L)", "aa": "aa", "exp": "exp",
                  "pixelsize": "Pixel size (µm)"}
        for i, k in enumerate(["cruise", "ship", "sampletype"]):
            ttk.Label(a, text=labels[k]).grid(row=0, column=2 * i, sticky="w", padx=(0, 4))
            ttk.Entry(a, textvariable=self.globals[k], width=24 if k != "sampletype" else 6).grid(
                row=0, column=2 * i + 1, sticky="w", padx=(0, 18))
        ttk.Label(a, text="Camera (read from the data):", foreground="gray35").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        for i, k in enumerate(["volimage", "aa", "exp", "pixelsize"]):
            ttk.Label(a, text=labels[k], foreground="gray35").grid(row=2, column=2 * i, sticky="w")
            ttk.Entry(a, textvariable=self.globals[k], width=10).grid(row=2, column=2 * i + 1, sticky="w")

        b = ttk.LabelFrame(t, text="B. Deployment metadata (one row per rosette dive = one RAW file)",
                           padding=8)
        b.pack(fill="both", expand=True, pady=(10, 0))
        bar = ttk.Frame(b)
        bar.pack(fill="x")
        ttk.Button(bar, text="Pre-fill from existing UVPapp header…",
                   command=self.load_uvpapp_header).pack(side="left")
        ttk.Button(bar, text="Load metadata CSV…", command=self.load_meta).pack(side="left", padx=6)
        ttk.Button(bar, text="Save metadata (CSV)…", command=self.save_meta).pack(side="left")
        ttk.Button(bar, text="Next: segmentation  →",
                   command=lambda: self.nb.select(self.tabs["run"])).pack(side="right")
        ttk.Label(b, text="Profile names are made automatically: <Cruise ID>_<ctdref>_d01, "
                          "_d02… for downcasts and _u01… for upcasts. "
                          "Untick \"include\" to leave a deployment out.",
                  foreground="gray35").pack(anchor="w", pady=4)

        holder = ttk.Frame(b)
        holder.pack(fill="both", expand=True)
        canvas = tk.Canvas(holder, highlightthickness=0)
        vs = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        hs = ttk.Scrollbar(holder, orient="horizontal", command=canvas.xview)
        self.grid_frame = ttk.Frame(canvas)
        self.grid_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        canvas.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

    def fill_table(self, names, metas):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self.rows = {}
        heads = ["include", "deployment (date-time)"] + DEPLOY_FIELDS + ["profile names", "segments"]
        for c, h in enumerate(heads):
            ttk.Label(self.grid_frame, text=h, font=("", 9, "bold")).grid(
                row=0, column=c, padx=3, pady=2, sticky="w")
        for r, name in enumerate(names, start=1):
            row = {"include": tk.BooleanVar(value=True)}
            ttk.Checkbutton(self.grid_frame, variable=row["include"]).grid(row=r, column=0)
            ttk.Label(self.grid_frame, text=name).grid(row=r, column=1, sticky="w", padx=3)
            for c, k in enumerate(DEPLOY_FIELDS, start=2):
                row[k] = tk.StringVar(value=metas.get(name, {}).get(k, ""))
                ttk.Entry(self.grid_frame, textvariable=row[k], width=FIELD_WIDTH[k]).grid(
                    row=r, column=c, padx=1, pady=1)
            row["names"] = ttk.Label(self.grid_frame, text="", foreground="navy")
            row["names"].grid(row=r, column=len(DEPLOY_FIELDS) + 2, sticky="w", padx=4)
            row["status"] = ttk.Label(self.grid_frame, text="not run yet", foreground="gray35")
            row["status"].grid(row=r, column=len(DEPLOY_FIELDS) + 3, sticky="w", padx=4)
            row["ctdref"].trace_add("write", lambda *a, n=name: self.update_names(n))
            self.rows[name] = row
        self.update_names()

    def update_names(self, only=None):
        cruise = self.globals["cruise"].get().strip()
        for name, row in self.rows.items():
            if only and name != only:
                continue
            ref = row["ctdref"].get().strip()
            if ref:
                text = (f"{profile_name(cruise, ref, 'down', 1)}, "
                        f"{profile_name(cruise, ref, 'up', 1)}…")
            else:
                text = "(enter ctdref)"
            row["names"].config(text=text)

    def load_uvpapp_header(self):
        if not self.rows:
            messagebox.showinfo("UVPapp header", "List the deployments first (step 1).")
            return
        start = find_uvpapp_header(self.project.get())
        path = filedialog.askopenfilename(
            title="Existing UVPapp header (meta/uvp6_header_*.txt)",
            initialdir=str(start.parent) if start else None,
            filetypes=[("UVPapp header", "*.txt"), ("All", "*.*")])
        if path:
            self.apply_uvpapp_header(path)

    def apply_uvpapp_header(self, path):
        try:
            metas, common, conflicts = read_uvpapp_header(path)
        except (ValueError, OSError) as err:
            messagebox.showerror("UVPapp header", str(err))
            return
        filled = 0
        for name, m in metas.items():
            row = self.rows.get(name)
            if row is not None:
                filled += 1
                for k, v in m.items():
                    row[k].set(v)
        for k, v in common.items():       # cruise and ship: only when still empty
            if v and not self.globals[k].get().strip():
                self.globals[k].set(v)
        missing = [n for n in self.rows if n not in metas]
        msg = f"{filled} of {len(self.rows)} deployments pre-filled from\n{path}"
        if missing:
            msg += f"\n\nNot in the header ({len(missing)}): " + ", ".join(missing[:8])
        if conflicts:
            msg += ("\n\nValues that differ between profiles of one deployment "
                    "(check them):\n" + "\n".join(conflicts[:10]))
        messagebox.showinfo("UVPapp header", msg)

    def load_meta(self):
        if not self.rows:
            messagebox.showinfo("Metadata", "List the deployments first (step 1).")
            return
        path = filedialog.askopenfilename(title="Metadata CSV",
                                          filetypes=[("CSV", "*.csv *.txt"), ("All", "*.*")])
        if not path:
            return
        text = Path(path).read_text(errors="replace")
        first = text.splitlines()[0] if text else ""
        delim = ";" if first.count(";") >= first.count(",") else ","
        rows = list(csv.DictReader(text.splitlines(), delimiter=delim))
        if not rows or "filename" not in rows[0]:
            messagebox.showerror("CSV", "The CSV needs a \"filename\" column "
                                        "(deployment date-time, e.g. 20250810-102258).")
            return
        matched, unknown = 0, []
        for r in rows:
            row = self.rows.get(r["filename"])
            if row is None:
                unknown.append(r["filename"])
                continue
            matched += 1
            for k in DEPLOY_FIELDS:
                if r.get(k) is not None:
                    row[k].set(r[k])
            if r.get("include") is not None:
                row["include"].set(r["include"].strip().lower() not in ("0", "false", "no", "non"))
        for k in ("cruise", "ship"):
            if rows[0].get(k):
                self.globals[k].set(rows[0][k])
        msg = f"{matched} deployments filled."
        if unknown:
            msg += f"\n{len(unknown)} rows with no matching deployment: " + ", ".join(unknown[:5])
        messagebox.showinfo("CSV loaded", msg)

    def save_meta(self):
        if not self.rows:
            messagebox.showinfo("Metadata", "List the deployments first (step 1).")
            return
        path = filedialog.asksaveasfilename(title="Save metadata", defaultextension=".csv",
                                            initialdir=self.out_dir.get() or None,
                                            initialfile="deployments_metadata.csv")
        if not path:
            return
        with open(path, "w", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["filename", "include", "cruise", "ship"] + DEPLOY_FIELDS)
            for name, row in self.rows.items():
                w.writerow([name, int(row["include"].get()), self.globals["cruise"].get(),
                            self.globals["ship"].get()] + [row[k].get() for k in DEPLOY_FIELDS])
        messagebox.showinfo("Metadata", f"Saved:\n{path}")

    # ---- 3. segmentation and header -----------------------------------------

    def build_run_tab(self):
        t = self.tabs["run"]
        self.buffer = tk.StringVar(value="5")
        top = ttk.Frame(t)
        top.pack(fill="x")
        ttk.Label(top, text="Bottom stop band (dbar):").pack(side="left")
        ttk.Entry(top, textvariable=self.buffer, width=5).pack(side="left", padx=(4, 16))
        self.run_btn = ttk.Button(top, text="Run segmentation", command=self.run_segmentation)
        self.run_btn.pack(side="left")
        self.progress = ttk.Progressbar(top, mode="indeterminate", length=200)
        self.progress.pack(side="left", padx=10)
        self.create_btn = ttk.Button(top, text="Create sample_header…", command=self.create_header)
        self.create_btn.pack(side="right")
        self.create_btn.state(["disabled"])
        self.log_box = tk.Text(t, height=30)
        self.log_box.pack(fill="both", expand=True, pady=(10, 0))

    def log(self, msg):
        self.queue.put(("log", msg))

    def run_segmentation(self):
        if not self.rows:
            messagebox.showinfo("Segmentation", "List the deployments first (step 1).")
            return
        try:
            buffer_dbar = float(self.buffer.get())
        except ValueError:
            messagebox.showerror("Buffer", "The bottom stop band must be a number (dbar).")
            return
        src = Path(self.project.get()) / self.source.get()
        out = Path(self.out_dir.get())
        mode = self.source.get()
        self.run_btn.state(["disabled"])
        self.create_btn.state(["disabled"])
        self.progress.start(10)
        self.log_box.delete("1.0", "end")

        def work():
            try:
                fn = segment_raw if mode == "raw" else segment_ecodata
                deployments, hw = fn(src, buffer_dbar, self.log)
                (out / "figures").mkdir(parents=True, exist_ok=True)
                write_segments_csv(deployments, out / "all_segments.csv")
                self.log("Figures…")
                for d in deployments.values():
                    save_png(d, out / "figures" / f"{d['filename']}_segments.png")
                self.log(f"\n{len(deployments)} deployments segmented.\nSegments: "
                         f"{out / 'all_segments.csv'}\nFigures: {out / 'figures'}")
                self.queue.put(("done", (deployments, hw)))
            except Exception as err:      # shown to the user, not swallowed
                self.queue.put(("error", str(err)))

        threading.Thread(target=work, daemon=True).start()

    def poll_queue(self):
        while not self.queue.empty():
            kind, payload = self.queue.get()
            if kind == "log":
                self.log_box.insert("end", payload + "\n")
                self.log_box.see("end")
            elif kind == "done":
                self.finish_segmentation(*payload)
            elif kind == "error":
                self.progress.stop()
                self.run_btn.state(["!disabled"])
                messagebox.showerror("Segmentation", payload)
        self.after(100, self.poll_queue)

    def finish_segmentation(self, deployments, hw):
        self.progress.stop()
        self.run_btn.state(["!disabled"])
        self.deployments = deployments
        for k, v in hw.items():
            if not self.globals[k].get():
                self.globals[k].set(v)
        for name, row in self.rows.items():
            d = deployments.get(name)
            if d is None:
                row["status"].config(text="no data (deck test?)", foreground="firebrick")
                row["include"].set(False)
            else:
                nd = sum(s["direction"] == "down" for s in d["segments"])
                row["status"].config(text=f"{nd} down / {len(d['segments']) - nd} up",
                                     foreground="black")
        self.fig_choice["values"] = list(deployments)
        if deployments:
            self.fig_choice.current(0)
            self.show_figure()
        self.create_btn.state(["!disabled"])
        self.log("\nNext: check the figures (tab 4), then click \"Create sample_header…\".")

    def create_header(self):
        if not self.deployments:
            messagebox.showinfo("sample_header", "Run the segmentation first.")
            return
        g = {k: v.get().strip() for k, v in self.globals.items()}
        metas = {name: {k: row[k].get().strip() for k in DEPLOY_FIELDS}
                 for name, row in self.rows.items()
                 if row["include"].get() and name in self.deployments}
        problems = [f"Cruise metadata: {k} is empty" for k in ("cruise", "ship") if not g[k]]
        if not metas:
            problems.append("No deployment is included.")
        refs = {}
        for name, m in metas.items():
            if not m["ctdref"]:
                problems.append(f"{name}: ctdref is empty")
            else:
                refs.setdefault(m["ctdref"], []).append(name)
            for k in ("latitude", "longitude"):
                v = m[k]
                if v and v.lower() != "nan":
                    try:
                        float(v)
                    except ValueError:
                        problems.append(f"{name}: {k} \"{v}\" is not a number")
        problems += [f"ctdref {r} is used by several deployments: {', '.join(ns)}"
                     for r, ns in refs.items() if len(ns) > 1]
        if problems:
            messagebox.showerror("Missing information", "\n".join(problems[:20]) +
                                 ("\n…" if len(problems) > 20 else ""))
            self.nb.select(self.tabs["meta"])
            return
        project = Path(self.project.get())
        target = header_path(project)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        first = next(iter(metas))
        n_prof = sum(len(self.deployments[n]["segments"]) for n in metas)
        example = profile_name(g["cruise"], metas[first]["ctdref"], "down", 1)
        question = (f"{n_prof} profiles, named like {example}.\n\n"
                    f"The header will be written as\n{target}\n(the name UVPapp reads).")
        if target.exists():
            question += (f"\n\nThe existing header will first be moved to\n"
                         f"{target.parent / 'archives' / (target.name + '.backup_' + stamp)}")
        if not messagebox.askyesno("Create sample_header", question + "\n\nContinue?"):
            return
        # write next to the target first: the old header is only archived
        # once the new one is complete
        tmp = target.with_name(target.name + ".new")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            lines = write_header(tmp, self.deployments, g, metas)
            target, archived = install_header(tmp, project, stamp)
        except OSError as err:
            messagebox.showerror("Create sample_header", f"Nothing was replaced.\n{err}")
            return
        msg = f"{lines} profiles written:\n{target}"
        if archived:
            msg += f"\n\nPrevious header archived as:\n{archived}"
        self.log("\n" + msg)
        messagebox.showinfo("sample_header", msg)

    # ---- 4. figures --------------------------------------------------------

    def build_fig_tab(self):
        t = self.tabs["fig"]
        bar = ttk.Frame(t)
        bar.pack(fill="x")
        ttk.Label(bar, text="Deployment:").pack(side="left")
        self.fig_choice = ttk.Combobox(bar, state="readonly", width=24)
        self.fig_choice.pack(side="left", padx=5)
        self.fig_choice.bind("<<ComboboxSelected>>", lambda e: self.show_figure())
        ttk.Button(bar, text="Download this figure (PNG)…",
                   command=self.download_one).pack(side="left", padx=5)
        ttk.Button(bar, text="Download all figures…", command=self.download_all).pack(side="left")
        self.fig = Figure(figsize=(10, 5))
        self.fig_canvas = FigureCanvasTkAgg(self.fig, master=t)
        NavigationToolbar2Tk(self.fig_canvas, t).update()     # zoom, pan
        self.fig_canvas.get_tk_widget().pack(fill="both", expand=True)

    def show_figure(self):
        name = self.fig_choice.get()
        if name in self.deployments:
            d = self.deployments[name]
            if name in self.rows:
                d["meta"]["ctdref"] = self.rows[name]["ctdref"].get()
            draw_deployment(self.fig, d)
            self.fig_canvas.draw()

    def download_one(self):
        name = self.fig_choice.get()
        if name not in self.deployments:
            return
        path = filedialog.asksaveasfilename(defaultextension=".png",
                                            initialfile=f"{name}_segments.png",
                                            filetypes=[("PNG", "*.png")])
        if path:
            save_png(self.deployments[name], path)

    def download_all(self):
        if not self.deployments:
            return
        d = filedialog.askdirectory(title="Folder to save the figures in")
        if not d:
            return
        for dep in self.deployments.values():
            save_png(dep, Path(d) / f"{dep['filename']}_segments.png")
        messagebox.showinfo("Figures", f"{len(self.deployments)} figures saved in\n{d}")


if __name__ == "__main__":
    App().mainloop()
