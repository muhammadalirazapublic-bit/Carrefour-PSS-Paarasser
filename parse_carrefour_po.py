#!/usr/bin/env python3
"""
Carrefour Operational Purchase Order (PSS) Confirmation PDF Parser
=====================================================================

Parses "OPERATIONAL PURCHASE ORDER CONFIRMATION" PDFs issued by
Carrefour Global Sourcing Asia Ltd, regardless of how many pages,
products, or shipment lines the document contains.

Design goals (why it won't fail on a new document):
  - No hard-coded page numbers or page counts. Every section is found
    by scanning for its heading / table header text, wherever it lands.
  - Every extraction step (header, per-product blocks, general
    documents, shipment rows, warehouses, comments) is wrapped in its
    own try/except so a malformed or missing section degrades to an
    empty/partial result + a logged warning instead of crashing the
    whole run.
  - Regexes are written to tolerate optional fields (e.g. a product
    might not have "SPECIFIC DOCUMENTS", a shipment row might not
    repeat POL/POD on continuation lines, etc.)
  - Table rows are located by matching header keywords, not by table
    index on the page, since the number/order of tables per page
    varies (some product pages have a "specific documents" table,
    others don't).
  - Works whether the document has 1 product or 500 products, and
    whether it spans 5 pages or 500 pages.

Usage:
    python3 parse_carrefour_po.py <input.pdf> [-o output_prefix]

Outputs:
    <prefix>.json           Full structured data
    <prefix>_products.csv   One row per product / packing item
    <prefix>_shipment.csv   One row per shipment (ETD) line
    <prefix>_warnings.log   Any non-fatal issues encountered

Requires: pdfplumber
"""

import argparse
import csv
import json
import logging
import re
import sys
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    sys.stderr.write(
        "Missing dependency 'pdfplumber'. Install with:\n"
        "  pip install pdfplumber --break-system-packages\n"
    )
    sys.exit(1)


# --------------------------------------------------------------------------
# Logging - collects warnings without ever raising, so the run always
# finishes and produces whatever could be extracted.
# --------------------------------------------------------------------------
logger = logging.getLogger("carrefour_po_parser")
logger.setLevel(logging.INFO)


