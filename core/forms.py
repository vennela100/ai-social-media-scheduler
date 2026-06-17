"""Forms for the core app."""

from django import forms

from .models import AIContent, Platform

# --- Upload limits (decision point — tune to your needs) ---
# Separate caps per media type: video is large, images are small. Extensions are
# an allowlist; Cloudinary's resource_type is the real backstop against fakes.
VIDEO_MAX_MB = 500
IMAGE_MAX_MB = 20
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
MAX_MB_BY_TYPE = {"video": VIDEO_MAX_MB, "image": IMAGE_MAX_MB}


def media_type_for(filename: str) -> str | None:
    """Map a filename to "video" / "image" / None (unsupported) by extension."""
    name = (filename or "").lower()
    if any(name.endswith(ext) for ext in VIDEO_EXTENSIONS):
        return "video"
    if any(name.endswith(ext) for ext in IMAGE_EXTENSIONS):
        return "image"
    return None


class VideoUploadForm(forms.Form):
    """A video or image to push to Cloudinary, plus which platforms to draft for."""

    video = forms.FileField(
        label="Video or image file",
        help_text=f"Video ({', '.join(sorted(VIDEO_EXTENSIONS))}) up to {VIDEO_MAX_MB} MB · "
                  f"Image ({', '.join(sorted(IMAGE_EXTENSIONS))}) up to {IMAGE_MAX_MB} MB. "
                  f"Images can be posted to Instagram and LinkedIn (not YouTube).",
    )
    title = forms.CharField(
        label="Title (optional)",
        required=False,
        help_text="Leave blank to use the filename.",
    )
    category = forms.CharField(
        label="Category / niche (optional)",
        required=False,
        help_text="e.g. fitness, tech tutorials, cooking — helps the AI pick the right keywords.",
        widget=forms.TextInput(attrs={"placeholder": "fitness, tech, education…"}),
    )
    description = forms.CharField(
        label="What is this about?",
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "Describe the content, the key points, the tone you want. The more you say here, the better the AI's output."}),
        required=False,
        help_text="This is the main thing the AI writes from — your words become the source of truth.",
    )
    platforms = forms.MultipleChoiceField(
        choices=Platform.choices,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Auto-generate content for",
        help_text="We'll write tuned copy for each platform you tick — review and edit before scheduling.",
    )

    def clean_video(self):
        f = self.cleaned_data["video"]

        # Extension allowlist first, so we know which size cap to apply.
        media_type = media_type_for(f.name)
        if media_type is None:
            raise forms.ValidationError(
                f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}."
            )

        # Per-type size guard — reject before we waste an upload round-trip.
        max_mb = MAX_MB_BY_TYPE[media_type]
        if f.size > max_mb * 1024 * 1024:
            raise forms.ValidationError(
                f"This {media_type} is {f.size / 1024 / 1024:.1f} MB; the {media_type} limit is {max_mb} MB."
            )

        return f


class GenerateMetadataForm(forms.Form):
    """Pick a platform to add a draft for (writes from the video's description)."""

    platform = forms.ChoiceField(choices=Platform.choices)


class AIContentForm(forms.ModelForm):
    """Review/edit AI output before it's used to schedule a post."""

    class Meta:
        model = AIContent
        fields = ["generated_title", "generated_description", "generated_hashtags"]
        widgets = {
            "generated_title": forms.TextInput(),
            "generated_description": forms.Textarea(attrs={"rows": 6}),
            "generated_hashtags": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "generated_title": "Title",
            "generated_description": "Description",
            "generated_hashtags": "Hashtags",
        }
