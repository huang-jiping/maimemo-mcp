"""Local/manual entrypoint for the application's OpenAPI drift checker."""

from maimemo.openapi_drift import main

if __name__ == "__main__":
    raise SystemExit(main())