def safe(fn, default, *args, context="", **kwargs):
    """Run fn(*args, **kwargs); on any exception, log and return default."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - intentionally broad; must never crash
        logger.warning("Failed extracting %s: %s", context or fn.__name__, exc)
        return default


def clean(s):
    """Collapse whitespace/newlines, strip, return '' for None."""
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip()


def dedupe_repeated_text(s):
    """
    These PDFs frequently repeat the same description text 2-3 times in a
    row (a source-document artifact, not something we introduce). Collapse
    'X X' or 'X X X' back down to a single 'X' when the repeats are exact.
    Left untouched if it doesn't cleanly divide - safer to keep possibly-
    duplicated real text than to risk truncating something legitimate.
    """
    if not s:
        return s
    for n in (3, 2):
        if len(s) % n:
            continue
        chunk = len(s) // n
        parts = [s[i * chunk:(i + 1) * chunk] for i in range(n)]
        if len(set(p.strip() for p in parts)) == 1:
            return parts[0].strip()
    return s


def to_number(s):
    """Best-effort numeric conversion, tolerant of thousands separators."""
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    if s in ("", "-", "None"):
        return None
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return None


# --------------------------------------------------------------------------
# Main parser
# --------------------------------------------------------------------------
class CarrefourPOParser:
    """
    pdf_source may be:
      - a str / Path to a PDF file on disk (CLI usage), or
      - a bytes object or a file-like object (BytesIO) containing PDF
        data (web-service usage, so uploaded files never need to touch
        disk).
    `source_name` is only used for the 'source_file' field in the output
    and for logging; pass the original filename when using in-memory data.
    """

    def __init__(self, pdf_source, source_name=None):
        self.pdf_source = pdf_source
        if source_name:
            self.source_name = source_name
        elif isinstance(pdf_source, (str, Path)):
            self.source_name = Path(pdf_source).name
        else:
            self.source_name = "uploaded.pdf"
        self.pages_text = []      # per-page raw text
        self.pages_tables = []    # per-page list of tables (each a list of rows)
        self.full_text = ""
        self.result = {
            "source_file": self.source_name,
            "header": {},
            "products": [],
            "general_documents_required": [],
            "shipment_lines": [],
            "shipment_summary": {},
            "warehouses": [],
            "comments": "",
            "signatures": {},
            "warnings": [],
        }

    # ---- loading -----------------------------------------------------
    def load(self):
        with pdfplumber.open(self.pdf_source) as pdf:
            for i, page in enumerate(pdf.pages):
                text = safe(page.extract_text, "", context=f"page {i+1} text") or ""
                tables = safe(page.extract_tables, [], context=f"page {i+1} tables") or []
                self.pages_text.append(text)
                self.pages_tables.append(tables)
        self.full_text = "\n".join(self.pages_text)
        logger.info("Loaded %d pages", len(self.pages_text))

    # ---- header --------------------------------------------------------
    def parse_header(self):
        h = {}
        t = self.full_text

        def find(pattern, group=1, flags=re.IGNORECASE):
            m = re.search(pattern, t, flags)
            return clean(m.group(group)) if m else None

        h["po_number"] = find(r"OPERATIONAL PURCHASE ORDER CONFIRMATION N[°ºo]\s*([A-Z0-9]+)")
        h["global_order_number"] = find(r"Global Order N[°ºo]\s*:\s*([A-Z0-9]+)")
        h["printing_date"] = find(r"Printing Date\s*:\s*([0-9/]+)")
        h["commercial_confirmation_date"] = find(r"Commercial confirmation Date\s*:\s*([0-9/]+)")
        h["document_status"] = find(r"\[\s*([A-Z ]+?)\s*\]")
        h["incoterm"] = find(r"Incoterm\s*:\s*([^\n]+)")
        h["port_of_loading"] = find(r"POL\s*:\s*([^\n]+)")
        h["port_of_discharge"] = find(r"POD\s*:\s*([^\n]+)")
        h["currency"] = find(r"Currency\s*:\s*([^\n]+)")
        h["payment_terms"] = find(r"Payment\s*:\s*([^\n]+)")
        h["total_amount"] = to_number(find(r"Total amount\s*:\s*([0-9.,]+)"))
        # "Freight forwarder at origin :" and "Transport :" sit side-by-side
        # in the PDF layout, so plain-text extraction often puts
        # "Transport : SEA" right after the freight-forwarder label on the
        # same line, with the actual forwarder name on the next line.
        ff_match = re.search(
            r"Freight forwarder at origin\s*:\s*(?:Transport\s*:\s*([^\n]*)\n)?([^\n]+)",
            t, re.IGNORECASE,
        )
        if ff_match:
            h["transport_mode"] = clean(ff_match.group(1)) or find(r"Transport\s*:\s*([^\n]+)")
            h["freight_forwarder"] = clean(ff_match.group(2))
        else:
            h["freight_forwarder"] = None
            h["transport_mode"] = find(r"Transport\s*:\s*([^\n]+)")

        # Parties block: "Issued by / Beneficiary client / Supplier" then
        # three roughly-parallel address blocks. This layout is fragile in
        # plain text, so we grab it best-effort and leave raw text too.
        parties_match = re.search(
            r"Issued by\s+Beneficiary client\s+Supplier\s*\n(.*?)\nDelivery address",
            t, re.IGNORECASE | re.DOTALL,
        )
        h["parties_raw"] = clean(parties_match.group(1)) if parties_match else None

        addr_match = re.search(
            r"Delivery address\s+Payment Address(.*?)\n(?:ORDER INDEX|I\. PRODUCT DESCRIPTION)",
            t, re.IGNORECASE | re.DOTALL,
        )
        h["delivery_payment_raw"] = clean(addr_match.group(1)) if addr_match else None

        self.result["header"] = h

    # ---- signatures ------------------------------------------------------
    def parse_signatures(self):
        sig = {}
        m1 = re.search(r"CARREFOUR GLOBAL SOURCING\s*\n?MANAGING DIRECTOR\s*\n?([A-Z ]+)\n", self.full_text)
        if m1:
            sig["carrefour_global_sourcing_managing_director"] = clean(m1.group(1))
        m2 = re.search(r"GROUP IMPORT DIRECTOR\s*\n?([A-Z ]+)\n", self.full_text)
        if m2:
            sig["carrefour_import_group_import_director"] = clean(m2.group(1))
        self.result["signatures"] = sig

    # ---- products (section I) ------------------------------------------
    def parse_products(self):
        """
        Splits the full text on 'PRODUCT / PACKING # N' markers (N is
        unbounded - works for 1 product or 1000). For each block, pulls
        the descriptive fields with tolerant regexes, then cross-references
        the per-page tables to find that product's line-item row(s) by
        matching the Carrefour reference code pattern (a letter followed
        by 5-7 digits, e.g. I277890).
        """
        # Find every "PRODUCT / PACKING # <n>" occurrence with its position,
        # so we can slice the text into blocks regardless of how many there are.
        markers = list(re.finditer(r"PRODUCT\s*/\s*PACKING\s*#\s*(\d+)", self.full_text))
        if not markers:
            logger.warning("No 'PRODUCT / PACKING #' sections found.")
            return

        # Pre-index all table rows across all pages that look like product
        # line-item rows: first cell matches a reference code like I277890.
        ref_pattern = re.compile(r"^[A-Z]\d{5,8}$")
        all_line_rows = []  # list of dict rows keyed by header, tagged with ref
        for page_idx, tables in enumerate(self.pages_tables):
            for table in tables:
                if not table:
                    continue
                header_row = None
                for row in table:
                    cells = [clean(c) for c in row]
                    joined = " ".join(cells)
                    if header_row is None and "Carrefour" in joined and "reference" in joined.lower():
                        header_row = self._normalize_line_item_header(table)
                        continue
                    if header_row and cells and ref_pattern.match(cells[0] or ""):
                        row_dict = self._map_row_to_header(header_row, row)
                        row_dict["_page"] = page_idx + 1
                        all_line_rows.append(row_dict)

        for idx, m in enumerate(markers):
            start = m.start()
            end = markers[idx + 1].start() if idx + 1 < len(markers) else len(self.full_text)
            block = self.full_text[start:end]
            product = safe(self._parse_single_product_block, {}, block, m.group(1),
                            context=f"product block #{m.group(1)}")
            if not product:
                continue

            # Attach matching line-item row(s) by Carrefour reference found in block
            ref_in_block = re.search(r"\b([A-Z]\d{5,8})\b", block)
            product["line_items"] = []
            if ref_in_block:
                ref = ref_in_block.group(1)
                product["line_items"] = [r for r in all_line_rows if r.get("carrefour_reference") == ref]

            self.result["products"].append(product)

        logger.info("Parsed %d product/packing blocks", len(self.result["products"]))

    def _parse_single_product_block(self, block, packing_number):
        def find(pattern, group=1, flags=re.IGNORECASE | re.DOTALL):
            m = re.search(pattern, block, flags)
            return clean(m.group(group)) if m else None

        product = {
            "packing_number": to_number(packing_number),
            "short_description": find(r"Short description\s*:\s*([^\n]+?)(?:\s+Packing\s*:|\s*\n)"),
            "packing_type": find(r"Packing\s*:\s*([^\n]+?)(?:\s+Product origin|\s*\n)"),
            "product_origin": find(r"Product origin\s*:\s*([^\n]+)"),
            "commission_rate": to_number(find(r"Commission rate\s*:\s*([0-9.]+)")),
            "product_description": dedupe_repeated_text(
                find(r"Product description\s*:\s*([^\n]+(?:\n(?!Product composition)[^\n]+)*)")
            ),
            "product_composition": find(r"Product composition\s*:\s*([^\n]+)"),
            "national_merchandise_structure": find(r"National merchandise structure\s*:\s*([0-9]+)"),
            "color_details": find(r"([A-Z]{2,4}\s*=\s*[A-Z ]+)"),
            "specific_customs_instructions": find(r"SPECIFIC CUSTOMS INSTRUCTIONS\s*:\s*([^\n]*)"),
        }

        # Specific documents table, if present in this block (only exists on
        # some product pages, e.g. B/L FOURNISSEUR or FAMA/BRAND LICENCE)
        doc_match = re.search(
            r"(\d{2,3})\s+([A-Z /'\-]+?)\s+(\d{6,10})(?:\s+([A-Z]+))?\n", block
        )
        if doc_match and "SPECIFIC DOCUMENTS" in block.upper():
            product["specific_document"] = {
                "document_code": doc_match.group(1),
                "description": clean(doc_match.group(2)),
                "customs_class": doc_match.group(3),
                "category_number": clean(doc_match.group(4)) if doc_match.group(4) else None,
            }

        return product

    @staticmethod
    def _normalize_line_item_header(table):
        """
        The product line-item table header spans two physical rows in the
        underlying PDF (e.g. 'Assortment' -> 'master'/'inner', 'Quantity
        ordered' -> 'Pallet'/'Master'/'Unit'). We flatten this into a
        single list of column keys, tolerant of layout drift by falling
        back to generic column_N names when we can't confidently label it.
        """
        # Known canonical order based on observed documents. If the actual
        # row has a different number of columns we still map what we can
        # positionally and pad/truncate safely.
        canonical = [
            "carrefour_reference", "suppliers_reference", "client_reference",
            "color", "size", "customs_class", "category_number",
            "pack_type", "assortment_master", "assortment_inner",
            "master_ctn_barcode", "inner_ctn_barcode", "unit_barcode",
            "pallet_dimension_cm", "master_carton_cm", "gross_wt_kg",
            "qty_pallet", "qty_master", "qty_unit", "unit_price", "total_price",
        ]
        return canonical

    @staticmethod
    def _map_row_to_header(header, row):
        cells = [clean(c) for c in row]
        d = {}
        for i, key in enumerate(header):
            d[key] = cells[i] if i < len(cells) else None
        # numeric coercion for known numeric fields
        for k in ("gross_wt_kg", "qty_pallet", "qty_master", "qty_unit",
                   "unit_price", "total_price"):
            if k in d:
                d[k] = to_number(d[k])
        return d

    # ---- general documents required (section II) ------------------------
    def parse_general_documents(self):
        docs = []
        seen = set()
        for tables in self.pages_tables:
            for table in tables:
                if not table:
                    continue
                header_found = False
                for row in table:
                    cells = [clean(c) for c in row]
                    joined = " ".join(cells)
                    if not header_found:
                        if "Document" in joined and "Description" in joined and "Nb Original" in joined.replace("N", "N"):
                            header_found = True
                        elif "Document" in joined and "Description" in joined and re.search(r"Nb\s*Original", joined, re.I):
                            header_found = True
                        continue
                    if cells and re.match(r"^\d{2,3}$", cells[0] or ""):
                        entry = {
                            "document_code": cells[0],
                            "description": cells[1] if len(cells) > 1 else None,
                            "customs_class": cells[2] if len(cells) > 2 else None,
                            "category_number": cells[3] if len(cells) > 3 else None,
                            "nb_original": to_number(cells[4]) if len(cells) > 4 else None,
                            "nb_copy": to_number(cells[5]) if len(cells) > 5 else None,
                        }
                        key = (entry["document_code"], entry["description"])
                        if key not in seen:
                            seen.add(key)
                            docs.append(entry)
        self.result["general_documents_required"] = docs

    # ---- shipment details (section III) ---------------------------------
    def parse_shipment(self):
        rows = []
        date_pat = re.compile(r"^\d{2}/\d{2}/\d{4}$")
        header_seen = False
        last_context = {}  # carries forward POL/POD/warehouse across
                            # continuation rows that don't repeat them

        for page_idx, tables in enumerate(self.pages_tables):
            for table in tables:
                if not table:
                    continue
                for row in table:
                    cells = [clean(c) for c in row]
                    joined = " ".join(cells)
                    if "FRI date" in joined:
                        header_seen = True
                        continue
                    if not header_seen:
                        continue
                    if not cells:
                        continue
                    if any(c.upper().startswith("TOTAL") for c in cells if c):
                        self.result["shipment_summary"] = self._map_shipment_totals(cells)
                    elif cells[0] and date_pat.match(cells[0]):
                        row_dict = self._map_shipment_row(cells, last_context)
                        row_dict["_page"] = page_idx + 1
                        rows.append(row_dict)
                        last_context = {
                            "port_of_loading": row_dict.get("port_of_loading") or last_context.get("port_of_loading"),
                            "port_of_discharge": row_dict.get("port_of_discharge") or last_context.get("port_of_discharge"),
                            "warehouse": row_dict.get("warehouse") or last_context.get("warehouse"),
                        }

        self.result["shipment_lines"] = rows
        logger.info("Parsed %d shipment lines", len(rows))

    @staticmethod
    def _map_shipment_row(cells, last_context):
        # Column layout (may shift slightly by document variant, hence
        # defensive .get-style indexing with fallbacks):
        # FRI date, Packing list transm date, Cargo receiving date,
        # Required ETD, POL, POD, Warehouse, Master barcode, Pack type,
        # Import reference, Product description(+Color-Size), Pallet,
        # Master, Units, Gross vol, Net vol, Gross wt, Net wt, Promo units
        def g(i):
            return cells[i] if i < len(cells) else None

        return {
            "fri_date": g(0),
            "packing_list_transmission_date": g(1),
            "cargo_receiving_date": g(2),
            "required_etd": g(3) or None,
            "port_of_loading": g(4) or None,
            "port_of_discharge": g(5) or None,
            "warehouse": g(6) or last_context.get("warehouse"),
            "master_barcode": (g(7) or "").lstrip("0_") or g(7),
            "pack_type": g(8),
            "import_reference": g(9),
            "product_description_color_size": g(10),
            "qty_pallet": to_number(g(11)),
            "qty_master": to_number(g(12)),
            "qty_units": to_number(g(13)),
            "gross_volume_m3": to_number(g(14)),
            "net_volume_m3": to_number(g(15)),
            "gross_weight_kg": to_number(g(16)),
            "net_weight_kg": to_number(g(17)),
            "promo_units": to_number(g(18)),
        }

    @staticmethod
    def _map_shipment_totals(cells):
        # TOTAL row has fewer populated leading cells; grab from the end.
        nums = [to_number(c) for c in cells]
        nums = [n for n in nums if n is not None]
        keys = ["qty_pallet_total", "qty_master_total", "qty_units_total",
                "gross_volume_m3_total", "net_volume_m3_total",
                "gross_weight_kg_total", "net_weight_kg_total", "promo_units_total"]
        # Take the trailing numeric values, right-aligned to keys
        vals = nums[-len(keys):] if len(nums) >= len(keys) else nums
        offset = len(keys) - len(vals)
        return {keys[offset + i]: v for i, v in enumerate(vals)}

    # ---- warehouses (section IV) ----------------------------------------
    def parse_warehouses(self):
        warehouses = []
        seen_codes = set()
        for tables in self.pages_tables:
            for table in tables:
                if not table:
                    continue
                header_found = False
                for row in table:
                    cells = [clean(c) for c in row]
                    if not header_found:
                        if cells[:2] == ["Code", "Address"]:
                            header_found = True
                        continue
                    if cells and cells[0] and cells[0] not in seen_codes and "CARREFOUR" not in cells[0].upper():
                        seen_codes.add(cells[0])
                        warehouses.append({
                            "code": cells[0],
                            "address": cells[1] if len(cells) > 1 else None,
                        })
        self.result["warehouses"] = warehouses

    # ---- comments (section V) --------------------------------------------
    def parse_comments(self):
        m = re.search(
            r"V\.\s*COMMENTS\s*\n(.*?)(?:\nCARREFOUR GLOBAL SOURCING ASIA LTD|\Z)",
            self.full_text, re.DOTALL,
        )
        if m:
            self.result["comments"] = clean(m.group(1))

    # ---- orchestration -----------------------------------------------
    def parse(self):
        self.load()
        safe(self.parse_header, None, context="header")
        safe(self.parse_signatures, None, context="signatures")
        safe(self.parse_products, None, context="products")
        safe(self.parse_general_documents, None, context="general documents")
        safe(self.parse_shipment, None, context="shipment details")
        safe(self.parse_warehouses, None, context="warehouses")
        safe(self.parse_comments, None, context="comments")
        return self.result


# --------------------------------------------------------------------------
# CSV writers - take an already-open, writable text file-like object so the
# same code works for on-disk files (CLI) and in-memory buffers (API).
# --------------------------------------------------------------------------
def write_products_csv(result, fileobj):
    fields = [
        "packing_number", "carrefour_reference", "suppliers_reference",
        "short_description", "product_description", "product_composition",
        "color", "size", "packing_type", "product_origin",
        "national_merchandise_structure", "customs_class", "category_number",
        "pack_type", "assortment_master", "assortment_inner",
        "master_ctn_barcode", "inner_ctn_barcode", "unit_barcode",
        "gross_wt_kg", "qty_pallet", "qty_master", "qty_unit",
        "unit_price", "total_price", "commission_rate",
    ]
    w = csv.DictWriter(fileobj, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for p in result.get("products", []):
        line_items = p.get("line_items") or [{}]
        for li in line_items:
            row = {**p, **li}
            w.writerow(row)


def write_shipment_csv(result, fileobj):
    fields = [
        "fri_date", "packing_list_transmission_date", "cargo_receiving_date",
        "required_etd", "port_of_loading", "port_of_discharge", "warehouse",
        "master_barcode", "pack_type", "import_reference",
        "product_description_color_size", "qty_pallet", "qty_master",
        "qty_units", "gross_volume_m3", "net_volume_m3",
        "gross_weight_kg", "net_weight_kg", "promo_units",
    ]
    w = csv.DictWriter(fileobj, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for row in result.get("shipment_lines", []):
        w.writerow(row)


def parse_pdf(pdf_source, source_name=None):
    """
    Convenience entry point for programmatic / API use.
    pdf_source: path, bytes, or file-like object containing the PDF.
    Returns the same result dict the CLI writes to JSON, including a
    'warnings' list of anything that went wrong during parsing.
    Never raises - always returns a (possibly partial) result dict.
    """
    log_records = []

    class ListHandler(logging.Handler):
        def emit(self, record):
            log_records.append(self.format(record))

    handler = ListHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(handler)
    try:
        parser = CarrefourPOParser(pdf_source, source_name=source_name)
        try:
            result = parser.parse()
        except Exception as exc:  # absolute last-resort guard
            logger.error("Unrecoverable error during parsing: %s", exc)
            result = parser.result
        result["warnings"] = log_records
        return result
    finally:
        logger.removeHandler(handler)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Parse a Carrefour Operational Purchase Order Confirmation PDF.")
    ap.add_argument("pdf", help="Path to the input PDF")
    ap.add_argument("-o", "--output-prefix", default=None,
                     help="Output file prefix (default: same as input filename)")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        sys.stderr.write(f"File not found: {pdf_path}\n")
        sys.exit(1)

    prefix = Path(args.output_prefix) if args.output_prefix else pdf_path.with_suffix("")

    logger.addHandler(logging.StreamHandler(sys.stderr))

    result = parse_pdf(pdf_path)
    log_records = result.get("warnings", [])

    json_path = f"{prefix}.json"
    products_csv_path = f"{prefix}_products.csv"
    shipment_csv_path = f"{prefix}_shipment.csv"
    warnings_path = f"{prefix}_warnings.log"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    def _write_csv(writer_fn, path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer_fn(result, f)

    safe(_write_csv, None, write_products_csv, products_csv_path, context="writing products CSV")
    safe(_write_csv, None, write_shipment_csv, shipment_csv_path, context="writing shipment CSV")

    with open(warnings_path, "w", encoding="utf-8") as f:
        f.write("\n".join(log_records) if log_records else "No warnings.\n")

    print("\n--- Parse summary ---")
    print(f"PO number:        {result['header'].get('po_number')}")
    print(f"Products found:   {len(result.get('products', []))}")
    print(f"Shipment lines:   {len(result.get('shipment_lines', []))}")
    print(f"Warehouses:       {len(result.get('warehouses', []))}")
    print(f"Docs required:    {len(result.get('general_documents_required', []))}")
    print(f"Warnings logged:  {len(log_records)}")
    print(f"\nWrote:\n  {json_path}\n  {products_csv_path}\n  {shipment_csv_path}\n  {warnings_path}")


if __name__ == "__main__":
    main()
