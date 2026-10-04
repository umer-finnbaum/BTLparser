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
import datetime

# ---------------------------------------------------------------------------
# Configuration  — edit these two paths to match your environment
# ---------------------------------------------------------------------------

OUTPUT_DIR   = r"C:\FBtemp\356\BTL"
MAPPING_PATH = r"C:\FBtemp\356\BTL\mapping.txt"
PARSE_STATUS_FILE = "ParseStatusProcess.txt"

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

def parse_count(token):
    """Parse a COUNT value. Missing, invalid or < 1 -> 1."""
    try:
        n = int(float(token))
    except (ValueError, TypeError):
        return 1
    return n if n > 1 else 1


COPY_ID_START = 20000   # first ID handed out to extra copies
MAX_ID_VALUE  = 32000   # CX Supervisor integer limit
MAX_BASE_ID   = 9999    # highest original SINGLEMEMBERNUMBER expected
COUNT_ENABLED = False    # False -> COUNT is ignored (treated as 1), no duplicate IDs
# All four can be overridden by BTLsettings.txt (see load_id_settings).

BTL_SETTINGS_PATH = r"C:\FBtemp\356\BTL\BTLsettings.txt"


def load_id_settings(path=None):
    """
    Override COPY_ID_START, MAX_ID_VALUE, MAX_BASE_ID and COUNT_ENABLED from BTLsettings.txt.

    File format (one setting per line, spaces optional):
        COPY_ID_START = 20000
        MAX_ID_VALUE = 32000
        MAX_BASE_ID = 9999
        COUNT_ENABLED = 1      (1/0, ON/OFF, TRUE/FALSE, YES/NO)

    Missing file      -> built-in defaults are used (no warning).
    Invalid line/value -> warning printed, that setting keeps its default.
    """
    global COPY_ID_START, MAX_ID_VALUE, MAX_BASE_ID, COUNT_ENABLED
    path = path or BTL_SETTINGS_PATH

    if not os.path.isfile(path):
        return

    values = {}
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            for line_num, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line:
                    continue
                if "=" not in line:
                    print("WARNING: BTLsettings line {} ignored (no '='): '{}'"
                          .format(line_num, line), file=sys.stderr)
                    continue
                key, val = (p.strip() for p in line.split("=", 1))
                key = key.upper()
                if key == "COUNT_ENABLED":
                    flag = val.upper()
                    if flag in ("1", "ON", "TRUE", "YES"):
                        values[key] = True
                    elif flag in ("0", "OFF", "FALSE", "NO"):
                        values[key] = False
                    else:
                        print("WARNING: BTLsettings line {} ignored (invalid value '{}' for "
                              "COUNT_ENABLED, use 1 or 0).".format(line_num, val), file=sys.stderr)
                    continue
                if key not in ("COPY_ID_START", "MAX_ID_VALUE", "MAX_BASE_ID"):
                    print("WARNING: BTLsettings line {} ignored (unknown setting '{}')."
                          .format(line_num, key), file=sys.stderr)
                    continue
                try:
                    num = int(val)
                    if num <= 0:
                        raise ValueError
                except ValueError:
                    print("WARNING: BTLsettings line {} ignored (invalid value '{}' for {})."
                          .format(line_num, val, key), file=sys.stderr)
                    continue
                values[key] = num
    except OSError as e:
        print("WARNING: Could not read '{}': {}. Using defaults.".format(path, e),
              file=sys.stderr)
        return

    COPY_ID_START = values.get("COPY_ID_START", COPY_ID_START)
    MAX_ID_VALUE  = values.get("MAX_ID_VALUE",  MAX_ID_VALUE)
    MAX_BASE_ID   = values.get("MAX_BASE_ID",   MAX_BASE_ID)
    COUNT_ENABLED = values.get("COUNT_ENABLED", COUNT_ENABLED)

    # Sanity checks - warn only, never stop the run
    if COPY_ID_START <= MAX_BASE_ID:
        print("WARNING: COPY_ID_START ({}) must be above MAX_BASE_ID ({}), "
              "otherwise copy IDs can collide with original IDs."
              .format(COPY_ID_START, MAX_BASE_ID), file=sys.stderr)
    if COPY_ID_START > MAX_ID_VALUE:
        print("WARNING: COPY_ID_START ({}) is above MAX_ID_VALUE ({})."
              .format(COPY_ID_START, MAX_ID_VALUE), file=sys.stderr)

    print("ID settings: COPY_ID_START={}, MAX_ID_VALUE={}, MAX_BASE_ID={}, COUNT_ENABLED={}"
          .format(COPY_ID_START, MAX_ID_VALUE, MAX_BASE_ID, "ON" if COUNT_ENABLED else "OFF"))


