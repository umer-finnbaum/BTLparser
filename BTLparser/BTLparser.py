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
  ModuleNum, TimberGrade, Designation, CutType, ProcessType

Bucket 0 / FileARR (unclassified package):
  PartNum, SingleMemberNum, MaterialNum, Length, Height, Width,
  ModuleNum, TimberGrade, Designation, CutType, OriginalPackage, ProcessType

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
Derived from each PROCESSKEY and the PROCESSPARAMETERS line after it.
PROCESSKEY = <group>-<process>-<side>.

Cut Type (one value per part): only normal Cuts (1/2-010-X), from P06 / P07
(9000 = 90.00 degrees). The part gets the highest Cut Type of its Cuts:
  0 = no normal Cut
  1 = both angles 90
  2 = one angle 90, the other 40-140 degrees
  3 = neither angle 90, both 40-140 degrees
  4 = one angle 90, the other below 40 or above 140 degrees
  5 = neither angle 90, one or both below 40 or above 140 degrees

Process Type (one value per part, last column of FileARR): the HIGHEST
priority of all processes in the part, see PROCESS_PRIORITIES:
  0 = no listed process
  1 = Marking / Labeling (3/4-060-X)        lowest priority
  2 = Cut (1/2-010-X)
  3 = Lap Joint (3/4-030-X)
  4 = Longitudinal Cut (0/3/4-010-X)
  5 - 47 = Double Cut ... Variant, in the order of the BTL list of processings

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
import codecs
import traceback
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

# ---------------------------------------------------------------------------
# Cut Type and Process Type (one value each per part)
# ---------------------------------------------------------------------------
# PROCESSKEY = <group>-<process>-<side>, e.g. 1-010-2 = group 1, process 010.
#
# Cut Type: only normal Cuts (1/2-010-X), from P06 and P07 (1/100 degree).
# The part gets the highest Cut Type of its normal Cuts (0 = none):
#   1 = both angles 90
#   2 = one angle 90, the other between 40 and 140 degrees
#   3 = neither angle 90, both between 40 and 140 degrees
#   4 = one angle 90, the other below 40 or above 140 degrees
#   5 = neither angle 90, one or both below 40 or above 140 degrees
ANGLE_90       = 9000    # 90.00 degrees
ANGLE_STEEP_LO = 4000    # below 40.00 degrees  -> steep (Type 4 / 5)
ANGLE_STEEP_HI = 14000   # above 140.00 degrees -> steep (Type 4 / 5)

# Process Type: priority of each process, lowest first. The part gets the
# HIGHEST priority number of all its processes (0 = no listed process).
# (groups, process number, priority, name); after Lap Joint the order is the
# BTL "List of processings".
PROCESS_PRIORITIES = [
    ("3/4",       "060",  1, "Marking / Labeling"),
    ("1/2",       "010",  2, "Cut"),
    ("3/4",       "030",  3, "Lap Joint"),
    ("0/3/4",     "010",  4, "Longitudinal Cut"),
    ("1/2",       "011",  5, "Double Cut"),
    ("0",         "012",  6, "Ridge or Valley Cut"),
    ("0/3/4",     "013",  7, "Saw Cut"),
    ("3/4",       "016",  8, "Slot"),
    ("3/4",       "017",  9, "Front Slot"),
    ("3/4",       "020", 10, "Birds Mouth"),
    ("3/4",       "025", 11, "Hip or Valley Rafter Notch"),
    ("1/2",       "030", 12, "Ridge Lap"),
    ("3/4",       "032", 13, "Notch / Rabbet"),
    ("3/4",       "033", 14, "Block House Half Lap, Stair Riser Dado"),
    ("3/4",       "034", 15, "Seathing Cut"),
    ("1/2",       "035", 16, "French Ridge Lap"),
    ("3/4",       "036", 17, "Chamfer"),
    ("4",         "037", 18, "Block House Half Lap"),
    ("3/4",       "038", 19, "Block House Front"),
    ("4",         "039", 20, "Pocket"),
    ("3/4",       "040", 21, "Drilling"),
    ("1/2",       "050", 22, "Tenon"),
    ("3/4",       "050", 23, "Mortise"),
    ("3/4",       "051", 24, "Mortise Front"),
    ("3/4",       "052", 25, "House"),
    ("3/4",       "053", 26, "House Mortise"),
    ("1/2",       "055", 27, "Dovetail Tenon"),
    ("3/4",       "055", 28, "Dovetail Mortise"),
    ("3/4",       "056", 29, "Dovetail Mortise Front"),
    ("4",         "061", 30, "Text"),
    ("1/2",       "070", 31, "Simple Scarf"),
    ("1/2",       "071", 32, "Scarf Joint"),
    ("1/2",       "080", 33, "Step Joint"),
    ("3/4",       "080", 34, "Step Joint Notch"),
    ("3/4",       "090", 35, "Planing"),
    ("3/4",       "100", 36, "Profile Front"),
    ("3/4",       "101", 37, "Profile Head concave"),
    ("3/4",       "102", 38, "Profile Head convex"),
    ("3/4",       "103", 39, "Profile Head cambered"),
    ("4",         "104", 40, "Round Arch"),
    ("3/4",       "106", 41, "Profile Head"),
    ("3/4",       "107", 42, "Sphere"),
    ("4",         "120", 43, "Triangle Cut"),
    ("1/2/3/4",   "136", 44, "Tyrolean Dovetail"),
    ("1/2/3/4",   "138", 45, "Dovetail"),
    ("0/3/4",     "250", 46, "Free Contour"),
    ("0/1/2/3/4", "900", 47, "Variant"),
]

