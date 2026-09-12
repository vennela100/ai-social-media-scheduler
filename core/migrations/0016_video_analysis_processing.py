from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0015_remove_ai_thumbnail_generation")]

    operations = [
        migrations.AddField(
            model_name="video",
            name="ai_analysis_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="video",
            name="ai_analysis_status",
            field=models.CharField(
                choices=[("pending", "Pending"), ("processing", "Processing"),
                         ("done", "Done"), ("skipped", "Skipped"), ("failed", "Failed")],
                default="pending", max_length=10,
            ),
        ),
    ]
