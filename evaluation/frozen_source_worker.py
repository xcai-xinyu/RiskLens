"""Isolated resource-bounded parser. No provider configuration or Gold is loaded."""
import json
import resource
import socket
import sys
import time
from pathlib import Path


def set_limits():
    memory = 1024 ** 3
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return memory


def parse_complete(raw):
    from io import BytesIO

    from pypdf import PdfReader

    from app.rag.ingestion import chunk_sections, parse
    from evaluation.frozen_source import MAX_CHUNKS, MAX_PAGES
    reader = PdfReader(BytesIO(raw))
    if reader.is_encrypted or len(reader.pages) > MAX_PAGES:
        raise ValueError('frozen_import_pdf_bounds')
    sections = parse(raw, '.pdf')
    if len(chunk_sections('bounded', sections, 'WALMART_FAMILY', 'wmt-10k-2025-original')) > MAX_CHUNKS:
        raise ValueError('frozen_import_chunk_limit')
    return sections


def main():
    # Set limits before importing PDF/application code. The parent imposes wall time.
    memory = set_limits()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from evaluation.frozen_isolation import runtime_files

    path = root / 'evaluation/candidates/P4/p4-v3-candidate/sources/raw/walmart-2025-10k.pdf'
    if len(sys.argv) != 2 or Path(sys.argv[1]) != path:
        raise ValueError('frozen_import_not_allowlisted')
    def deny_network(*args, **kwargs):
        raise PermissionError('parser_network_forbidden')
    socket.socket.connect = deny_network
    socket.create_connection = deny_network
    start = time.monotonic()
    with runtime_files(root, [path]):
        from evaluation.frozen_source import MAX_OUTPUT, SOURCE_SHA, read_attested
        raw = read_attested(path)
        sections = parse_complete(raw)
        result = {'source_hash': SOURCE_SHA, 'sections': sections, 'resources': {
            'elapsed_seconds': time.monotonic() - start,
            'max_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'memory_limit_bytes': memory, 'cpu_limit_seconds': 25, 'wall_limit_seconds': 30}}
        data = json.dumps(result).encode()
        if len(data) > MAX_OUTPUT:
            raise ValueError('frozen_import_output_limit')
        sys.stdout.buffer.write(data)


if __name__ == '__main__':
    try:
        main()
    except Exception:  # noqa: BLE001 - redact untrusted parser failures at process boundary
        # No raw parser errors (or document instructions) are printed.
        sys.exit(2)