# (group, process) -> priority, built once from the table above
PROCESS_PRIORITY_LOOKUP = {
    (group, process): priority
    for groups, process, priority, _name in PROCESS_PRIORITIES
    for group in groups.split("/")
}


def _split_key(process_key):
    """'1-010-2' -> ('1', '010'); malformed -> (None, None)."""
    parts = process_key.split("-")
    if len(parts) < 2:
        return None, None
    return parts[0], parts[1]


def cut_type_from_angles(angle1, angle2):
    """Cut Type 1-5 for a normal Cut from P06 / P07 (None = angle missing -> 0)."""
    if angle1 is None or angle2 is None:
        return 0

    def steep(a):
        return a < ANGLE_STEEP_LO or a > ANGLE_STEEP_HI

    if angle1 == ANGLE_90 and angle2 == ANGLE_90:
        return 1
    if angle1 == ANGLE_90 or angle2 == ANGLE_90:
        other = angle2 if angle1 == ANGLE_90 else angle1
        return 4 if steep(other) else 2
    return 5 if steep(angle1) or steep(angle2) else 3


def process_cut_type(process_key, angle1=None, angle2=None):
    """Cut Type for one process: 1-5 for a normal Cut, 0 for anything else."""
    group, process = _split_key(process_key)
    if group in ("1", "2") and process == "010":
        return cut_type_from_angles(angle1, angle2)
    return 0


def process_priority(process_key):
    """Process Type (priority) for one process, 0 = not in PROCESS_PRIORITIES."""
    group, process = _split_key(process_key)
    return PROCESS_PRIORITY_LOOKUP.get((group, process), 0)


# Header written as line 2 of every output file (line 1 is the part count).
FILE_HEADER = "PartNum,ID,MaterialType,Length,Heigth,Width,ModuleNum,Timbergrade,ElemName,Type,ProcessType"
# FileARR.txt (parts without a supported PACKAGE) has the package before ProcessType
FILE_HEADER_BUCKET0 = "PartNum,ID,MaterialType,Length,Heigth,Width,ModuleNum,Timbergrade,ElemName,Type,OriginalPackage,ProcessType"


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
COUNT_ENABLED = True    # False -> COUNT is ignored (treated as 1), no duplicate IDs
MESSAGE_WIDTH = 80      # max characters per error/warning line in the status file
STATUS_ENCODING = "cp1252"  # status file encoding: cp1252 (ANSI, same as Supervisor's
                            # mapping.txt), utf-8 or utf-8-sig (UTF-8 with BOM)
# All six can be overridden by BTLsettings.txt (see load_id_settings).

