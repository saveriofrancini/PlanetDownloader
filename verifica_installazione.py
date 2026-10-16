"""Local dependency diagnostic, with no network access or Planet orders."""
import importlib
import platform
import struct
import sys

def main():
    print(f"Python {platform.python_version()} | {struct.calcsize('P')*8} bit | {platform.system()}")
    failures = []
    for name in ("flask", "requests", "numpy", "geopandas", "pyogrio", "shapely", "pyproj", "rasterio", "tkinter"):
        try:
            module = importlib.import_module(name)
            print(f"OK {name}")
        except Exception as exc:
            print(f"ERROR {name}: {exc}")
            failures.append(name)
    if not failures:
        from pyproj import Transformer
        from shapely.geometry import box
        from rasterio.io import MemoryFile
        assert box(0, 0, 1, 1).area == 1
        assert all(abs(x) < 1e8 for x in Transformer.from_crs(4326, 32632, always_xy=True).transform(11, 44))
        with MemoryFile():
            pass
        print("Local check complete. No Planet requests were made.")
    return bool(failures)

if __name__ == "__main__":
    sys.exit(main())
