"""Forms for the core app."""

from django import forms

from django.utils import timezone

from .models import AIContent, Platform, ScheduledPost, SocialAccount, Video

# --- Upload limits (decision point — tune to your needs) ---
# Cloudinary's free tier caps a single video around 100 MB, so staying under
# that avoids hard API rejections. Extensions are an allowlist: we check the
# filename suffix AND let Cloudinary reject anything that isn't really a video.
MAX_UPLOAD_MB = 100
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".webm", ".avi", ".mkv"}


class VideoUploadForm(forms.Form):
    """A video file to push to Cloudinary, plus which platforms to draft for."""

    video = forms.FileField(
        label="Video file",
        help_text=f"Up to {MAX_UPLOAD_MB} MB. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
    )
    platforms = forms.MultipleChoiceField(
        choices=Platform.choices,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Auto-generate content for",
        help_text="We'll write tuned copy for each platform you tick — review and edit before scheduling.",
    )
    brief = forms.CharField(
        label="What's the video about? (optional)",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "e.g. A 60s walkthrough of our new CSV import feature, friendly tone"}),
        required=False,
        help_text="One line of context sharpens the AI's output.",
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


class ScheduledPostForm(forms.ModelForm):
    """Schedule a video to publish to a connected account at a UTC time."""

    class Meta:
        model = ScheduledPost
        fields = ["video", "social_account", "ai_content", "final_caption", "scheduled_time_utc"]
        widgets = {
            "final_caption": forms.Textarea(attrs={"rows": 5}),
            "scheduled_time_utc": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
        }
        labels = {"scheduled_time_utc": "Scheduled time (your local time)"}
        help_texts = {
            "scheduled_time_utc": "Pick the time in your own timezone — we convert it to UTC for storage.",
            "ai_content": "Optional — link an AI draft for reference.",
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Scope every choice to the signed-in user's own objects.
        self.fields["video"].queryset = Video.objects.filter(user=user)
        self.fields["social_account"].queryset = SocialAccount.objects.filter(user=user)
        self.fields["ai_content"].queryset = AIContent.objects.filter(video__user=user)
        self.fields["ai_content"].required = False
        # The HTML datetime-local control submits without seconds/tz; accept it.
        self.fields["scheduled_time_utc"].input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"]

    def clean_scheduled_time_utc(self):
        when = self.cleaned_data["scheduled_time_utc"]
        # The datetime-local control submits a naive wall-clock value. The
        # TimezoneMiddleware has activated the viewer's timezone, so interpret
        # the input in THAT zone and convert to an aware (UTC-backed) datetime.
        # make_aware uses the current timezone; the DB column stores UTC.
        if when and timezone.is_naive(when):
            when = timezone.make_aware(when, timezone.get_current_timezone())
        if when and when <= timezone.now():
            raise forms.ValidationError("Pick a time in the future.")
        return when

    def clean(self):
        cleaned = super().clean()
        video = cleaned.get("video")
        account = cleaned.get("social_account")
        # Guard: the caption's destination account must belong to the same user
        # as the video (querysets already enforce per-user; this catches mixups).
        if video and account and video.user_id != account.user_id:
            raise forms.ValidationError("Video and account belong to different users.")
        return cleaned