# ---------------------------------------------------------------------------
# Exit codes (also written as CODE= / RESULT= on lines 1-2 of the status file)
# Same table in BTLparser and BTLparser2.
# ---------------------------------------------------------------------------
RESULT_CODES = {
    "OK":                 0,   # Done - warnings may still be listed
    "CRASH":              1,   # unexpected program error (also Python's own crash code)
    "ARGUMENT_ERROR":    10,   # wrong number of arguments / manual mode without BTL path
    "BTL_NOT_FOUND":     11,   # BTL file missing or path is not a file
    "BTL_EMPTY":         12,   # BTL file is 0 bytes
    "BTL_READ_ERROR":    13,   # BTL file could not be read
    "NO_DATA":           14,   # BTL read fine, but no parts / processes found
    "OUTPUT_DIR_ERROR":  15,   # output folder cannot be created
    "WRITE_ERROR":       16,   # an output file could not be written
    "MAPPING_NOT_FOUND": 17,   # mapping.txt missing (BTLparser2)
    "MAPPING_ERROR":     18,   # mapping.txt unreadable / invalid (BTLparser2)
    "ID_LIMIT_ERROR":    19,   # COUNT copy IDs: ID above MAX_BASE_ID, copy ID above
                              # MAX_ID_VALUE, or inconsistent ID settings
}

BTL_SETTINGS_PATH = r"C:\FBtemp\356\Configuration\BTLsettings.txt"


def load_id_settings(path=None):
    """
    Override COPY_ID_START, MAX_ID_VALUE, MAX_BASE_ID, COUNT_ENABLED and
    MESSAGE_WIDTH from BTLsettings.txt.

    File format (one setting per line, spaces optional):
        COPY_ID_START = 20000
        MAX_ID_VALUE = 32000
        MAX_BASE_ID = 9999
        COUNT_ENABLED = 1      (1/0, ON/OFF, TRUE/FALSE, YES/NO)
        MESSAGE_WIDTH = 80

    Missing file      -> built-in defaults are used (no warning).
    Invalid line/value -> warning printed, that setting keeps its default.
    """
    global COPY_ID_START, MAX_ID_VALUE, MAX_BASE_ID, COUNT_ENABLED, MESSAGE_WIDTH, STATUS_ENCODING
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
                if key == "STATUS_ENCODING":
                    enc = {"ANSI": "cp1252", "WINDOWS-1252": "cp1252"}.get(val.upper(), val.lower())
                    try:
                        codecs.lookup(enc)
                        values[key] = enc
                    except LookupError:
                        print("WARNING: BTLsettings line {} ignored (unknown encoding '{}')."
                              .format(line_num, val), file=sys.stderr)
                    continue
                if key not in ("COPY_ID_START", "MAX_ID_VALUE", "MAX_BASE_ID", "MESSAGE_WIDTH"):
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
    MESSAGE_WIDTH = values.get("MESSAGE_WIDTH", MESSAGE_WIDTH)
    STATUS_ENCODING = values.get("STATUS_ENCODING", STATUS_ENCODING)
    if MESSAGE_WIDTH < 20:
        print("WARNING: MESSAGE_WIDTH ({}) too small, using 20.".format(MESSAGE_WIDTH),
              file=sys.stderr)
        MESSAGE_WIDTH = 20

    print("ID settings: COPY_ID_START={}, MAX_ID_VALUE={}, MAX_BASE_ID={}, COUNT_ENABLED={}, "
          "MESSAGE_WIDTH={}".format(COPY_ID_START, MAX_ID_VALUE, MAX_BASE_ID,
                                    "ON" if COUNT_ENABLED else "OFF", MESSAGE_WIDTH))

def validate_id_settings():
    """
    Check that the ID settings fit together. Returns a list of ERROR messages
    (empty when OK). Only relevant while COUNT handling is on.
      MAX_BASE_ID <= MAX_ID_VALUE
    COPY_ID_START may lie inside the original ID range: an original ID at or
    above COPY_ID_START is only a warning when no copies are made, and an
    error when copies are made (see CopyIdAllocator). Too few copy IDs between
    COPY_ID_START and MAX_ID_VALUE is also checked per run.
    """
    if not COUNT_ENABLED:
        return []
    errors = []
    if MAX_BASE_ID > MAX_ID_VALUE:
        errors.append("ERROR: ID settings: MAX_BASE_ID ({}) is above MAX_ID_VALUE ({}), "
                      "original IDs could exceed the ID limit.".format(MAX_BASE_ID, MAX_ID_VALUE))
    return errors


