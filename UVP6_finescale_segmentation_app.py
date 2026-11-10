"""UVP6 finescale segmentation in the browser: from a UVP6 project to UVPapp's sample header.

Start it with start_UVP6_finescale_segmentation.bat (Windows) or:
    python -m streamlit run UVP6_finescale_segmentation_app.py
Everything runs on this computer; no data leaves it.

The calculations are those of UVP6_finescale_segmentation_tool.py (first-passage no-stop
segmentation, UVPapp numbering from 0, header archived in meta/archives/).
"""
import importlib.util
import io
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("finescale", HERE / "UVP6_finescale_segmentation_tool.py")
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)

FIELDS = core.DEPLOY_FIELDS          # ctdref, stationid, latitude, longitude, bottomdepth, comment, constantdepth
SHORT_FILE = 100_000                 # bytes: a RAW _data.txt this small is a deck test, not a dive
BUTTON_WIDTH = 420

ICON = HERE / "assets" / "UVP6_finescale_segmentation_256.png"
st.set_page_config(page_title="UVP6 finescale segmentation",
                   page_icon=str(ICON) if ICON.exists() else "🌊", layout="wide")


# ---------------------------------------------------------------- folders
def browse(key):
    """Folder dialog of this computer (the app runs locally)."""
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.wm_attributes("-topmost", 1)
    start = st.session_state.get(key) or str(Path.home())
    path = filedialog.askdirectory(initialdir=start, parent=root)
    root.destroy()
    if path:
        st.session_state[key] = str(Path(path))
        if key == "project":
            on_project_change()


def folder_input(label, key, help_, status=None, on_change=None):
    """Folder field; `status` = (ok, text) is shown in its title, green when ok, red otherwise."""
    if status is not None:
        ok, text = status
        label = f":green[✅ **{label}** · {text}]" if ok else f":red[⚠️ **{label}** · {text}]"
    c1, c2 = st.columns([5, 1], vertical_alignment="bottom")
    c1.text_input(label, key=key, help=help_, on_change=on_change)
    c2.button("Browse…", key=f"b_{key}", on_click=browse, args=(key,), width="stretch")
    return Path(st.session_state[key]) if st.session_state.get(key) else None


def sources_in(project):
    """raw / ecodata folders of the project that hold something."""
    return [s for s in ("raw", "ecodata")
            if (project / s).is_dir() and any((project / s).iterdir())]


def source_flag(project, source):
    """What the raw/ or ecodata/ folder of the project holds, as a short flag."""
    folder = project / source
    if not folder.is_dir():
        return "⚠️ no folder"
    n = sum(1 for p in folder.iterdir() if p.is_dir())
    if n == 0:
        return "⚠️ empty"
    return f"✅ {n} {'deployments' if source == 'raw' else 'UVPapp profiles'}"


def project_status(project):
    if not project:
        return False, "not chosen yet"
    if not project.is_dir():
        return False, "folder not found"
    has = sources_in(project)
    if not has:
        return False, "no data in raw/ or ecodata/"
    n = len(st.session_state.get("meta_df", []))
    return True, f"{' and '.join(has)} found, {n} deployments"


def on_project_change():
    """List the deployments of the project and pre-fill what the data already says."""
    st.session_state.pop("result", None)
    p = st.session_state.get("project")
    project = Path(p) if p else None
    if not project or not project.is_dir() or not sources_in(project):
        st.session_state["meta_df"] = empty_meta()
        return
    has = sources_in(project)
    if st.session_state.get("source") not in has:
        st.session_state["source"] = has[0]
    st.session_state["out_dir"] = str(project / "casts_segmented")
    load_deployments()


