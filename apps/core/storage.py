from whitenoise.storage import CompressedManifestStaticFilesStorage


class AdminStaticFilesStorage(CompressedManifestStaticFilesStorage):
    def stored_name(self, name):
        # Jazzmin 3 uses this directory URL to construct theme filenames in JS.
        # A directory cannot appear in Django's file manifest. Keep strict
        # manifest checking for every actual static file.
        if name == "vendor/bootswatch":
            return name
        return super().stored_name(name)
