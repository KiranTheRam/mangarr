from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mangarr")
except PackageNotFoundError:
    __version__ = "0.0.0+local"

USER_AGENT = f"Mangarr/{__version__} (https://github.com/KiranTheRam/mangarr)"