def load_deployments():
    project, source = Path(st.session_state["project"]), st.session_state["source"]
    names, metas, hw, common = core.list_deployments(project, source)
    rows = []
    for n in names:
        m = metas.get(n, {})
        note = ""
        if source == "raw":
            size = (project / "raw" / n / f"{n}_data.txt").stat().st_size
            note = "very short file: deck test?" if size < SHORT_FILE else ""
        rows.append({"include": note == "", "deployment": n,
                     **{k: m.get(k, "nan" if k == "constantdepth" else "") for k in FIELDS},
                     "note": note})
    st.session_state.pop("meta_edit", None)
    st.session_state["meta_df"] = pd.DataFrame(rows, columns=["include", "deployment", *FIELDS, "note"])
    st.session_state["editor_version"] = st.session_state.get("editor_version", 0) + 1
    for k in ("cruise", "ship"):
        st.session_state[k] = ""
    for k, v in list(hw.items()) + list(common.items()):
        st.session_state[k] = v
    from_config = core.read_cruise_info(project)
    st.session_state["cruise_from"] = (
        f"Pre-filled from config/cruise_info.txt: {', '.join(f'{k} = {v}' for k, v in from_config.items())}."
        if from_config else "")
    # the deployment metadata is read first from the project's meta/ folder
    st.session_state["meta_source"] = None
    header = core.find_uvpapp_header(project)
    if header:
        read_meta_header(header)


def empty_meta():
    return pd.DataFrame(columns=["include", "deployment", *FIELDS, "note"])


def set_meta(updates):
    """updates = {deployment: {field: value}} applied to the table (then the editor is reset)."""
    df = current_meta().copy()
    for i, name in enumerate(df["deployment"]):
        for k, v in updates.get(name, {}).items():
            if k in df.columns:
                df.at[i, k] = v
    st.session_state["meta_df"] = df
    st.session_state["editor_version"] += 1


def current_meta():
    return st.session_state.get("meta_edit", st.session_state["meta_df"])


def apply_metadata(metas, common, conflicts, label):
    """Put per-deployment metadata into the table; cruise and ship only when still empty."""
    known = set(current_meta()["deployment"])
    set_meta({n: m for n, m in metas.items() if n in known})
    for k, v in common.items():
        if v and not str(st.session_state.get(k, "")).strip():
            st.session_state[k] = v
    st.session_state["prefill_msg"] = (
        f"{len(known & set(metas))} of {len(known)} deployments filled from {label}.",
        [c for c in conflicts if c.split(" ")[0] in known], sorted(known - set(metas)))


def read_meta_header(path):
    """The UVPapp header in the project's meta/ folder."""
    path = Path(path)
    apply_metadata(*core.read_uvpapp_header(path), label=f"meta/{path.name}")
    st.session_state["meta_source"] = path


def read_uploaded(up):
    """A metadata file from elsewhere: a UVPapp header (filename + profileid columns)
    or a CSV with a filename (or deployment) column."""
    raw = up.getvalue()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:          # files saved by Excel or Windows tools
        text = raw.decode("cp1252", errors="replace")
    first = text.splitlines()[0] if text else ""
    if "profileid" in first and "filename" in first:
        apply_metadata(*core.parse_uvpapp_header(text), label=up.name)
        return
    df = pd.read_csv(io.StringIO(text), sep=";" if first.count(";") >= first.count(",") else ",",
                     dtype=str, keep_default_na=False)
    col = "filename" if "filename" in df.columns else "deployment"
    if col not in df.columns:
        st.session_state["upload_error"] = ("The file needs a 'filename' column (deployment date-time, "
                                            "e.g. 20250810-102258), or must be a UVPapp header.")
        return
    common = {k: df[k].iloc[0] for k in ("cruise", "ship") if k in df.columns and len(df)}
    apply_metadata({r[col]: {k: r[k] for k in FIELDS if k in df.columns} for _, r in df.iterrows()},
                   common, [], up.name)


# ---------------------------------------------------------------- station map
CONSTANTDEPTH_HELP = ("UVPapp header field 'constantdepth'. nan by default (not taken from an old header); "
                      "change it here only if needed.")


def positions(meta):
    """Numeric positions of the deployments (DataFrame) and the problems found:
    not a number, out of range, or far from every other station (sign or typo?)."""
    import math
    rows, bad = [], []
    for _, r in meta.iterrows():
        lat_s, lon_s = str(r["latitude"]).strip(), str(r["longitude"]).strip()
        if lat_s.lower() in ("", "nan") and lon_s.lower() in ("", "nan"):
            continue
        try:
            lat, lon = float(lat_s), float(lon_s)
        except ValueError:
            bad.append(f"{r['deployment']}: latitude/longitude \"{lat_s}\", \"{lon_s}\" is not a number.")
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            bad.append(f"{r['deployment']}: position {lat}, {lon} is out of range.")
            continue
        label = str(r["ctdref"]).strip() or r["deployment"]
        rows.append({"deployment": r["deployment"], "label": label, "stationid": str(r["stationid"]),
                     "lat": lat, "lon": lon})
    pos = pd.DataFrame(rows, columns=["deployment", "label", "stationid", "lat", "lon"])

    def km(a, b):
        p1, p2 = math.radians(a.lat), math.radians(b.lat)
        h = (math.sin((p2 - p1) / 2) ** 2
             + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b.lon - a.lon) / 2) ** 2)
        return 12742 * math.asin(math.sqrt(h))
    if len(pos) >= 3:
        pts = list(pos.itertuples())
        for a in pts:
            nearest = min(km(a, b) for b in pts if b is not a)
            if nearest > 1000:
                bad.append(f"{a.deployment} ({a.label}): {nearest:,.0f} km from the nearest other station "
                           f"({a.lat}, {a.lon}): sign or typo?")
    return pos, bad


