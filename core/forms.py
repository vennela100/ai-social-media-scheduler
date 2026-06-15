"""Forms for the core app."""

from django import forms

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