def wrap_message(msg):
    """
    Word-wrap one error/warning to MESSAGE_WIDTH characters per line.
    Continuation lines start with two spaces, so every new message still
    begins with "ERROR:" / "WARNING:" at column 1.
    Words longer than a line (e.g. file paths) are split after a \\ or /
    where possible, otherwise at the line limit.
    """
    indent = "  "
    lines, cur = [], ""

    def room():
        # first line has the full width, continuation lines lose the indent
        return MESSAGE_WIDTH - (len(indent) if lines else 0)

    for word in msg.split():
        candidate = word if not cur else cur + " " + word
        if len(candidate) <= room():
            cur = candidate
            continue
        if cur:
            lines.append(cur)
            cur = ""
        while len(word) > room():
            cut = room()
            sep = max(word.rfind("\\", 0, cut), word.rfind("/", 0, cut))
            if sep > 0:
                cut = sep + 1          # keep the separator at the end of the line
            lines.append(word[:cut])
            word = word[cut:]
        cur = word
    if cur:
        lines.append(cur)
    if not lines:
        return [msg]
    return [lines[0]] + [indent + l for l in lines[1:]]



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
        self.start   = self.next_id
        self.limit   = MAX_ID_VALUE  if limit is None else limit
        self.base_ids_too_high = []     # original IDs above MAX_BASE_ID
        self.base_ids_overlap  = []     # original IDs >= COPY_ID_START (not above MAX_BASE_ID)

    def expand(self, base_id, count):
        """Return the list of IDs (as strings) for one part."""
        if not COUNT_ENABLED:
            count = 1   # COUNT handling switched off -> one entry, original ID
        if COUNT_ENABLED:
            try:
                n = int(base_id)
                if n > MAX_BASE_ID:
                    if base_id not in self.base_ids_too_high:
                        self.base_ids_too_high.append(base_id)
                elif n >= self.start and base_id not in self.base_ids_overlap:
                    self.base_ids_overlap.append(base_id)
            except (ValueError, TypeError):
                pass

        ids = [base_id]
        for _ in range(count - 1):
            ids.append(str(self.next_id))
            self.next_id += 1
        return ids

    @staticmethod
    def _id_list(ids):
        """First 10 IDs, plus the total when there are more."""
        shown = ", ".join(ids[:10])
        if len(ids) > 10:
            shown += ", ... ({} in total)".format(len(ids))
        return shown

    def copies_made(self):
        return self.next_id - self.start

    def errors(self, source=""):
        """
        ERROR messages for this allocator (empty list when all IDs are valid).
          - original ID above MAX_BASE_ID
          - original ID at or above COPY_ID_START while copy IDs were generated
          - more copies than fit between COPY_ID_START and MAX_ID_VALUE
        """
        where = " in {}".format(source) if source else ""
        errors = []
        if self.base_ids_too_high:
            errors.append("ERROR: Original ID(s) above MAX_BASE_ID ({}){}: {}."
                          .format(MAX_BASE_ID, where, self._id_list(self.base_ids_too_high)))
        used      = self.copies_made()                 # copy IDs handed out
        available = max(self.limit - self.start + 1, 0)
        if self.base_ids_overlap and used > 0:
            errors.append("ERROR: Original ID(s) at or above COPY_ID_START ({}){} overlap the "
                          "{} copy IDs generated: {}."
                          .format(self.start, where, used, self._id_list(self.base_ids_overlap)))
        if used > available:
            errors.append("ERROR: {} copy IDs needed{}, but only {} fit between "
                          "COPY_ID_START ({}) and MAX_ID_VALUE ({}). Last copy ID would be {}."
                          .format(used, where, available, self.start, self.limit,
                                  self.next_id - 1))
        return errors

    def warnings(self, source=""):
        """
        WARNING messages: original ID(s) at or above COPY_ID_START while no copy
        IDs were generated - allowed, but they lie inside the copy ID range.
        """
        if not self.base_ids_overlap or self.copies_made() > 0:
            return []
        where = " in {}".format(source) if source else ""
        return ["WARNING: Original ID(s) at or above COPY_ID_START ({}){} overlap the copy "
                "ID range: {}. No copy IDs were generated, so no IDs collide."
                .format(self.start, where, self._id_list(self.base_ids_overlap))]


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
        part.get("cut_type",     "0"),    # normal Cuts only, from P06/P07
        part.get("process_type", "0"),    # highest process priority
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
        part.get("process_type", "0"),    # highest process priority
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


def update_cut_type(part, cut_type):
    """Keep the highest Cut Type seen in the part (it never goes down)."""
    if cut_type > part.get("type_num", 0):
        part["type_num"] = cut_type
        part["cut_type"] = str(cut_type)


def update_process_type(part, priority):
    """Keep the highest process priority seen in the part (it never goes down)."""
    if priority > part.get("process_num", 0):
        part["process_num"]  = priority
        part["process_type"] = str(priority)


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