def grouped_labels(pos, step=0.05):
    """One label per place: deployments within ~`step` degrees share a label, e.g. 'CTD004, CTD005',
    or 'CTD010 … CTD030 (14)' when there are many."""
    groups = {}
    for _, r in pos.iterrows():
        key = (round(r["lat"] / step), round(r["lon"] / step))
        g = groups.setdefault(key, {"lat": [], "lon": [], "refs": []})
        ref = r["label"].split("_", 1)[1] if "_" in r["label"] and not r["label"][:8].isdigit() else r["label"]
        g["lat"].append(r["lat"])
        g["lon"].append(r["lon"])
        g["refs"].append(ref)
    rows = []
    for g in groups.values():
        refs = sorted(g["refs"])
        label = ", ".join(refs) if len(refs) <= 3 else f"{refs[0]} … {refs[-1]} ({len(refs)})"
        rows.append({"lat": max(g["lat"]), "lon": sum(g["lon"]) / len(g["lon"]), "label": label})
    return pd.DataFrame(rows, columns=["lat", "lon", "label"])


def station_map(pos):
    """Interactive map of the stations (pydeck), labelled by ctdref."""
    import math
    import pydeck as pdk
    span = max(pos["lat"].max() - pos["lat"].min(), (pos["lon"].max() - pos["lon"].min()) / 2, 0.05)
    view = pdk.ViewState(latitude=float(pos["lat"].mean()), longitude=float(pos["lon"].mean()),
                         zoom=max(1.0, min(12.0, math.log2(180 / span))))
    dots = pdk.Layer("ScatterplotLayer", pos, get_position="[lon, lat]", get_radius=6,
                     radius_units="pixels", get_fill_color=[42, 120, 214, 220],
                     get_line_color=[255, 255, 255], line_width_min_pixels=1, stroked=True, pickable=True)
    labels = pdk.Layer("TextLayer", grouped_labels(pos), get_position="[lon, lat]", get_text="label",
                       get_size=12, get_color=[20, 20, 20], get_pixel_offset=[0, -14])
    st.pydeck_chart(pdk.Deck(layers=[dots, labels], initial_view_state=view, map_style="light",
                             tooltip={"text": "{label}\n{deployment}\nstation {stationid}\n{lat}, {lon}"}),
                    height=420)


def save_station_png(pos, path):
    """Static map of the stations (for the figures folder): lon/lat with labels."""
    import math
    from matplotlib.figure import Figure
    fig = Figure(figsize=(9, 7))
    ax = fig.add_subplot(111)
    ax.scatter(pos["lon"], pos["lat"], s=30, color="#2a78d6", edgecolor="white", zorder=3)
    span = max(pos["lat"].max() - pos["lat"].min(), pos["lon"].max() - pos["lon"].min(), 0.1)
    for _, r in grouped_labels(pos, step=span / 25).iterrows():
        ax.annotate(r["label"], (r["lon"], r["lat"]), xytext=(5, 4), textcoords="offset points", fontsize=7)
    ax.set_aspect(1 / max(0.1, math.cos(math.radians(pos["lat"].mean()))), adjustable="datalim")
    ax.margins(0.12)
    ax.set_xlabel("longitude (°E)")
    ax.set_ylabel("latitude (°N)")
    ax.grid(alpha=0.3)
    ax.set_title(f"Stations ({len(pos)} deployments)", loc="left")
    fig.tight_layout()
    fig.savefig(path, dpi=130)


