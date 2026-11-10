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
        rows.append({"include": note == "", "deployment": n, **{k: m.get(k, "") for k in FIELDS},
                     "note": note})
    st.session_state["meta_df"] = pd.DataFrame(rows, columns=["include", "deployment", *FIELDS, "note"])
    st.session_state["editor_version"] = st.session_state.get("editor_version", 0) + 1
    for k, v in list(hw.items()) + list(common.items()):
        st.session_state[k] = v
    from_config = core.read_cruise_info(project)
    st.session_state["cruise_from"] = (
        f"Pre-filled from config/cruise_info.txt: {', '.join(f'{k} = {v}' for k, v in from_config.items())}."
        if from_config else "")


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


def prefill_from_header(path):
    metas, common, conflicts = core.read_uvpapp_header(path)
    known = set(current_meta()["deployment"])
    set_meta({n: m for n, m in metas.items() if n in known})
    for k, v in common.items():       # cruise and ship: only when still empty
        if v and not str(st.session_state.get(k, "")).strip():
            st.session_state[k] = v
    st.session_state["prefill_msg"] = (
        f"{len(known & set(metas))} of {len(known)} deployments pre-filled from {Path(path).name}.",
        conflicts, sorted(known - set(metas)))


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
if project and project.is_dir():
    found = core.find_uvpapp_header(project)
    b1, b2, b3 = st.columns(3)
    if found:
        b1.button(f"Pre-fill from the existing header ({found.name})", on_click=prefill_from_header,
                  args=(found,), width="stretch",
                  help="Takes ctdref, station, GPS, comment… from it, not its segmentation")
    up = b2.file_uploader("Load a metadata CSV", type=["csv", "txt"], label_visibility="collapsed",
                          help="Columns: filename (or deployment) + any of " + ", ".join(FIELDS))
    if up is not None and st.session_state.get("csv_loaded") != up.file_id:
        text = up.getvalue().decode(errors="replace")
        first = text.splitlines()[0] if text else ""
        csv_df = pd.read_csv(io.StringIO(text), sep=";" if first.count(";") >= first.count(",") else ",",
                             dtype=str, keep_default_na=False)
        col = "filename" if "filename" in csv_df.columns else "deployment"
        if col in csv_df.columns:
            set_meta({r[col]: {k: r[k] for k in FIELDS if k in csv_df.columns} for _, r in csv_df.iterrows()})
            for k in ("cruise", "ship"):
                if k in csv_df.columns and len(csv_df) and csv_df[k].iloc[0]:
                    st.session_state[k] = csv_df[k].iloc[0]
            st.session_state["prefill_msg"] = (f"Metadata loaded from {up.name}.", [], [])
        else:
            st.error("The CSV needs a 'filename' column (deployment date-time, e.g. 20250810-102258).")
        st.session_state["csv_loaded"] = up.file_id
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
        column_config={
            "include": st.column_config.CheckboxColumn("include", width="small"),
            "deployment": st.column_config.TextColumn("deployment (date-time)"),
            "ctdref": st.column_config.TextColumn("ctdref", help="e.g. CTD001: gives <Cruise ID>_CTD001_d01…"),
            "stationid": st.column_config.TextColumn("stationid"),
            "latitude": st.column_config.TextColumn("latitude", help="decimal degrees, e.g. 63.849"),
            "longitude": st.column_config.TextColumn("longitude", help="decimal degrees, e.g. -52.816"),
            "bottomdepth": st.column_config.TextColumn("bottomdepth"),
            "comment": st.column_config.TextColumn("comment", width="large"),
            "constantdepth": st.column_config.TextColumn("constantdepth"),
            "note": st.column_config.TextColumn("note"),
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

    t1, t2, t3 = st.tabs(["Figures", "Segments", "Messages"])
    with t1:
        pick = st.selectbox("Deployment", list(res["deployments"]))
        png = fig_dir / f"{pick}_segments.png"
        if png.exists():
            st.image(str(png), caption=f"{pick}: blue = downcast kept, red = upcast kept, grey = all images")
            st.download_button(f"Download {png.name}", png.read_bytes(), file_name=png.name, mime="image/png")
    with t2:
        st.dataframe(pd.read_csv(res["out_dir"] / "all_segments.csv", sep=";"), hide_index=True,
                     width="stretch")
    with t3:
        st.code("\n".join(res["log"]) or "(none)")
    st.caption(f"All figures are in {fig_dir}.")
