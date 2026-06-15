"""Forms for the core app."""

from django import forms

from .models import AIContent, Platform

# --- Upload limits (decision point — tune to your needs) ---
# Cloudinary's free tier caps a single video around 100 MB, so staying under
# that avoids hard API rejections. Extensions are an allowlist: we check the
# filename suffix AND let Cloudinary reject anything that isn't really a video.
MAX_UPLOAD_MB = 100
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".webm", ".avi", ".mkv"}


class VideoUploadForm(forms.Form):
    """A single video file to push to Cloudinary."""

    video = forms.FileField(
        label="Video file",
        help_text=f"Up to {MAX_UPLOAD_MB} MB. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
    )

    def clean_video(self):
        f = self.cleaned_data["video"]

        # Size guard — reject before we waste an upload round-trip.
        max_bytes = MAX_UPLOAD_MB * 1024 * 1024
        if f.size > max_bytes:
            raise forms.ValidationError(
                f"File is {f.size / 1024 / 1024:.1f} MB; the limit is {MAX_UPLOAD_MB} MB."
            )

        # Extension allowlist — a cheap first filter. Cloudinary's
        # resource_type='video' is the real backstop against non-video files.
        name = (f.name or "").lower()
        if not any(name.endswith(ext) for ext in ALLOWED_EXTENSIONS):
            raise forms.ValidationError(
                f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}."
            )

        return f


class GenerateMetadataForm(forms.Form):
    """Pick a platform and give the AI a short brief to write from."""

    platform = forms.ChoiceField(choices=Platform.choices)
    brief = forms.CharField(
        label="What is this video about?",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "e.g. A 60s demo of our new CSV import feature, casual tone"}),
        required=False,
        help_text="Optional, but better briefs produce better captions.",
    )


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