# ---------------------------------------------------------------- checks
def problems_of(meta, project_ok):
    out = []
    if not project_ok:
        out.append("Choose a UVP6 project folder with data in raw/ or ecodata/.")
    for k, label in (("cruise", "Cruise ID"), ("ship", "Ship")):
        if not st.session_state.get(k, "").strip():
            out.append(f"Cruise metadata: {label} is empty.")
    inc = meta[meta["include"] == True]  # noqa: E712 (pandas)
    if project_ok and inc.empty:
        out.append("No deployment is included.")
    refs = {}
    for _, r in inc.iterrows():
        ref = str(r["ctdref"] or "").strip()
        if not ref:
            out.append(f"{r['deployment']}: ctdref is empty.")
        else:
            refs.setdefault(ref, []).append(r["deployment"])
        for k in ("latitude", "longitude"):
            v = str(r[k] or "").strip()
            if v and v.lower() != "nan":
                try:
                    float(v)
                except ValueError:
                    out.append(f"{r['deployment']}: {k} \"{v}\" is not a number.")
    out += [f"ctdref {ref} is used by several deployments: {', '.join(ns)}."
            for ref, ns in refs.items() if len(ns) > 1]
    return out


# ---------------------------------------------------------------- pipeline
def run(project, source, out_dir, buffer_dbar, meta):
    """Segmentation (kept between runs while project, source and band do not change),
    figures, then the sample header."""
    key = (str(project), source, buffer_dbar)
    cache = st.session_state.get("seg_cache")
    log = []
    if cache and cache[0] == key:
        deployments, hw, log = cache[1], cache[2], list(cache[3])
        st.status("1. Segmentation: already done for this project, reused", state="complete")
    else:
        with st.status("1. Segment each deployment (first-passage no-stop)", expanded=True) as s:
            bar = st.progress(0.0)
            n_total = max(1, len(meta))

            def note(msg):
                log.append(msg)
                bar.progress(min(1.0, len(log) / n_total), text=msg)

            fn = core.segment_raw if source == "raw" else core.segment_ecodata
            deployments, hw = fn(project / source, buffer_dbar, note)
            s.update(label=f"1. {len(deployments)} deployments segmented", state="complete", expanded=False)
        st.session_state["seg_cache"] = (key, deployments, hw, list(log))

    with st.status("2. Save the segments and the figures", expanded=False) as s:
        (out_dir / "figures").mkdir(parents=True, exist_ok=True)
        core.write_segments_csv(deployments, out_dir / "all_segments.csv")
        for name, d in deployments.items():
            d["meta"]["ctdref"] = str(meta.set_index("deployment").at[name, "ctdref"]) \
                if name in set(meta["deployment"]) else ""
            core.save_png(d, out_dir / "figures" / f"{name}_segments.png")
        pos, _ = positions(meta[meta["include"] == True])  # noqa: E712
        if not pos.empty:
            save_station_png(pos, out_dir / "figures" / "stations_map.png")
        s.update(label=f"2. Segments and {len(deployments)} figures saved in {out_dir}", state="complete")

    with st.status("3. Create the UVPapp sample header", expanded=False) as s:
        g = {k: str(st.session_state.get(k, "")).strip() for k in core.GLOBAL_FIELDS}
        for k, v in hw.items():
            g[k] = g[k] or v
        inc = meta[(meta["include"] == True) & meta["deployment"].isin(list(deployments))]  # noqa: E712
        metas = {r["deployment"]: {k: str(r[k] if pd.notna(r[k]) else "").strip() for k in FIELDS}
                 for _, r in inc.iterrows()}
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = core.header_path(project)
        tmp = target.with_name(target.name + ".new")
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = core.write_header(tmp, deployments, g, metas)
        target, archived = core.install_header(tmp, project, stamp)
        s.update(label=f"3. Sample header written: {target.name} ({lines} profiles)", state="complete")

    skipped = sorted(set(meta["deployment"]) - set(deployments))
    return {"header": target, "archived": archived, "profiles": lines, "deployments": deployments,
            "used": list(metas), "skipped": skipped, "out_dir": out_dir, "log": log}


# ---------------------------------------------------------------- page
st.title("UVP6 finescale segmentation")
st.caption("Splits each rosette dive of a UVP6 project into clean downcast and upcast segments "
           "(images taken while the rosette moves, in water seen for the first time), then writes "
           "the UVPapp sample header. Everything runs on this computer.")

