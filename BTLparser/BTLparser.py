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

Usage
-----
  python btl_parser.py <input.btl>
"""

import os
import sys

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAT_STOCK_PATH = r"C:\FBtemp\356\Configuration\fb_MAT_STOCK.txt"
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

def parse_btl(input_path, mat_lookup, encoding=None):
    """
    Parse the BTL file.

    Returns:
      numbered_buckets : {bucket_int: [row_str, ...]}
      bucket0_rows     : [row_str, ...]
    """
    # Determine encoding to use for reading; if not provided, detect it.
    if encoding is None:
        encoding = detect_text_encoding(input_path)

    numbered_buckets = {b: [] for b in NUMBERED_BUCKETS}
    numbered_elems   = {b: [] for b in NUMBERED_BUCKETS}  # parallel: designations only
    bucket0_rows = []
    bucket0_elems = []
    current      = None
    process_cut  = False   # True when PROCESSKEY line triggers angle inspection

    def new_part():
        return {
            "single_member": "",
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
        if bucket != 0:
            numbered_buckets.setdefault(bucket, []).append(make_numbered_row(part))
            numbered_elems.setdefault(bucket, []).append(desig)
        else:
            bucket0_rows.append(make_bucket0_row(part))
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

        elif key == "MATERIAL:" and len(tokens) > 1:
            raw_name = strip_quotes(" ".join(tokens[1:]))
            current["material_num"] = get_material_number(raw_name, mat_lookup)

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

def write_error_file(output_dir, messages, encoding="utf-8-sig"):
    """
    Write (overwrite) an error.txt file to the output directory containing messages.
    If messages is empty, write a single line "0". Otherwise the first line is
    the number of errors and each subsequent line is one error message.
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
        # If even writing the error file fails, there's nothing we can do here.
        pass


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
    # Exit codes match the CX Supervisor VBScript convention:
    #   0 = failure / empty (ELSEIF exitCode=0 -> "EMPTY")
    #   1 = success / OK    (IF exitCode=1     -> "OK")
    #   2 = bad argument    (ELSEIF exitCode=2 -> "Wrong argument / BTL path")

    if len(argv) != 2:
        print("Usage: BTLparser.exe <input.btl>", file=sys.stderr)
        return 2

    input_path = argv[1]

    # --- Input validation ---------------------------------------------------
    if not os.path.exists(input_path):
        msg = "ERROR: BTL file not found: {}".format(input_path)
        print(msg, file=sys.stderr)
        # write an error file too (use default encoding)
        write_error_file(OUTPUT_DIR, [msg])
        return 2

    if not os.path.isfile(input_path):
        msg = "ERROR: Path is not a file: {}".format(input_path)
        print(msg, file=sys.stderr)
        write_error_file(OUTPUT_DIR, [msg])
        return 2

    if os.path.getsize(input_path) == 0:
        msg = "ERROR: BTL file is empty: {}".format(input_path)
        print(msg, file=sys.stderr)
        write_error_file(OUTPUT_DIR, [msg])
        return 0

    # --- Output directory ---------------------------------------------------
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
    except OSError as e:
        msg = "ERROR: Cannot create output directory '{}': {}".format(OUTPUT_DIR, e)
        print(msg, file=sys.stderr)
        write_error_file(OUTPUT_DIR, [msg])
        return 0

    # --- Material stock -----------------------------------------------------
    # Now detect MAT_STOCK encoding and use it when reading so Finnish chars survive.
    mat_lookup = load_material_stock(MAT_STOCK_PATH)
    # load_material_stock already warns if the file is missing; parsing continues
    # with all materials defaulting to 100.

    # --- Detect input encoding so we can preserve Finnish characters in outputs
    try:
        btl_encoding = detect_text_encoding(input_path)
    except Exception:
        btl_encoding = "utf-8"  # safe default

    # --- Parse --------------------------------------------------------------
    try:
        numbered_buckets, numbered_elems, bucket0_rows, bucket0_elems = parse_btl(input_path, mat_lookup, encoding=btl_encoding)
    except UnicodeDecodeError as e:
        msg = "ERROR: Could not read BTL file (encoding problem): {}".format(e)
        print(msg, file=sys.stderr)
        write_error_file(OUTPUT_DIR, [msg])
        return 0
    except OSError as e:
        msg = "ERROR: Failed to read BTL file: {}".format(e)
        print(msg, file=sys.stderr)
        write_error_file(OUTPUT_DIR, [msg])
        return 0
    except Exception as e:
        msg = "ERROR: Unexpected error while parsing BTL: {}".format(e)
        print(msg, file=sys.stderr)
        write_error_file(OUTPUT_DIR, [msg])
        return 0

    total_numbered = sum(len(v) for v in numbered_buckets.values())
    total_b0       = len(bucket0_rows)

    if total_numbered + total_b0 == 0:
        msg = "WARNING: No [PART] entries found in '{}'.".format(input_path)
        print(msg, file=sys.stderr)
        # This is considered a warning/empty result — preserve that in error.txt
        write_error_file(OUTPUT_DIR, [msg])
        return 0  # Treated as empty/failure by CX Supervisor

    # --- Write outputs ------------------------------------------------------
    try:
        write_outputs(numbered_buckets, numbered_elems, bucket0_rows, bucket0_elems, OUTPUT_DIR, encoding=btl_encoding)
    except OSError as e:
        msg = "ERROR: Failed to write output files: {}".format(e)
        print(msg, file=sys.stderr)
        write_error_file(OUTPUT_DIR, [msg])
        return 0

    # --- Clear previous error file (no errors) -------------------------------
    # Overwrite error.txt with "0" to indicate no errors — avoids stale old content.
    write_error_file(OUTPUT_DIR, [])

    # --- Summary ------------------------------------------------------------
    print("Parsed {} parts total ({} classified, {} unclassified).".format(
        total_numbered + total_b0, total_numbered, total_b0))
    print("  FileARR  (unclassified): {}".format(total_b0))
    for bucket in NUMBERED_BUCKETS:
        count = len(numbered_buckets.get(bucket, []))
        if count:
            print("  FileARR{}: {}".format(bucket, count))
    print("Output written to: {}".format(OUTPUT_DIR))

    return 1  # Success


if __name__ == "__main__":
    sys.exit(main(sys.argv))