def parse_btl(input_path, mat_lookup, mat_overrides=None, encoding=None, id_alloc=None):
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
    if id_alloc is None:
        id_alloc = CopyIdAllocator()       # IDs for COUNT > 1 copies
    numbered_elems   = {b: [] for b in NUMBERED_BUCKETS}  # parallel: designations only
    bucket0_rows = []
    bucket0_elems = []
    current      = None
    pending_key  = ""      # PROCESSKEY waiting for its PROCESSPARAMETERS line

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
            "process_type":  "0",
            "process_num":   0,
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
            pending_key = ""
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
            pending_key = tokens[1]

        elif key == "PROCESSPARAMETERS:" and pending_key:
            a1, a2 = parse_process_params(tokens[1:])
            update_cut_type(current, process_cut_type(pending_key, a1, a2))
            update_process_type(current, process_priority(pending_key))
            pending_key = ""

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

    Standard header (same structure in all status files):
        CODE=0                    see RESULT_CODES (0 = done, 1 = crash, 10+ = errors)
        RESULT=OK                 name from RESULT_CODES
        TIME=2026-10-01 14:05:12
        BTL=C:\\path\\to\\file.btl
        TOTAL PARTS=12            all parts in the FileARR files, incl. COUNT copies
        ERRORS=1
        WARNINGS=1
        ERROR: ...                errors first, then warnings; each wrapped to
          continued text          MESSAGE_WIDTH chars, continuation lines start with 2 spaces
        WARNING: ...

    Program-specific part:
        MATERIALS=46              materials loaded from fb_MAT_STOCK.txt
        MATOVERRIDES=1            pairs loaded from MAT_SETTINGS.txt

        [FileARR.txt]             no-package files first, then FileARR1 ... FileARR25
        STATUS=WARNING            OK | EMPTY | WARNING | WRITE_ERROR | NOT_WRITTEN
                                  (WARNING = FileARR/ElemFileARR contain parts without a package)
        PARTS=5                   parts incl. COUNT copies (same as line 1 of the file)

        [ElemFileARR.txt]
        STATUS=WARNING
        PARTS=5
        ...
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
        # Parts in the no-package files cannot be processed by CX Supervisor
        if status == "OK" and name in ("FileARR.txt", "ElemFileARR.txt"):
            status = "WARNING"
        self.files.append((name, status, rows))

    def finish(self, result):
        """Write the status file and return the exit code for `result`."""
        self.write(result)
        return RESULT_CODES[result]

    def write(self, result, exit_code=None, encoding=None):
        # CODE comes from RESULT_CODES; encoding is always STATUS_ENCODING
        # (exit_code / encoding arguments are kept only for compatibility)
        encoding = STATUS_ENCODING
        # --- Standard header (same structure in all status files) ---------
        lines = [
            "CODE={}".format(RESULT_CODES[result]),
            "RESULT={}".format(result),
            "TIME={}".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "BTL={}".format(self.btl),
            "TOTAL PARTS={}".format(self.total_parts),
        ]
        # Line 6 + messages from line 7: warnings when the run is done (CODE 0),
        # otherwise errors only (warnings are not relevant when the run failed)
        if RESULT_CODES[result] == 0:
            lines.append("WARNINGS={}".format(len(self.warnings)))
            messages = self.warnings
        else:
            messages = self.errors or [
                "ERROR: Run ended with RESULT={} (CODE={}).".format(result, RESULT_CODES[result])]
            lines.append("ERRORS={}".format(len(messages)))
        for msg in messages:            # each message word-wrapped to MESSAGE_WIDTH
            lines += wrap_message(msg)
        # --- Program-specific part -----------------------------------------
        lines += [
            "MATERIALS={}".format(self.materials),
            "MATOVERRIDES={}".format(self.mat_overrides),
        ]
        # FileARR / ElemFileARR (no package) first, then the numbered files
        plain    = [f for f in self.files if f[0] in ("FileARR.txt", "ElemFileARR.txt")]
        numbered = [f for f in self.files if f[0] not in ("FileARR.txt", "ElemFileARR.txt")]
        for name, status, rows in plain + numbered:
            lines += ["", "[{}]".format(name), "STATUS={}".format(status), "PARTS={}".format(rows)]

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

    def write_file(path, rows, header=FILE_HEADER):
        """Write count, optional header line, then one row per line."""
        with open(path, "w", encoding=encoding, newline="") as f:
            f.write(str(len(rows)))
            f.write("\r\n")
            if header:
                f.write(header)
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
            write_file(os.path.join(output_dir, "ElemFileARR{}.txt".format(bucket)), elems, header=None)
        except OSError as e:
            # Bubble the exception up to caller for consistent handling
            raise

    # Bucket 0 — FileARR and ElemFileARR (unclassified parts)
    write_file(os.path.join(output_dir, "FileARR.txt"),     bucket0_rows, header=FILE_HEADER_BUCKET0)
    write_file(os.path.join(output_dir, "ElemFileARR.txt"), bucket0_elems, header=None)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv):
    load_id_settings()   # optional BTLsettings.txt overrides
    # Exit codes: see RESULT_CODES (0 = done, 1 = crash, 10+ = errors)

    if len(argv) != 2:
        status = ParseStatus()
        status.error("ERROR: Wrong number of arguments ({}). Usage: BTLparser.exe <input.btl>"
                     .format(len(argv) - 1))
        return status.finish("ARGUMENT_ERROR")

    input_path = argv[1]
    status = ParseStatus(input_path)

    # --- Input validation ---------------------------------------------------
    if not os.path.exists(input_path):
        status.error("ERROR: BTL file not found: {}".format(input_path))
        return status.finish("BTL_NOT_FOUND")

    if not os.path.isfile(input_path):
        status.error("ERROR: Path is not a file: {}".format(input_path))
        return status.finish("BTL_NOT_FOUND")

    if os.path.getsize(input_path) == 0:
        status.error("ERROR: BTL file is empty: {}".format(input_path))
        return status.finish("BTL_EMPTY")

    # --- Output directory ---------------------------------------------------
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
    except OSError as e:
        status.error("ERROR: Cannot create output directory '{}': {}".format(OUTPUT_DIR, e))
        return status.finish("OUTPUT_DIR_ERROR")

    # --- ID settings (only when COUNT handling is on) -----------------------
    for msg in validate_id_settings():
        status.error(msg)
    if status.errors:
        return status.finish("ID_LIMIT_ERROR")

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
    id_alloc = CopyIdAllocator()
    try:
        numbered_buckets, numbered_elems, bucket0_rows, bucket0_elems = parse_btl(
            input_path, mat_lookup, mat_overrides=mat_overrides, encoding=btl_encoding,
            id_alloc=id_alloc)
    except UnicodeDecodeError as e:
        status.error("ERROR: Could not read BTL file (encoding problem): {}".format(e))
        return status.finish("BTL_READ_ERROR")
    except OSError as e:
        status.error("ERROR: Failed to read BTL file: {}".format(e))
        return status.finish("BTL_READ_ERROR")
    except Exception as e:
        status.error("ERROR: Unexpected error while parsing BTL: {}".format(e))
        return status.finish("BTL_READ_ERROR")

    total_numbered = sum(len(v) for v in numbered_buckets.values())
    total_b0       = len(bucket0_rows)
    status.total_parts = total_numbered + total_b0

    if total_numbered + total_b0 == 0:
        status.error("ERROR: No [PART] entries found in '{}'.".format(input_path))
        return status.finish("NO_DATA")

    # --- ID limits: original IDs and copy IDs must stay in range ------------
    for msg in id_alloc.errors():
        status.error(msg)
    if status.errors:
        return status.finish("ID_LIMIT_ERROR")     # no FileARR files written
    for msg in id_alloc.warnings():
        status.warning(msg)

    # Parts without a supported package end up in FileARR / ElemFileARR.
    # CX Supervisor cannot process these, so report them as a warning (not an error).
    if bucket0_rows:
        packages = sorted({row.rsplit(",", 2)[-2] or "(empty)" for row in bucket0_rows})
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
        return status.finish("WRITE_ERROR")

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

    return status.finish("OK")   # warnings, if any, are listed in the status file


def run(argv):
    """Run main(); any unexpected exception becomes CODE=1 / RESULT=CRASH."""
    try:
        return main(argv)
    except Exception as e:
        traceback.print_exc()
        crash = ParseStatus()
        crash.btl = argv[1] if len(argv) > 1 else ""
        crash.error("ERROR: Unexpected program error: {}: {}".format(type(e).__name__, e))
        return crash.finish("CRASH")


if __name__ == "__main__":
    sys.exit(run(sys.argv))