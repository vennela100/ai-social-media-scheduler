"""Forms for the core app."""

from django import forms

from .models import AIContent, Platform

# --- Upload limits (decision point — tune to your needs) ---
# Cloudinary's free tier caps a single video around 100 MB, so staying under
# that avoids hard API rejections. Extensions are an allowlist: we check the
# filename suffix AND let Cloudinary reject anything that isn't really media.
MAX_UPLOAD_MB = 100
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".avi", ".mkv"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS


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
        help_text=f"Up to {MAX_UPLOAD_MB} MB. Video: {', '.join(sorted(VIDEO_EXTENSIONS))}. "
                  f"Image: {', '.join(sorted(IMAGE_EXTENSIONS))} (images can be posted to LinkedIn).",
    )
    title = forms.CharField(
        label="Video title (optional)",
        required=False,
        help_text="Leave blank to use the filename.",
    )
    description = forms.CharField(
        label="What is this video about?",
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "Describe what happens in the video, the key points, the tone you want. The more you say here, the better the AI's output."}),
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

        # Size guard — reject before we waste an upload round-trip.
        max_bytes = MAX_UPLOAD_MB * 1024 * 1024
        if f.size > max_bytes:
            raise forms.ValidationError(
                f"File is {f.size / 1024 / 1024:.1f} MB; the limit is {MAX_UPLOAD_MB} MB."
            )

        # Extension allowlist — a cheap first filter. Cloudinary's resource_type
        # is the real backstop against files that lie about their extension.
        if media_type_for(f.name) is None:
            raise forms.ValidationError(
                f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}."
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
