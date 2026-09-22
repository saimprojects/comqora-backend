"""Bounded subprocess entry point; never load Django, secrets or network providers."""

import json
import sys


def main():
    if sys.platform != "win32":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (50, 50))
    from .documents import MAX_BYTES, extract_document, render_page

    source = sys.stdin.buffer.read(MAX_BYTES + 1)
    try:
        if len(source) > MAX_BYTES:
            raise ValueError("Upload a statement of at most 8 MB.")
        if sys.argv[1] == "preview":
            sys.stdout.buffer.write(render_page(source, int(sys.argv[2])))
        else:
            filename = sys.argv[2] if len(sys.argv) > 2 else ""
            courier_name = sys.argv[3] if len(sys.argv) > 3 else ""
            sys.stdout.write(json.dumps(extract_document(source, filename, courier_name)))
    except Exception:
        # PDF internals can contain private data; return a safe, actionable error.
        sys.stderr.write(
            "The statement could not be processed safely. Use an original PDF, Excel or CSV export of at most 8 MB."
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
