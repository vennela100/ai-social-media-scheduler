from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0016_video_analysis_processing")]

    operations = [
        migrations.AddField(
            model_name="video", name="ai_analysis_error_code",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="aicontent", name="generation_error_code",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
    ]