for k, v in (("project", ""), ("source", "raw"), ("out_dir", ""), ("cruise", ""), ("ship", ""),
             ("sampletype", "P"), ("volimage", ""), ("aa", ""), ("exp", ""), ("pixelsize", ""),
             ("editor_version", 0)):
    st.session_state.setdefault(k, v)
st.session_state.setdefault("meta_df", empty_meta())

# 1 ------------------------------------------------------------------------
st.header("1 · Choose the UVP6 project")
st.write("Choose the UVP6 project folder (the one holding **raw**, **ecodata** and **meta**), then the data "
         "to work from. The deployments are listed in step 3: one deployment = one rosette dive = one RAW file.")
project_path = Path(st.session_state["project"]) if st.session_state["project"] else None
ok_project, _ = project_status(project_path)
with st.container(border=True):
    project = folder_input("UVP6 project folder", "project",
                           "uvp6_<serial>_<date>_<cruise>_…: holds raw/, ecodata/ and meta/",
                           status=project_status(project_path), on_change=on_project_change)
    if project and project.is_dir() and sources_in(project):
        has = sources_in(project)
        flags = {s: source_flag(project, s) for s in ("raw", "ecodata")}
        st.radio("Work from", ["raw", "ecodata"], key="source", horizontal=True,
                 format_func=lambda s: {"raw": "raw (RAW files, recommended)",
                                        "ecodata": "ecodata (UVPapp profiles)"}[s] + f"  ·  {flags[s]}",
                 on_change=load_deployments)
        if st.session_state["source"] not in has:
            st.caption(f":red[There is no {st.session_state['source']} data in this project.]")
    out_dir = folder_input("Segments and figures folder", "out_dir", "Where all_segments.csv and the figures are saved",
                           status=(bool(st.session_state["out_dir"]),
                                   "not chosen yet" if not st.session_state["out_dir"]
                                   else ("found" if Path(st.session_state["out_dir"]).is_dir() else "will be created")))

# 2 ------------------------------------------------------------------------
st.header("2 · Cruise metadata")
st.write("The same for every deployment of the project.")
if st.session_state.get("cruise_from"):
    st.caption(st.session_state["cruise_from"] + " Change it here if needed.")
c1, c2, c3 = st.columns([2, 2, 1])
c1.text_input("Cruise ID", key="cruise", help="Starts every profile name, e.g. jc282_Rebels2")
c2.text_input("Ship", key="ship", help="e.g. RRS James Cook")
c3.text_input("Sample type", key="sampletype", help="P for a profile")
with st.expander("Camera settings (read from the data, change only if needed)"):
    d1, d2, d3, d4 = st.columns(4)
    d1.text_input("Image volume (L) · volimage", key="volimage")
    d2.text_input("aa", key="aa")
    d3.text_input("exp", key="exp")
    d4.text_input("Pixel size (µm) · pixelsize", key="pixelsize")

# 3 ------------------------------------------------------------------------
st.header("3 · Deployment metadata")
st.write("One row per deployment (date-time of the RAW file). Fill in the CTD reference, station, GPS position, "
         "comment… Profiles are named automatically **<Cruise ID>_<ctdref>_d01**, _d02… for downcasts and "
         "_u01… for upcasts. Untick **include** to leave a deployment out.")
