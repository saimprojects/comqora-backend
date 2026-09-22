import io
import warnings

from django import forms
from PIL import Image

from .models import PaymentBank


class PaymentBankForm(forms.ModelForm):
    icon_upload = forms.FileField(
        required=False, label="Bank icon", help_text="PNG, JPG or WebP, up to 2 MB."
    )
    remove_icon = forms.BooleanField(required=False)

    class Meta:
        model = PaymentBank
        exclude = ["icon"]

    def clean_icon_upload(self):
        upload = self.cleaned_data.get("icon_upload")
        if not upload:
            return None
        if upload.size > 2 * 1024 * 1024:
            raise forms.ValidationError("Icon must be 2 MB or smaller.")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(upload) as image:
                    if (
                        image.format not in {"PNG", "JPEG", "WEBP"}
                        or image.width * image.height > 4_000_000
                    ):
                        raise ValueError()
                    image.load()
                    image = image.convert("RGBA")
                    image.thumbnail((256, 256))
                    output = io.BytesIO()
                    image.save(output, format="PNG")
                    return output.getvalue()
        except (OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError):
            raise forms.ValidationError("Upload a valid PNG, JPG or WebP up to 4 megapixels.")

    def save(self, commit=True):
        obj = super().save(commit=False)
        if self.cleaned_data.get("remove_icon"):
            obj.icon = None
        if self.cleaned_data.get("icon_upload"):
            obj.icon = self.cleaned_data["icon_upload"]
        if commit:
            obj.save()
            self.save_m2m()
        return obj
