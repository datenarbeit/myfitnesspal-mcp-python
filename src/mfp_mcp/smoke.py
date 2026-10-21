"""Live smoke check: a standard food search must return real hits.

Uses the same login path as the server (stored cookies, credentials or browser
auto-discovery). Run where the server runs, e.g. inside its container:

    python -m mfp_mcp.smoke            # searches "Banane"
    python -m mfp_mcp.smoke "chicken"  # any other query

Exit code 0 = hits found, 1 = no hits or an error.
"""
import sys

from mfp_mcp.server import get_mfp_client, search_foods


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    query = args[0] if args else "Banane"
    try:
        results = search_foods(get_mfp_client(), query)
    except Exception as e:
        print(f"FAIL: search for {query!r} raised: {e}")
        return 1
    if not results:
        print(f"FAIL: search for {query!r} returned no results")
        return 1
    print(f"OK: search for {query!r} returned {len(results)} results, e.g.:")
    for item in results[:3]:
        print(f"  {item.mfp_id}  {item.name}  ({item.brand or 'no brand'}, {item.calories} kcal)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