if project and project.is_dir() and not st.session_state["meta_df"].empty:
    with st.container(border=True):
        src = st.session_state.get("meta_source")
        found = core.find_uvpapp_header(project)
        if src:
            c1, c2 = st.columns([3, 1], vertical_alignment="center")
            c1.markdown(f":green[✅ **Metadata read from the project:** meta/{Path(src).name}] · edit it in the "
                        "table below (only its metadata is used, not its segmentation).")
            c2.button(f"Read meta/{Path(src).name} again", on_click=read_meta_header, args=(src,),
                      width="stretch", help="Puts the file's values back in the table, replacing your edits of those values")
        elif found:
            c1, c2 = st.columns([3, 1], vertical_alignment="center")
            c1.markdown(f"The project has a UVPapp header in meta/: **{found.name}**.")
            c2.button("Read it", on_click=read_meta_header, args=(found,), width="stretch")
        else:
            st.markdown(":orange[⚠️ **No UVPapp header in the project's meta/ folder.**] "
                        "Fill in the table below, or load a file from elsewhere.")
        up = st.file_uploader("Or load a metadata file from elsewhere (UVPapp header .txt, or CSV)",
                              type=["csv", "txt"],
                              help="A UVPapp header (uvp6_header_*.txt), or a CSV with a filename (or deployment) "
                                   "column and any of " + ", ".join(FIELDS))
        if up is not None and st.session_state.get("upload_done") != up.file_id:
            read_uploaded(up)
            st.session_state["upload_done"] = up.file_id
            st.rerun()
    if "upload_error" in st.session_state:
        st.error(st.session_state.pop("upload_error"))
    msg = st.session_state.pop("prefill_msg", None)
    if msg:
        text, conflicts, missing = msg
        st.success(text, icon="✅")
        if missing:
            st.warning(f"Not in the header ({len(missing)}): " + ", ".join(missing), icon="⚠️")
        if conflicts:
            st.warning("Values that differ between profiles of one deployment, check them:\n\n- "
                       + "\n- ".join(conflicts), icon="⚠️")

meta_df = st.session_state["meta_df"]
if meta_df.empty:
    st.caption(":red[Choose the UVP6 project in step 1: its deployments will be listed here.]")
    meta = meta_df
else:
    meta = st.data_editor(
        meta_df, key=f"editor_{st.session_state['editor_version']}", hide_index=True,
        width="stretch", num_rows="fixed", disabled=["deployment", "note"],
        # fixed widths so that the whole table fits on the page (no column hidden on the right)
        column_config={
            "include": st.column_config.CheckboxColumn("include", width=60),
            "deployment": st.column_config.TextColumn("deployment", width=130,
                                                      help="Date-time of the RAW file (yyyymmdd-HHMMSS)"),
            "ctdref": st.column_config.TextColumn("ctdref", width=110,
                                                  help="e.g. CTD001: gives <Cruise ID>_CTD001_d01…"),
            "stationid": st.column_config.TextColumn("stationid", width=110),
            "latitude": st.column_config.TextColumn("latitude", width=85, help="decimal degrees, e.g. 63.849"),
            "longitude": st.column_config.TextColumn("longitude", width=85, help="decimal degrees, e.g. -52.816"),
            "bottomdepth": st.column_config.TextColumn("bottomdepth", width=95, help="Bottom depth (m), or nan"),
            "comment": st.column_config.TextColumn("comment", width=230),
            "constantdepth": st.column_config.TextColumn("constantdepth", width=105, help=CONSTANTDEPTH_HELP),
            "note": st.column_config.TextColumn("note", width=160),
        })
    meta = meta.fillna("")
    st.session_state["meta_edit"] = meta
    cruise = st.session_state["cruise"].strip()
    names = meta[meta["include"] == True]  # noqa: E712
    names = names[names["ctdref"].astype(str).str.strip() != ""]
    with st.expander(f"Profile names that will be created ({len(names)} deployments with a ctdref)"):
        st.dataframe(pd.DataFrame({
            "deployment": names["deployment"],
            "first downcast": [core.profile_name(cruise, str(r).strip(), "down", 1) for r in names["ctdref"]],
            "first upcast": [core.profile_name(cruise, str(r).strip(), "up", 1) for r in names["ctdref"]],
        }), hide_index=True, width="stretch")
    st.subheader("Station map: check the GPS positions")
    pos, bad = positions(meta[meta["include"] == True])  # noqa: E712
    for b in bad:
        st.warning(b, icon="⚠️")
    if pos.empty:
        st.caption("Enter latitude and longitude in the table: the stations appear here.")
    else:
        station_map(pos)
        st.caption(f"{len(pos)} included deployments with a position. Hover a point to see which one it is.")
    csv_out = meta.drop(columns=["note"]).rename(columns={"deployment": "filename"})
    csv_out.insert(2, "cruise", st.session_state["cruise"])
    csv_out.insert(3, "ship", st.session_state["ship"])
    st.download_button("Save this metadata (CSV)", csv_out.to_csv(sep=";", index=False).encode(),
                       file_name="deployments_metadata.csv", mime="text/csv")

# 4 ------------------------------------------------------------------------
st.header("4 · Run")
st.write("Segments every deployment, saves the segments and one figure per deployment, then writes the sample "
         "header for UVPapp in meta/, under the only name UVPapp reads.")
