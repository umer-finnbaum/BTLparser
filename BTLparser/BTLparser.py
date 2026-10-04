"""
BTL file parser for OMRON CX Supervisor offload.

Reads a .btl text file, classifies each PART by its PACKAGE value, and writes:
  - FileARR<N>.txt  for each numbered package bucket (1,2,3,4,8,12,13,18,20,21,22,25)
  - FileARR.txt     for parts whose package is not in the known list (bucket 0)

Each output file:
  Line 1  : count of parts stored in this file
  Line N  : comma-separated part data (see ROW FORMAT section below)

ROW FORMAT
----------
Numbered buckets (FileARR1 ... FileARR25):
  PartNum, SingleMemberNum, MaterialNum, Length, Height, Width,
  ModuleNum, TimberGrade, Designation, CutType

Bucket 0 / FileARR (unclassified package):
  PartNum, SingleMemberNum, MaterialNum, Length, Height, Width,
  ModuleNum, TimberGrade, Designation, CutType, OriginalPackage

PACKAGE -> FileARR bucket mapping
----------------------------------
  1, 2, 3, 4, 8, 12, 13, 18, 20, 21  ->  same number
  1.1                                 ->  22
  8.1                                 ->  25
  anything else                       ->  0  (FileARR)

NOTE: A PACKAGE 1 part that also has a MODULENUMBER line is reclassified to
bucket 22, matching the VBScript behaviour.

CUT TYPE logic
--------------
Derived from PROCESSKEY + PROCESSPARAMETERS lines within each part.
Only the following PROCESSKEY values trigger angle inspection:
  1-010-1, 1-010-2, 1-010-3, 1-010-4
  2-010-1, 2-010-2, 2-010-3, 2-010-4

From the matching PROCESSPARAMETERS line, tokens starting with 'P' are parsed
as 'P<index>:<value>' pairs. Index 6 = angle1, index 7 = angle2.
Angles are integers (9000 = 90.00 degrees in BTL units).

Cut type is assigned by a priority ladder (typeNum never goes backwards):
  typeNum < 1: angle1==9000 and angle2==9000  -> CutType "1"
  typeNum < 2: angle1==9000 and angle2!=9000  -> CutType "2"
  typeNum < 2: angle1!=9000 and angle2==9000  -> CutType "2"
  typeNum < 3: angle1!=9000 and angle2!=9000  -> CutType "3"

MATERIAL LOOKUP
---------------
The MATERIAL name from the BTL is matched (case-insensitive) against the Name
column of fb_MAT_STOCK.txt (semicolon-delimited). Returns the material number
or "100" if not found. Path is hardcoded to MAT_STOCK_PATH below.

MATERIAL OVERRIDE
------------------
After the material number is resolved, it is checked against MAT_SETTINGS.txt
(path hardcoded to MAT_SETTINGS_PATH). Each line couples two materials that
are the same stock at different lengths:
  <main>,<overridden>
Example: "6,7" means every part resolved to material 7 is reassigned to
material 6 in the output FileARR rows. Missing settings file -> no overrides.

Usage
-----
  python btl_parser.py <input.btl>
"""

import os
import sys
import datetime

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAT_STOCK_PATH = r"C:\FBtemp\356\Configuration\fb_MAT_STOCK.txt"
MAT_SETTINGS_PATH = r"C:\FBtemp\356\Configuration\MAT_SETTINGS.txt"
OUTPUT_DIR     = r"C:\FBtemp\356\BTL"

PACKAGE_TO_BUCKET = {
    "1": 1, "2": 2, "3": 3, "4": 4,
    "8": 8, "12": 12, "13": 13, "18": 18, "20": 20, "21": 21,
    "1.1": 22,
    "8.1": 25,
    # Uncomment if these packages become active:
    # "1.2": 23,
    # "6.1": 24,
    # "14.1": 26,
}

NUMBERED_BUCKETS = [1, 2, 3, 4, 8, 12, 13, 18, 20, 21, 22, 25]

