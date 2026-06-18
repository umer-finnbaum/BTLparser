"""
BTL Process Extractor

Reads the mapping file at MAPPING_PATH and extracts all machining processes
from the corresponding BTL files, writing output to OUTPUT_DIR.

TWO MODES
---------
Normal mode  (ProjectID and BuildingID present in mapping):
  BTL path constructed as: Z:\\Saha\\<ProjectID>\\<BuildingID>\\<ProjectID>.btl
  One Processes<N>.txt written per unique ProjectID+BuildingID combination.
  Usage: btl_process_extractor.exe

Manual mode  (ProjectID and BuildingID empty in every mapping row):
  A single BTL path is passed as the only argument.
  All elements map to Processes1.txt.
  Usage: btl_process_extractor.exe <btl_path>

OUTPUT FILES  (written to OUTPUT_DIR)
--------------------------------------
Processes<N>.txt
  Line 1 : full BTL file path
  Line 2 : header
  Line N : one row per process

MAPPING_PATH  (overwritten in place with ProcessesFile filled in)
  Header + same rows, ProcessesFile column updated.

Process row format:
  ID, NoOfProcesses, ProcessKey, P1..P15, P15, Type

  P1-P14  : numeric, divided by 100
  P15     : string (quotes stripped)
  Type    : cut-angle classification per part (same logic as btl_parser):
              0 = no qualifying PROCESSKEY found
              1 = both angles 90
              2 = one angle 90, one not
              3 = both angles not 90
            Priority ladder — once a higher type is reached it never decreases.
            Only PROCESSKEY values 1-010-1..4 and 2-010-1..4 qualify.

Exit codes: 1 = success, 0 = failure, 2 = bad arguments
"""

import os
import sys
import csv

# ---------------------------------------------------------------------------
# Configuration  — edit these two paths to match your environment
# ---------------------------------------------------------------------------

OUTPUT_DIR   = r"C:\FBtemp\356\BTL"
MAPPING_PATH = r"C:\FBtemp\356\BTL\mapping.txt"

BTL_ROOT     = r"Z:\Saha"
BTL_TEMPLATE = "{root}\\{project}\\{building}\\{project}.btl"

PROCESS_HEADER = (
    "ID,NoOfProcesses,ProcessKey,"
    "P1,P2,P3,P4,P5,P6,P7,P8,P9,P10,P11,P12,P13,P14,P15,Type"
)
MAPPING_HEADER = "Element,ProjectID,BuildingID,ProcessesFile"

NUM_PARAMS = 15

CUT_PROCESS_KEYS = {
    "1-010-1", "1-010-2", "1-010-3", "1-010-4",
    "2-010-1", "2-010-2", "2-010-3", "2-010-4",
}


# ---------------------------------------------------------------------------
# Mapping CSV
# ---------------------------------------------------------------------------

def detect_text_encoding(path):
    """
    Detect whether a text file is UTF-8 (with or without BOM) or a
    Windows ANSI codepage (cp1252, used by Finnish Windows for ä, ö, å).

    Returns one of: "utf-8-sig", "utf-8", "cp1252".
    """
    with open(path, "rb") as f:
        raw = f.read()

    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"

    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        # Not valid UTF-8 — most likely Windows-1252 (Finnish ä/ö/å etc.)
        return "cp1252"