buffer_dbar = st.number_input("Bottom stop band (dbar)", min_value=0.5, value=5.0, step=0.5,
                              help="Images within this band of the maximum pressure are the bottom stop: "
                                   "neither downcast nor upcast. Also skips deck tests shallower than this.")
if ok_project:
    target = core.header_path(project)
    if target.exists():
        st.info(f"This project already has a sample header ({target.name}): it will be moved to "
                f"meta/archives/ as {target.name}.backup_<date-time>, then the new one written.", icon="ℹ️")
problems = problems_of(meta, ok_project and st.session_state["source"] in sources_in(project))
if problems:
    st.caption(":red[To do before running:]\n\n" + "\n".join(f":red[- {p}]" for p in problems[:15])
               + ("\n\n:red[…]" if len(problems) > 15 else ""))
if st.button("Run the segmentation and create the sample header", type="primary",
             disabled=bool(problems), width=BUTTON_WIDTH):
    try:
        st.session_state["result"] = run(project, st.session_state["source"], Path(st.session_state["out_dir"]),
                                         float(buffer_dbar), meta)
        st.session_state.pop("error", None)
    except Exception as e:  # shown in the page
        st.session_state["error"] = f"{type(e).__name__}: {e}"
        st.session_state.pop("result", None)

if "error" in st.session_state:
    st.error(f"The run stopped: {st.session_state['error']}. Nothing was replaced in meta/.", icon="🛑")

# 5 ------------------------------------------------------------------------
res = st.session_state.get("result")
if res:
    st.header("5 · Results: sample header, segments and figures")
    header = res["header"]
    st.download_button("Download the sample header for UVPapp", header.read_bytes(), file_name=header.name,
                       type="primary", width=BUTTON_WIDTH)
    st.caption(f"Saved in the UVP6 project: {header}"
               + (f"  ·  previous header archived as {res['archived'].name}" if res["archived"] else ""))
    segs = [s for n in res["used"] for s in res["deployments"][n]["segments"]]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Deployments in the header", len(res["used"]))
    c2.metric("Profiles", res["profiles"])
    c3.metric("Downcast segments", sum(s["direction"] == "down" for s in segs))
    c4.metric("Upcast segments", sum(s["direction"] == "up" for s in segs))
    if res["skipped"]:
        st.warning("No data kept (deck test or empty file), left out: " + ", ".join(res["skipped"]), icon="⚠️")

    fig_dir = res["out_dir"] / "figures"
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(fig_dir.glob("*.png")):
            z.write(f, f.name)
        z.write(res["out_dir"] / "all_segments.csv", "all_segments.csv")
    st.download_button("Download all figures and the segments table (zip)", zipped.getvalue(),
                       file_name=f"{project.name}_segments.zip", mime="application/zip", width=BUTTON_WIDTH)

    t1, t4, t2, t3 = st.tabs(["Figures", "Station map", "Segments", "Messages"])
    with t1:
        st.caption("Blue = downcast kept, red = upcast kept, grey = all images.")
        names = list(res["deployments"])
        refs = st.session_state.get("meta_edit", st.session_state["meta_df"]).set_index("deployment")["ctdref"]
        cols = st.columns(2)
        for i, name in enumerate(names):
            png = fig_dir / f"{name}_segments.png"
            if not png.exists():
                continue
            with cols[i % 2]:
                ref = str(refs.get(name, "")).strip()
                st.image(str(png), caption=f"{name}" + (f" · {ref}" if ref else ""))
                st.download_button(f"Download {png.name}", png.read_bytes(), file_name=png.name,
                                   mime="image/png", key=f"dl_{name}")
    with t4:
        map_png = fig_dir / "stations_map.png"
        if map_png.exists():
            st.image(str(map_png), caption="Positions entered in step 3 (also in the figures folder)")
            st.download_button("Download stations_map.png", map_png.read_bytes(), file_name=map_png.name,
                               mime="image/png", key="dl_map")
        else:
            st.caption("No position was entered in step 3.")
    with t2:
        st.dataframe(pd.read_csv(res["out_dir"] / "all_segments.csv", sep=";"), hide_index=True,
                     width="stretch")
    with t3:
        st.code("\n".join(res["log"]) or "(none)")
    st.caption(f"All figures are in {fig_dir}.")