CUT_PROCESS_KEYS = {
    "1-010-1", "1-010-2", "1-010-3", "1-010-4",
    "2-010-1", "2-010-2", "2-010-3", "2-010-4",
}


# Header written as line 2 of every output file (line 1 is the part count).
FILE_HEADER = "PartNum,ID,MaterialType,Length,Heigth,Width,ModuleNum,Timbergrade,ElemName,Type"


# ---------------------------------------------------------------------------
# Encoding detection (keep Finnish characters correct)
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


# ---------------------------------------------------------------------------
# Material stock lookup
# ---------------------------------------------------------------------------

def load_material_stock(path):
    """
    Parse fb_MAT_STOCK.txt and return {name_upper: material_number_str}.
    File is semicolon-delimited with header: Material;Name;Width;Height;Length

    This now attempts to detect the file encoding to preserve Finnish characters.
    """
    lookup = {}
    if not os.path.exists(path):
        # Preserve previous behavior: warn and continue with empty lookup
        print(
            "WARNING: Material stock file not found: '{}'. "
            "All materials will be reported as 100.".format(path),
            file=sys.stderr,
        )
        return lookup

    # Detect encoding and open accordingly. Use replace for robustness.
    try:
        encoding = detect_text_encoding(path)
    except Exception:
        encoding = "utf-8"

    try:
        with open(path, "r", encoding=encoding, errors="replace") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line or i == 0:      # skip empty lines and header
                    continue
                parts = line.split(";")
                if len(parts) >= 2:
                    mat_num  = parts[0].strip()
                    mat_name = parts[1].strip()
                    lookup[mat_name.upper()] = mat_num
    except Exception as e:
        # If something unexpected goes wrong, warn and continue (same overall behavior).
        print(
            "WARNING: Failed to read material stock '{}': {}. "
            "All materials will be reported as 100.".format(path, e),
            file=sys.stderr,
        )
    return lookup


def get_material_number(name, lookup):
    """Return the material number string for a name, or '100' if not found."""
    return lookup.get(name.upper(), "100")


# ---------------------------------------------------------------------------
# Material override (couple materials that are the same stock, e.g. 6 & 7)
# ---------------------------------------------------------------------------