class CopyIdAllocator:
    """
    Assigns IDs to parts with COUNT > 1, keeping every ID a small integer.

      - The first copy keeps its original ID.
      - Each extra copy gets the next free ID from a counter starting at
        COPY_ID_START (default 20000), shared by all parts in the same BTL file.

    Example (parts in file order):
      ID 100, COUNT 3  ->  100, 20000, 20001
      ID 205, COUNT 1  ->  205
      ID 300, COUNT 2  ->  300, 20002

    One allocator is used per BTL file, so the same file always produces the
    same IDs - FileARR and Processes files stay consistent.
    """

    def __init__(self, start=None, limit=None):
        # Read the module values at call time so BTLsettings.txt overrides apply
        self.next_id = COPY_ID_START if start is None else start
        self.limit   = MAX_ID_VALUE  if limit is None else limit
        self._warned_limit = False

    def expand(self, base_id, count):
        """Return the list of IDs (as strings) for one part."""
        if not COUNT_ENABLED:
            count = 1   # COUNT handling switched off -> one entry, original ID
        try:
            if int(base_id) > MAX_BASE_ID:
                print("WARNING: ID {} exceeds {} and may collide with copy IDs "
                      "starting at {}.".format(base_id, MAX_BASE_ID, COPY_ID_START),
                      file=sys.stderr)
        except (ValueError, TypeError):
            pass

        ids = [base_id]
        for _ in range(count - 1):
            if self.next_id > self.limit and not self._warned_limit:
                print("WARNING: Copy ID {} exceeds the limit of {}."
                      .format(self.next_id, self.limit), file=sys.stderr)
                self._warned_limit = True
            ids.append(str(self.next_id))
            self.next_id += 1
        return ids


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
            current     = {"id": "", "count": 1, "processes": []}
            pending_key = ""
            continue

        if current is None:
            continue

        if kw == "SINGLEMEMBERNUMBER:" and len(tokens) > 1:
            current["id"] = tokens[1]
        elif kw == "COUNT:" and len(tokens) > 1:
            current["count"] = parse_count(tokens[1])
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
    id_alloc = CopyIdAllocator()   # IDs for COUNT > 1 copies, per BTL file
    for part in parts:
        n_procs  = str(len(part["processes"]))
        cut_type = compute_cut_type(part["processes"])
        # COUNT > 1 -> the full process list is repeated for each copy,
        # first copy keeps the ID, extra copies get 10001, 10002, ...
        for copy_id in id_alloc.expand(part["id"], part["count"]):
            for proc in part["processes"]:
                rows.append(
                    [copy_id, n_procs, proc["key"]]
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


class ParseStatus:
    """
    Collects everything that happens during a run and writes ParseStatusProcess.txt.

    File layout (CRLF, one KEY=value per line, then one block per BTL file):

        RESULT=OK                     OK | PATH_ERROR | ARGUMENT_ERROR | NO_DATA | ERROR
        EXITCODE=1
        TIME=2026-10-01 14:05:12
        MODE=MANUAL                   MANUAL | AUTO | UNKNOWN
        FILES=1
        ERRORS=0
        WARNINGS=0

        [Processes1.txt]
        BTL=Z:\\Saha\\356\\12\\356.btl
        STATUS=OK                     OK | NOT_FOUND | READ_ERROR | WRITE_ERROR | NO_PROCESSES
        IDS=12
        PROCESSROWS=40
        Element,ProjectID,BuildingID,ProcessesFile
        1,,,1
        2,,,1

        [MESSAGES]
        ERROR: ...
        WARNING: ...
    """

    def __init__(self):
        self.mode     = "UNKNOWN"
        self.files    = []      # list of dicts, one per Processes<N>.txt
        self.errors   = []
        self.warnings = []

    def error(self, msg):
        print(msg, file=sys.stderr)
        self.errors.append(msg)

    def warning(self, msg):
        print(msg, file=sys.stderr)
        self.warnings.append(msg)

    def add_file(self, file_num, btl_path, status, process_rows, mapping_rows):
        ids = len({r[0] for r in process_rows})
        self.files.append({
            "file_num":     file_num,
            "btl":          btl_path,
            "status":       status,
            "ids":          ids,
            "rows":         len(process_rows),
            "mapping_rows": mapping_rows,
        })

    def write(self, result, exit_code, encoding="utf-8"):
        lines = [
            "RESULT={}".format(result),
            "EXITCODE={}".format(exit_code),
            "TIME={}".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "MODE={}".format(self.mode),
            "FILES={}".format(len(self.files)),
            "ERRORS={}".format(len(self.errors)),
            "WARNINGS={}".format(len(self.warnings)),
        ]
        for f in self.files:
            lines += [
                "",
                "[Processes{}.txt]".format(f["file_num"]),
                "BTL={}".format(f["btl"]),
                "STATUS={}".format(f["status"]),
                "IDS={}".format(f["ids"]),
                "PROCESSROWS={}".format(f["rows"]),
                MAPPING_HEADER,
            ]
            for r in f["mapping_rows"]:
                lines.append("{},{},{},{}".format(
                    r["element"], r["project"], r["building"], r["file_num"]))
        if self.errors or self.warnings:
            lines += ["", "[MESSAGES]"] + self.errors + self.warnings

        try:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            with open(os.path.join(OUTPUT_DIR, PARSE_STATUS_FILE), "w",
                      encoding=encoding, errors="replace", newline="") as sf:
                for line in lines:
                    sf.write(line)
                    sf.write("\r\n")
        except OSError as e:
            print("ERROR: Could not write {}: {}".format(PARSE_STATUS_FILE, e), file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv):
    load_id_settings()   # optional BTLsettings.txt overrides
    # Exit codes:
    #   1 = success — all Processes files written with rows
    #   2 = BTL path error or wrong arguments
    #       (missing/invalid BTL path, wrong number of args, manual mode
    #        invoked without a BTL path argument)
    #   0 = no parts/processes produced
    #       (file found and readable but yielded no usable data,
    #        or a non-path error such as a write failure)

    status = ParseStatus()

    # argv[0] = exe name; optional argv[1] = manual BTL path
    if len(argv) not in (1, 2):
        status.error(
            "ERROR: Wrong number of arguments ({}). Usage: "
            "btl_process_extractor.exe [<btl_path>]".format(len(argv) - 1)
        )
        status.write("ARGUMENT_ERROR", 2)
        return 2

    manual_btl_path = argv[1] if len(argv) == 2 else None

    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
    except OSError as e:
        status.error("ERROR: Cannot create output directory '{}': {}".format(OUTPUT_DIR, e))
        status.write("ERROR", 0)
        return 0

    # --- Load mapping -------------------------------------------------------
    try:
        rows, manual_mode, mapping_encoding, mapping_delimiter = load_mapping(MAPPING_PATH)
    except FileNotFoundError as e:
        status.error("ERROR: {}".format(e))
        status.write("PATH_ERROR", 2)
        return 2
    except (OSError, ValueError) as e:
        status.error("ERROR: {}".format(e))
        status.write("ERROR", 0)
        return 0

    status.mode = "MANUAL" if manual_mode else "AUTO"
    # Same encoding as mapping.txt, so CX Supervisor reads both files the same way
    # (BOM dropped so line 1 always reads exactly "RESULT=...")
    status_encoding = "utf-8" if mapping_encoding in (None, "utf-8-sig") else mapping_encoding

    # Validate argument consistency
    if manual_mode and manual_btl_path is None:
        status.error(
            "ERROR: Mapping has empty ProjectID/BuildingID (manual mode) "
            "but no BTL path was provided as argument."
        )
        status.write("ARGUMENT_ERROR", 2, status_encoding)
        return 2

    if not manual_mode and manual_btl_path is not None:
        status.warning(
            "WARNING: BTL path argument supplied but mapping has "
            "ProjectID/BuildingID values - argument will be ignored."
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
                    status.warning("WARNING: Could not delete '{}': {}".format(fname, e))
    except OSError as e:
        status.warning("WARNING: Could not list output directory '{}': {}".format(OUTPUT_DIR, e))

    # --- Parse each BTL and write Processes<N>.txt --------------------------
    files_written  = 0
    total_rows     = 0
    failed_paths   = []   # BTL paths that raised FileNotFoundError

    for file_num, btl_path in pairs:
        out_path = os.path.join(OUTPUT_DIR, "Processes{}.txt".format(file_num))
        file_status = "OK"

        try:
            process_rows = parse_btl_processes(btl_path)
            print("Parsed '{}': {} process row(s).".format(btl_path, len(process_rows)))
            if not process_rows:
                file_status = "NO_PROCESSES"
        except FileNotFoundError as e:
            status.error("ERROR: {}".format(e))
            failed_paths.append(btl_path)
            process_rows = []
            file_status = "NOT_FOUND"
        except (OSError, ValueError) as e:
            status.error("ERROR: {}".format(e))
            process_rows = []
            file_status = "READ_ERROR"

        try:
            # Write Processes file using mapping encoding to preserve Finnish chars in mapping-related text
            write_process_file(out_path, btl_path, process_rows, encoding=mapping_encoding or "utf-8")
            files_written += 1
            total_rows    += len(process_rows)
        except OSError as e:
            status.error("ERROR: Could not write '{}': {}".format(out_path, e))
            file_status = "WRITE_ERROR"

        status.add_file(
            file_num, btl_path, file_status, process_rows,
            [r for r in rows if r["file_num"] == file_num],
        )

    # --- Update mapping -----------------------------------------------------
    try:
        write_mapping(MAPPING_PATH, rows, encoding=mapping_encoding, delimiter=mapping_delimiter)
        print("Mapping updated: {} ({} element(s)).".format(MAPPING_PATH, len(rows)))
    except OSError as e:
        status.error("ERROR: Could not update mapping file: {}".format(e))

    print("Done: {} Processes file(s), {} total process row(s).".format(
        files_written, total_rows))

    # Determine exit code
    if failed_paths:
        exit_code, result = 2, "PATH_ERROR"   # at least one BTL path was not found
    elif total_rows > 0:
        exit_code, result = 1, "OK"           # all paths OK and processes were produced
    else:
        exit_code, result = 0, "NO_DATA"      # all paths OK but no processes extracted

    status.write(result, exit_code, status_encoding)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))