def load_mapping(csv_path):
    """
    Parse the mapping CSV. Returns (rows, manual_mode, encoding, delimiter).

    rows        : list of dicts — element, project, building, file_num
    manual_mode : True when every row has empty ProjectID and BuildingID
    encoding    : detected text encoding string to use when rewriting
    delimiter   : detected CSV delimiter (',' or ';')

    Raises FileNotFoundError, ValueError, or OSError on failure.
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError("Mapping file not found: {}".format(csv_path))
    if os.path.getsize(csv_path) == 0:
        raise ValueError("Mapping file is empty: {}".format(csv_path))

    encoding = detect_text_encoding(csv_path)

    with open(csv_path, "r", encoding=encoding, errors="strict") as f:
        sample = f.read(1024)
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","

    with open(csv_path, "r", encoding=encoding, errors="strict", newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter)

        if reader.fieldnames is None:
            raise ValueError("Mapping file has no header row.")

        fieldmap = {n.strip().lower(): n for n in reader.fieldnames}
        required = {"element", "projectid", "buildingid"}
        missing  = required - set(fieldmap.keys())
        if missing:
            raise ValueError(
                "Mapping file missing column(s): {}. "
                "Expected: Element, ProjectID, BuildingID".format(
                    ", ".join(sorted(missing)))
            )

        seen = {}
        rows = []

        for row in reader:
            element  = row[fieldmap["element"]].strip()
            project  = row[fieldmap["projectid"]].strip()
            building = row[fieldmap["buildingid"]].strip()

            if project or building:
                key = (project, building)
                if key not in seen:
                    seen[key] = len(seen) + 1
                file_num = seen[key]
            else:
                file_num = 1   # manual mode — all go to Processes1

            rows.append({
                "element":  element,
                "project":  project,
                "building": building,
                "file_num": file_num,
            })

    if not rows:
        raise ValueError("Mapping file contains no valid data rows.")

    manual_mode = all(not r["project"] and not r["building"] for r in rows)
    return rows, manual_mode, encoding, delimiter


def write_mapping(csv_path, rows, encoding="utf-8-sig", delimiter=","):
    """
    Overwrite mapping with ProcessesFile column filled in.
    Writes using the provided encoding and delimiter to preserve the original
    file's character encoding and CSV style (important for Finnish characters).

    encoding: should be one of the values returned by detect_text_encoding()
    delimiter: usually ',' or ';'
    """
    # Recreate header using the chosen delimiter to match original format.
    header = delimiter.join(["Element", "ProjectID", "BuildingID", "ProcessesFile"])
    with open(csv_path, "w", encoding=encoding, newline="") as f:
        f.write(header)
        f.write("\r\n")
        for r in rows:
            # Join fields using delimiter and ensure no extra spaces are added
            f.write(delimiter.join([r["element"], r["project"], r["building"], str(r["file_num"])]))
            f.write("\r\n")


# ---------------------------------------------------------------------------
# BTL path builder
# ---------------------------------------------------------------------------

def build_btl_path(project_id, building_id):
    return BTL_TEMPLATE.format(
        root=BTL_ROOT, project=project_id, building=building_id,
    )


# ---------------------------------------------------------------------------
# Parameter helpers
# ---------------------------------------------------------------------------

def strip_quotes(s):
    return s.replace('"', '')


def scale_param(raw):
    try:
        v = float(raw) / 100.0
        return str(int(v)) if v == int(v) else str(v)
    except (ValueError, TypeError):
        return "0"


def parse_param_tokens(tokens):
    """Return {param_index: raw_value} from a PROCESSPARAMETERS token list."""
    params = {}
    for tok in tokens:
        if not tok or tok[0].upper() != "P":
            continue
        body = tok[1:]
        if ":" not in body:
            continue
        idx_str, val = body.split(":", 1)
        try:
            idx = int(idx_str)
        except ValueError:
            continue
        if 1 <= idx <= NUM_PARAMS:
            params[idx] = val
    return params


def params_to_fields(params):
    """Convert {index: raw} to a list of 15 formatted strings."""
    fields = []
    for i in range(1, NUM_PARAMS + 1):
        raw = params.get(i)
        if raw is None:
            fields.append("" if i == 15 else "0")
        elif i == 15:
            fields.append(strip_quotes(raw))
        else:
            fields.append(scale_param(raw))
    return fields


# ---------------------------------------------------------------------------
# Cut-type classifier  (same priority-ladder logic as btl_parser.py)
# ---------------------------------------------------------------------------

def compute_cut_type(processes):
    """
    Derive the cut Type for a part from its list of process dicts.

    Each process dict has keys "key" (PROCESSKEY str) and "params" ({int: str}).
    P6 = angle1, P7 = angle2, stored as raw BTL integers (9000 = 90 degrees).

    Returns the Type string: "0", "1", "2", or "3".
    """
    type_num = 0

    for proc in processes:
        if proc["key"] not in CUT_PROCESS_KEYS:
            continue

        try:
            angle1 = int(proc["params"].get(6, "0"))
            angle2 = int(proc["params"].get(7, "0"))
        except ValueError:
            continue

        if angle1 == 9000 and angle2 == 9000 and type_num < 1:
            type_num = 1
        elif angle1 == 9000 and angle2 != 9000 and type_num < 2:
            type_num = 2
        elif angle1 != 9000 and angle2 == 9000 and type_num < 2:
            type_num = 2
        elif angle1 != 9000 and angle2 != 9000 and type_num < 3:
            type_num = 3

    return str(type_num)


# ---------------------------------------------------------------------------
# BTL parser
# ---------------------------------------------------------------------------

def parse_btl_processes(btl_path):
    """
    Parse one BTL file and return a list of process rows.
    Each row: [id, no_of_processes, process_key, p1..p15, type]

    Raises FileNotFoundError, ValueError, or OSError on failure.
    """
    if not os.path.exists(btl_path):
        raise FileNotFoundError("BTL file not found: {}".format(btl_path))
    if not os.path.isfile(btl_path):
        raise ValueError("BTL path is not a file: {}".format(btl_path))
    if os.path.getsize(btl_path) == 0:
        raise ValueError("BTL file is empty: {}".format(btl_path))

    # BTL files are saved as UTF-8 (utf-8-sig tolerates an optional BOM).
    # Fall back to windows-1252 only if the file turns out not to be valid
    # UTF-8 — this avoids silently mangling Finnish characters (ä, ö, å)
    # the way decoding UTF-8 bytes as windows-1252 would.
    try:
        with open(btl_path, "r", encoding="utf-8-sig", errors="strict") as f:
            raw_lines = f.readlines()
    except UnicodeDecodeError:
        with open(btl_path, "r", encoding="windows-1252", errors="replace") as f:
            raw_lines = f.readlines()

    parts       = []
    current     = None
    pending_key = ""

    for raw in raw_lines:
        tokens = raw.rstrip("\r\n").split()
        if not tokens:
            continue
        kw = tokens[0]

        if kw == "[PART]":
            if current is not None:
                parts.append(current)
            current     = {"id": "", "processes": []}
            pending_key = ""
            continue

        if current is None:
            continue

        if kw == "SINGLEMEMBERNUMBER:" and len(tokens) > 1:
            current["id"] = tokens[1]
        elif kw == "PROCESSKEY:" and len(tokens) > 1:
            pending_key = tokens[1]
        elif kw == "PROCESSPARAMETERS:":
            current["processes"].append({
                "key":    pending_key,
                "params": parse_param_tokens(tokens[1:]),
            })
            pending_key = ""

    if current is not None:
        parts.append(current)

    if not parts:
        raise ValueError("No [PART] entries found in: {}".format(btl_path))

    rows = []
    for part in parts:
        n_procs  = str(len(part["processes"]))
        cut_type = compute_cut_type(part["processes"])
        for proc in part["processes"]:
            rows.append(
                [part["id"], n_procs, proc["key"]]
                + params_to_fields(proc["params"])
                + [cut_type]
            )

    return rows


# ---------------------------------------------------------------------------
# Output writer
# ---------------------------------------------------------------------------

def write_process_file(path, btl_path, rows, encoding="utf-8"):
    with open(path, "w", encoding=encoding, newline="") as f:
        f.write(btl_path)
        f.write("\r\n")
        f.write(PROCESS_HEADER)
        f.write("\r\n")
        for row in rows:
            f.write(",".join(row))
            f.write("\r\n")


def write_error_file(output_dir, messages, encoding="utf-8-sig"):
    """
    Overwrite OUTPUT_DIR/error.txt. Format:
      - If messages is empty: first line "0"
      - Otherwise: first line = number of errors, then each error on a new line.
    Uses utf-8-sig by default so Notepad shows Finnish characters correctly.
    """
    try:
        os.makedirs(output_dir, exist_ok=True)
        err_path = os.path.join(output_dir, "error.txt")
        with open(err_path, "w", encoding=encoding, newline="") as ef:
            if not messages:
                ef.write("0")
                ef.write("\r\n")
            else:
                ef.write(str(len(messages)))
                ef.write("\r\n")
                for m in messages:
                    ef.write(m)
                    ef.write("\r\n")
    except OSError:
        # If even writing the error file fails, nothing we can do here.
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv):
    # Exit codes:
    #   1 = success — all Processes files written with rows
    #   2 = BTL path error or wrong arguments
    #       (missing/invalid BTL path, wrong number of args, manual mode
    #        invoked without a BTL path argument)
    #   0 = no parts/processes produced
    #       (file found and readable but yielded no usable data,
    #        or a non-path error such as a write failure)

    # argv[0] = exe name; optional argv[1] = manual BTL path
    if len(argv) not in (1, 2):
        print(
            "Usage:\n"
            "  Normal: btl_process_extractor.exe\n"
            "  Manual: btl_process_extractor.exe <btl_path>",
            file=sys.stderr,
        )
        return 2

    manual_btl_path = argv[1] if len(argv) == 2 else None

    # Collect non-fatal error messages to write into error.txt at the end.
    errors = []

    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
    except OSError as e:
        msg = "ERROR: Cannot create output directory '{}': {}".format(OUTPUT_DIR, e)
        print(msg, file=sys.stderr)
        # Try to write error file (will attempt to create the dir); fallback encoding
        write_error_file(OUTPUT_DIR, [msg])
        return 0

    # --- Load mapping -------------------------------------------------------
    try:
        rows, manual_mode, mapping_encoding, mapping_delimiter = load_mapping(MAPPING_PATH)
    except FileNotFoundError as e:
        msg = "ERROR: {}".format(e)
        print(msg, file=sys.stderr)
        write_error_file(OUTPUT_DIR, [msg])
        return 2
    except (OSError, ValueError) as e:
        msg = "ERROR: {}".format(e)
        print(msg, file=sys.stderr)
        # If mapping exists but couldn't be parsed, write error file
        write_error_file(OUTPUT_DIR, [msg])
        return 0

    # Validate argument consistency
    if manual_mode and manual_btl_path is None:
        msg = (
            "ERROR: Mapping has empty ProjectID/BuildingID (manual mode) "
            "but no BTL path was provided as argument."
        )
        print(msg, file=sys.stderr)
        write_error_file(OUTPUT_DIR, [msg])
        return 2

    if not manual_mode and manual_btl_path is not None:
        print(
            "WARNING: BTL path argument supplied but mapping has "
            "ProjectID/BuildingID values — argument will be ignored.",
            file=sys.stderr,
        )

    # --- Build (file_num, btl_path) pairs -----------------------------------
    if manual_mode:
        pairs = [(1, manual_btl_path)]
        print("Manual mode: using '{}'.".format(manual_btl_path))
    else:
        seen_pairs = {}
        for r in rows:
            key = (r["project"], r["building"])
            if key not in seen_pairs:
                seen_pairs[key] = r["file_num"]
        pairs = [
            (file_num, build_btl_path(proj, bldg))
            for (proj, bldg), file_num
            in sorted(seen_pairs.items(), key=lambda x: x[1])
        ]

    # --- Delete stale Processes<N>.txt files from previous runs -------------
    needed = {file_num for file_num, _ in pairs}
    try:
        for fname in os.listdir(OUTPUT_DIR):
            if not fname.startswith("Processes") or not fname.endswith(".txt"):
                continue
            stem = fname[len("Processes"):-len(".txt")]
            try:
                existing_num = int(stem)
            except ValueError:
                continue
            if existing_num not in needed:
                try:
                    os.remove(os.path.join(OUTPUT_DIR, fname))
                    print("Deleted stale: {}".format(fname))
                except OSError as e:
                    warn = "WARNING: Could not delete '{}': {}".format(fname, e)
                    print(warn, file=sys.stderr)
                    errors.append(warn)
    except OSError as e:
        warn = "WARNING: Could not list output directory '{}': {}".format(OUTPUT_DIR, e)
        print(warn, file=sys.stderr)
        errors.append(warn)

    # --- Parse each BTL and write Processes<N>.txt --------------------------
    files_written  = 0
    total_rows     = 0
    failed_paths   = []   # BTL paths that raised FileNotFoundError

    for file_num, btl_path in pairs:
        out_path = os.path.join(OUTPUT_DIR, "Processes{}.txt".format(file_num))

        try:
            process_rows = parse_btl_processes(btl_path)
            print("Parsed '{}': {} process row(s).".format(btl_path, len(process_rows)))
        except FileNotFoundError as e:
            msg = "ERROR: {}".format(e)
            print(msg, file=sys.stderr)
            failed_paths.append(btl_path)
            errors.append(msg)
            process_rows = []
        except (OSError, ValueError) as e:
            msg = "ERROR: {}".format(e)
            print(msg, file=sys.stderr)
            errors.append(msg)
            process_rows = []

        try:
            # Write Processes file using mapping encoding to preserve Finnish chars in mapping-related text
            write_process_file(out_path, btl_path, process_rows, encoding=mapping_encoding or "utf-8")
            files_written += 1
            total_rows    += len(process_rows)
        except OSError as e:
            msg = "ERROR: Could not write '{}': {}".format(out_path, e)
            print(msg, file=sys.stderr)
            errors.append(msg)

    # --- Write status.txt ---------------------------------------------------
    # Always written; first line = number of BTL path errors, then one path
    # per line. Zero errors produces a single line containing "0".
    status_path = os.path.join(OUTPUT_DIR, "status.txt")
    try:
        with open(status_path, "w", encoding=mapping_encoding or "utf-8", newline="") as sf:
            sf.write(str(len(failed_paths)))
            sf.write("\r\n")
            for fp in failed_paths:
                sf.write(fp)
                sf.write("\r\n")
    except OSError as e:
        msg = "ERROR: Could not write status.txt: {}".format(e)
        print(msg, file=sys.stderr)
        errors.append(msg)

    # --- Update mapping -----------------------------------------------------
    try:
        write_mapping(MAPPING_PATH, rows, encoding=mapping_encoding, delimiter=mapping_delimiter)
        print("Mapping updated: {} ({} element(s)).".format(MAPPING_PATH, len(rows)))
    except OSError as e:
        msg = "ERROR: Could not update mapping file: {}".format(e)
        print(msg, file=sys.stderr)
        errors.append(msg)

    # Always overwrite error.txt: no errors => write "0", else write collected messages.
    write_error_file(OUTPUT_DIR, errors)

    print("Done: {} Processes file(s), {} total process row(s).".format(
        files_written, total_rows))

    # Determine exit code
    if failed_paths:
        return 2                    # at least one BTL path was not found
    elif total_rows > 0:
        return 1                    # all paths OK and processes were produced
    else:
        return 0                    # all paths OK but no processes extracted


if __name__ == "__main__":
    sys.exit(main(sys.argv))