def load_material_overrides(path):
    """
    Parse MAT_SETTINGS.txt and return {overridden_num: main_num}.

    Each line: <main>,<overridden>   (comma or semicolon delimited)
    Example:   6,7
               means material 7 is reassigned to material 6 in the output.

    Missing file -> empty dict (warning printed, no overrides applied).
    Malformed lines are skipped with a warning; parsing continues.
    """
    overrides = {}

    if not os.path.exists(path):
        print(
            "WARNING: Material settings file not found: '{}'. "
            "No material overrides will be applied.".format(path),
            file=sys.stderr,
        )
        return overrides

    try:
        encoding = detect_text_encoding(path)
    except Exception:
        encoding = "utf-8"

    try:
        with open(path, "r", encoding=encoding, errors="replace") as f:
            for line_num, raw_line in enumerate(f, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                delimiter = ";" if ";" in line else ","
                parts = [p.strip() for p in line.split(delimiter)]
                if len(parts) < 2 or not parts[0] or not parts[1]:
                    print(
                        "WARNING: Skipping malformed line {} in material settings: '{}'".format(
                            line_num, line),
                        file=sys.stderr,
                    )
                    continue
                main_num, overridden_num = parts[0], parts[1]
                overrides[overridden_num] = main_num
    except Exception as e:
        print(
            "WARNING: Failed to read material settings '{}': {}. "
            "No material overrides will be applied.".format(path, e),
            file=sys.stderr,
        )
        return {}

    return overrides


def apply_material_override(material_num, overrides):
    """Return the overriding material number if one is configured, else unchanged."""
    return overrides.get(material_num, material_num)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def strip_quotes(s):
    """Remove all double-quote characters (VBScript Replace(s, Chr(34), ''))."""
    return s.replace('"', '')


def scale_dimension(token):
    """
    Divide a BTL raw dimension by 100. Return as a plain integer string when
    the result is whole, otherwise as a float string.
    """
    try:
        v = float(token) / 100.0
        if v == int(v):
            return str(int(v))
        return str(v)
    except ValueError:
        return "0"


# ---------------------------------------------------------------------------
# Row builders
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


def make_numbered_row(part):
    """
    Row for a named bucket (FileARR1 ... FileARR25).
    Matches the helper script output:
      Dfield[10], Dfield[1], Dfield[2->material_num], Dfield[5], Dfield[6],
      Dfield[7], Dfield[9], Dfield[11], Dfield[12], Dfield[14]
    """
    return ",".join([
        part.get("part_num",     "0"),    # ANNOTATION
        part.get("single_member",""),     # SINGLEMEMBERNUMBER
        part.get("material_num", "100"),  # resolved via scrGETmaterial
        part.get("length",       "0"),
        part.get("height",       "0"),
        part.get("width",        "0"),
        part.get("module_num",   "0"),    # MODULENUMBER (part after "-")
        part.get("timber_grade", "0"),    # TIMBERGRADE
        part.get("designation",  ""),     # DESIGNATION
        part.get("cut_type",     "0"),    # derived from PROCESSKEY/PROCESSPARAMETERS
    ])


def make_bucket0_row(part):
    """
    Row for the unclassified bucket (FileARR).
    Same as numbered row but appends the original PACKAGE value at the end.
    """
    return ",".join([
        part.get("part_num",     "0"),
        part.get("single_member",""),
        part.get("material_num", "100"),
        part.get("length",       "0"),
        part.get("height",       "0"),
        part.get("width",        "0"),
        part.get("module_num",   "0"),
        part.get("timber_grade", "0"),
        part.get("designation",  ""),
        part.get("cut_type",     "0"),
        part.get("package",      ""),     # Dfield[4] — original PACKAGE value
    ])


# ---------------------------------------------------------------------------
# PROCESSPARAMETERS parser
# ---------------------------------------------------------------------------

def parse_process_params(tokens):
    """
    Scan tokens for 'P<index>:<value>' pairs.
    Returns (angle1, angle2) as ints, or (None, None) if not found.
    """
    angle1 = angle2 = None
    for tok in tokens:
        if not tok or tok[0].upper() != "P":
            continue
        body = tok[1:]
        if ":" not in body:
            continue
        idx_str, val_str = body.split(":", 1)
        try:
            idx = int(float(idx_str))
            val = int(float(val_str))
        except ValueError:
            continue
        if idx == 6:
            angle1 = val
        elif idx == 7:
            angle2 = val
    return angle1, angle2


def update_cut_type(part, angle1, angle2):
    """Apply the priority-ladder cut-type logic to part dict in-place."""
    if angle1 is None or angle2 is None:
        return
    type_num = part.get("type_num", 0)
    if angle1 == 9000 and angle2 == 9000 and type_num < 1:
        part["cut_type"] = "1"
        part["type_num"] = 1
    elif angle1 == 9000 and angle2 != 9000 and type_num < 2:
        part["cut_type"] = "2"
        part["type_num"] = 2
    elif angle1 != 9000 and angle2 == 9000 and type_num < 2:
        part["cut_type"] = "2"
        part["type_num"] = 2
    elif angle1 != 9000 and angle2 != 9000 and type_num < 3:
        part["cut_type"] = "3"
        part["type_num"] = 3


# ---------------------------------------------------------------------------
# Bucket classifier
# ---------------------------------------------------------------------------

def classify_bucket(package_value, module_seen):
    """
    Return bucket number for a part.
    Package "1" with a MODULENUMBER present -> bucket 22 (matches VBScript).
    Unknown packages -> 0 (FileARR).
    """
    if package_value == "1" and module_seen:
        return 22
    return PACKAGE_TO_BUCKET.get(package_value, 0)


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def parse_btl(input_path, mat_lookup, mat_overrides=None, encoding=None):
    """
    Parse the BTL file.

    mat_overrides: {overridden_material_num: main_material_num} dict from
                   load_material_overrides(). Applied after the material name
                   is resolved to a number, so FileARR always contains the
                   final (overridden) material number.

    Returns:
      numbered_buckets : {bucket_int: [row_str, ...]}
      bucket0_rows     : [row_str, ...]
    """
    if mat_overrides is None:
        mat_overrides = {}

    # Determine encoding to use for reading; if not provided, detect it.
    if encoding is None:
        encoding = detect_text_encoding(input_path)

    numbered_buckets = {b: [] for b in NUMBERED_BUCKETS}
    id_alloc         = CopyIdAllocator()   # IDs for COUNT > 1 copies
    numbered_elems   = {b: [] for b in NUMBERED_BUCKETS}  # parallel: designations only
    bucket0_rows = []
    bucket0_elems = []
    current      = None
    process_cut  = False   # True when PROCESSKEY line triggers angle inspection

    def new_part():
        return {
            "single_member": "",
            "count":         1,
            "material_num":  "100",
            "package":       "",
            "part_num":      "0",
            "designation":   "",
            "length":        "0",
            "height":        "0",
            "width":         "0",
            "module_num":    "0",
            "timber_grade":  "0",
            "cut_type":      "0",
            "type_num":      0,
            "module_seen":   False,
        }

    def flush(part):
        if part is None:
            return
        bucket = classify_bucket(part["package"], part["module_seen"])
        desig  = part.get("designation", "")
        base_id = part.get("single_member", "")
        # COUNT > 1 -> one entry per copy first copy keeps the ID, extra copies get 10001, 10002, ...
        for copy_id in id_alloc.expand(base_id, part.get("count", 1)):
            copy = dict(part, single_member=copy_id)
            if bucket != 0:
                numbered_buckets.setdefault(bucket, []).append(make_numbered_row(copy))
                numbered_elems.setdefault(bucket, []).append(desig)
            else:
                bucket0_rows.append(make_bucket0_row(copy))
                bucket0_elems.append(desig)

    # Try reading with the detected encoding; if strict UTF-8 fails, fall back to cp1252
    try:
        with open(input_path, "r", encoding=encoding, errors="strict") as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        # If the detected encoding was utf-8 or utf-8-sig, fall back to cp1252 with replace
        with open(input_path, "r", encoding="cp1252", errors="replace") as f:
            lines = f.readlines()

    for raw_line in lines:
        line   = raw_line.strip()
        tokens = line.split() if line else []
        if not tokens:
            continue
        key = tokens[0]

        # ----------------------------------------------------------------
        if key == "[PART]":
            flush(current)
            current     = new_part()
            process_cut = False
            continue

        if current is None:
            continue   # lines before the first [PART]

        # ----------------------------------------------------------------
        if key == "SINGLEMEMBERNUMBER:" and len(tokens) > 1:
            current["single_member"] = tokens[1]

        elif key == "COUNT:" and len(tokens) > 1:
            current["count"] = parse_count(tokens[1])

        elif key == "MATERIAL:" and len(tokens) > 1:
            raw_name = strip_quotes(" ".join(tokens[1:]))
            resolved = get_material_number(raw_name, mat_lookup)
            current["material_num"] = apply_material_override(resolved, mat_overrides)

        elif key == "MODULENUMBER:" and len(tokens) > 1:
            raw = strip_quotes(tokens[1])
            # VBScript: SplitBTL2 = Split(Dfield(9), "-") : Dfield(9) = SplitBTL2(1)
            parts_split = raw.split("-")
            current["module_num"]  = parts_split[1] if len(parts_split) >= 2 else raw
            current["module_seen"] = True

        elif key == "PACKAGE:" and len(tokens) > 1:
            current["package"] = strip_quotes(tokens[1])

        elif key == "ANNOTATION:" and len(tokens) > 1:
            val = strip_quotes(tokens[1])
            current["part_num"] = val if val else "0"

        elif key == "DESIGNATION:" and len(tokens) > 1:
            current["designation"] = strip_quotes(tokens[1])

        elif key == "LENGTH:" and len(tokens) > 1:
            current["length"] = scale_dimension(tokens[1])

        elif key == "HEIGHT:" and len(tokens) > 1:
            current["height"] = scale_dimension(tokens[1])

        elif key == "WIDTH:" and len(tokens) > 1:
            current["width"] = scale_dimension(tokens[1])

        elif key == "TIMBERGRADE:" and len(tokens) > 1:
            val = strip_quotes(tokens[1])
            current["timber_grade"] = val if val else "0"

        elif key == "PROCESSKEY:" and len(tokens) > 1:
            process_cut = tokens[1] in CUT_PROCESS_KEYS

        elif key == "PROCESSPARAMETERS:" and process_cut and len(tokens) > 1:
            a1, a2 = parse_process_params(tokens[1:])
            update_cut_type(current, a1, a2)

    # Flush the final part (no trailing [PART] to trigger it inside the loop).
    flush(current)

    return numbered_buckets, numbered_elems, bucket0_rows, bucket0_elems


# ---------------------------------------------------------------------------
# Output writer
# ---------------------------------------------------------------------------

PARSE_STATUS_FILE = "ParseStatusElements.txt"


def output_file_names():
    """All FileARR / ElemFileARR names, in the exact order write_outputs() writes them."""
    names = []
    for bucket in NUMBERED_BUCKETS:
        names += ["FileARR{}.txt".format(bucket), "ElemFileARR{}.txt".format(bucket)]
    names += ["FileARR.txt", "ElemFileARR.txt"]
    return names


class ParseStatus:
    """
    Collects everything that happens during a run and writes ParseStatusElements.txt.
    Same layout as ParseStatusProcess.txt from BTLparser2:

        RESULT=OK                 OK | PATH_ERROR | ARGUMENT_ERROR | NO_DATA | ERROR
        EXITCODE=1
        TIME=2026-10-01 14:05:12
        BTL=C:\\path\\to\\file.btl
        MATERIALS=46              materials loaded from fb_MAT_STOCK.txt
        MATOVERRIDES=1            pairs loaded from MAT_SETTINGS.txt
        TOTAL PARTS=12            all parts in the FileARR files, incl. COUNT copies
        ERRORS=0
        WARNINGS=0

        [FileARR1.txt]
        STATUS=OK                 OK | EMPTY | WRITE_ERROR | NOT_WRITTEN
        PARTS=5                   parts incl. COUNT copies (same as line 1 of the file)

        [ElemFileARR1.txt]
        STATUS=OK
        PARTS=5
        ...
        [MESSAGES]
        ERROR: ...
        WARNING: ...
    """

    def __init__(self, btl_path=""):
        self.btl          = btl_path
        self.materials    = ""
        self.mat_overrides = ""
        self.total_parts  = 0
        self.files        = []      # (name, status, rows)
        self.errors       = []
        self.warnings     = []

    def error(self, msg):
        print(msg, file=sys.stderr)
        self.errors.append(msg)

    def warning(self, msg):
        print(msg, file=sys.stderr)
        self.warnings.append(msg)

    def add_file(self, name, status, rows):
        self.files.append((name, status, rows))

    def write(self, result, exit_code, encoding="utf-8"):
        if encoding in (None, "utf-8-sig"):
            encoding = "utf-8"          # no BOM, so line 1 reads exactly "RESULT=..."
        lines = [
            "RESULT={}".format(result),
            "EXITCODE={}".format(exit_code),
            "TIME={}".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "BTL={}".format(self.btl),
            "MATERIALS={}".format(self.materials),
            "MATOVERRIDES={}".format(self.mat_overrides),
            "TOTAL PARTS={}".format(self.total_parts),
            "ERRORS={}".format(len(self.errors)),
            "WARNINGS={}".format(len(self.warnings)),
        ]
        for name, status, rows in self.files:
            lines += ["", "[{}]".format(name), "STATUS={}".format(status), "PARTS={}".format(rows)]
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


def write_outputs(numbered_buckets, numbered_elems, bucket0_rows, bucket0_elems, output_dir, encoding="utf-8"):
    """Write all output files with Windows CRLF line endings using the given encoding."""
    os.makedirs(output_dir, exist_ok=True)

    def write_file(path, rows, header=True):
        """Write count, optional header, then one row per line."""
        with open(path, "w", encoding=encoding, newline="") as f:
            f.write(str(len(rows)))
            f.write("\r\n")
            if header:
                f.write(FILE_HEADER)
                f.write("\r\n")
            for row in rows:
                f.write(row)
                f.write("\r\n")

    # Numbered buckets — FileARR<N> and ElemFileARR<N>
    for bucket in NUMBERED_BUCKETS:
        rows  = numbered_buckets.get(bucket, [])
        elems = numbered_elems.get(bucket, [])
        try:
            write_file(os.path.join(output_dir, "FileARR{}.txt".format(bucket)), rows)
            write_file(os.path.join(output_dir, "ElemFileARR{}.txt".format(bucket)), elems, header=False)
        except OSError as e:
            # Bubble the exception up to caller for consistent handling
            raise

    # Bucket 0 — FileARR and ElemFileARR (unclassified parts)
    write_file(os.path.join(output_dir, "FileARR.txt"),     bucket0_rows)
    write_file(os.path.join(output_dir, "ElemFileARR.txt"), bucket0_elems, header=False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv):
    load_id_settings()   # optional BTLsettings.txt overrides
    # Exit codes match the CX Supervisor VBScript convention:
    #   0 = failure / empty (ELSEIF exitCode=0 -> "EMPTY")
    #   1 = success / OK    (IF exitCode=1     -> "OK")
    #   2 = bad argument    (ELSEIF exitCode=2 -> "Wrong argument / BTL path")

    if len(argv) != 2:
        status = ParseStatus()
        status.error("ERROR: Wrong number of arguments ({}). Usage: BTLparser.exe <input.btl>"
                     .format(len(argv) - 1))
        status.write("ARGUMENT_ERROR", 2)
        return 2

    input_path = argv[1]
    status = ParseStatus(input_path)

    # --- Input validation ---------------------------------------------------
    if not os.path.exists(input_path):
        status.error("ERROR: BTL file not found: {}".format(input_path))
        status.write("PATH_ERROR", 2)
        return 2

    if not os.path.isfile(input_path):
        status.error("ERROR: Path is not a file: {}".format(input_path))
        status.write("PATH_ERROR", 2)
        return 2

    if os.path.getsize(input_path) == 0:
        status.error("ERROR: BTL file is empty: {}".format(input_path))
        status.write("NO_DATA", 0)
        return 0

    # --- Output directory ---------------------------------------------------
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
    except OSError as e:
        status.error("ERROR: Cannot create output directory '{}': {}".format(OUTPUT_DIR, e))
        status.write("ERROR", 0)
        return 0

    # --- Material stock -----------------------------------------------------
    # Now detect MAT_STOCK encoding and use it when reading so Finnish chars survive.
    mat_lookup = load_material_stock(MAT_STOCK_PATH)
    # load_material_stock already warns if the file is missing; parsing continues
    # with all materials defaulting to 100.
    status.materials = len(mat_lookup)
    if not mat_lookup:
        status.warning("WARNING: No materials loaded from '{}' - all materials reported as 100."
                       .format(MAT_STOCK_PATH))

    # --- Material overrides (e.g. material 7 -> material 6) ------------------
    mat_overrides = load_material_overrides(MAT_SETTINGS_PATH)
    # load_material_overrides already warns if the file is missing; parsing
    # continues with no overrides applied.
    status.mat_overrides = len(mat_overrides)

    # --- Detect input encoding so we can preserve Finnish characters in outputs
    try:
        btl_encoding = detect_text_encoding(input_path)
    except Exception:
        btl_encoding = "utf-8"  # safe default

    # --- Parse --------------------------------------------------------------
    try:
        numbered_buckets, numbered_elems, bucket0_rows, bucket0_elems = parse_btl(
            input_path, mat_lookup, mat_overrides=mat_overrides, encoding=btl_encoding)
    except UnicodeDecodeError as e:
        status.error("ERROR: Could not read BTL file (encoding problem): {}".format(e))
        status.write("ERROR", 0, btl_encoding)
        return 0
    except OSError as e:
        status.error("ERROR: Failed to read BTL file: {}".format(e))
        status.write("ERROR", 0, btl_encoding)
        return 0
    except Exception as e:
        status.error("ERROR: Unexpected error while parsing BTL: {}".format(e))
        status.write("ERROR", 0, btl_encoding)
        return 0

    total_numbered = sum(len(v) for v in numbered_buckets.values())
    total_b0       = len(bucket0_rows)
    status.total_parts = total_numbered + total_b0

    if total_numbered + total_b0 == 0:
        status.warning("WARNING: No [PART] entries found in '{}'.".format(input_path))
        status.write("NO_DATA", 0, btl_encoding)
        return 0  # Treated as empty/failure by CX Supervisor

    # Parts without a supported package end up in FileARR / ElemFileARR.
    # CX Supervisor cannot process these, so report them as a warning (not an error).
    if bucket0_rows:
        packages = sorted({row.rsplit(",", 1)[-1] or "(empty)" for row in bucket0_rows})
        status.warning(
            "WARNING: {} part(s) without a supported PACKAGE written to FileARR.txt / "
            "ElemFileARR.txt (PACKAGE: {}).".format(len(bucket0_rows), ", ".join(packages))
        )

    # Row counts per output file, in write order
    row_counts = {}
    for bucket in NUMBERED_BUCKETS:
        row_counts["FileARR{}.txt".format(bucket)]     = len(numbered_buckets.get(bucket, []))
        row_counts["ElemFileARR{}.txt".format(bucket)] = len(numbered_elems.get(bucket, []))
    row_counts["FileARR.txt"]     = len(bucket0_rows)
    row_counts["ElemFileARR.txt"] = len(bucket0_elems)

    # --- Write outputs ------------------------------------------------------
    try:
        write_outputs(numbered_buckets, numbered_elems, bucket0_rows, bucket0_elems, OUTPUT_DIR, encoding=btl_encoding)
    except OSError as e:
        status.error("ERROR: Failed to write output files: {}".format(e))
        # Files before the failing one were written, the failing one and later ones were not
        failed = os.path.basename(getattr(e, "filename", "") or "")
        reached = False
        for name in output_file_names():
            if name == failed:
                status.add_file(name, "WRITE_ERROR", row_counts[name])
                reached = True
            elif reached or not failed:
                status.add_file(name, "NOT_WRITTEN", row_counts[name])
            else:
                status.add_file(name, "OK" if row_counts[name] else "EMPTY", row_counts[name])
        status.write("ERROR", 0, btl_encoding)
        return 0

    for name in output_file_names():
        status.add_file(name, "OK" if row_counts[name] else "EMPTY", row_counts[name])

    # --- Summary ------------------------------------------------------------
    print("Parsed {} parts total ({} classified, {} unclassified).".format(
        total_numbered + total_b0, total_numbered, total_b0))
    print("  FileARR  (unclassified): {}".format(total_b0))
    for bucket in NUMBERED_BUCKETS:
        count = len(numbered_buckets.get(bucket, []))
        if count:
            print("  FileARR{}: {}".format(bucket, count))
    print("Output written to: {}".format(OUTPUT_DIR))

    status.write("OK", 1, btl_encoding)
    return 1  # Success


if __name__ == "__main__":
    sys.exit(main(sys.argv))