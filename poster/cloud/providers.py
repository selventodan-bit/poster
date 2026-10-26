"""Official notebook authentication adapters; all file I/O uses Legacy Drive REST.

Provider secrets are explicit named bindings. They are never widget values,
notebook source/output, process environment hand-offs, or Windows queue keys.
"""

import asyncio
import os
from pathlib import Path
import sys

from cryptography.fernet import Fernet

from poster.storage.drive import Drive, GoogleToken
from .drive_files import DriveFiles, Journal
from .errors import CloudError
from .library import Library

SCOPES = ["https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/drive.readonly"]
GDRIVE_FSSPEC_COMMIT = "dd9bb3025b26091d4a29ac4e81cc3d88910203bd"


class ProviderConnection:
    def __init__(self, library, transport):
        self.library, self.transport = library, transport

    async def close(self):
        await self.transport.close()


async def connect(provider, root, *, filesystem=None, credential=None):
    """Interactive Colab auth / Molab native cached connection / explicit Jupyter ADC.

    Molab uses its filesystem credential object, NOT drive.mount(), an OOB code,
    service-account JSON embedded in a notebook, or a copied access token.
    """
    try:
        if credential is None:
            if provider == "colab":
                from google.colab import auth

                await asyncio.to_thread(auth.authenticate_user)
                import google.auth

                credential, _ = await asyncio.to_thread(google.auth.default, scopes=SCOPES)
            elif provider == "molab":
                if filesystem is None:
                    from gdrive_fsspec import GoogleDriveFileSystem

                    filesystem = await asyncio.to_thread(GoogleDriveFileSystem, token="cache")
                credential = filesystem.authed_http.credentials
            elif provider == "generic":
                import google.auth

                credential, _ = await asyncio.to_thread(google.auth.default, scopes=SCOPES)
            else:
                raise CloudError("CAPABILITY")
        if credential is None or not hasattr(credential, "refresh"):
            raise CloudError("AUTH_REQUIRED")
        token = GoogleToken(credential=credential, scopes=SCOPES)
        transport = Drive("", Path(root) / "cache", token)
        # This recovery key is local to this disposable notebook session. It is
        # neither sent to Drive nor related to any managed Windows identity.
        journal = Journal(Path(root) / "transport", key=Fernet.generate_key())
        return ProviderConnection(Library(DriveFiles(transport, journal)), transport)
    except CloudError:
        raise
    except Exception:
        raise CloudError("AUTH_REQUIRED") from None


def resolve_bindings(provider, bindings):
    values = {}
    if not isinstance(bindings, dict) or len(bindings) > 1000:
        raise CloudError("SECRET_REQUIRED")
    for ref, binding in bindings.items():
        if not isinstance(binding, str) or not binding or len(binding) > 200:
            raise CloudError("SECRET_REQUIRED")
        try:
            if provider == "colab":
                from google.colab import userdata

                value = userdata.get(binding)
            else:
                # Only operator-selected private environment entries in the
                # provider's secret settings. Never enumerate ambient secrets.
                if not binding.startswith("POSTER_SECRET_"):
                    raise ValueError()
                value = os.environ[binding]
            if not isinstance(value, str) or not value or len(value) > 200000:
                raise ValueError()
            values[ref] = value
        except Exception:
            raise CloudError("SECRET_REQUIRED") from None
    return values


def colab_automatic_capability():
    import importlib.metadata
    import shutil

    if sys.platform not in {"linux", "darwin"} or not shutil.which("colab"):
        return dict(available=False, reason="Official Colab CLI requires Linux/macOS and installation.")
    try:
        version = importlib.metadata.version("google-colab-cli")
    except importlib.metadata.PackageNotFoundError:
        return dict(available=False, reason="Official Colab CLI package is unavailable.")
    # Installing a CLI is not proof of entitlement, authenticated account or
    # allowed resource type. No paid runtime is provisioned by a GUI click.
    return dict(
        available=False,
        installed=version,
        reason="Account entitlement, current usage and explicit paid-resource consent are not verified.",
    )
